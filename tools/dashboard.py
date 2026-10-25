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
import os
import re
import sqlite3
import subprocess
import sys
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
    from tools.quality_gate import assess, load_policy
except ModuleNotFoundError:  # Running as ``python tools/dashboard.py``.
    from perf import analyze, diagnose, observed_overlap, path_for
    from quality_gate import assess, load_policy

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
RUN_LIMIT = 500
EVENT_LIMIT = 10000
LOG_LIMIT = 8 * 1024 * 1024
ASSET_DIR = Path(__file__).resolve().parent / "dashboard_assets"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
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
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


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
    for index, line in enumerate(lines):
        try:
            value = json.loads(line)
        except ValueError:
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
        span["native_hash_short"] = _short(span.pop("native_hash"), 12)
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
        result.append({"id": check_id, "at": at, "exit_code": code,
                       "passed": code == 0, "source": "native-tool"})
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
            "fingerprint": _run_fingerprint(path, root),
            "partial": False,
        }
    fingerprint = _run_fingerprint(path, root)
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
        "checks_passed": sum(check["passed"] for check in checks),
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
    fingerprint = _run_fingerprint(path, root)
    summary = _build_summary(root, run_id, events, partial, fingerprint)
    return _detail_payload(run_id, events, summary, partial=partial)


def _run_files(root: Path) -> list[Path]:
    run_dir = root / ".auto-orchestrator" / "runs"
    try:
        paths = [path for path in run_dir.glob("*.jsonl")
                 if path.is_file() and not path.is_symlink() and ID.fullmatch(path.stem)]
    except OSError:
        return []
    return sorted(paths, key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)[:RUN_LIMIT]


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
                try:
                    bindings = {
                        (row["task_id"], row["attempt"]): bool(row["stopped"])
                        for row in connection.execute(
                            "SELECT task_id, attempt, stopped FROM native_bindings WHERE run_id=?",
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
                    "native_stop_observed": bool(bindings.get((task.get("id"), task.get("attempts")), False))
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
            self._demo_runs, self._demo_state = _demo_data()

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
            data["generated_at"] = time.time()
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
        supervisor = self._supervisor_snapshot()
        worktrees = self._worktrees_snapshot()
        return {
            "version": 1,
            "demo": False,
            "generated_at": time.time(),
            "project": str(self.root),
            "fingerprint": _fingerprint(self.root, files)
            + ":" + hashlib.sha256(json.dumps(supervisor, sort_keys=True, default=str).encode()).hexdigest()[:8]
            + ":" + hashlib.sha256(json.dumps(worktrees, sort_keys=True, default=str).encode()).hexdigest()[:8],
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
            data["run"]["generated_at"] = time.time()
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


def _demo_data() -> tuple[dict[str, tuple[dict, dict]], dict]:
    now = time.time()
    runs: dict[str, tuple[dict, dict]] = {}

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
            events.append({"event": "agent-start", "agent_id": span["id"],
                           "time": started + span["start"]})
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

    add("demo-verified", "opencode", 90,
        [{"id": "a1f4c9e2b7d01358", "start": 0, "end": 24},
         {"id": "b82d4e6a91c3f057", "start": 18, "end": 62},
         {"id": "c93e5f7b22a4d168", "start": 50, "end": 76}],
        ("success", 80), checks=[("unit", 0, 66), ("lint", 0, 70)],
        mutations=[22, 40, 55, 61], route={"mode": "consider-parallel", "max_agents": 3,
                                          "selected_count": 1, "latency_ms": 18},
        verified=True)
    add("demo-unverified", "codex", 1500,
        [{"id": "d04f6a8c33b5e279", "start": 0, "end": 64},
         {"id": "e15a7b9d44c6f380", "start": 22, "end": 81}],
        ("success", 84), checks=[("unit", 1, 70)], mutations=[18, 44],
        route={"mode": "single", "max_agents": 1, "selected_count": 0, "latency_ms": 9})
    add("demo-incomplete", "antigravity", 100,
        [{"id": "f26b8c0e55d70491", "start": 0, "end": 55, "open": True}],
        None, mutations=[26, 48],
        diagnostics=[("event_stream_closed", 56)])
    add("demo-failed", "cursor", 7200,
        [{"id": "073c9d1f66e815a2", "start": 0, "end": 51},
         {"id": "184d0e2a77f926b3", "start": 14, "end": 73}],
        ("failed", 76), checks=[("unit", 2, 74)], mutations=[12, 39, 65])

    supervisor = {"available": True, "runs": [{
        "id": "demo-project", "created": now - 900, "max_parallel": 3,
        "native_supervision": True, "require_review": True, "complete": False,
        "tasks": [
            {"id": "schema", "role": "explorer", "objective": "Map the billing schema and expose contract risks to the implementation tasks.",
             "state": "completed", "dependencies": [], "read_scope": ["db/"], "write_scope": [],
             "attempts": 1, "max_retries": 2, "timeout_seconds": 900, "review_attested": True,
             "review_digest": "4d9e02c7a13f", "native_stop_observed": True, "dependencies_met": True,
             "result": {"summary": "Contract mapped; no migration required.", "changed_files": [],
                        "tests": [{"name": "schema-contract", "status": "passed"}]}},
            {"id": "api", "role": "implementer", "objective": "Implement idempotent invoice retry handling behind the existing feature flag.",
             "state": "completed", "dependencies": ["schema"], "read_scope": ["db/"], "write_scope": ["src/billing/"],
             "attempts": 2, "max_retries": 2, "timeout_seconds": 1200, "review_attested": True,
             "review_digest": "9ab3c41d7e02", "native_stop_observed": True, "dependencies_met": True,
             "result": {"summary": "Retry queue is idempotent; 4 unit tests added.", "changed_files": ["src/billing/retry.ts"],
                        "tests": [{"name": "billing-retry", "status": "passed"}]}},
            {"id": "tests", "role": "tester", "objective": "Add regression coverage for duplicate webhook delivery and verify the retry path end to end.",
             "state": "running", "dependencies": ["api"], "read_scope": ["src/"], "write_scope": ["tests/"],
             "attempts": 1, "max_retries": 1, "timeout_seconds": 900, "review_attested": False,
             "review_digest": None, "native_stop_observed": False, "dependencies_met": True,
             "result": None},
            {"id": "review", "role": "reviewer", "objective": "Independently review the integrated diff, with no access to the implementer history.",
             "state": "pending", "dependencies": ["api", "tests"], "read_scope": ["src/", "tests/"], "write_scope": [],
             "attempts": 0, "max_retries": 0, "timeout_seconds": 600, "review_attested": False,
             "review_digest": None, "native_stop_observed": False, "dependencies_met": False,
             "result": None},
            {"id": "docs", "role": "implementer", "objective": "Document the retry contract and the operational rollback path.",
             "state": "failed", "dependencies": ["api"], "read_scope": ["src/"], "write_scope": ["docs/"],
             "attempts": 2, "max_retries": 2, "timeout_seconds": 600, "review_attested": False,
             "review_digest": None, "native_stop_observed": True, "dependencies_met": True,
             "result": None, "reason": "Retry limit reached after a broken link check."},
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
        "version": 1, "demo": True, "generated_at": now, "project": "demo-project",
        "fingerprint": "demo-v1", "overview": _overview(summaries),
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
        "checks_passed": sum(check["passed"] for check in checks),
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
    return {
        "run": summary, "agents": spans,
        "checks": [{**check, "offset": round(max(0.0, check["at"] - started), 3)}
                   for check in checks],
        "route": _route(events),
        "finish": {"at": finish.get("time"), "offset": round(max(0.0, finish["time"] - started), 3),
                   "status": finish.get("status"), "verified": bool(finish.get("verified"))}
        if finish else None,
        "verified_event": {"at": verified_event.get("time"),
                           "offset": round(max(0.0, verified_event["time"] - started), 3),
                           "source": verified_event.get("source"),
                           "check_ids": [x for x in verified_event.get("check_ids", [])
                                         if isinstance(x, str)]} if verified_event else None,
        "mutations": [round(max(0.0, event["time"] - started), 3)
                      for event in events if event.get("event") == "mutation"],
        "markers": [{"at": check["at"], "offset": round(max(0.0, check["at"] - started), 3),
                     "id": check["id"], "passed": check["passed"], "exit_code": check["exit_code"]}
                    for check in checks],
        "events": [_event_view(event, started) for event in events],
        "partial": partial,
        "report_command": f"python tools/perf.py --root . report --run-id {run_id}",
        "diagnose_command": f"python tools/perf.py --root . diagnose --run-id {run_id}",
    }


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store: Store):
        self.store = store
        super().__init__(address, DashboardHandler)


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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()

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

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ASSETS:
            filename, content_type = ASSETS[route]
            return self._asset(filename, content_type)
        if route == "/api/state":
            try:
                state = self.server.store.state()
            except (OSError, ValueError) as exc:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            etag = f'"{state["fingerprint"]}"'
            if self._if_none_match(etag):
                return self._not_modified(etag)
            return self._json(state, etag=etag)
        if route.startswith("/api/run/"):
            run_id = unquote(route[len("/api/run/"):])
            try:
                return self._json(self.server.store.detail(run_id))
            except DashboardError as exc:
                return self._error(HTTPStatus.NOT_FOUND, str(exc))
            except (OSError, ValueError) as exc:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        if route == "/api/health":
            return self._json({"ok": True})
        return self._error(HTTPStatus.NOT_FOUND, "Not found")

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path in ASSETS:
            filename, content_type = ASSETS[parsed.path]
            return self._asset(filename, content_type, body=False)
        return self._error(HTTPStatus.METHOD_NOT_ALLOWED, "HEAD is limited to static assets")

    def do_POST(self):
        return self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Dashboard is read-only")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", type=Path, help="Project directory (default: current directory)")
    parser.add_argument("--port", default=8765, type=int, help="Loopback port (default: 8765)")
    parser.add_argument("--demo", action="store_true", help="Serve clearly labelled synthetic sample evidence")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"Project directory does not exist: {root}")
    store = Store(root, demo=args.demo)
    try:
        server = DashboardServer(("127.0.0.1", args.port), store)
    except OSError as exc:
        print(f"DASHBOARD ERROR: cannot bind 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 1
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    print("Auto-Orchestrator dashboard — read-only, loopback only")
    print(f"  {url}")
    print(f"  root: {root}")
    print(f"  mode: {'DEMO (synthetic, not a benchmark)' if args.demo else 'live evidence'}")
    print("  Ctrl-C to stop")
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
