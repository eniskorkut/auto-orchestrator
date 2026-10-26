#!/usr/bin/env python3
"""Read-only local evidence dashboard for Auto-Orchestrator.

Serves a zero-dependency web UI over the project's existing native run logs,
Supervisor SQLite database and managed worktree manifests. It never starts,
stops or mutates an agent. Binds to loopback only.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    from tools.perf import analyze, diagnose, observed_overlap, path_for
    from tools.project_memory import ProjectMemory
    from tools.quality_gate import assess, load_policy
    from tools.run_owner import (lease_held, owner_alive, read_json,
                                 read_recovery_state)
except ModuleNotFoundError:  # Running as ``python tools/dashboard.py``.
    from perf import analyze, diagnose, observed_overlap, path_for
    try:
        from project_memory import ProjectMemory
    except ModuleNotFoundError:  # pragma: no cover
        ProjectMemory = None
    from quality_gate import assess, load_policy
    try:
        from run_owner import (lease_held, owner_alive, read_json,
                               read_recovery_state)
    except ModuleNotFoundError:  # pragma: no cover - older install
        owner_alive = read_json = read_recovery_state = None

try:
    from tools import event_stream, office_model, project_registry
except ModuleNotFoundError:  # Installed copies may only contain dashboard.py.
    try:
        import event_stream
        import office_model
        import project_registry
    except ModuleNotFoundError:
        event_stream = office_model = project_registry = None

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
PROJECT_ID = re.compile(r"^p_[0-9a-f]{16}$")
RUN_LIMIT = 500
EVENT_LIMIT = 10000
LOG_LIMIT = 8 * 1024 * 1024
ASSET_DIR = Path(__file__).resolve().parent / "dashboard_assets"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/office.js": ("office.js", "text/javascript; charset=utf-8"),
    "/assets/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
    "/assets/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/assets/office/tilemap.js": ("office/tilemap.js", "text/javascript; charset=utf-8"),
    "/assets/office/pathfinding.js": ("office/pathfinding.js", "text/javascript; charset=utf-8"),
    "/assets/office/office-layouts.js": ("office/office-layouts.js", "text/javascript; charset=utf-8"),
    "/assets/office/room-manager.js": ("office/room-manager.js", "text/javascript; charset=utf-8"),
    "/assets/office/agent-state-machine.js": ("office/agent-state-machine.js", "text/javascript; charset=utf-8"),
    "/assets/office/agent-sprites.js": ("office/agent-sprites.js", "text/javascript; charset=utf-8"),
    "/assets/office/office-scene.js": ("office/office-scene.js", "text/javascript; charset=utf-8"),
    "/assets/office/layout-editor.js": ("office/layout-editor.js", "text/javascript; charset=utf-8"),
}
ACTIVE_WINDOW = 180.0
STATE_WORKERS = max(1, min(os.cpu_count() or 1, 8))
DETAIL_CACHE_LIMIT = 100
WORKTREE_TTL = 6.0

_ASSET_CACHE: dict[str, tuple[int, int, bytes, bytes | None]] = {}
_ASSET_LOCK = threading.Lock()


class DashboardError(ValueError):
    pass


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _ident(value):
    return value if isinstance(value, str) and ID.fullmatch(value) else None


def _short(value, limit=10):
    return value[:limit] if isinstance(value, str) else None


def _asset_payload(filename: str) -> tuple[bytes, bytes | None] | None:
    """Return cached ``(raw, gzip)`` bytes for a whitelisted static asset."""
    path = ASSET_DIR / filename
    try:
        if path.is_symlink() or not path.is_file():
            return None
        stat = path.stat()
    except OSError:
        return None
    signature = (stat.st_mtime_ns, stat.st_size)
    with _ASSET_LOCK:
        entry = _ASSET_CACHE.get(filename)
        if entry is not None and (entry[0], entry[1]) == signature:
            return entry[2], entry[3]
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    with _ASSET_LOCK:
        entry = _ASSET_CACHE.get(filename)
        if entry is not None and (entry[0], entry[1]) == signature:
            return entry[2], entry[3]
        _ASSET_CACHE[filename] = (signature[0], signature[1], raw, None)
    return raw, None


def _asset_gzip(filename: str, raw: bytes) -> bytes:
    """Compress an asset once and reuse the bytes for later requests."""
    with _ASSET_LOCK:
        entry = _ASSET_CACHE.get(filename)
        if entry is not None and entry[2] is raw:
            if entry[3] is not None:
                return entry[3]
            packed = gzip.compress(raw, compresslevel=6, mtime=0)
            _ASSET_CACHE[filename] = (entry[0], entry[1], entry[2], packed)
            return packed
    return gzip.compress(raw, compresslevel=6, mtime=0)


def _supervisor_signature(root: Path) -> str:
    """Cheap change detector for the SQLite database plus its WAL sidecar."""
    base = root / ".auto-orchestrator" / "supervisor.sqlite3"
    if base.is_symlink():
        return "symlink"
    parts = []
    for candidate in (base, base.with_name(base.name + "-wal")):
        try:
            stat = candidate.stat()
        except OSError:
            parts.append("-")
        else:
            parts.append(f"{stat.st_mtime_ns}:{stat.st_size}")
    return "|".join(parts)


def _read_jsonl(path: Path) -> tuple[list[dict], bool]:
    """Read display events, tolerating only a torn final line."""
    if path.is_symlink() or not path.is_file():
        raise DashboardError("Run log is not a regular file")
    stat = path.stat()
    if stat.st_size > LOG_LIMIT:
        raise DashboardError("Run log is too large")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DashboardError("Run log is unreadable") from exc
    lines = [line for line in text.splitlines() if line.strip()]
    events = []
    partial = False

    def reject_constant(_value):
        raise ValueError("non-finite JSON number")

    for index, line in enumerate(lines):
        try:
            value = json.loads(line, parse_constant=reject_constant)
        except (ValueError, RecursionError):
            if index == len(lines) - 1:
                partial = True
                break
            raise DashboardError("Run log contains malformed evidence")
        if not isinstance(value, dict) or not isinstance(value.get("event"), str):
            raise DashboardError("Run log contains an invalid event")
        events.append(value)
        if len(events) > EVENT_LIMIT:
            raise DashboardError("Run log contains too many events")
    if not events:
        raise DashboardError("Run log is empty")
    return events, partial


def _policy(root: Path):
    try:
        return load_policy(root)
    except (OSError, ValueError):
        return None


def _policy_signature(root: Path) -> str:
    path = root / ".auto-orchestrator" / "verification.json"
    try:
        stat = path.stat()
    except OSError:
        return "-"
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _run_fingerprint(path: Path, root: Path) -> str:
    """Run-file identity plus the verification policy it is judged against."""
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}:{_policy_signature(root)}"


def _safe_run_fingerprint(path: Path, root: Path) -> str:
    try:
        return _run_fingerprint(path, root)
    except OSError:
        return f"vanished:{_policy_signature(root)}"


def _refresh_live(summary: dict) -> None:
    """Decay the display-only live flag without re-reading the log."""
    if summary.get("status") == "open":
        last = summary.get("last_event_at") or 0
        summary["live"] = bool(last and time.time() - last <= ACTIVE_WINDOW)
    else:
        summary["live"] = False


def _agent_spans(events: list[dict], end_time: float) -> tuple[list[dict], int]:
    active: dict[str, dict] = {}
    spans: list[dict] = []
    for event in events:
        kind = event.get("event")
        agent = _ident(event.get("agent_id"))
        at = _number(event.get("time"))
        if agent is None or at is None:
            continue
        if kind == "agent-start":
            active.setdefault(agent, {"id": agent, "start": at,
                                      "native_hash": _short(event.get("native_hash"), 64)})
        elif kind == "agent-stop" and agent in active:
            begun = active.pop(agent)
            spans.append({**begun, "end": max(at, begun["start"]), "open": False})
    for begun in active.values():
        spans.append({**begun, "end": max(end_time, begun["start"]), "open": True})
    spans.sort(key=lambda item: (item["start"], item["id"]))
    for span in spans:
        span["short_id"] = span["id"][:8]
        span["duration"] = round(max(0.0, span["end"] - span["start"]), 3)
        native_hash = _short(span.pop("native_hash"), 64)
        span["native_hash"] = native_hash
        span["native_hash_short"] = _short(native_hash, 12)
    concurrency = 0
    if spans:
        points = []
        for span in spans:
            points.append((span["start"], 1))
            points.append((span["end"], -1))
        points.sort(key=lambda item: (item[0], item[1]))
        running = 0
        for _, delta in points:
            running += delta
            concurrency = max(concurrency, running)
    return spans, concurrency


def _checks(events: list[dict]) -> list[dict]:
    result = []
    for event in events:
        if event.get("event") != "check":
            continue
        check_id = _ident(event.get("check_id"))
        at = _number(event.get("time"))
        code = event.get("exit_code")
        if check_id is None or at is None or type(code) is not int:
            continue
        source = "native-tool" if event.get("source") == "native-tool" else "recorded"
        result.append({"id": check_id, "at": at, "exit_code": code,
                       "passed": code == 0, "source": source,
                       "native": source == "native-tool"})
    return result


def _route(events: list[dict]) -> dict | None:
    route = None
    for event in events:
        if event.get("event") != "route":
            continue
        mode = event.get("delegation")
        limit = event.get("max_agents")
        if mode not in ("single", "consider-parallel") or type(limit) is not int:
            continue
        latency = _number(event.get("latency_ms"))
        selected = event.get("selected_count")
        route = {
            "mode": mode,
            "max_agents": max(1, min(3, limit)),
            "latency_ms": round(latency, 1) if latency is not None and 0 <= latency <= 60000 else None,
            "selected_count": selected if type(selected) is int and 0 <= selected <= 2 else None,
            "at": _number(event.get("time")),
        }
    return route


def _adherence(route: dict | None, native_starts: int, max_concurrency: int) -> bool | None:
    if route is None:
        return None
    if route["mode"] == "single":
        return native_starts == 0
    return max_concurrency <= route["max_agents"]


def _verification(events: list[dict], finish: dict | None, verified_event: dict | None,
                  policy) -> dict:
    """Separate gate-verified evidence from a manual self-report."""
    empty = {"status": "none", "verified": False, "self_reported": False,
             "reason": "No terminal event", "check_ids": []}
    if finish is None:
        return empty
    if finish.get("status") != "success":
        return {**empty, "reason": "Run did not finish successfully"}
    proofs = [event for event in events if event.get("event") == "verified"]
    if len(proofs) > 1:
        return {**empty, "status": "rejected", "reason": "Duplicate verification events"}
    if verified_event is None:
        if finish.get("verified"):
            return {"status": "self-reported", "verified": False, "self_reported": True,
                    "reason": "Finish carried a verification flag without an evidence event",
                    "check_ids": []}
        return {**empty, "reason": "No verification event was recorded"}
    source = verified_event.get("source")
    ids = [x for x in verified_event.get("check_ids", []) if isinstance(x, str)] \
        if isinstance(verified_event.get("check_ids"), list) else []
    if source != "quality-gate":
        return {"status": "self-reported", "verified": False, "self_reported": True,
                "reason": "Verification was not attested by the native quality gate",
                "check_ids": ids}
    try:
        decision = assess(events, policy)
    except (ValueError, TypeError, KeyError):
        decision = {"verified": False, "reason": "Verification evidence is invalid", "check_ids": []}
    if decision.get("verified") and set(ids) == set(decision.get("check_ids", [])):
        return {"status": "quality-gate", "verified": True, "self_reported": False,
                "reason": decision.get("reason", "All configured native checks passed"),
                "check_ids": ids}
    return {"status": "rejected", "verified": False, "self_reported": False,
            "reason": decision.get("reason", "Verification evidence was rejected"),
            "check_ids": ids}


def _summary(root: Path, run_id: str, path: Path) -> dict:
    try:
        events, partial = _read_jsonl(path)
    except DashboardError as exc:
        return {
            "id": run_id, "platform": "unknown", "status": "invalid", "live": False,
            "verified": False, "self_reported": False,
            "verification": {"status": "none", "verified": False, "self_reported": False,
                             "reason": str(exc), "check_ids": []},
            "error": str(exc),
            "started_at": 0, "last_event_at": 0, "duration_seconds": None,
            "agent_count": 0, "open_agent_count": 0, "max_concurrency": 0,
            "checks_passed": 0, "checks_failed": 0, "mutation_count": 0,
            "diagnostics": [], "route": None, "routing_advice_followed": None,
            "has_valid_estimate": False, "reason": str(exc),
            "fingerprint": _safe_run_fingerprint(path, root),
            "partial": False,
        }
    fingerprint = _safe_run_fingerprint(path, root)
    return _build_summary(root, run_id, events, partial, fingerprint)


def _build_summary(root: Path, run_id: str, events: list[dict], partial: bool,
                   fingerprint: str) -> dict:
    first = events[0]
    started = _number(first.get("time")) or 0.0
    end_time = max((_number(event.get("time")) or started) for event in events)
    spans, max_concurrency = _agent_spans(events, end_time)
    checks = _checks(events)
    route = _route(events)
    finish = next((event for event in reversed(events) if event.get("event") == "finish"), None)
    verified_event = next((event for event in reversed(events) if event.get("event") == "verified"), None)
    finish_at = _number(finish.get("time")) if finish else None
    diagnostics = sorted({event.get("reason") for event in events
                          if event.get("event") == "diagnostic"
                          and isinstance(event.get("reason"), str)})
    native_starts = sum(event.get("event") == "agent-start" for event in events)
    policy = _policy(root)
    try:
        decision = analyze(run_id, events, policy)
        reason = decision.reason
        has_estimate = decision.has_valid_estimate
        estimate = decision.estimated_parallelism_seconds
        overlap = decision.observed_overlap_seconds
    except (ValueError, TypeError, KeyError):
        reason = "Evidence could not be analyzed"
        has_estimate = False
        estimate = None
        overlap = observed_overlap([(span["id"], span["start"], span["end"])
                                    for span in spans if not span["open"]])
    try:
        diagnosis = diagnose(run_id, events)
    except (ValueError, TypeError, KeyError):
        diagnosis = {"classification": "missing_terminal_reason_unknown", "finished": False}
    now = time.time()
    status = finish.get("status") if finish else "open"
    if status not in ("success", "failed", "cancelled"):
        status = "open"
    verification = _verification(events, finish, verified_event, policy)
    return {
        "id": run_id,
        "platform": first.get("platform") if isinstance(first.get("platform"), str) else "unknown",
        "status": status,
        "live": finish is None and now - end_time <= ACTIVE_WINDOW,
        "verified": verification["verified"],
        "self_reported": verification["self_reported"],
        "verification": verification,
        "started_at": started,
        "last_event_at": end_time,
        "finished_at": finish_at,
        "duration_seconds": round(max(0.0, end_time - started), 3),
        "agent_count": len(spans),
        "open_agent_count": sum(span["open"] for span in spans),
        "max_concurrency": max_concurrency,
        "native_starts": native_starts,
        "observed_overlap_seconds": round(overlap, 3) if overlap is not None else None,
        "estimated_parallelism_seconds": round(estimate, 3) if estimate is not None else None,
        "has_valid_estimate": bool(has_estimate),
        "reason": reason,
        "route": route,
        "routing_advice_followed": _adherence(route, native_starts, max_concurrency),
        "checks_passed": sum(check["passed"] and check.get("native") for check in checks),
        "checks_failed": sum(not check["passed"] for check in checks),
        "mutation_count": sum(event.get("event") == "mutation" for event in events),
        "diagnostics": diagnostics,
        "diagnosis": diagnosis.get("classification", "unknown"),
        "partial": partial,
        "fingerprint": fingerprint,
    }


def _event_view(event: dict, started: float) -> dict:
    kind = event.get("event") if isinstance(event.get("event"), str) else "unknown"
    at = _number(event.get("time"))
    offset = round(max(0.0, at - started), 3) if at is not None else None
    item = {"event": kind, "offset": offset}
    if kind in ("agent-start", "agent-stop"):
        item["agent_id"] = _short(event.get("agent_id"), 8)
        item["native_hash_short"] = _short(event.get("native_hash"), 12)
    elif kind == "check":
        item["check_id"] = event.get("check_id")
        item["exit_code"] = event.get("exit_code")
    elif kind == "route":
        item["delegation"] = event.get("delegation")
        item["max_agents"] = event.get("max_agents")
        item["selected_count"] = event.get("selected_count")
        item["latency_ms"] = _number(event.get("latency_ms"))
    elif kind == "verified":
        item["source"] = event.get("source")
        ids = event.get("check_ids")
        item["check_ids"] = [x for x in ids if isinstance(x, str)][:16] if isinstance(ids, list) else []
    elif kind == "finish":
        item["status"] = event.get("status")
        item["verified"] = bool(event.get("verified"))
    elif kind == "diagnostic":
        item["reason"] = event.get("reason")
    return item


def _detail(root: Path, run_id: str, path: Path) -> dict:
    events, partial = _read_jsonl(path)
    fingerprint = _safe_run_fingerprint(path, root)
    summary = _build_summary(root, run_id, events, partial, fingerprint)
    return _detail_payload(run_id, events, summary, partial=partial)


def _run_files(root: Path) -> list[Path]:
    run_dir = root / ".auto-orchestrator" / "runs"
    try:
        paths = [path for path in run_dir.glob("*.jsonl")
                 if path.is_file() and not path.is_symlink() and ID.fullmatch(path.stem)]
    except OSError:
        return []
    def sort_key(path):
        try:
            return (path.stat().st_mtime_ns, path.name)
        except OSError:
            return (0, path.name)
    return sorted(paths, key=sort_key, reverse=True)[:RUN_LIMIT]


def _fingerprint(root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size};".encode())
    for name in ("supervisor.sqlite3", "verification.json"):
        path = root / ".auto-orchestrator" / name
        try:
            stat = path.stat()
            digest.update(f"{name}:{stat.st_mtime_ns}:{stat.st_size};".encode())
        except OSError:
            pass
    return digest.hexdigest()[:24]


def _git_dir(root: Path) -> Path | None:
    try:
        process = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if process.returncode != 0:
        return None
    value = process.stdout.strip()
    return Path(value).resolve() if value else None


def _worktrees(root: Path) -> list[dict]:
    git_dir = _git_dir(root)
    if git_dir is None:
        return []
    manifest_dir = git_dir / "auto-orchestrator" / "worktrees"
    try:
        paths = sorted(manifest_dir.glob("*.json"))[:20]
    except OSError:
        return []
    result = []
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not _ident(data.get("task")):
            continue
        worktree = data.get("path") if isinstance(data.get("path"), str) else None
        result.append({
            "task": data.get("task"),
            "branch": data.get("branch") if isinstance(data.get("branch"), str) else None,
            "base_sha": _short(data.get("base_sha"), 12),
            "path": worktree,
            "exists": bool(worktree and Path(worktree).is_dir()),
            "dirty_files": None,
        })

    def dirty_count(item: dict):
        if not item["exists"]:
            return None
        try:
            status = subprocess.run(
                ["git", "-C", str(item["path"]), "status", "--porcelain"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if status.returncode == 0:
                return sum(1 for line in status.stdout.splitlines() if line.strip())
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    if result:
        with ThreadPoolExecutor(max_workers=min(4, len(result))) as pool:
            for item, dirty in zip(result, pool.map(dirty_count, result)):
                item["dirty_files"] = dirty
    return result


def _supervisor(root: Path) -> dict:
    path = root / ".auto-orchestrator" / "supervisor.sqlite3"
    if path.is_symlink() or not path.is_file():
        return {"available": False, "runs": []}
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return {"available": False, "runs": []}
    connection.row_factory = sqlite3.Row
    try:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}
        if not {"id", "created", "max_parallel"}.issubset(columns):
            return {"available": False, "runs": []}
        native = "native_supervision" if "native_supervision" in columns else "0"
        review = "require_review" if "require_review" in columns else "0"
        runs = []
        for run in connection.execute(
            f"SELECT id, created, max_parallel, {native} AS native_supervision, "
            f"{review} AS require_review FROM runs ORDER BY created DESC LIMIT 20"
        ):
            task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            rows = connection.execute(
                "SELECT * FROM tasks WHERE run_id=? ORDER BY rowid", (run["id"],)
            ).fetchall()
            bindings = {}
            if "native_bindings" in {r[0] for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}:
                columns = {row["name"] for row in connection.execute(
                    "PRAGMA table_info(native_bindings)")}
                started = "started" if "started" in columns else "0"
                try:
                    bindings = {
                        (row["task_id"], row["attempt"]): (bool(row["started"]),
                                                           bool(row["stopped"]))
                        for row in connection.execute(
                            f"SELECT task_id, attempt, {started} AS started, stopped "
                            "FROM native_bindings WHERE run_id=?",
                            (run["id"],),
                        )
                    }
                except sqlite3.Error:
                    bindings = {}
            tasks = []
            for row in rows:
                task = dict(row)
                task.pop("lease", None)
                for key in ("dependencies", "read_scope", "write_scope"):
                    try:
                        value = json.loads(task.get(key) or "[]")
                        task[key] = [x for x in value if isinstance(x, str)][:40]
                    except ValueError:
                        task[key] = []
                result = None
                if isinstance(task.get("result"), str):
                    try:
                        payload = json.loads(task["result"])
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict):
                        result = {
                            "summary": payload.get("summary") if isinstance(payload.get("summary"), str) else "",
                            "changed_files": [x for x in payload.get("changed_files", [])
                                              if isinstance(x, str)][:100]
                            if isinstance(payload.get("changed_files"), list) else [],
                            "tests": [{"name": x.get("name"), "status": x.get("status")}
                                      for x in payload.get("tests", [])
                                      if isinstance(x, dict) and isinstance(x.get("name"), str)
                                      and x.get("status") in ("passed", "failed", "skipped")][:100]
                            if isinstance(payload.get("tests"), list) else [],
                        }
                tasks.append({
                    "id": task.get("id"),
                    "role": task.get("role"),
                    "objective": task.get("objective"),
                    "state": task.get("state"),
                    "department": (task.get("department")
                                   if "department" in task_columns
                                   and isinstance(task.get("department"), str) else None),
                    "dependencies": task.get("dependencies", []),
                    "read_scope": task.get("read_scope", []),
                    "write_scope": task.get("write_scope", []),
                    "attempts": task.get("attempts", 0),
                    "max_retries": task.get("max_retries", 0),
                    "timeout_seconds": task.get("timeout_seconds"),
                    "started": task.get("started"),
                    "finished": task.get("finished"),
                    "reason": task.get("reason") if isinstance(task.get("reason"), str) else None,
                    "review_attested": bool(task.get("reviewed")) if "reviewed" in task_columns else False,
                    "review_digest": _short(task.get("review_digest"), 12)
                    if "review_digest" in task_columns else None,
                    "native_start_observed": bool(
                        bindings.get((task.get("id"), task.get("attempts")), (False, False))[0])
                    if task.get("state") != "pending" else False,
                    "native_stop_observed": bool(
                        bindings.get((task.get("id"), task.get("attempts")), (False, False))[1])
                    if task.get("state") != "pending" else False,
                    "result": result,
                })
            by_id = {task["id"]: task for task in tasks}
            for task in tasks:
                task["dependencies_met"] = all(
                    by_id.get(dep, {}).get("state") == "completed" for dep in task["dependencies"]
                )
            runs.append({
                "id": run["id"],
                "created": run["created"],
                "max_parallel": run["max_parallel"],
                "native_supervision": bool(run["native_supervision"]),
                "require_review": bool(run["require_review"]),
                "tasks": tasks,
                "complete": bool(tasks) and all(task["state"] == "completed" for task in tasks),
            })
        return {"available": True, "runs": runs}
    except sqlite3.Error:
        return {"available": False, "runs": []}
    finally:
        connection.close()


def _overview(runs: list[dict]) -> dict:
    verified = [run for run in runs if run.get("verified")]
    open_runs = [run for run in runs if run.get("status") == "open"]
    durations = sorted(run["duration_seconds"] for run in verified
                       if run.get("duration_seconds") is not None)
    platforms: dict[str, int] = {}
    for run in runs:
        platform = run.get("platform", "unknown")
        platforms[platform] = platforms.get(platform, 0) + 1
    return {
        "runs": len(runs),
        "verified": len(verified),
        "self_reported": sum(bool(run.get("self_reported")) for run in runs),
        "benchmark_valid": sum(bool(run.get("has_valid_estimate")) for run in runs),
        "verified_rate": round(len(verified) / len(runs), 3) if runs else 0,
        "open": len(open_runs),
        "live": sum(bool(run.get("live")) for run in runs),
        "failed": sum(run.get("status") == "failed" for run in runs),
        "agents": sum(run.get("agent_count", 0) for run in runs),
        "open_agents": sum(run.get("open_agent_count", 0) for run in runs),
        "mutations": sum(run.get("mutation_count", 0) for run in runs),
        "overlap_seconds": round(max((run.get("observed_overlap_seconds") or 0 for run in runs), default=0), 3),
        "overlap_total_seconds": round(sum(run.get("observed_overlap_seconds") or 0 for run in runs), 3),
        "median_verified_seconds": round(durations[len(durations) // 2], 3) if durations else None,
        "platforms": platforms,
    }


@dataclass
class Store:
    root: Path
    demo: bool = False
    demo_profile: str = "ai"
    _cache: dict[str, tuple[str, dict]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _detail_cache: dict[str, tuple[str, dict]] = field(default_factory=dict)
    _supervisor_cache: tuple[str, dict] | None = None
    _worktrees_cache: tuple[float, list[dict]] | None = None
    _demo_runs: dict[str, tuple[dict, dict]] = field(default_factory=dict)
    _demo_state: dict = field(default_factory=dict)

    def __post_init__(self):
        self.root = self.root.resolve()
        if self.demo:
            self._demo_runs, self._demo_state = _demo_data(self.demo_profile)

    def _files(self) -> list[Path]:
        return [] if self.demo else _run_files(self.root)

    def fingerprint(self) -> str:
        if self.demo:
            return self._demo_state["fingerprint"]
        return _fingerprint(self.root, self._files())

    def _supervisor_snapshot(self) -> dict:
        key = _supervisor_signature(self.root)
        with self._lock:
            cached = self._supervisor_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        snapshot = _supervisor(self.root)
        with self._lock:
            self._supervisor_cache = (key, snapshot)
        return snapshot

    def _worktrees_snapshot(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            cached = self._worktrees_cache
        if cached is not None and now - cached[0] < WORKTREE_TTL:
            return cached[1]
        worktrees = _worktrees(self.root)
        with self._lock:
            self._worktrees_cache = (now, worktrees)
        return worktrees

    def state(self) -> dict:
        if self.demo:
            data = json.loads(json.dumps(self._demo_state))
            for run in data["runs"]:
                _refresh_live(run)
            data["overview"] = _overview(data["runs"])
            live = ",".join(f"{run['id']}:{int(bool(run.get('live')))}" for run in data["runs"])
            data["fingerprint"] = "demo-v1:" + hashlib.sha256(live.encode()).hexdigest()[:8]
            return data
        files = self._files()
        summaries_by_id: dict[str, dict] = {}
        pending: list[tuple[Path, str]] = []
        with self._lock:
            cached_runs = dict(self._cache)
        for path in files:
            try:
                fingerprint = _run_fingerprint(path, self.root)
            except OSError:
                continue
            cached = cached_runs.get(path.stem)
            if cached is not None and cached[0] == fingerprint:
                summaries_by_id[path.stem] = cached[1]
            else:
                pending.append((path, fingerprint))
        if pending:
            def build(item: tuple[Path, str]) -> tuple[str, str, dict]:
                path, fingerprint = item
                return path.stem, fingerprint, _summary(self.root, path.stem, path)

            if len(pending) > 1 and STATE_WORKERS > 1:
                with ThreadPoolExecutor(max_workers=min(STATE_WORKERS, len(pending))) as pool:
                    built = list(pool.map(build, pending))
            else:
                built = [build(item) for item in pending]
            with self._lock:
                for run_id, fingerprint, summary in built:
                    self._cache[run_id] = (fingerprint, summary)
                    summaries_by_id[run_id] = summary
                if len(self._cache) > RUN_LIMIT + 100:
                    keep = set(summaries_by_id)
                    self._cache = {key: value for key, value in self._cache.items()
                                   if key in keep}
        summaries = list(summaries_by_id.values())
        summaries.sort(key=lambda item: (item.get("last_event_at") or 0), reverse=True)
        for summary in summaries:
            _refresh_live(summary)
        live_signature = ",".join(
            f"{summary['id']}:{int(bool(summary.get('live')))}"
            for summary in summaries
        )
        supervisor = self._supervisor_snapshot()
        worktrees = self._worktrees_snapshot()
        return {
            "version": 1,
            "demo": False,
            "project": str(self.root),
            "fingerprint": _fingerprint(self.root, files)
            + ":" + hashlib.sha256(json.dumps(supervisor, sort_keys=True, default=str).encode()).hexdigest()[:8]
            + ":" + hashlib.sha256(json.dumps(worktrees, sort_keys=True, default=str).encode()).hexdigest()[:8]
            + ":" + hashlib.sha256(live_signature.encode()).hexdigest()[:8],
            "overview": _overview(summaries),
            "runs": summaries[:RUN_LIMIT],
            "supervisor": supervisor,
            "worktrees": worktrees,
        }

    def detail(self, run_id: str) -> dict:
        if not isinstance(run_id, str) or not ID.fullmatch(run_id):
            raise DashboardError("Invalid run ID")
        if self.demo:
            if run_id not in self._demo_runs:
                raise DashboardError("Run not found")
            _, stored = self._demo_runs[run_id]
            data = json.loads(json.dumps(stored))
            _refresh_live(data["run"])
            return data
        path = path_for(self.root, run_id)
        if path.is_symlink() or not path.is_file():
            raise DashboardError("Run not found")
        try:
            fingerprint = _run_fingerprint(path, self.root)
        except OSError as exc:
            raise DashboardError("Run not found") from exc
        with self._lock:
            cached = self._detail_cache.get(run_id)
        if cached is not None and cached[0] == fingerprint:
            _refresh_live(cached[1]["run"])
            return cached[1]
        payload = _detail(self.root, run_id, path)
        _refresh_live(payload["run"])
        with self._lock:
            self._detail_cache[run_id] = (fingerprint, payload)
            while len(self._detail_cache) > DETAIL_CACHE_LIMIT:
                self._detail_cache.pop(next(iter(self._detail_cache)))
        return payload


def _demo_hash(profile: str, label: str) -> str:
    return hashlib.sha256(
        f"auto-orchestrator-demo|{profile}|{label}".encode("utf-8")).hexdigest()


def _demo_data(profile: str = "ai") -> tuple[dict[str, tuple[dict, dict]], dict]:
    """Clearly synthetic demo evidence. This function never writes to disk."""
    if profile not in ("ai", "security", "web"):
        profile = "ai"
    now = time.time()
    runs: dict[str, tuple[dict, dict]] = {}
    prefix = "" if profile == "ai" else f"{profile}-"
    run_ids = {
        "verified": f"{prefix}demo-verified",
        "unverified": f"{prefix}demo-unverified",
        "incomplete": f"{prefix}demo-incomplete",
        "failed": f"{prefix}demo-failed",
    }
    supervisor_run = "demo-project" if profile == "ai" else f"demo-{profile}"

    def add(run_id, platform, age, spans, finish, checks=(), mutations=(), route=None,
            verified=False, diagnostics=()):
        started = now - age
        events = [{"event": "start", "platform": platform, "time": started}]
        if route:
            events.append({"event": "route", "time": started + 1,
                           "delegation": route["mode"], "max_agents": route["max_agents"],
                           "selected_count": route.get("selected_count", 1),
                           "latency_ms": route.get("latency_ms", 14)})
        for span in spans:
            payload = {"event": "agent-start", "agent_id": span["id"],
                       "time": started + span["start"]}
            if span.get("native_hash"):
                payload["native_hash"] = span["native_hash"]
            events.append(payload)
            if not span.get("open"):
                events.append({"event": "agent-stop", "agent_id": span["id"],
                               "time": started + span["end"]})
        events.extend({"event": "mutation", "time": started + offset} for offset in mutations)
        events.extend({"event": "check", "check_id": name, "exit_code": code,
                       "source": "native-tool", "time": started + offset}
                      for name, code, offset in checks)
        events.extend({"event": "diagnostic", "reason": reason, "time": started + offset}
                      for reason, offset in diagnostics)
        if finish:
            events.append({"event": "finish", "status": finish[0], "time": started + finish[1],
                           "verified": False})
            if verified:
                events.append({"event": "verified", "source": "quality-gate",
                               "time": started + finish[1] + 0.2,
                               "check_ids": sorted(name for name, code, _ in checks if code == 0)})
        events.sort(key=lambda event: event["time"])
        # Build the display detail directly, without writing synthetic files.
        summary = _summary_from_events(run_id, events)
        detail = _detail_payload(run_id, events, summary)
        runs[run_id] = (summary, detail)

    add(run_ids["verified"], "opencode", 90,
        [{"id": "a1f4c9e2b7d01358", "start": 0, "end": 24,
          "native_hash": _demo_hash(profile, "verified-0")},
         {"id": "b82d4e6a91c3f057", "start": 18, "end": 62},
         {"id": "c93e5f7b22a4d168", "start": 50, "end": 76}],
        ("success", 80), checks=[("unit", 0, 66), ("lint", 0, 70)],
        mutations=[22, 40, 55, 61], route={"mode": "consider-parallel", "max_agents": 3,
                                          "selected_count": 1, "latency_ms": 18},
        verified=True)
    add(run_ids["unverified"], "codex", 1500,
        [{"id": "d04f6a8c33b5e279", "start": 0, "end": 64,
          "native_hash": _demo_hash(profile, "unverified-0")},
         {"id": "e15a7b9d44c6f380", "start": 22, "end": 81}],
        ("success", 84), checks=[("unit", 1, 70)], mutations=[18, 44],
        route={"mode": "single", "max_agents": 1, "selected_count": 0, "latency_ms": 9})
    add(run_ids["incomplete"], "antigravity", 100,
        [{"id": "f26b8c0e55d70491", "start": 0, "end": 55, "open": True,
          "native_hash": _demo_hash(profile, "open-0")}],
        None, mutations=[26, 48],
        diagnostics=[("event_stream_closed", 56)])
    add(run_ids["failed"], "cursor", 7200,
        [{"id": "073c9d1f66e815a2", "start": 0, "end": 51,
          "native_hash": _demo_hash(profile, "failed-0")},
         {"id": "184d0e2a77f926b3", "start": 14, "end": 73}],
        ("failed", 76), checks=[("unit", 2, 74)], mutations=[12, 39, 65])

    def task(task_id, role, objective, state, dependencies, read_scope, write_scope,
             attempts, max_retries, timeout_seconds, review_attested, review_digest,
             native_start, native_stop, dependencies_met, result, reason=None,
             department=None):
        return {"id": task_id, "role": role, "objective": objective, "state": state,
                "department": department,
                "dependencies": dependencies, "read_scope": read_scope,
                "write_scope": write_scope, "attempts": attempts,
                "max_retries": max_retries, "timeout_seconds": timeout_seconds,
                "review_attested": review_attested, "review_digest": review_digest,
                "native_start_observed": native_start,
                "native_stop_observed": native_stop,
                "dependencies_met": dependencies_met, "result": result,
                **({"reason": reason} if reason else {})}

    supervisor = {"available": True, "runs": [{
        "id": supervisor_run, "created": now - 900, "max_parallel": 3,
        "native_supervision": True, "require_review": True, "complete": False,
        "tasks": [
            task("schema", "explorer",
                 "Map the billing schema and expose contract risks to the implementation tasks.",
                 "completed", [], ["db/"], [], 1, 2, 900, True, "4d9e02c7a13f", True, True,
                 True, {"summary": "Contract mapped; no migration required.", "changed_files": [],
                        "tests": [{"name": "schema-contract", "status": "passed"}]},
                 department="research"),
            task("api", "implementer",
                 "Implement idempotent invoice retry handling behind the existing feature flag.",
                 "completed", ["schema"], ["db/"], ["src/billing/"], 2, 2, 1200, True,
                 "9ab3c41d7e02", True, True, True,
                 {"summary": "Retry queue is idempotent; 4 unit tests added.",
                  "changed_files": ["src/billing/retry.ts"],
                  "tests": [{"name": "billing-retry", "status": "passed"}]},
                 department="backend"),
            task("tests", "tester",
                 "Add regression coverage for duplicate webhook delivery and verify the retry path end to end.",
                 "running", ["api"], ["src/"], ["tests/"], 1, 1, 900, False, None,
                 False, False, True, None, department="qa"),
            task("review", "reviewer",
                 "Independently review the integrated diff, with no access to the implementer history.",
                 "pending", ["api", "tests"], ["src/", "tests/"], [], 0, 0, 600, False, None,
                 False, False, False, None, department="review"),
            task("docs", "implementer",
                 "Document the retry contract and the operational rollback path.",
                 "failed", ["api"], ["src/"], ["docs/"], 2, 2, 600, False, None,
                 True, True, True, None,
                 reason="Retry limit reached after a broken link check.",
                 department="frontend"),
        ],
    }]}
    worktrees = [
        {"task": "schema", "branch": "auto-orchestrator/schema", "base_sha": "3b1aeb4f2c8d",
         "path": "/tmp/demo/schema", "exists": True, "dirty_files": 0},
        {"task": "api", "branch": "auto-orchestrator/api", "base_sha": "3b1aeb4f2c8d",
         "path": "/tmp/demo/api", "exists": True, "dirty_files": 2},
    ]
    summaries = [summary for summary, _ in runs.values()]
    summaries.sort(key=lambda item: item["last_event_at"], reverse=True)
    state = {
        "version": 1, "demo": True, "project": supervisor_run,
        "fingerprint": "demo-v1" if profile == "ai" else f"demo-{profile}-v1",
        "overview": _overview(summaries),
        "runs": summaries, "supervisor": supervisor, "worktrees": worktrees,
    }
    return runs, state


def _summary_from_events(run_id: str, events: list[dict]) -> dict:
    """Demo-only equivalent of _summary without reading a file."""
    first = events[0]
    started = _number(first.get("time")) or 0.0
    end_time = max((_number(event.get("time")) or started) for event in events)
    spans, max_concurrency = _agent_spans(events, end_time)
    checks = _checks(events)
    route = _route(events)
    finish = next((event for event in reversed(events) if event.get("event") == "finish"), None)
    verified_event = next((event for event in reversed(events) if event.get("event") == "verified"), None)
    diagnostics = sorted({event.get("reason") for event in events
                          if event.get("event") == "diagnostic" and isinstance(event.get("reason"), str)})
    native_starts = sum(event.get("event") == "agent-start" for event in events)
    status = finish.get("status") if finish else "open"
    if status not in ("success", "failed", "cancelled"):
        status = "open"
    checks_ok = all(check["passed"] for check in checks)
    if verified_event is not None and verified_event.get("source") == "quality-gate" and checks_ok \
            and status == "success":
        verification = {"status": "quality-gate", "verified": True, "self_reported": False,
                        "reason": "Synthetic demo evidence: all checks passed", "check_ids": sorted(
                            check["id"] for check in checks if check["passed"])}
    elif finish is not None and finish.get("verified"):
        verification = {"status": "self-reported", "verified": False, "self_reported": True,
                        "reason": "Synthetic demo self-report", "check_ids": []}
    else:
        verification = {"status": "none", "verified": False, "self_reported": False,
                        "reason": "Synthetic demo without verification", "check_ids": []}
    estimate = round(max(0.0, sum(span["duration"] for span in spans)
                         - (end_time - started)), 3)
    return {
        "id": run_id, "platform": first.get("platform", "unknown"), "status": status,
        "live": finish is None and time.time() - end_time <= ACTIVE_WINDOW,
        "verified": verification["verified"],
        "self_reported": verification["self_reported"],
        "verification": verification,
        "started_at": started, "last_event_at": end_time,
        "finished_at": _number(finish.get("time")) if finish else None,
        "duration_seconds": round(max(0.0, end_time - started), 3),
        "agent_count": len(spans), "open_agent_count": sum(span["open"] for span in spans),
        "max_concurrency": max_concurrency, "native_starts": native_starts,
        "observed_overlap_seconds": round(observed_overlap(
            [(span["id"], span["start"], span["end"]) for span in spans]), 3),
        "estimated_parallelism_seconds": estimate,
        "has_valid_estimate": bool(verification["verified"] and status == "success"
                                   and len(spans) >= 2 and estimate > 0),
        "reason": "Demo observation; not a benchmark",
        "route": route,
        "routing_advice_followed": _adherence(route, native_starts, max_concurrency),
        "checks_passed": sum(check["passed"] and check.get("native") for check in checks),
        "checks_failed": sum(not check["passed"] for check in checks),
        "mutation_count": sum(event.get("event") == "mutation" for event in events),
        "diagnostics": diagnostics,
        "diagnosis": "terminal_recorded" if finish else "missing_terminal_subscription_interrupted",
        "partial": False,
        "fingerprint": f"demo:{len(events)}",
    }


def _detail_payload(run_id: str, events: list[dict], summary: dict,
                    partial: bool = False) -> dict:
    started = summary["started_at"]
    end_time = summary["last_event_at"]
    spans, _ = _agent_spans(events, end_time)
    checks = _checks(events)
    finish = next((event for event in reversed(events) if event.get("event") == "finish"), None)
    verified_event = next((event for event in reversed(events) if event.get("event") == "verified"), None)
    for span in spans:
        span["start_offset"] = round(max(0.0, span["start"] - started), 3)
        span["end_offset"] = round(max(0.0, span["end"] - started), 3)
    finish_at = _number(finish.get("time")) if finish else None
    verified_at = _number(verified_event.get("time")) if verified_event else None
    mutations = []
    for event in events:
        if event.get("event") != "mutation":
            continue
        at = _number(event.get("time"))
        if at is not None:
            mutations.append(round(max(0.0, at - started), 3))
    return {
        "run": summary, "agents": spans,
        "checks": [{**check, "offset": round(max(0.0, check["at"] - started), 3)}
                   for check in checks],
        "route": _route(events),
        "finish": {"at": finish_at,
                   "offset": round(max(0.0, finish_at - started), 3)
                   if finish_at is not None else None,
                   "status": finish.get("status"), "verified": bool(finish.get("verified"))}
        if finish else None,
        "verified_event": {"at": verified_at,
                           "offset": round(max(0.0, verified_at - started), 3)
                           if verified_at is not None else None,
                           "source": verified_event.get("source"),
                           "check_ids": [x for x in verified_event.get("check_ids", [])
                                         if isinstance(x, str)]} if verified_event else None,
        "mutations": mutations,
        "markers": [{"at": check["at"], "offset": round(max(0.0, check["at"] - started), 3),
                     "id": check["id"], "passed": check["passed"], "exit_code": check["exit_code"]}
                    for check in checks],
        "events": [_event_view(event, started) for event in events],
        "partial": partial,
        "report_command": f"python tools/perf.py --root . report --run-id {run_id}",
        "diagnose_command": f"python tools/perf.py --root . diagnose --run-id {run_id}",
    }


def _project_meta_digest(meta: dict) -> str:
    try:
        encoded = json.dumps(meta, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        encoded = str(meta)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


def _project_id_for(root: Path) -> str:
    """Deterministic project id, matching ``project_registry`` when present."""
    if project_registry is not None:
        return project_registry.project_id_for(root)
    key = os.path.normcase(os.path.abspath(os.fspath(root)))
    digest = hashlib.sha256(("auto-orchestrator|project|" + key).encode()).hexdigest()
    return "p_" + digest[:16]


@dataclass
class CatalogProject:
    project: dict
    store: Store
    bindings: list[dict] = field(default_factory=list)
    error: str | None = None
    bindings_error: str | None = None


@dataclass
class Catalog:
    """Multi-project view over per-project Stores plus the local registry."""

    projects: dict[str, CatalogProject]
    default_id: str
    registry: object | None = None
    registry_path: Path | None = None
    demo: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _streams: object | None = None

    def __post_init__(self):
        if event_stream is not None:
            self._streams = event_stream.StreamManager(self._provider)

    # -- constructors ---------------------------------------------------
    @classmethod
    def from_store(cls, store: Store) -> "Catalog":
        """Compatibility wrapper for the legacy single-project server."""
        root = store.root
        project_id = _project_id_for(root)
        name = "Demo Project" if store.demo else (root.name or "Default project")
        description = ("Synthetic demo evidence; not a benchmark."
                       if store.demo else "")
        project = {
            "project_id": project_id,
            "name": name,
            "description": description,
            "root": str(root),
            "theme": "ai" if store.demo else "custom",
            "created_at": 0,
            "updated_at": 0,
            "agents": [],
        }
        bindings: list[dict] = []
        bindings_error = None
        if not store.demo and project_registry is not None:
            try:
                bindings = project_registry.load_bindings(root)["bindings"]
            except (project_registry.RegistryError, OSError, ValueError) as exc:
                bindings_error = str(exc)[:200]
        entry = CatalogProject(project=project, store=store, bindings=bindings,
                               bindings_error=bindings_error)
        return cls({project_id: entry}, project_id, demo=store.demo)

    @classmethod
    def load(cls, registry_path=None, *, root=None, no_register=False) -> "Catalog":
        if project_registry is None:
            raise DashboardError("Project registry support is not installed")
        registry = project_registry.ProjectRegistry(registry_path)
        try:
            meta_list = registry.load()["projects"]
        except (project_registry.RegistryError, OSError) as exc:
            if root is not None and no_register:
                meta_list = []
            else:
                raise DashboardError(f"Registry is invalid: {exc}") from exc

        default_id = None
        if root is not None:
            try:
                canonical = project_registry.canonical_root(root)
            except project_registry.RegistryError as exc:
                raise DashboardError(str(exc)) from exc
            default_id = project_registry.project_id_for(canonical)
            if no_register:
                existing = None
                try:
                    existing = registry.find_by_root(canonical)
                except (project_registry.RegistryError, OSError):
                    existing = None
                if existing is not None:
                    default_id = existing["project_id"]
                else:
                    meta_list = meta_list + [{
                        "project_id": default_id,
                        "name": canonical.name or "default",
                        "description": "",
                        "root": str(canonical),
                        "theme": "custom",
                        "created_at": 0,
                        "updated_at": 0,
                        "agents": [],
                    }]
            else:
                try:
                    project, _ = registry.add(canonical)
                except (project_registry.RegistryError, OSError) as exc:
                    raise DashboardError(f"Cannot register project root: {exc}") from exc
                default_id = project["project_id"]
                meta_list = registry.load()["projects"]
        if not meta_list:
            try:
                canonical = Path.cwd().resolve(strict=True)
                if no_register:
                    project_id = project_registry.project_id_for(canonical)
                    meta_list = [{
                        "project_id": project_id, "name": canonical.name or "default",
                        "description": "", "root": str(canonical), "theme": "custom",
                        "created_at": 0, "updated_at": 0, "agents": [],
                    }]
                    default_id = project_id
                else:
                    project, _ = registry.add(canonical)
                    meta_list = registry.load()["projects"]
                    default_id = project["project_id"]
            except (project_registry.RegistryError, OSError) as exc:
                raise DashboardError(f"Cannot initialise a project: {exc}") from exc
        if default_id is None:
            default_id = meta_list[0]["project_id"]
        if not any(item["project_id"] == default_id for item in meta_list):
            raise DashboardError("Default project is not registered")

        projects: dict[str, CatalogProject] = {}
        for meta in meta_list:
            entry_root = Path(meta["root"])
            store = Store(entry_root)
            error = None
            if not entry_root.is_dir():
                error = "Project root is unavailable"
            bindings: list[dict] = []
            bindings_error = None
            if error is None:
                try:
                    bindings = registry.load_bindings(entry_root)["bindings"]
                except (project_registry.RegistryError, OSError) as exc:
                    bindings_error = str(exc)[:200]
            projects[meta["project_id"]] = CatalogProject(
                project=meta, store=store, bindings=bindings, error=error,
                bindings_error=bindings_error)
        return cls(projects, default_id, registry=registry,
                   registry_path=registry.path, demo=False)

    # -- lookup ---------------------------------------------------------
    @property
    def default_store(self) -> Store:
        return self.projects[self.default_id].store

    def _entry(self, project_id: str) -> CatalogProject:
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise DashboardError("Unknown project")
        entry = self.projects.get(project_id)
        if entry is None:
            raise DashboardError("Unknown project")
        return entry

    def project_meta(self, project_id: str) -> dict:
        return self._entry(project_id).project

    def project_state(self, project_id: str, *, legacy=False) -> dict:
        entry = self._entry(project_id)
        if entry.error:
            raise DashboardError(entry.error)
        data = entry.store.state()
        if legacy:
            data["project_id"] = project_id
            data["project_meta"] = entry.project
            data["demo"] = bool(entry.store.demo or self.demo)
        else:
            data["project_id"] = project_id
            data["project"] = entry.project
            data["root"] = entry.project["root"]
            data["demo"] = bool(entry.store.demo or self.demo)
        return data

    # Keep the historical name used by the legacy handler.
    state = project_state

    def project_detail(self, project_id: str, run_id: str, *, legacy=False) -> dict:
        entry = self._entry(project_id)
        if entry.error:
            raise DashboardError(entry.error)
        data = dict(entry.store.detail(run_id))
        if not legacy:
            data["project_id"] = project_id
            data["project"] = entry.project
        return data

    def bindings(self, project_id: str) -> list[dict]:
        entry = self._entry(project_id)
        if self.demo:
            return list(entry.bindings)
        if entry.error or project_registry is None:
            if entry.bindings_error:
                raise DashboardError("Binding evidence is unavailable")
            return list(entry.bindings)
        try:
            return project_registry.load_bindings(entry.project["root"])["bindings"]
        except (project_registry.RegistryError, OSError, ValueError) as exc:
            raise DashboardError("Binding evidence is unavailable") from exc

    # -- aggregate views ------------------------------------------------
    def global_overview(self) -> dict:
        entries = list(self.projects.items())

        def build(item: tuple[str, CatalogProject]) -> tuple[dict, str]:
            project_id, entry = item
            meta = entry.project if isinstance(entry.project, dict) else {}
            agent_list = meta.get("agents")
            agent_count = len(agent_list) if isinstance(agent_list, list) else 0
            fingerprint = f"error:{project_id}"
            base = {
                "project_id": project_id,
                "name": meta.get("name"),
                "description": meta.get("description", ""),
                "root": meta.get("root"),
                "theme": meta.get("theme", "custom"),
                "overview": {
                    "runs": 0, "verified": 0, "open": 0, "live": 0, "agents": 0,
                    "tasks": 0, "active_tasks": 0, "blockers": 0,
                    "last_activity": None, "verified_rate": 0,
                },
                "agent_count": agent_count,
                "linked_agent_count": 0,
                "status": "ok",
                "error": None,
            }
            try:
                state = self.project_state(project_id)
                runs = state.get("runs") or []
                overview = state.get("overview") or {}
                supervisor = state.get("supervisor") or {}
                tasks = [task for run in supervisor.get("runs") or []
                         for task in run.get("tasks") or []]
                active = sum(task.get("state") in ("running", "cancel_requested",
                                                   "timed_out") for task in tasks)
                blockers = sum(task.get("state") in ("failed", "timed_out")
                               for task in tasks)
                stamps = [run.get("last_event_at") or 0 for run in runs]
                stamps.extend(task.get("finished") or task.get("started") or 0
                              for task in tasks)
                stamps.extend(run.get("created") or 0 for run in supervisor.get("runs") or [])
                last = max(stamps) if stamps else None
                bindings = self.bindings(project_id)
                linked = {binding.get("agent_definition_id") for binding in bindings}
                agent_ids = {agent.get("agent_definition_id")
                             for agent in meta.get("agents") or []}
                base["overview"] = {
                    "runs": overview.get("runs", 0),
                    "verified": overview.get("verified", 0),
                    "open": overview.get("open", 0),
                    "live": overview.get("live", 0),
                    "agents": overview.get("agents", 0),
                    "tasks": len(tasks),
                    "active_tasks": active,
                    "blockers": blockers,
                    "last_activity": last or None,
                    "verified_rate": overview.get("verified_rate", 0),
                }
                base["linked_agent_count"] = len(linked & agent_ids)
                fingerprint = str(state.get("fingerprint") or f"ok:{project_id}")
            except Exception as exc:
                base["status"] = "error"
                base["error"] = str(exc)[:200]
                base["linked_agent_count"] = 0
            return base, fingerprint

        if len(entries) > 1 and STATE_WORKERS > 1:
            with ThreadPoolExecutor(max_workers=min(STATE_WORKERS, len(entries))) as pool:
                built = list(pool.map(build, entries))
        else:
            built = [build(item) for item in entries]
        rows = [row for row, _ in built]
        totals = {
            "projects": len(rows),
            "runs": sum(row["overview"]["runs"] for row in rows),
            "verified": sum(row["overview"]["verified"] for row in rows),
            "open": sum(row["overview"]["open"] for row in rows),
            "agents": sum(row["overview"]["agents"] for row in rows),
            "active_tasks": sum(row["overview"]["active_tasks"] for row in rows),
            "blockers": sum(row["overview"]["blockers"] for row in rows),
            "errors": sum(row["status"] == "error" for row in rows),
        }
        digest = hashlib.sha256()
        for (project_id, entry), (_, fingerprint) in zip(entries, built):
            row = next((item for item in rows if item["project_id"] == project_id), {})
            digest.update(f"{project_id}:{fingerprint}:"
                          f"{entry.project.get('name')}:{entry.project.get('theme')}:"
                          f"{len(entry.project.get('agents') or [])}:"
                          f"{row.get('linked_agent_count', 0)}:"
                          f"{entry.project.get('description', '')}:"
                          f"{row.get('error') or ''};".encode())
        return {
            "version": 1,
            "fingerprint": digest.hexdigest()[:24],
            "demo": bool(self.demo),
            "totals": totals,
            "projects": rows,
        }

    def projects_metadata(self) -> dict:
        rows = [{**meta.project} for meta in self.projects.values()]
        return {
            "version": 1,
            "registry_path": str(self.registry_path) if self.registry_path else None,
            "projects": rows,
        }

    # -- office / inspector --------------------------------------------
    def office(self, project_id: str) -> dict:
        if office_model is None:
            raise DashboardError("Agent office support is not installed")
        entry = self._entry(project_id)
        if entry.error:
            raise DashboardError(entry.error)
        state = self.project_state(project_id)
        return office_model.build_office(
            entry.project, state, bindings=self.bindings(project_id),
            detail_provider=entry.store.detail)

    def inspector(self, project_id: str, agent_definition_id: str) -> dict:
        if office_model is None:
            raise DashboardError("Agent office support is not installed")
        entry = self._entry(project_id)
        if entry.error:
            raise DashboardError(entry.error)
        state = self.project_state(project_id)
        try:
            return office_model.build_inspector(
                entry.project, agent_definition_id, state,
                bindings=self.bindings(project_id),
                detail_provider=entry.store.detail)
        except office_model.OfficeModelError as exc:
            raise DashboardError(str(exc)) from exc

    # -- teamwork milestones -------------------------------------------
    @staticmethod
    def _public_session(row: dict) -> dict:
        session_id = row.get("session_id")
        return {
            "task_id": row.get("task_id"),
            "attempt": row.get("attempt"),
            "provider": row.get("provider"),
            "status": row.get("status"),
            "started_at": row.get("started_at"),
            "updated_at": row.get("updated_at"),
            "finished_at": row.get("finished_at"),
            "model": row.get("model"),
            "cost": row.get("cost"),
            "tokens": row.get("tokens"),
            "error": row.get("error"),
            "cancel_state": row.get("cancel_state"),
            "resume_of": row.get("resume_of"),
            "pid_verified": bool(row.get("pid") and row.get("pid_start_token")),
            "session_hash": (hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
                             if isinstance(session_id, str) and session_id else None),
        }

    @staticmethod
    def _attach_recovery(root: Path, run: dict) -> dict:
        run_id = run.get("run_id")
        if not isinstance(run_id, str):
            return run
        base = Path(root) / ".auto-orchestrator" / "teamwork"
        active = read_json(base / "active.json", {}) if read_json else {}
        if not isinstance(active, dict) or active.get("run_id") != run_id:
            active = {}
        connected = bool(owner_alive(active.get("owner"))) if owner_alive else False
        if not connected and lease_held is not None:
            connected = lease_held(root, run_id)
        stored = read_recovery_state(root, run_id) if read_recovery_state else None
        stored = stored if isinstance(stored, dict) else {}
        stored_state = stored.get("state")
        run_status = run.get("status")
        recovery_transitions = {
            "reconciling", "orphaned", "retry_ready", "resuming", "verifying",
        }
        if run_status == "passed":
            state = (stored_state if stored_state in recovery_transitions | {"recovered"}
                     else None)
        elif run_status == "failed":
            state = "recovery_failed" if stored_state in recovery_transitions else None
        elif run_status == "cancelled":
            state = "cancelled"
        elif not connected:
            state = "runner_disconnected"
        else:
            state = stored_state or "active"
        sessions = run.get("managed_sessions", [])
        orphaned = 0
        if not connected and run_status not in ("passed", "cancelled"):
            for session in sessions:
                if session.get("status") in ("starting", "running"):
                    session["effective_status"] = "orphaned"
                    orphaned += 1
        run["owner_connected"] = connected
        run["recovery_state"] = state
        run["recovery"] = {
            "state": state,
            "owner_connected": connected,
            "last_transition": stored,
            "local_signal_only": True,
            "native_cancel_ack": False,
        }
        run["orphaned_managed_sessions"] = orphaned
        if isinstance(run.get("scheduler"), dict) and not connected:
            run["scheduler"] = {**run["scheduler"], "active_sessions": 0}
        return run

    @classmethod
    def _public_milestones(cls, status: dict) -> dict:
        runs = dict(status)
        runs["managed_sessions"] = [cls._public_session(row)
                                    for row in status.get("managed_sessions", [])]
        for milestone in runs.get("milestones", []):
            handoff = milestone.get("handoff_path")
            milestone["handoff"] = (Path(handoff).name if isinstance(handoff, str)
                                    else None)
            milestone.pop("handoff_path", None)
        runs.pop("tasks", None)
        runs["demo"] = False
        return runs

    def memory(self, project_id: str) -> dict:
        entry = self._entry(project_id)
        if entry.store.demo:
            return {
                "available": False, "demo": True, "project_id": project_id,
                "summary": "Demo project memory is not generated.",
                "decisions": [], "requirements": [], "handoff": None,
                "state": {"last_platform": "demo", "stale": False},
            }
        root = Path(entry.store.root)
        if ProjectMemory is None or not (
                root / ".auto-orchestrator" / "memory.sqlite3").is_file():
            return {"available": False, "project_id": project_id,
                    "summary": "No project memory has been recorded yet.",
                    "decisions": [], "requirements": [], "handoff": None,
                    "state": {}}
        try:
            with ProjectMemory(root) as memory:
                interviews = []
                for row in memory.conn.execute(
                        "SELECT interview_id,origin_platform,status,request_summary,"
                        "mode,updated_at FROM interviews WHERE project_id=? "
                        "ORDER BY updated_at DESC LIMIT 5", (memory.project_id,)):
                    interviews.append(dict(row))
                question = None
                row = memory.conn.execute(
                    "SELECT q.* FROM interview_questions q JOIN interviews i "
                    "ON i.interview_id=q.interview_id WHERE i.project_id=? "
                    "AND q.status='open' ORDER BY q.ordinal LIMIT 1",
                    (memory.project_id,)).fetchone()
                if row is not None:
                    question = dict(row)
                    try:
                        question["options"] = json.loads(question.get("options") or "[]")
                    except ValueError:
                        question["options"] = []
                return {
                    "available": True, "project_id": memory.project_id,
                    "summary": memory.context_summary(limit=2400),
                    "state": memory.context_state(),
                    "decisions": memory.list_decisions(limit=50),
                    "requirements": memory.list_requirements(limit=50),
                    "handoff": memory.latest_handoff(),
                    "interviews": interviews,
                    "question": question,
                }
        except (OSError, ValueError, sqlite3.Error) as exc:
            return {"available": False, "project_id": project_id,
                    "summary": "Project memory could not be read.",
                    "error": str(exc)[:300], "decisions": [],
                    "requirements": [], "handoff": None, "state": {}}

    def milestones(self, project_id: str, run_id: str | None = None) -> dict:
        entry = self._entry(project_id)
        if entry.store.demo:
            payload = _demo_milestones(project_id, entry.project.get("theme", "ai"))
            if run_id is not None:
                selected = next((run for run in payload["runs"] if run["run_id"] == run_id), None)
                if selected is None:
                    raise DashboardError("Unknown teamwork run")
                return selected
            return payload
        root = Path(entry.store.root)
        plans = sorted((root / ".auto-orchestrator" / "plans").glob("*.json"))
        try:
            try:
                from tools.teamwork import TeamworkEngine, TeamworkError
            except ModuleNotFoundError:
                from teamwork import TeamworkEngine, TeamworkError  # type: ignore
        except ModuleNotFoundError:
            TeamworkEngine = None  # type: ignore
            TeamworkError = ValueError  # type: ignore
        rows = []
        engine = None
        if TeamworkEngine is not None:
            try:
                for path in plans:
                    if path.is_symlink() or not path.is_file():
                        continue
                    try:
                        plan = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if not isinstance(plan, dict) or not plan.get("milestones"):
                        continue
                    current_run = plan.get("run_id")
                    if not isinstance(current_run, str) or not ID.fullmatch(current_run):
                        continue
                    if run_id is not None and current_run != run_id:
                        continue
                    if engine is None:
                        engine = TeamworkEngine(root)
                    try:
                        rows.append(self._attach_recovery(
                            root, self._public_milestones(engine.status(current_run))))
                    except (TeamworkError, OSError, ValueError):
                        continue
            finally:
                if engine is not None:
                    engine.close()
        rows.sort(key=lambda item: item.get("milestones", [{}])[0].get("started_at") or 0,
                  reverse=True)
        if run_id is not None:
            if not rows:
                raise DashboardError("Unknown teamwork run")
            return rows[0]
        passed = sum(1 for run in rows for milestone in run.get("milestones", [])
                     if milestone.get("status") == "passed")
        total = sum(len(run.get("milestones", [])) for run in rows)
        return {
            "version": 1,
            "project_id": project_id,
            "runs": rows,
            "current_run_id": rows[0]["run_id"] if rows else None,
            "progress": {
                "passed": passed,
                "total": total,
                "verified": passed,
                "denominator": total,
            },
            "verified_progress": (passed / total) if total else 0.0,
            "estimated_progress": None,
            "demo": False,
        }

    # -- live events ----------------------------------------------------
    def _provider(self, project_id: str):
        return lambda: self.project_state(project_id)

    def stream_hub(self, project_id: str):
        entry = self._entry(project_id)
        if entry.error:
            raise DashboardError(entry.error)
        if self._streams is None:
            raise DashboardError("Live event streaming is not installed")
        return self._streams.hub(project_id)

    def close(self) -> None:
        if self._streams is not None:
            self._streams.close()


def _demo_milestones(project_id: str, theme: str = "ai") -> dict:
    """Clearly synthetic milestones for the demo catalog; never written to disk."""
    milestones = [
        {
            "id": "M1", "objective": "Discover the existing interfaces",
            "dependencies": [], "status": "passed", "started_at": 0, "completed_at": 0,
            "repair_iterations": 0, "max_repair": 1,
            "acceptance": ["Boundaries and test strategy recorded"],
            "verification_policy": {"require_roles": [], "max_repair": 1},
            "require_roles": [], "satisfied_roles": [], "missing_roles": [],
            "verdicts": {}, "handoff": "M1-demo.json", "last_error": None,
            "tasks": [{
                "id": "schema", "role": "explorer", "state": "completed",
                "milestone": "M1", "phase": "build", "attempts": 1,
                "max_retries": 1, "department": "research", "reason": None,
                "result": {"summary": "Synthetic demo discovery", "tests": [], "findings": [],
                           "verdict": None},
                "native_stop_observed": False, "review_attested": False,
                "provider": "demo", "model": None, "verify_commands": [],
            }],
        },
        {
            "id": "M2", "objective": "Build and verify the feature",
            "dependencies": ["M1"], "status": "verifying", "started_at": 0,
            "completed_at": None, "repair_iterations": 0, "max_repair": 2,
            "acceptance": ["Tests pass", "Reviewer verdict is pass"],
            "verification_policy": {"require_roles": ["reviewer"], "max_repair": 2},
            "require_roles": ["reviewer"], "satisfied_roles": [], "missing_roles": ["reviewer"],
            "verdicts": {}, "handoff": None, "last_error": None,
            "tasks": [
                {"id": "api", "role": "implementer", "state": "completed",
                 "milestone": "M2", "phase": "build", "attempts": 1, "max_retries": 2,
                 "department": "backend", "reason": None,
                 "result": {"summary": "Synthetic demo API", "tests": [{"name": "demo", "status": "passed"}],
                            "findings": [], "verdict": None},
                 "native_stop_observed": False, "review_attested": False,
                 "provider": "demo", "model": None, "verify_commands": []},
                {"id": "review", "role": "reviewer", "state": "pending",
                 "milestone": "M2", "phase": "verify", "attempts": 0, "max_retries": 1,
                 "department": "review", "reason": None, "result": None,
                 "native_stop_observed": False, "review_attested": False,
                 "provider": "demo", "model": None, "verify_commands": []},
            ],
        },
        {
            "id": "M3", "objective": "Final success audit",
            "dependencies": ["M2"], "status": "planned", "started_at": None,
            "completed_at": None, "repair_iterations": 0, "max_repair": 1,
            "acceptance": ["Final E2E passes"],
            "verification_policy": {"require_roles": ["success-auditor"], "max_repair": 1},
            "require_roles": ["success-auditor"], "satisfied_roles": [],
            "missing_roles": ["success-auditor"], "verdicts": {}, "handoff": None,
            "last_error": None, "tasks": [],
        },
    ]
    passed = sum(1 for milestone in milestones if milestone["status"] == "passed")
    run_id = "demo-project" if theme == "ai" else f"demo-{theme}"
    return {
        "version": 1, "project_id": project_id, "demo": True,
        "runs": [{
            "run_id": run_id, "pattern": "distributed_coding",
            "work_mode": "balanced", "risk_assessment": {
                "mode": "balanced", "score": 26, "confidence": "low",
                "reason": "demo default safe budget",
                "factors": {"profile_available": False},
            },
            "max_parallel": 3, "status": "running", "complete": False,
            "milestones": milestones, "task_count": 3, "milestone_count": 3,
            "passed_milestones": passed, "verified_progress": passed / 3,
            "estimated_progress": None,
            "progress": {"passed": passed, "total": 3, "verified": passed,
                         "denominator": 3},
            "managed_sessions": [], "demo": True,
            "scheduler": {"active_sessions": 1, "queued_tasks": 1,
                          "repairing": False, "max_parallel": 3},
            "owner_connected": True,
            "recovery_state": "active",
            "recovery": {
                "state": "active", "owner_connected": True,
                "last_transition": {"state": "active", "updated_at": None},
                "local_signal_only": True, "native_cancel_ack": False,
            },
            "orphaned_managed_sessions": 0,
            "usage": {"cost_total_usd": None, "cost_measured": False,
                      "tool_count_total": None,
                      "tokens": {"input": None, "output": None, "cache_read": None},
                      "tokens_measured": False},
        }],
        "current_run_id": run_id,
        "progress": {"passed": passed, "total": 3, "verified": passed, "denominator": 3},
        "verified_progress": passed / 3, "estimated_progress": None, "demo": True,
    }


def _demo_catalog() -> Catalog:
    """Three fixed synthetic projects. Never reads or writes evidence."""
    base = Path(tempfile.gettempdir()) / "auto-orchestrator-demo"
    profiles = (
        {
            "slug": "ai-lab", "profile": "ai", "name": "AI Research Lab", "theme": "ai",
            "description": "Synthetic demo: retrieval experiments and evaluation runs.",
            "agents": [
                ("a_000000000001", "Ada", "RAG Engineer", "opencode", None, "#7dd3fc",
                 None),
                ("a_000000000002", "Grace", "Evaluation Lead", "codex", None, "#fbbf24",
                 None),
                ("a_000000000003", "Linus", "Runtime Engineer", "antigravity", None,
                 "#34d399", "research"),
                ("a_000000000004", "Margaret", "Research Director", "cursor", None,
                 "#c084fc", None),
                ("a_000000000005", "Sofia", "Release Engineer", "opencode", None,
                 "#fb7185", None),
            ],
            "bindings": [
                ("a_000000000001", "task", "api"),
                ("a_000000000002", "task", "tests"),
                ("a_000000000003", "hash", "open-0"),
                ("a_000000000004", "task", "review"),
                ("a_000000000005", "task", "docs"),
            ],
        },
        {
            "slug": "security-lab", "profile": "security", "name": "Security Lab",
            "theme": "security",
            "description": "Synthetic demo: patch verification and incident review.",
            "agents": [
                ("a_000000000011", "Mira", "Security Engineer", "cursor", None,
                 "#f87171", None),
                ("a_000000000012", "Kai", "AppSec Reviewer", "opencode", None,
                 "#fbbf24", None),
                ("a_000000000013", "Tomas", "Threat Modeler", "codex", None,
                 "#60a5fa", None),
            ],
            "bindings": [
                ("a_000000000011", "hash", "failed-0"),
                ("a_000000000012", "hash", "unverified-0"),
            ],
        },
        {
            "slug": "web-studio", "profile": "web", "name": "Web Studio", "theme": "web",
            "description": "Synthetic demo: interface work and release verification.",
            "agents": [
                ("a_000000000021", "Noor", "Frontend Engineer", "opencode", None,
                 "#34d399", None),
                ("a_000000000022", "Ravi", "Accessibility Lead", "codex", None,
                 "#7dd3fc", None),
                ("a_000000000023", "Ines", "Design Engineer", "antigravity", None,
                 "#c084fc", None),
            ],
            "bindings": [
                ("a_000000000021", "hash", "verified-0"),
            ],
        },
    )
    projects: dict[str, CatalogProject] = {}
    default_id = None
    for spec in profiles:
        root = base / spec["slug"]
        project_id = _project_id_for(root)
        if default_id is None:
            default_id = project_id
        agents = [
            {
                "agent_definition_id": agent_id,
                "name": name,
                "role": role,
                "platform": platform,
                "model": model,
                "color": color,
                "created_at": 0,
                **({"default_room": default_room} if default_room else {}),
            }
            for agent_id, name, role, platform, model, color, default_room in spec["agents"]
        ]
        project = {
            "project_id": project_id,
            "name": spec["name"],
            "description": "DEMO — " + spec["description"],
            "root": str(root),
            "theme": spec["theme"],
            "created_at": 0,
            "updated_at": 0,
            "agents": agents,
        }
        supervisor_run = "demo-project" if spec["profile"] == "ai" \
            else f"demo-{spec['profile']}"
        run_ids = {
            "verified": ("demo-verified" if spec["profile"] == "ai"
                         else f"{spec['profile']}-demo-verified"),
            "unverified": ("demo-unverified" if spec["profile"] == "ai"
                           else f"{spec['profile']}-demo-unverified"),
            "incomplete": ("demo-incomplete" if spec["profile"] == "ai"
                           else f"{spec['profile']}-demo-incomplete"),
            "failed": ("demo-failed" if spec["profile"] == "ai"
                       else f"{spec['profile']}-demo-failed"),
        }
        hash_runs = {"open-0": run_ids["incomplete"], "failed-0": run_ids["failed"],
                     "unverified-0": run_ids["unverified"], "verified-0": run_ids["verified"]}
        bindings = []
        for agent_id, kind, label in spec["bindings"]:
            digest = _demo_hash(spec["profile"], label)
            if kind == "task":
                bindings.append({
                    "agent_definition_id": agent_id,
                    "native_hash": digest,
                    "run_id": supervisor_run,
                    "session_id": None,
                    "task_id": label,
                    "bound_at": 0,
                })
            else:
                bindings.append({
                    "agent_definition_id": agent_id,
                    "native_hash": digest,
                    "run_id": hash_runs[label],
                    "session_id": None,
                    "task_id": None,
                    "bound_at": 0,
                })
        store = Store(root, demo=True, demo_profile=spec["profile"])
        projects[project_id] = CatalogProject(project=project, store=store,
                                              bindings=bindings)
    return Catalog(projects, default_id, registry=None, registry_path=None, demo=True)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store: Store | None = None, *,
                 catalog: Catalog | None = None):
        if catalog is None:
            if store is None:
                raise DashboardError("A Store or Catalog is required")
            catalog = Catalog.from_store(store)
        self.catalog = catalog
        self.store = store if store is not None else catalog.default_store
        self.stopping = threading.Event()
        super().__init__(address, DashboardHandler)

    def handle_error(self, request, client_address):
        """Browsers routinely reset SSE/keep-alive sockets; do not spam stderr."""
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)

    def shutdown(self):
        self.stopping.set()
        super().shutdown()

    def server_close(self):
        self.stopping.set()
        self.catalog.close()
        super().server_close()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AutoOrchestratorDashboard/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _headers(self, status, content_type, length, cache="no-store", *,
                 encoding=None, vary=False, etag=None):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        if encoding:
            self.send_header("Content-Encoding", encoding)
        if vary:
            self.send_header("Vary", "Accept-Encoding")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache)
        self._security_headers()
        self.end_headers()

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )

    def _accepts_gzip(self) -> bool:
        header = self.headers.get("Accept-Encoding")
        if not header:
            return False
        for item in header.split(","):
            pieces = [piece.strip() for piece in item.split(";") if piece.strip()]
            if not pieces or pieces[0].lower() not in ("gzip", "*"):
                continue
            quality = 1.0
            for parameter in pieces[1:]:
                name, _, value = parameter.partition("=")
                if name.strip().lower() == "q":
                    try:
                        quality = float(value)
                    except ValueError:
                        quality = 0.0
            if quality > 0:
                return True
        return False

    def _transport(self, body: bytes) -> tuple[bytes, str | None]:
        if body and self._accepts_gzip():
            return gzip.compress(body, compresslevel=6, mtime=0), "gzip"
        return body, None

    @staticmethod
    def _normalize_etag(value: str) -> str:
        value = value.strip()
        if value.startswith("W/"):
            value = value[2:].strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        return value

    def _if_none_match(self, etag: str) -> bool:
        header = self.headers.get("If-None-Match")
        if not header:
            return False
        for candidate in header.split(","):
            candidate = candidate.strip()
            if candidate == "*":
                return True
            if self._normalize_etag(candidate) == self._normalize_etag(etag):
                return True
        return False

    def _json(self, payload, status=HTTPStatus.OK, etag=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        body, encoding = self._transport(body)
        self._headers(status, "application/json; charset=utf-8", len(body),
                      encoding=encoding, vary=True, etag=etag)
        self.wfile.write(body)

    def _not_modified(self, etag: str):
        self._headers(HTTPStatus.NOT_MODIFIED, "application/json; charset=utf-8", None,
                      vary=True, etag=etag)

    def _error(self, status, message):
        self._json({"error": message}, status)

    def _asset(self, filename, content_type, *, body=True):
        payload = _asset_payload(filename)
        if payload is None:
            return self._error(HTTPStatus.NOT_FOUND, "Asset not found")
        raw, compressed = payload
        if self._accepts_gzip():
            if compressed is None:
                compressed = _asset_gzip(filename, raw)
            data, encoding = compressed, "gzip"
        else:
            data, encoding = raw, None
        self._headers(HTTPStatus.OK, content_type, len(data), cache="no-cache",
                      encoding=encoding, vary=True)
        if body:
            self.wfile.write(data)

    def _trusted_request(self) -> bool:
        """Reject DNS-rebinding hosts and cross-site browser requests."""
        port = str(self.server.server_address[1])
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme != "http" or (parsed.netloc or "").lower() not in allowed:
                return False
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        return True

    def do_GET(self):
        if not self._trusted_request():
            return self._error(HTTPStatus.FORBIDDEN, "Untrusted Host or Origin")
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ASSETS:
            filename, content_type = ASSETS[route]
            return self._asset(filename, content_type)
        if route == "/api/health":
            return self._json({"ok": True})
        if route == "/api/state":
            return self._legacy_state()
        if route.startswith("/api/run/"):
            return self._legacy_detail(route)
        if route == "/api/global":
            return self._global_state()
        if route == "/api/projects":
            return self._json(self.server.catalog.projects_metadata())
        if route.startswith("/api/projects/"):
            return self._project_route(route)
        return self._error(HTTPStatus.NOT_FOUND, "Not found")

    # -- legacy single-project routes ----------------------------------
    def _legacy_state(self):
        catalog = self.server.catalog
        try:
            state = catalog.project_state(catalog.default_id, legacy=True)
        except (OSError, ValueError) as exc:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        meta = catalog.project_meta(catalog.default_id)
        etag = f'"legacy-{state["fingerprint"]}-{_project_meta_digest(meta)}"'
        if self._if_none_match(etag):
            return self._not_modified(etag)
        return self._json(state, etag=etag)

    def _legacy_detail(self, route: str):
        run_id = unquote(route[len("/api/run/"):])
        try:
            return self._json(self.server.catalog.project_detail(
                self.server.catalog.default_id, run_id, legacy=True))
        except DashboardError as exc:
            return self._error(HTTPStatus.NOT_FOUND, str(exc))
        except (OSError, ValueError) as exc:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    # -- aggregate ------------------------------------------------------
    def _global_state(self):
        try:
            payload = self.server.catalog.global_overview()
        except (OSError, ValueError) as exc:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        etag = f'"g-{payload["fingerprint"]}"'
        if self._if_none_match(etag):
            return self._not_modified(etag)
        return self._json(payload, etag=etag)

    # -- project-scoped routes -----------------------------------------
    def _project_failure(self, project_id: str, exc):
        entry = self.server.catalog.projects.get(project_id)
        if entry is not None and entry.error:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        return self._error(HTTPStatus.NOT_FOUND, str(exc))

    def _project_route(self, route: str):
        parts = route[len("/api/projects/"):].split("/")
        project_id = unquote(parts[0]) if parts else ""
        if not PROJECT_ID.fullmatch(project_id):
            return self._error(HTTPStatus.NOT_FOUND, "Unknown project")
        catalog = self.server.catalog
        if project_id not in catalog.projects:
            return self._error(HTTPStatus.NOT_FOUND, "Unknown project")
        if len(parts) == 2 and parts[1] == "state":
            try:
                state = catalog.project_state(project_id)
            except DashboardError as exc:
                return self._project_failure(project_id, exc)
            except (OSError, ValueError) as exc:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            etag = (f'"project-{state["fingerprint"]}-'
                    f'{_project_meta_digest(catalog.project_meta(project_id))}"')
            if self._if_none_match(etag):
                return self._not_modified(etag)
            return self._json(state, etag=etag)
        if len(parts) == 2 and parts[1] == "office":
            try:
                return self._json(catalog.office(project_id))
            except DashboardError as exc:
                return self._project_failure(project_id, exc)
            except (OSError, ValueError) as exc:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        if len(parts) == 2 and parts[1] == "milestones":
            try:
                return self._json(catalog.milestones(project_id))
            except DashboardError as exc:
                return self._project_failure(project_id, exc)
            except (OSError, ValueError) as exc:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        if len(parts) == 3 and parts[1] == "milestones":
            run_id = unquote(parts[2])
            if not ID.fullmatch(run_id):
                return self._error(HTTPStatus.NOT_FOUND, "Unknown teamwork run")
            try:
                return self._json(catalog.milestones(project_id, run_id))
            except DashboardError as exc:
                return self._project_failure(project_id, exc)
            except (OSError, ValueError) as exc:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        if len(parts) == 2 and parts[1] == "memory":
            try:
                return self._json(catalog.memory(project_id))
            except DashboardError as exc:
                return self._project_failure(project_id, exc)
            except (OSError, ValueError, sqlite3.Error) as exc:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        if len(parts) == 2 and parts[1] == "events":
            return self._events(project_id)
        if len(parts) == 3 and parts[1] == "runs":
            run_id = unquote(parts[2])
            try:
                return self._json(catalog.project_detail(project_id, run_id))
            except DashboardError as exc:
                return self._project_failure(project_id, exc)
            except (OSError, ValueError) as exc:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        if len(parts) == 3 and parts[1] == "agents":
            agent_id = unquote(parts[2])
            try:
                return self._json(catalog.inspector(project_id, agent_id))
            except DashboardError as exc:
                return self._project_failure(project_id, exc)
            except (OSError, ValueError) as exc:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        return self._error(HTTPStatus.NOT_FOUND, "Not found")

    # -- Server-Sent Events --------------------------------------------
    def _events(self, project_id: str):
        if event_stream is None:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE,
                               "Live event streaming is not installed")
        try:
            hub = self.server.catalog.stream_hub(project_id)
        except DashboardError as exc:
            return self._project_failure(project_id, exc)
        except (OSError, ValueError) as exc:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        try:
            subscriber = hub.subscribe(self.headers.get("Last-Event-ID"))
        except event_stream.StreamError as exc:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._security_headers()
        self.end_headers()
        heartbeat = subscriber.heartbeat
        try:
            self.connection.settimeout(max(5.0, heartbeat * 2))
            self.wfile.write(event_stream.RETRY_FRAME)
            self.wfile.flush()
            for event in subscriber.initial:
                self.wfile.write(event.encode())
            self.wfile.flush()
            next_beat = time.monotonic() + heartbeat
            while not self.server.stopping.is_set():
                now = time.monotonic()
                timeout = max(0.05, min(1.0, next_beat - now))
                try:
                    item = subscriber.next(timeout)
                except queue.Empty:
                    if time.monotonic() >= next_beat:
                        self.wfile.write(event_stream.HEARTBEAT_FRAME)
                        self.wfile.flush()
                        next_beat = time.monotonic() + heartbeat
                    continue
                if item is None:
                    break
                self.wfile.write(item.encode())
                self.wfile.flush()
                next_beat = time.monotonic() + heartbeat
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError,
                TypeError):
            pass
        finally:
            subscriber.close()

    def do_HEAD(self):
        if not self._trusted_request():
            return self._error(HTTPStatus.FORBIDDEN, "Untrusted Host or Origin")
        parsed = urlparse(self.path)
        if parsed.path in ASSETS:
            filename, content_type = ASSETS[parsed.path]
            return self._asset(filename, content_type, body=False)
        return self._error(HTTPStatus.METHOD_NOT_ALLOWED, "HEAD is limited to static assets")

    def do_POST(self):
        if not self._trusted_request():
            return self._error(HTTPStatus.FORBIDDEN, "Untrusted Host or Origin")
        return self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Dashboard is read-only")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None, type=Path,
                        help="Default project directory (auto-registered); omit to use the first registered project")
    parser.add_argument("--registry", default=None, type=Path,
                        help="Project registry (default: $AUTO_ORCHESTRATOR_HOME/projects.json)")
    parser.add_argument("--no-register", action="store_true",
                        help="Serve --root without writing it to the registry")
    parser.add_argument("--port", default=8765, type=int, help="Loopback port (default: 8765)")
    parser.add_argument("--demo", action="store_true", help="Serve clearly labelled synthetic sample evidence")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.root is not None:
        root = args.root.expanduser()
        if not root.is_dir():
            parser.error(f"Project directory does not exist: {root}")
    else:
        root = None
    try:
        catalog = _demo_catalog() if args.demo else Catalog.load(
            args.registry, root=root, no_register=args.no_register)
    except (DashboardError, OSError, ValueError) as exc:
        print(f"DASHBOARD ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        server = DashboardServer(("127.0.0.1", args.port), catalog.default_store,
                                 catalog=catalog)
    except OSError as exc:
        catalog.close()
        print(f"DASHBOARD ERROR: cannot bind 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 1
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    print("Auto-Orchestrator dashboard — read-only, loopback only")
    print(f"  {url}")
    print(f"  default project: {catalog.project_meta(catalog.default_id)['root']}")
    print(f"  projects: {len(catalog.projects)}")
    if catalog.registry_path is not None:
        print(f"  registry: {catalog.registry_path}")
    print(f"  mode: {'DEMO (synthetic, not a benchmark)' if args.demo else 'live evidence'}")
    print("  Ctrl-C to stop")
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.stopping.set()
        server.server_close()
        catalog.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
