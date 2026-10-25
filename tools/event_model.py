#!/usr/bin/env python3
"""Whitelisted, privacy-preserving event normalisation for the V7.1 console.

Run logs are written by native hooks and the perf CLI. They are untrusted local
input, so every field that crosses into the dashboard is rebuilt here from a
closed whitelist. Prompts, source code, file contents, command lines and raw
native identifiers never pass through.

Only the Python standard library is used.
"""
from __future__ import annotations

import math
import re
import threading

SCHEMA_VERSION = 1
MAX_EVENTS = 10000
MAX_SEQUENCE = 2 ** 63 - 1
MAX_PAYLOAD_ITEMS = 16

PROJECT_ID = re.compile(r"^p_[0-9a-f]{16}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
NATIVE_HASH = re.compile(r"^[0-9a-f]{64}$")

PLATFORMS = ("opencode", "codex", "antigravity", "cursor", "unknown")
EVENT_TYPES = ("start", "route", "agent-start", "agent-stop", "check",
               "mutation", "verified", "finish", "diagnostic")
TERMINAL_STATUSES = ("success", "failed", "cancelled")
DELEGATION_MODES = ("single", "consider-parallel")
DIAGNOSTIC_REASONS = (
    "session_ownership_lost",
    "session_deleted_without_terminal",
    "event_stream_closed",
    "event_stream_error",
    "plugin_unloaded",
)


class EventModelError(ValueError):
    """Invalid identity, timestamp or event input."""


class SequenceConflict(EventModelError):
    """A sequence was reused or went backwards for the same run."""


def _checked_project(project_id: str) -> str:
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
        raise EventModelError("Invalid project id")
    return project_id


def _checked_run(run_id: str) -> str:
    if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
        raise EventModelError("Invalid run id")
    return run_id


def _safe_token(value):
    return value if isinstance(value, str) and SAFE_ID.fullmatch(value) else None


def _optional_id(event: dict, key: str):
    value = event.get(key)
    if value is None:
        return None
    if isinstance(value, str) and SAFE_ID.fullmatch(value):
        return value
    return None


def _optional_attempt(event: dict):
    value = event.get("attempt_id", event.get("attempt"))
    if type(value) is int and 0 <= value <= 1000000:
        return value
    if isinstance(value, str) and SAFE_ID.fullmatch(value):
        return value
    return None


def _timestamp(event: dict) -> float:
    value = event.get("time")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventModelError("Event is missing a numeric timestamp")
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp < 0 or timestamp >= 1e12:
        raise EventModelError("Event timestamp is out of range")
    return timestamp


def _integer(value, minimum: int, maximum: int):
    if type(value) is not int or not minimum <= value <= maximum:
        return None
    return value


def _payload(event_type: str, event: dict) -> dict:
    """Rebuild a small, typed payload from known-safe fields only."""
    payload: dict = {}
    if event_type in ("agent-start", "agent-stop"):
        agent = _safe_token(event.get("agent_id"))
        if agent is not None:
            payload["agent_id"] = agent
        native_hash = event.get("native_hash")
        if isinstance(native_hash, str) and NATIVE_HASH.fullmatch(native_hash):
            payload["native_hash_short"] = native_hash[:12]
    elif event_type == "route":
        if event.get("delegation") in DELEGATION_MODES:
            payload["delegation"] = event["delegation"]
        limit = _integer(event.get("max_agents"), 1, 3)
        if limit is not None:
            payload["max_agents"] = limit
        selected = _integer(event.get("selected_count"), 0, 2)
        if selected is not None:
            payload["selected_count"] = selected
        latency = event.get("latency_ms")
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            latency = float(latency)
            if math.isfinite(latency) and 0 <= latency <= 60000:
                payload["latency_ms"] = round(latency, 1)
    elif event_type == "check":
        check_id = _safe_token(event.get("check_id"))
        if check_id is None:
            return {}
        payload["check_id"] = check_id
        code = _integer(event.get("exit_code"), -255, 255)
        if code is not None:
            payload["exit_code"] = code
            payload["passed"] = code == 0
        if event.get("source") == "native-tool":
            payload["source"] = "native-tool"
    elif event_type == "verified":
        if event.get("source") == "quality-gate":
            payload["source"] = "quality-gate"
        ids = event.get("check_ids")
        if isinstance(ids, list):
            payload["check_ids"] = [item for item in ids
                                    if isinstance(item, str) and SAFE_ID.fullmatch(item)][
                                        :MAX_PAYLOAD_ITEMS]
    elif event_type == "finish":
        if event.get("status") in TERMINAL_STATUSES:
            payload["status"] = event["status"]
        payload["verified"] = bool(event.get("verified")) if isinstance(
            event.get("verified"), bool) else False
    elif event_type == "diagnostic":
        if event.get("reason") in DIAGNOSTIC_REASONS:
            payload["reason"] = event["reason"]
    return payload


def _evidence_source(event_type: str, event: dict) -> str:
    if event_type == "unknown":
        return "unknown"
    if event_type == "check":
        return "native-tool" if event.get("source") == "native-tool" else "run-log"
    if event_type == "verified":
        return "quality-gate" if event.get("source") == "quality-gate" else "run-log"
    if event_type in ("agent-start", "agent-stop"):
        return "native-hook"
    return "run-log"


def normalize_run_event(project_id: str, run_id: str, event: dict,
                        sequence: int) -> dict:
    """Return the closed V7.1 event schema for one observed run event.

    Unknown event kinds are preserved as ``unknown`` with an empty payload so
    the console can show that something happened without interpreting it.
    """
    project = _checked_project(project_id)
    run = _checked_run(run_id)
    if not isinstance(event, dict):
        raise EventModelError("Event must be an object")
    if type(sequence) is not int or not 1 <= sequence <= MAX_SEQUENCE:
        raise EventModelError("Sequence must be a positive integer")
    timestamp = _timestamp(event)
    kind = event.get("event")
    event_type = kind if isinstance(kind, str) and kind in EVENT_TYPES else "unknown"
    platform = event.get("platform")
    if platform not in PLATFORMS:
        platform = "unknown"
    agent_id = _optional_id(event, "agent_id")
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project,
        "run_id": run,
        "task_id": _optional_id(event, "task_id"),
        "attempt_id": _optional_attempt(event),
        "agent_id": agent_id,
        "session_id": _optional_id(event, "session_id"),
        "platform": platform,
        "event_type": event_type,
        "timestamp": timestamp,
        "sequence_id": sequence,
        "evidence_source": _evidence_source(event_type, event),
        "confidence": "observed" if event_type != "unknown" else "unknown",
        "payload": _payload(event_type, event),
    }


def normalize_events(project_id: str, run_id: str, events: list,
                     *, strict=True) -> list[dict]:
    """Normalise events in their recorded order with 1-based sequences."""
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise EventModelError("Events must be a bounded array")
    result = []
    for index, event in enumerate(events, start=1):
        try:
            result.append(normalize_run_event(project_id, run_id, event, index))
        except EventModelError:
            if strict:
                raise
    return result


class RunSequencer:
    """Monotonic per-run sequence with duplicate detection."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last: dict[tuple[str, str], int] = {}

    @staticmethod
    def _key(project_id: str, run_id: str) -> tuple[str, str]:
        return _checked_project(project_id), _checked_run(run_id)

    def next(self, project_id: str, run_id: str) -> int:
        key = self._key(project_id, run_id)
        with self._lock:
            value = self._last.get(key, 0) + 1
            if value > MAX_SEQUENCE:
                raise SequenceConflict("Sequence exhausted for run")
            self._last[key] = value
            return value

    def observe(self, project_id: str, run_id: str, sequence: int) -> int:
        key = self._key(project_id, run_id)
        if type(sequence) is not int or not 1 <= sequence <= MAX_SEQUENCE:
            raise EventModelError("Sequence must be a positive integer")
        with self._lock:
            last = self._last.get(key, 0)
            if sequence <= last:
                raise SequenceConflict("Duplicate or out-of-order sequence")
            self._last[key] = sequence
            return sequence

    def last(self, project_id: str, run_id: str) -> int:
        key = self._key(project_id, run_id)
        with self._lock:
            return self._last.get(key, 0)
