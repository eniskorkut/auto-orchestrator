#!/usr/bin/env python3
"""Evidence-mapped agent office and inspector payloads for V7.1.

Every status is derived from explicit evidence: a project-local binding, an
observed native run span, a Supervisor task, or nothing. Role names, task text
and timing are never used to guess what an agent is doing. An unbound agent is
Unknown, not Idle. There is no authenticated terminal feed, so the inspector
reports ``terminal.supported = false`` instead of inventing one.

Only the Python standard library is used.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path

try:
    from tools.event_model import EventModelError, normalize_run_event
except ModuleNotFoundError:  # Running as ``python tools/office_model.py``.
    try:
        from event_model import EventModelError, normalize_run_event
    except ModuleNotFoundError:  # pragma: no cover - defensive installation path.
        EventModelError = ValueError
        normalize_run_event = None

AGENT_ID = re.compile(r"^a_[0-9a-f]{12}$")
PROJECT_ID = re.compile(r"^p_[0-9a-f]{16}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
DEPARTMENT_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")

ROOMS = ("frontend", "backend", "qa", "review", "research", "meeting", "lobby")
ROLE_ROOMS = {
    "tester": "qa",
    "reviewer": "review",
    "explorer": "research",
    # V7.3 verification roles. They are review-room work, not production.
    "critic": "review",
    "challenger": "review",
    "auditor": "review",
    "success-auditor": "review",
}

ACTIVE_TASK_STATES = ("running", "cancel_requested", "timed_out")
FAILED_TASK_STATES = ("failed", "cancelled")

STATUS_LABELS = {
    "unknown": "Unknown",
    "working": "Working",
    "dispatching": "Dispatching",
    "completed": "Completed",
    "failed": "Failed",
    "reviewing": "Reviewing",
    "running-tests": "Running tests",
}

NO_BINDING_DETAIL = "No explicit agent binding"
WORKING_DETAIL = "Native start observed; stop not observed; not proof of liveness."
STALE_DETAIL = "Stale open span; no stop observed."
DISPATCHING_DETAIL = "Task running; native start not observed."
TERMINAL_REASON = ("No authenticated live terminal stream is attached. "
                   "Observed lifecycle events only.")
MAX_EVENTS = 50
MAX_UNLINKED = 50
MAX_UNLINKED_RUNS = 10


class OfficeModelError(ValueError):
    pass


def _empty_current() -> dict:
    return {
        "run_id": None,
        "task_id": None,
        "task_role": None,
        "task_state": None,
        "department": None,
        "session_id": None,
        "native_hash": None,
        "platform": None,
        "started_at": None,
        "last_event_at": None,
        "last_event": None,
    }


def _empty_evidence() -> dict:
    return {
        "source": "none",
        "confidence": "unknown",
        "detail": NO_BINDING_DETAIL,
        "open_span": False,
        "native_stop_observed": False,
    }


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(float(value)):
            return value
    return None


def _department_id(value) -> str | None:
    """Normalise an explicit department/room id without guessing."""
    if not isinstance(value, str):
        return None
    normalised = value.strip().lower()
    if not DEPARTMENT_ID.fullmatch(normalised):
        return None
    return normalised


def _task_department(task: dict | None) -> str | None:
    if not isinstance(task, dict):
        return None
    return (_department_id(task.get("department"))
            or _department_id(task.get("assigned_room")))


def _room_assignment(agent: dict, task: dict | None) -> tuple[str, str]:
    """Return ``(room, room_source)`` using only explicit fields and task role.

    Order: task department/assigned room, agent default/assigned room, task
    role mapping, lobby. Display names, objectives and timing are never read.
    """
    explicit_task = _task_department(task)
    if explicit_task in ROOMS:
        return explicit_task, "task.department"
    for key in ("default_room", "assigned_room"):
        explicit_agent = _department_id(agent.get(key))
        if explicit_agent in ROOMS:
            return explicit_agent, "agent.default_room"
    role = task.get("role") if isinstance(task, dict) else None
    if role in ROLE_ROOMS:
        return ROLE_ROOMS[role], "task.role"
    return "lobby", "unassigned"


def _session_key(binding: dict | None) -> str | None:
    if not isinstance(binding, dict):
        return None
    agent_id = binding.get("agent_definition_id")
    run_id = binding.get("run_id")
    digest = binding.get("native_hash")
    if (not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id)
            or not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id)
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        return None
    return f"{agent_id}:{run_id}:{digest[:12]}"


def _project_agents(project: dict) -> list[dict]:
    raw = project.get("agents") if isinstance(project, dict) else None
    if not isinstance(raw, list):
        return []
    agents = []
    seen = set()
    for agent in raw:
        if not isinstance(agent, dict):
            continue
        agent_id = agent.get("agent_definition_id")
        if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id):
            continue
        if agent_id in seen:
            continue
        seen.add(agent_id)
        agents.append(agent)
    return agents


def _project_meta(project: dict) -> dict:
    return {
        "project_id": project.get("project_id"),
        "name": project.get("name"),
        "theme": project.get("theme", "custom"),
        "agents": _project_agents(project),
    }


def _bindings_map(bindings) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if not isinstance(bindings, list):
        return result
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        agent_id = binding.get("agent_definition_id")
        digest = binding.get("native_hash")
        run_id = binding.get("run_id")
        if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id):
            continue
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
            continue
        if agent_id not in result:
            result[agent_id] = binding
    return result


def _supervisor_tasks(state: dict) -> dict[tuple[str, str], dict]:
    result: dict[tuple[str, str], dict] = {}
    supervisor = state.get("supervisor") if isinstance(state, dict) else None
    if not isinstance(supervisor, dict):
        return result
    for run in supervisor.get("runs") or []:
        if not isinstance(run, dict) or not isinstance(run.get("id"), str):
            continue
        for task in run.get("tasks") or []:
            if isinstance(task, dict) and isinstance(task.get("id"), str):
                result[(run["id"], task["id"])] = task
    return result


def _run_summaries(state: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if not isinstance(state, dict):
        return result
    for run in state.get("runs") or []:
        if isinstance(run, dict) and isinstance(run.get("id"), str):
            result[run["id"]] = run
    return result


def _match_span(detail: dict | None, native_hash: str) -> dict | None:
    if not isinstance(detail, dict):
        return None
    spans = detail.get("agents")
    if not isinstance(spans, list):
        return None
    for span in spans:
        if not isinstance(span, dict):
            continue
        if span.get("native_hash") == native_hash:
            return span
    return None


def _evaluate_binding(binding: dict, state: dict, supervisor: dict,
                      detail_provider) -> tuple[dict, dict]:
    """Return ``(office_agent_entry, context)`` for one explicit binding."""
    run_id = binding.get("run_id")
    task_id = binding.get("task_id") if isinstance(binding.get("task_id"), str) else None
    digest = binding.get("native_hash")
    run = _run_summaries(state).get(run_id)
    task = supervisor.get((run_id, task_id)) if task_id else None
    detail = None
    if callable(detail_provider) and isinstance(run_id, str):
        try:
            detail = detail_provider(run_id)
        except Exception:
            detail = None
    if not isinstance(detail, dict):
        detail = None
    if run is None and detail is not None and isinstance(detail.get("run"), dict):
        run = detail["run"]
    span = _match_span(detail, digest) if isinstance(digest, str) else None

    run_status = run.get("status") if isinstance(run, dict) else None
    run_live = bool(run.get("live")) if isinstance(run, dict) else False
    verified = bool(run.get("verified")) if isinstance(run, dict) else False
    if run is None and task is not None:
        run_status = "open" if task.get("state") in ACTIVE_TASK_STATES else None
        run_live = task.get("state") in ("running", "cancel_requested")

    task_state = task.get("state") if isinstance(task, dict) else None
    task_failed = task_state in FAILED_TASK_STATES
    task_completed = task_state == "completed"
    task_open = task_state in ACTIVE_TASK_STATES
    task_live = task_state in ("running", "cancel_requested")
    native_start = bool(task.get("native_start_observed")) if isinstance(task, dict) else False
    native_stop = bool(task.get("native_stop_observed")) if isinstance(task, dict) else False

    span_open = bool(span.get("open")) if isinstance(span, dict) else False
    span_closed = span is not None and not span_open
    open_observed = span_open or (task_open and native_start and not native_stop)
    closed_observed = span_closed or task_completed

    events = detail.get("events") if isinstance(detail, dict) else None
    last_event = None
    if isinstance(events, list) and events and isinstance(events[-1], dict):
        last_event = events[-1].get("event")
    started_at = None
    last_event_at = None
    if isinstance(span, dict):
        started_at = _number(span.get("start"))
        last_event_at = _number(span.get("end"))
    elif isinstance(task, dict):
        started_at = _number(task.get("started"))
        last_event_at = _number(task.get("finished")) or _number(task.get("started"))
    if isinstance(run, dict):
        last_event_at = _number(run.get("last_event_at")) or last_event_at
        if started_at is None:
            started_at = _number(run.get("started_at"))

    if isinstance(span, dict):
        source = "native-hook"
        confidence = "observed"
    elif isinstance(task, dict):
        source = "supervisor-sqlite"
        confidence = "observed"
    elif isinstance(run, dict):
        source = "native-hook"
        confidence = "observed"
    else:
        source = "none"
        confidence = "unknown"

    status = "unknown"
    detail_text = "No matching native run, task or span for the explicit binding."
    open_span = open_observed
    native_stop_observed = bool(native_stop or span_closed)

    if isinstance(run, dict) and run_status in ("failed", "cancelled"):
        status, detail_text = "failed", "Run failed or was cancelled."
    elif task_failed:
        status, detail_text = "failed", "Task failed or was cancelled."
    elif closed_observed and (run_status == "success" or task_completed):
        status = "completed"
        evidence_run = run if isinstance(run, dict) else {}
        verification = evidence_run.get("verification") if isinstance(
            evidence_run.get("verification"), dict) else None
        if verified:
            detail_text = ("Run finished successfully; quality-gate verification attested.")
        elif task_completed and task.get("review_attested"):
            detail_text = ("Task completed" + (" with native stop observed"
                                               if native_stop else "") +
                           "; review attestation recorded (independence not proven).")
        elif task_completed:
            detail_text = ("Task completed" + (" with native stop observed"
                                               if native_stop else "") +
                           "; no quality-gate verification.")
        elif verification and verification.get("self_reported"):
            detail_text = ("Run finished successfully; verification is a self-report only.")
        elif verification and verification.get("status") == "rejected":
            detail_text = ("Run finished successfully; verification evidence was rejected.")
        else:
            detail_text = "Run finished successfully; verification not attested."
    elif open_observed:
        live = run_live if isinstance(run, dict) else bool(task_live)
        if live:
            status, detail_text = "working", WORKING_DETAIL
        else:
            status, detail_text = "unknown", STALE_DETAIL
    elif task_state == "running" and not native_start:
        status, detail_text = "dispatching", DISPATCHING_DETAIL
    elif run_status == "success" and not closed_observed:
        status = "unknown"
        detail_text = "Run finished successfully but no bound native span was observed."
    elif isinstance(run, dict) and run_status in ("invalid",):
        status = "unknown"
        detail_text = "Run evidence is invalid."
    elif isinstance(run, dict) or isinstance(task, dict):
        status = "unknown"
        detail_text = "No terminal evidence for this binding."

    evidence = {
        "source": source,
        "confidence": ("inferred" if status == "dispatching" else
                       "unknown" if source == "none" else confidence),
        "detail": detail_text,
        "open_span": bool(open_span),
        "native_stop_observed": native_stop_observed,
    }
    current = _empty_current()
    if isinstance(run, dict) or isinstance(task, dict) or isinstance(span, dict):
        current["run_id"] = run_id
    if isinstance(task, dict):
        current["task_id"] = task_id
        current["task_role"] = task.get("role") if isinstance(task.get("role"), str) else None
        current["task_state"] = task.get("state") if isinstance(task.get("state"), str) else None
        current["department"] = _task_department(task)
    current["session_id"] = binding.get("session_id") if isinstance(
        binding.get("session_id"), str) else None
    current["native_hash"] = digest
    if isinstance(run, dict) and isinstance(run.get("platform"), str):
        current["platform"] = run["platform"]
    current["started_at"] = started_at
    current["last_event_at"] = last_event_at
    current["last_event"] = last_event

    entry = {
        "agent_definition_id": binding.get("agent_definition_id"),
        "status": status,
        "status_label": STATUS_LABELS.get(status, "Unknown"),
        "evidence": evidence,
        "current": current,
        "binding": binding,
    }
    context = {
        "run": run if isinstance(run, dict) else None,
        "task": task if isinstance(task, dict) else None,
        "span": span if isinstance(span, dict) else None,
        "detail": detail,
    }
    return entry, context


def _agent_entry(agent: dict, binding: dict | None, state: dict,
                 supervisor: dict, detail_provider) -> dict:
    base = {
        "agent_definition_id": agent.get("agent_definition_id"),
        "name": agent.get("name"),
        "role": agent.get("role"),
        "platform": agent.get("platform", "unknown"),
        "model": agent.get("model"),
        "color": agent.get("color"),
        "status": "unknown",
        "status_label": STATUS_LABELS["unknown"],
        "evidence": _empty_evidence(),
        "current": _empty_current(),
        "binding": None,
        "room": "lobby",
        "room_source": "unassigned",
        "session_key": None,
        "spawnable": False,
    }
    task = None
    if binding is not None:
        evaluated, context = _evaluate_binding(binding, state, supervisor, detail_provider)
        task = context["task"]
        base.update({
            "status": evaluated["status"],
            "status_label": evaluated["status_label"],
            "evidence": evaluated["evidence"],
            "current": evaluated["current"],
            "binding": binding,
        })
        base["session_key"] = _session_key(binding)
        base["spawnable"] = base["session_key"] is not None
    base["room"], base["room_source"] = _room_assignment(agent, task)
    return base


def build_office(project: dict, state: dict, *, bindings=None,
                 detail_provider=None) -> dict:
    """Build the office payload from registry agents and explicit evidence."""
    if not isinstance(project, dict):
        raise OfficeModelError("Project metadata is required")
    supervisor = _supervisor_tasks(state)
    agent_ids = {agent.get("agent_definition_id")
                 for agent in _project_agents(project)}
    bindings_by_agent = {
        agent_id: binding
        for agent_id, binding in _bindings_map(bindings).items()
        if agent_id in agent_ids
    }
    agents = []
    for agent in _project_agents(project):
        agents.append(_agent_entry(agent, bindings_by_agent.get(
            agent["agent_definition_id"]), state, supervisor, detail_provider))

    unlinked = _unlinked_sessions(project, state, bindings_by_agent, detail_provider)
    managed = _managed_sessions(project)
    summary = {
        "agents": len(agents),
        "working": sum(entry["status"] == "working" for entry in agents),
        "unknown": sum(entry["status"] == "unknown" for entry in agents),
        "completed": sum(entry["status"] == "completed" for entry in agents),
        "unlinked": len(unlinked),
        "managed": len(managed),
    }
    return {
        "project_id": project.get("project_id"),
        "project": _project_meta(project),
        "theme": project.get("theme", "custom"),
        "agents": agents,
        "unlinked_sessions": unlinked,
        "managed_sessions": managed,
        "summary": summary,
    }


MANAGED_STATUS = {
    "starting": "working",
    "running": "working",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "unknown",
}


def _managed_sessions(project: dict) -> list[dict]:
    """Read latest managed sessions from the project's Supervisor SQLite.

    This is managed-execution evidence, not a native hook. It is labelled
    separately and never claims native start/stop proof.
    """
    root = project.get("root")
    if not isinstance(root, str):
        return []
    database = Path(root) / ".auto-orchestrator" / "supervisor.sqlite3"
    if not database.is_file():
        return []
    rows = []
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            rows = list(connection.execute("""
              SELECT m.run_id,m.task_id,m.attempt,m.provider,m.session_id,m.status,
                     m.started_at,m.updated_at,m.finished_at,m.events_path,m.model,
                     m.cost,m.tokens,m.error,m.cancel_state,m.resume_of,m.tool_count,
                     t.role,t.department,t.objective,t.state AS task_state
              FROM managed_sessions m
              JOIN tasks t ON t.run_id=m.run_id AND t.id=m.task_id
              ORDER BY m.updated_at DESC, m.run_id, m.task_id, m.attempt DESC
              LIMIT 128
            """))
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError):
        return []
    latest = {}
    for row in rows:
        key = (row["run_id"], row["task_id"])
        if key not in latest:
            latest[key] = row
    result = []
    for row in latest.values():
        managed_status = row["status"]
        if managed_status not in MANAGED_STATUS:
            continue
        role = row["role"] if isinstance(row["role"], str) else "unknown"
        department = (row["department"] if row["department"] in ROOMS
                      else ROLE_ROOMS.get(role, "lobby"))
        session_id = row["session_id"] if isinstance(row["session_id"], str) else ""
        tokens = row["tokens"]
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except ValueError:
                tokens = None
        running = managed_status in ("starting", "running")
        result.append({
            "kind": "managed",
            "name": row["task_id"],
            "role": role,
            "objective": row["objective"],
            "platform": row["provider"],
            "provider": row["provider"],
            "model": row["model"],
            "status": MANAGED_STATUS[managed_status],
            "managed_status": managed_status,
            "status_label": managed_status.replace("-", " ").title(),
            "room": department,
            "room_source": "managed.task",
            "session_key": f"m:{row['run_id']}:{row['task_id']}:{row['attempt']}",
            "session_hash": (hashlib.sha256(session_id.encode()).hexdigest()[:12]
                             if session_id else None),
            "spawnable": True,
            "binding": None,
            "color": {
                "backend": "#e8a87c", "frontend": "#4f8cff", "qa": "#70e0a0",
                "review": "#c4b5fd", "research": "#7dd3fc", "meeting": "#f2c96a",
                "lobby": "#9aa7b8", "tester": "#70e0a0", "reviewer": "#c4b5fd",
                "critic": "#c4b5fd", "challenger": "#c4b5fd",
                "auditor": "#c4b5fd", "success-auditor": "#c4b5fd",
                "explorer": "#7dd3fc", "implementer": "#e8a87c",
            }.get(department, "#9aa7b8"),
            "current": {
                "run_id": row["run_id"], "task_id": row["task_id"],
                "task_role": role, "task_state": row["task_state"],
                "department": department, "run_status": managed_status,
            },
            "evidence": {
                "source": "managed-opencode" if row["provider"] == "opencode"
                          else f"managed-{row['provider']}",
                "confidence": "observed" if running else "recorded",
                "detail": ("Managed session event stream; native start/stop is "
                           "not claimed."),
                "open_span": running,
                "native_stop_observed": False,
            },
            "managed": {
                "run_id": row["run_id"], "task_id": row["task_id"],
                "attempt": row["attempt"], "provider": row["provider"],
                "status": managed_status, "model": row["model"],
                "cost": row["cost"], "tokens": tokens,
                "tool_count": row["tool_count"] or 0,
                "started_at": row["started_at"], "updated_at": row["updated_at"],
                "finished_at": row["finished_at"], "session_hash":
                    (hashlib.sha256(session_id.encode()).hexdigest()[:12]
                     if session_id else None),
                "events_name": (Path(row["events_path"]).name
                                if isinstance(row["events_path"], str) else None),
                "error": row["error"], "cancel_state": row["cancel_state"],
                "resume_of": row["resume_of"],
            },
        })
    return result


def _unlinked_sessions(project: dict, state: dict, bindings: dict,
                       detail_provider) -> list[dict]:
    if not callable(detail_provider):
        return []
    runs = _run_summaries(state)
    ordered = sorted(runs.values(), key=lambda run: run.get("last_event_at") or 0,
                     reverse=True)
    bound_hashes = {binding.get("native_hash", "")[:12] for binding in bindings.values()}
    # A task binding attributes the whole run; a hash binding attributes one
    # observed span and leaves sibling spans eligible as unlinked sessions.
    task_bound_runs = {binding.get("run_id") for binding in bindings.values()
                       if binding.get("task_id")}
    result = []
    seen_keys: set[str] = set()
    for run in ordered[:MAX_UNLINKED_RUNS]:
        if len(result) >= MAX_UNLINKED:
            break
        run_id = run.get("id")
        if not isinstance(run_id, str):
            continue
        if not run.get("agent_count"):
            continue
        if run_id in task_bound_runs:
            continue
        try:
            detail = detail_provider(run_id)
        except Exception:
            continue
        spans = detail.get("agents") if isinstance(detail, dict) else None
        if not isinstance(spans, list):
            continue
        live = bool(run.get("live"))
        status = run.get("status")
        for span in spans:
            if len(result) >= MAX_UNLINKED:
                break
            if not isinstance(span, dict):
                continue
            digest = span.get("native_hash")
            if not isinstance(digest, str):
                digest = span.get("native_hash_short")
            if isinstance(digest, str) and digest[:12] in bound_hashes:
                continue
            agent_id = span.get("id")
            session_key = (f"s:{run_id}:{agent_id}" if isinstance(agent_id, str)
                           else None)
            if session_key is not None:
                if session_key in seen_keys:
                    continue
                seen_keys.add(session_key)
            opened = bool(span.get("open"))
            if opened:
                item_status = "working" if live else "unknown"
            elif status == "failed" or status == "cancelled":
                item_status = "failed"
            elif status == "success":
                item_status = "completed"
            else:
                item_status = "unknown"
            result.append({
                "run_id": run_id,
                "agent_id": agent_id,
                "native_hash": digest if isinstance(digest, str) else None,
                "platform": run.get("platform", "unknown"),
                "status": item_status,
                "open": opened,
                "duration": _number(span.get("duration")),
                "last_event_at": _number(run.get("last_event_at")),
                "room": "lobby",
                "room_source": "unlinked",
                "session_key": session_key,
                "spawnable": session_key is not None,
            })
    result.sort(key=lambda item: item.get("last_event_at") or 0, reverse=True)
    return result


def _normalised_events(project_id: str, detail: dict | None) -> list[dict]:
    if normalize_run_event is None or not isinstance(detail, dict):
        return []
    run = detail.get("run") if isinstance(detail.get("run"), dict) else {}
    run_id = run.get("id")
    started = _number(run.get("started_at")) or 0
    if not isinstance(run_id, str) or not PROJECT_ID.fullmatch(project_id):
        return []
    events = detail.get("events")
    if not isinstance(events, list):
        return []
    start_index = max(0, len(events) - MAX_EVENTS)
    result = []
    for index in range(start_index, len(events)):
        item = events[index]
        if not isinstance(item, dict):
            continue
        offset = _number(item.get("offset")) or 0
        native = {"event": item.get("event"), "time": started + offset}
        for key in ("agent_id", "check_id", "exit_code", "delegation", "max_agents",
                    "selected_count", "latency_ms", "source", "check_ids", "status",
                    "verified", "reason"):
            if key in item:
                native[key] = item[key]
        try:
            result.append(normalize_run_event(project_id, run_id, native, index + 1))
        except EventModelError:
            continue
    return result


def build_inspector(project: dict, agent_definition_id: str, state: dict, *,
                    bindings=None, detail_provider=None) -> dict:
    """Build the inspector payload for one registered agent definition."""
    if not isinstance(project, dict):
        raise OfficeModelError("Project metadata is required")
    agent = None
    for item in _project_agents(project):
        if item["agent_definition_id"] == agent_definition_id:
            agent = item
            break
    if agent is None:
        raise OfficeModelError("Unknown agent definition")
    supervisor = _supervisor_tasks(state)
    binding = _bindings_map(bindings).get(agent_definition_id)
    entry = _agent_entry(agent, binding, state, supervisor, detail_provider)
    context = {"run": None, "task": None, "span": None, "detail": None}
    if binding is not None:
        _, context = _evaluate_binding(binding, state, supervisor, detail_provider)
    run = context["run"]
    task = context["task"]
    detail = context["detail"]

    task_summary = None
    if isinstance(task, dict):
        task_summary = {
            "id": task.get("id"),
            "role": task.get("role"),
            "state": task.get("state"),
            "attempts": task.get("attempts"),
            "max_retries": task.get("max_retries"),
            "started": _number(task.get("started")),
            "finished": _number(task.get("finished")),
            "review_attested": bool(task.get("review_attested")),
            "review_digest": task.get("review_digest"),
            "native_start_observed": bool(task.get("native_start_observed")),
            "native_stop_observed": bool(task.get("native_stop_observed")),
            "reason": task.get("reason") if isinstance(task.get("reason"), str) else None,
        }

    activity = {
        "run": run,
        "events": _normalised_events(project.get("project_id"), detail),
        "checks": [],
        "last_event_at": _number(run.get("last_event_at")) if isinstance(run, dict) else None,
    }
    checks = detail.get("checks") if isinstance(detail, dict) else None
    if isinstance(checks, list):
        activity["checks"] = [
            {"id": check.get("id"), "at": _number(check.get("at")),
             "passed": bool(check.get("passed")), "exit_code": check.get("exit_code"),
             "source": check.get("source")}
            for check in checks[:MAX_EVENTS] if isinstance(check, dict)
        ]

    changed_files = []
    if isinstance(task, dict) and isinstance(task.get("result"), dict):
        value = task["result"].get("changed_files")
        if isinstance(value, list):
            changed_files = [item for item in value if isinstance(item, str)][:100]
    worktree = None
    tree_list = state.get("worktrees") if isinstance(state, dict) else None
    if isinstance(tree_list, list) and isinstance(task, dict):
        for item in tree_list:
            if isinstance(item, dict) and item.get("task") == task.get("id") \
                    and isinstance(item.get("path"), str):
                worktree = item["path"]
                break

    verification = run.get("verification") if isinstance(run, dict) else None
    diagnostics = []
    if isinstance(run, dict) and isinstance(run.get("diagnostics"), list):
        diagnostics = [item for item in run["diagnostics"] if isinstance(item, str)][:20]
    if isinstance(task, dict) and isinstance(task.get("reason"), str) \
            and task["reason"] not in diagnostics:
        diagnostics.append(task["reason"])

    return {
        "project_id": project.get("project_id"),
        "project": _project_meta(project),
        "agent": {
            **agent,
            "status": entry["status"],
            "status_label": entry["status_label"],
            "evidence": entry["evidence"],
            "current": entry["current"],
            "binding": binding,
            "room": entry["room"],
            "room_source": entry["room_source"],
            "session_key": entry["session_key"],
            "spawnable": entry["spawnable"],
        },
        "binding": binding,
        "task": task_summary,
        "identity": {
            "platform": entry["current"].get("platform"),
            "verified_model": None,
            "model_note": "Model is not attested by run telemetry",
            "native_session": (binding.get("session_id")
                               if isinstance(binding, dict) else None),
        },
        "activity": activity,
        "files": {
            "worktree": worktree,
            "changed_files": changed_files,
            "diff_available": False,
            "commit": None,
            "merge_ready": None,
            "note": ("Changed files are reported by the task result; "
                     "no repository diff is read."),
        },
        "evidence": {
            "verification": verification if isinstance(verification, dict) else None,
            "native_stop_observed": entry["evidence"]["native_stop_observed"],
            "review_attested": bool(task.get("review_attested")) if isinstance(task, dict) else False,
            "review_note": ("Recorded attestation; independence is not proven."
                            if isinstance(task, dict) and task.get("review_attested")
                            else None),
            "diagnostics": diagnostics,
        },
        "terminal": {
            "supported": False,
            "reason": TERMINAL_REASON,
        },
    }
