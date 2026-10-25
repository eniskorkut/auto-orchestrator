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

import math
import re

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
    }
    if binding is None:
        return base
    evaluated, _ = _evaluate_binding(binding, state, supervisor, detail_provider)
    base.update({
        "status": evaluated["status"],
        "status_label": evaluated["status_label"],
        "evidence": evaluated["evidence"],
        "current": evaluated["current"],
        "binding": binding,
    })
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
    summary = {
        "agents": len(agents),
        "working": sum(entry["status"] == "working" for entry in agents),
        "unknown": sum(entry["status"] == "unknown" for entry in agents),
        "completed": sum(entry["status"] == "completed" for entry in agents),
        "unlinked": len(unlinked),
    }
    return {
        "project_id": project.get("project_id"),
        "project": _project_meta(project),
        "theme": project.get("theme", "custom"),
        "agents": agents,
        "unlinked_sessions": unlinked,
        "summary": summary,
    }


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
                "agent_id": span.get("id"),
                "native_hash": digest if isinstance(digest, str) else None,
                "platform": run.get("platform", "unknown"),
                "status": item_status,
                "open": opened,
                "duration": _number(span.get("duration")),
                "last_event_at": _number(run.get("last_event_at")),
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
