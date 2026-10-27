#!/usr/bin/env python3
"""Explicit, fenced ownership handoff between IDEs.

This module is deliberately small and file based. It never launches another
IDE, never sends signals and never claims that a native session acknowledged a
stop. Ownership is a capability token plus a monotonically increasing fence;
every mutation is a compare-and-swap performed under the project-wide lock and
written with the shared atomic JSON writer.

Read-only ``status`` only opens existing files and SQLite databases. It does
not create a project identity, schema or lock file, and it does not instantiate
the orchestration or project-memory engines.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import stat
import subprocess
import time
from pathlib import Path

try:
    from tools.project_identity import (
        ensure_project_identity,
        read_project_identity,
    )
    from tools.run_owner import (
        atomic_write_json,
        project_lock,
        read_process_identity,
    )
except ModuleNotFoundError:  # flat installed layout
    from project_identity import (  # type: ignore
        ensure_project_identity,
        read_project_identity,
    )
    from run_owner import (  # type: ignore
        atomic_write_json,
        project_lock,
        read_process_identity,
    )

OWNERSHIP_VERSION = 1
SNAPSHOT_VERSION = 1
STATES = frozenset({
    "inactive", "active", "handoff_prepared", "released", "transferred",
    "recovery_required", "blocked",
})
ACTIVE_OWNER_STATES = frozenset({"active", "transferred"})
ACCEPT_STATES = frozenset({"handoff_prepared", "released"})

DEFAULT_TTL_SECONDS = 14_400  # 4h native session; heartbeat refreshes it
MAX_OWNERSHIP_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_ACTIVE_BYTES = 64 * 1024
MAX_EVIDENCE_BYTES = 16 * 1024
MAX_CHANGED_FILES = 200
MAX_DISCOVERED_CHANGES = 2000
MAX_CONTENT_HASH_BYTES = 2 * 1024 * 1024
# Total bytes read for the dirty digest. Beyond this, entries keep their
# path/size/kind so a pathological dirty tree cannot stall a handoff.
MAX_DIRTY_CONTENT_BYTES = 128 * 1024 * 1024
MAX_LIST = 100
MAX_TEXT = 500

PROJECT_ID = re.compile(r"^p_[0-9a-f]{16}$")
PROJECT_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
TOKEN = re.compile(r"^[A-Za-z0-9._~+/=-]{4,128}$")
HANDOFF_ID = re.compile(r"^ho_[A-Za-z0-9][A-Za-z0-9._-]{3,79}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IDE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
OWNER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
CONTRACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,80}$")
INTERVIEW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,80}$")

OWNERSHIP_FIELDS = frozenset({
    "version", "project_id", "project_uuid", "state", "fence", "owner",
    "handoff", "previous_owner", "last_transition", "native_cancel_ack",
    "stop_ack",
})
OWNER_FIELDS = frozenset({
    "owner_id", "ide", "session_id", "pid_identity", "host", "activated_at",
    "heartbeat_at", "ttl_seconds", "token",
})
HANDOFF_FIELDS = frozenset({
    "handoff_id", "digest", "path", "source_fence", "target_ide",
})
SNAPSHOT_FIELDS = frozenset({
    "version", "schema_version", "project_id", "project_uuid", "source_ide",
    "target_ide", "source_session_id", "target_session_id", "run_id",
    "milestone_id", "task_id", "run_status", "milestone_status",
    "task_status", "mode", "branch", "head", "head_sha", "dirty",
    "dirty_digest", "changed_files", "completed_tasks", "pending_tasks",
    "last_error", "failed_tests", "verified_evidence_refs", "contract_id",
    "contract_version", "contract_sha256", "sha256", "interview_id",
    "open_question", "open_question_id", "request_hash",
    "blocking_questions", "confirmed_decision_refs", "blockers",
    "context_provenance", "context_refs", "next_action", "handoff_refs",
    "created_at", "digest",
})
CONTRACT_METADATA = frozenset({
    "sha256", "created_at", "approved_at", "status", "supersedes",
    "superseded_by", "path",
})
BLOCKING_QUESTION_STATES = frozenset({"open", "skipped", "invalidated"})

_SECRET_PATTERNS = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
     "Bearer [redacted]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|password|passwd|secret|"
                r"authorization|credential)s?\b\s*[:=]\s*[^\s,;]+"),
     r"\1=[redacted]"),
    (re.compile(r"\b(?:sk|ghp|gho|github_pat)[-_][A-Za-z0-9_-]{10,}"),
     "[redacted]"),
)


class HandoffError(ValueError):
    """Invalid input or unsafe project state."""


def _root(value) -> Path:
    try:
        root = Path(value).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise HandoffError("project root does not exist") from exc
    if not root.is_dir():
        raise HandoffError("project root must be a directory")
    return root


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _canonical_digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _reason(code, message, **details) -> dict:
    value = {"code": str(code), "message": str(message)}
    value.update({key: item for key, item in details.items()
                  if item is not None})
    return value


def _safe_text(value, limit=MAX_TEXT, *, single_line=True) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    text = value
    if single_line:
        text = text.replace("\r", " ").replace("\n", " ")
    text = text.replace("\x00", "")
    text = "".join(char for char in text
                   if char == "\t" or ord(char) >= 32)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text.strip()[:max(0, int(limit))]


def _state_dir(root: Path) -> Path:
    return root / ".auto-orchestrator"


def _ide_dir(root: Path) -> Path:
    return _state_dir(root) / "ide"


def _ownership_path(root: Path) -> Path:
    return _ide_dir(root) / "ownership.json"


def _snapshots_dir(root: Path) -> Path:
    return _ide_dir(root) / "handoffs"


def _teamwork_dir(root: Path) -> Path:
    return _state_dir(root) / "teamwork"


def _check_state_dir(root: Path):
    path = _state_dir(root)
    if path.is_symlink():
        raise HandoffError("project state directory must not be a symlink")


def _safe_dir(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise HandoffError(f"{label} must not be a symlink")
    if path.exists() and not path.is_dir():
        raise HandoffError(f"{label} must be a directory")
    return path


def _ensure_dir(path: Path, label: str) -> Path:
    _safe_dir(path, label)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise HandoffError(f"{label} must not be a symlink")
    return path


def _validate_ide(value) -> str:
    if not isinstance(value, str):
        raise HandoffError("ide must be a string")
    ide = value.strip().lower()
    if not IDE.fullmatch(ide):
        raise HandoffError("ide is invalid")
    return ide


def _validate_session(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HandoffError("session_id must be a string")
    text = value.strip()
    if not text or len(text) > 200 or any(ord(char) < 32 for char in text):
        raise HandoffError("session_id is invalid")
    return text


def _validate_token(value) -> str:
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise HandoffError("owner token is invalid")
    return value


def _validate_fence(value) -> int:
    if type(value) is not int or value < 0:
        raise HandoffError("fence must be a non-negative integer")
    return value


def _validate_handoff_id(value) -> str:
    if not isinstance(value, str) or not HANDOFF_ID.fullmatch(value):
        raise HandoffError("handoff id is invalid")
    return value


def _validate_digest(value) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise HandoffError("digest must be a sha256 hex string")
    return value


def _validate_ttl(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HandoffError("ttl must be a number")
    ttl = int(value)
    if ttl < 1 or ttl > 86400:
        raise HandoffError("ttl must be between 1 and 86400 seconds")
    return ttl


def _validate_owner_id(value) -> str:
    if value is None:
        return "ow_" + secrets.token_hex(8)
    if not isinstance(value, str) or not OWNER_ID.fullmatch(value):
        raise HandoffError("owner_id is invalid")
    return value


def _validate_pid(value) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0 or value > 2 ** 31 - 1:
        raise HandoffError("pid must be a positive integer")
    return value


def _new_record(project_id, project_uuid) -> dict:
    return {
        "version": OWNERSHIP_VERSION,
        "project_id": project_id,
        "project_uuid": project_uuid,
        "state": "inactive",
        "fence": 0,
        "owner": None,
        "handoff": None,
        "previous_owner": None,
        "last_transition": None,
        "native_cancel_ack": False,
        "stop_ack": False,
    }


def _validate_pid_identity(value):
    if value is None:
        return
    if not isinstance(value, dict):
        raise HandoffError("pid_identity must be an object or null")
    if type(value.get("pid")) is not int or value["pid"] <= 0:
        raise HandoffError("pid_identity is invalid")
    if not isinstance(value.get("start_token"), str) or not value["start_token"]:
        raise HandoffError("pid_identity is invalid")
    host = value.get("host")
    if host is not None and (not isinstance(host, str) or len(host) > 255):
        raise HandoffError("pid_identity host is invalid")


def _validate_owner(value, label="owner"):
    if value is None:
        return
    if not isinstance(value, dict):
        raise HandoffError(f"{label} must be an object or null")
    if set(value) != OWNER_FIELDS:
        raise HandoffError(f"{label} has unexpected fields")
    if not isinstance(value.get("owner_id"), str) or not OWNER_ID.fullmatch(
            value["owner_id"]):
        raise HandoffError(f"{label} id is invalid")
    if not isinstance(value.get("ide"), str) or not IDE.fullmatch(value["ide"]):
        raise HandoffError(f"{label} ide is invalid")
    _validate_session(value.get("session_id"))
    _validate_pid_identity(value.get("pid_identity"))
    host = value.get("host")
    if not isinstance(host, str) or not host or len(host) > 255:
        raise HandoffError(f"{label} host is invalid")
    for name in ("activated_at", "heartbeat_at"):
        if isinstance(value.get(name), bool) or not isinstance(
                value.get(name), (int, float)):
            raise HandoffError(f"{label} {name} is invalid")
    if type(value.get("ttl_seconds")) is not int or value["ttl_seconds"] < 1:
        raise HandoffError(f"{label} ttl is invalid")
    if not isinstance(value.get("token"), str) or not TOKEN.fullmatch(
            value["token"]):
        raise HandoffError(f"{label} token is invalid")


def _validate_handoff(value):
    if not isinstance(value, dict):
        raise HandoffError("handoff must be an object")
    if set(value) != HANDOFF_FIELDS:
        raise HandoffError("handoff has unexpected fields")
    if not isinstance(value.get("handoff_id"), str) or not HANDOFF_ID.fullmatch(
            value["handoff_id"]):
        raise HandoffError("handoff id is invalid")
    if not isinstance(value.get("digest"), str) or not SHA256.fullmatch(
            value["digest"]):
        raise HandoffError("handoff digest is invalid")
    if not isinstance(value.get("path"), str) or not value["path"]:
        raise HandoffError("handoff path is invalid")
    if type(value.get("source_fence")) is not int or value["source_fence"] < 0:
        raise HandoffError("handoff source fence is invalid")
    if (not isinstance(value.get("target_ide"), str)
            or not IDE.fullmatch(value["target_ide"])):
        raise HandoffError("handoff target ide is invalid")


def _validate_previous_owner(value):
    if value is None:
        return
    if not isinstance(value, dict):
        raise HandoffError("previous_owner must be an object or null")
    fields = set(value)
    if fields in (OWNER_FIELDS, OWNER_FIELDS | {"fence"}):
        owner = {key: item for key, item in value.items() if key != "fence"}
        _validate_owner(owner, "previous_owner")
        if "fence" in value and (type(value["fence"]) is not int
                                 or value["fence"] < 0):
            raise HandoffError("previous_owner fence is invalid")
        return
    if not {"owner_id", "ide"} <= fields:
        raise HandoffError("previous_owner is invalid")
    if fields - {"owner_id", "ide", "fence"}:
        raise HandoffError("previous_owner has unexpected fields")
    if not isinstance(value.get("owner_id"), str) or not OWNER_ID.fullmatch(
            value["owner_id"]):
        raise HandoffError("previous_owner id is invalid")
    if not isinstance(value.get("ide"), str) or not IDE.fullmatch(
            value["ide"]):
        raise HandoffError("previous_owner ide is invalid")
    if "fence" in value and (type(value["fence"]) is not int
                             or value["fence"] < 0):
        raise HandoffError("previous_owner fence is invalid")


def _previous_owner(owner, fence):
    if isinstance(owner, dict):
        value = dict(owner)
        value.setdefault("fence", fence)
        return value
    return owner


def _validate_record(record) -> dict:
    if not isinstance(record, dict):
        raise HandoffError("ownership record must be an object")
    if set(record) != OWNERSHIP_FIELDS:
        raise HandoffError("ownership record has unexpected fields")
    if record.get("version") != OWNERSHIP_VERSION:
        raise HandoffError("unsupported ownership version")
    project_id = record.get("project_id")
    project_uuid = record.get("project_uuid")
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
        raise HandoffError("ownership project id is invalid")
    if (not isinstance(project_uuid, str)
            or not PROJECT_UUID.fullmatch(project_uuid.lower())):
        raise HandoffError("ownership project uuid is invalid")
    if record.get("state") not in STATES:
        raise HandoffError("ownership state is invalid")
    if type(record.get("fence")) is not int or record["fence"] < 0:
        raise HandoffError("ownership fence is invalid")
    _validate_owner(record.get("owner"))
    _validate_previous_owner(record.get("previous_owner"))
    handoff = record.get("handoff")
    if handoff is not None:
        _validate_handoff(handoff)
    transition = record.get("last_transition")
    if transition is not None and not isinstance(transition, dict):
        raise HandoffError("last_transition must be an object or null")
    if record.get("native_cancel_ack") is not False:
        raise HandoffError("native_cancel_ack must be false")
    if record.get("stop_ack") is not False:
        raise HandoffError("stop_ack must be false")
    return record


def _load_ownership(root: Path) -> dict | None:
    _check_state_dir(root)
    ide_directory = _ide_dir(root)
    if ide_directory.is_symlink():
        raise HandoffError("project IDE directory must not be a symlink")
    path = _ownership_path(root)
    if path.is_symlink():
        raise HandoffError("ownership file must not be a symlink")
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_OWNERSHIP_BYTES:
            raise HandoffError("ownership record is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except HandoffError:
        raise
    except (OSError, ValueError) as exc:
        raise HandoffError("ownership record is invalid") from exc
    return _validate_record(value)


def _identity_readonly(root: Path):
    try:
        return read_project_identity(root)
    except Exception:
        return None


def _identity_mutation(root: Path) -> dict:
    try:
        return ensure_project_identity(root)
    except Exception as exc:
        raise HandoffError("project identity is invalid") from exc


def _current_record(root: Path, identity: dict) -> dict:
    record = _load_ownership(root)
    if record is None:
        return _new_record(identity["project_id"], identity["project_uuid"])
    return record


def _project_matches(record: dict, identity) -> bool:
    if not isinstance(identity, dict):
        return False
    return (record.get("project_id") == identity.get("project_id")
            and str(record.get("project_uuid", "")).lower()
            == str(identity.get("project_uuid", "")).lower())


def _write_ownership(root: Path, record: dict) -> None:
    _validate_record(record)
    _ensure_dir(_ide_dir(root), "project IDE directory")
    path = _ownership_path(root)
    if path.is_symlink():
        raise HandoffError("ownership file must not be a symlink")
    text = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    if len(text.encode("utf-8")) > MAX_OWNERSHIP_BYTES:
        raise HandoffError("ownership record is too large")
    atomic_write_json(path, record)


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return ""


def _snapshot_path(root: Path, handoff_id: str) -> Path:
    _validate_handoff_id(handoff_id)
    _check_state_dir(root)
    if _ide_dir(root).is_symlink():
        raise HandoffError("project IDE directory must not be a symlink")
    directory = _safe_dir(_snapshots_dir(root), "handoff directory")
    path = directory / f"{handoff_id}.json"
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise HandoffError("handoff path is invalid") from exc
    if resolved.parent != directory.resolve():
        raise HandoffError("handoff path escapes the handoff directory")
    return path


def _write_snapshot(root: Path, handoff_id: str, value: dict) -> Path:
    _ensure_dir(_snapshots_dir(root), "handoff directory")
    path = _snapshot_path(root, handoff_id)
    if path.is_symlink():
        raise HandoffError("handoff snapshot must not be a symlink")
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    if len(text.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise HandoffError("handoff snapshot is too large")
    atomic_write_json(path, value)
    return path


def _required_string(value, label, *, limit=MAX_TEXT, optional=True):
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise HandoffError(f"{label} is invalid")
    text = _safe_text(value, limit)
    if not text and not optional:
        raise HandoffError(f"{label} is empty")
    return text or None


def _safe_string_list(value, label, *, limit=MAX_LIST, item_limit=300):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise HandoffError(f"{label} must be a bounded list")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise HandoffError(f"{label} items must be strings")
        text = _safe_text(item, item_limit)
        if text:
            result.append(text)
    return result


def _validate_snapshot(value: dict) -> dict:
    if not isinstance(value, dict):
        raise HandoffError("handoff snapshot must be an object")
    if set(value) - SNAPSHOT_FIELDS:
        raise HandoffError("handoff snapshot has unexpected fields")
    versions = [value[key] for key in ("version", "schema_version")
                if key in value]
    if not versions or any(item != SNAPSHOT_VERSION for item in versions):
        raise HandoffError("unsupported handoff snapshot version")
    required = {
        "project_id", "project_uuid", "source_ide", "target_ide",
        "source_session_id", "target_session_id", "run_id", "milestone_id",
        "task_id", "run_status", "milestone_status", "task_status", "mode",
        "branch", "dirty", "dirty_digest", "changed_files", "last_error",
        "failed_tests", "verified_evidence_refs", "contract_id",
        "contract_version", "interview_id", "request_hash",
        "blocking_questions", "confirmed_decision_refs", "blockers",
        "context_provenance", "context_refs", "next_action", "handoff_refs",
        "created_at", "digest",
    }
    if required - set(value):
        raise HandoffError("handoff snapshot is missing required fields")
    if not ({"head", "head_sha"} & set(value)):
        raise HandoffError("handoff snapshot has no repository head")
    if not ({"contract_sha256", "sha256"} & set(value)):
        raise HandoffError("handoff snapshot has no contract digest")
    if not ({"open_question", "open_question_id"} & set(value)):
        raise HandoffError("handoff snapshot has no open question")
    if (not isinstance(value.get("project_id"), str)
            or not PROJECT_ID.fullmatch(value["project_id"])):
        raise HandoffError("snapshot project id is invalid")
    uuid_value = value.get("project_uuid")
    if not isinstance(uuid_value, str) or not PROJECT_UUID.fullmatch(
            uuid_value.lower()):
        raise HandoffError("snapshot project uuid is invalid")
    if (not isinstance(value.get("source_ide"), str)
            or not IDE.fullmatch(value["source_ide"])):
        raise HandoffError("snapshot source ide is invalid")
    if (not isinstance(value.get("target_ide"), str)
            or not IDE.fullmatch(value["target_ide"])):
        raise HandoffError("snapshot target ide is invalid")
    _validate_session(value.get("source_session_id"))
    _validate_session(value.get("target_session_id"))
    for field in ("run_id", "milestone_id", "task_id", "run_status",
                  "milestone_status", "task_status", "mode", "branch", "head",
                  "head_sha", "contract_id", "contract_version",
                  "contract_sha256", "sha256", "interview_id", "open_question",
                  "open_question_id", "request_hash", "context_provenance"):
        item = value.get(field)
        if item is not None and not isinstance(item, (str, int)):
            raise HandoffError(f"snapshot {field} is invalid")
        if isinstance(item, str) and len(item) > MAX_TEXT:
            raise HandoffError(f"snapshot {field} is too long")
    if (value.get("head") is not None and value.get("head_sha") is not None
            and value["head"] != value["head_sha"]):
        raise HandoffError("snapshot head aliases disagree")
    if (value.get("contract_sha256") is not None
            and value.get("sha256") is not None
            and value["contract_sha256"] != value["sha256"]):
        raise HandoffError("snapshot contract digest aliases disagree")
    if (value.get("open_question") is not None
            and value.get("open_question_id") is not None
            and value["open_question"] != value["open_question_id"]):
        raise HandoffError("snapshot open question aliases disagree")
    if not isinstance(value.get("dirty"), bool):
        raise HandoffError("snapshot dirty flag is invalid")
    digest = value.get("dirty_digest")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise HandoffError("snapshot dirty digest is invalid")
    changed = value.get("changed_files")
    if not isinstance(changed, list) or len(changed) > 500:
        raise HandoffError("snapshot changed files are invalid")
    for item in changed:
        if isinstance(item, str):
            if len(item) > 1000:
                raise HandoffError("snapshot changed file entry is invalid")
            continue
        if not isinstance(item, dict):
            raise HandoffError("snapshot changed file entry is invalid")
        allowed = {"status", "path", "content_digest", "size", "kind"}
        if set(item) - allowed:
            raise HandoffError("snapshot changed file entry is invalid")
        if (not isinstance(item.get("status"), str)
                or not isinstance(item.get("path"), str)
                or len(item["status"]) > 8 or len(item["path"]) > 1000):
            raise HandoffError("snapshot changed file entry is invalid")
        content = item.get("content_digest")
        if content is not None and (not isinstance(content, str)
                                    or not SHA256.fullmatch(content)):
            raise HandoffError("snapshot content digest is invalid")
        size = item.get("size")
        if size is not None and (type(size) is not int or size < 0):
            raise HandoffError("snapshot content size is invalid")
    _safe_string_list(value.get("completed_tasks"), "completed_tasks")
    _safe_string_list(value.get("pending_tasks"), "pending_tasks")
    _safe_string_list(value.get("failed_tests"), "failed_tests")
    _safe_string_list(value.get("verified_evidence_refs"),
                      "verified_evidence_refs")
    _safe_string_list(value.get("blocking_questions"), "blocking_questions",
                      item_limit=200)
    _safe_string_list(value.get("confirmed_decision_refs"),
                      "confirmed_decision_refs", item_limit=100)
    _safe_string_list(value.get("blockers"), "blockers", item_limit=200)
    _safe_string_list(value.get("context_refs"), "context_refs")
    _safe_string_list(value.get("handoff_refs"), "handoff_refs")
    _required_string(value.get("last_error"), "last_error", optional=True)
    _required_string(value.get("next_action"), "next_action", limit=2000,
                     optional=True)
    if isinstance(value.get("contract_version"), bool) or (
            value.get("contract_version") is not None
            and type(value.get("contract_version")) is not int):
        raise HandoffError("snapshot contract version is invalid")
    for field in ("contract_sha256", "sha256"):
        if (value.get(field) is not None
                and (not isinstance(value[field], str)
                     or not SHA256.fullmatch(value[field]))):
            raise HandoffError("snapshot contract digest is invalid")
    if (value.get("request_hash") is not None
            and (not isinstance(value["request_hash"], str)
                 or not SHA256.fullmatch(value["request_hash"]))):
        raise HandoffError("snapshot request hash is invalid")
    created = value.get("created_at")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        raise HandoffError("snapshot created_at is invalid")
    if not isinstance(value.get("digest"), str) or not SHA256.fullmatch(
            value["digest"]):
        raise HandoffError("snapshot digest is invalid")
    body = {key: item for key, item in value.items() if key != "digest"}
    if _canonical_digest(body) != value["digest"]:
        raise HandoffError("snapshot digest does not match its contents")
    return value


def _load_snapshot(root: Path, handoff_id: str) -> dict:
    path = _snapshot_path(root, handoff_id)
    if path.is_symlink():
        raise HandoffError("handoff snapshot must not be a symlink")
    if not path.is_file():
        raise HandoffError("handoff snapshot does not exist")
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise HandoffError("handoff snapshot is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except HandoffError:
        raise
    except (OSError, ValueError) as exc:
        raise HandoffError("handoff snapshot is invalid") from exc
    return _validate_snapshot(value)


# -- read-only probes ----------------------------------------------------
def _read_bounded_json(path: Path, limit: int):
    if path.is_symlink():
        return None
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > limit:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_state_file(root: Path, name: str) -> dict:
    value = _read_bounded_json(_teamwork_dir(root) / name, MAX_ACTIVE_BYTES)
    if not isinstance(value, dict):
        return {}
    run_id = value.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        return {}
    return value


def _active_state(root: Path) -> dict:
    """Return the current run record.

    Managed runs use ``active.json`` (PID, runner token). Native plans have no
    managed runner, so they publish ``native-active.json`` with the run and
    plan only. Handoff snapshots must carry run/task state for both.
    """
    value = _read_state_file(root, "active.json")
    if value:
        return value
    return _read_state_file(root, "native-active.json")


def _probe_lock(path: Path, *, mutation: bool):
    """Return True when held, False when free, None when not verifiable."""
    try:
        if not path.exists():
            return False
        if path.is_symlink():
            return None
    except OSError:
        return None
    if os.name == "nt":
        if not mutation:
            return None
        try:
            import msvcrt
            handle = open(path, "r+b")
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            else:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                return False
            finally:
                handle.close()
        except OSError:
            return None
    try:
        import fcntl
        with open(path, "rb") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return True
            else:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                return False
    except OSError:
        return None


def _pid_liveness(owner: dict) -> tuple[str, str, dict]:
    pid_identity = owner.get("pid_identity")
    if not isinstance(pid_identity, dict):
        heartbeat = owner.get("heartbeat_at")
        ttl = owner.get("ttl_seconds")
        if isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, float)):
            return "unknown", "heartbeat_missing", {}
        age = max(0.0, round(time.time() - float(heartbeat), 3))
        ttl_value = int(ttl) if isinstance(ttl, (int, float)) else 0
        details = {"heartbeat_age_seconds": age, "ttl_seconds": ttl_value}
        if ttl_value and age <= ttl_value:
            return "alive", "heartbeat_fresh", details
        return "unknown", "heartbeat_stale", details
    pid = pid_identity.get("pid")
    if type(pid) is not int or pid <= 0:
        return "unknown", "pid_identity_invalid", {}
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "dead", "pid_gone", {"pid": pid}
        except PermissionError:
            return "unknown", "pid_unverifiable", {"pid": pid}
        except OSError:
            return "unknown", "pid_unverifiable", {"pid": pid}
    live = read_process_identity(pid)
    if live is None:
        if os.name == "nt":
            return "dead", "pid_gone", {"pid": pid}
        return "unknown", "pid_identity_unavailable", {"pid": pid}
    state = str(live.get("state") or "").upper()
    if state.startswith("Z"):
        return "dead", "pid_zombie", {"pid": pid}
    if live.get("start_token") != pid_identity.get("start_token"):
        return "dead", "pid_reused", {"pid": pid}
    return "alive", "pid_verified", {"pid": pid}


def _liveness(root: Path, record: dict, *, mutation=False) -> dict:
    active = _active_state(root)
    run_id = active.get("run_id") if isinstance(active.get("run_id"), str) else None
    writer = _probe_lock(
        _teamwork_dir(root) / "project-writer.lock", mutation=mutation)
    runner = False
    if isinstance(run_id, str) and RUN_ID.fullmatch(run_id):
        runner = _probe_lock(
            _teamwork_dir(root) / f"runner-{run_id}.lock", mutation=mutation)
    reasons = []
    owner = record.get("owner")
    current_host = socket.gethostname()
    if not isinstance(owner, dict):
        owner_status = "none"
        owner_reason = "no_owner"
        details = {}
    elif owner.get("host") and owner.get("host") != current_host:
        owner_status = "unknown"
        owner_reason = "owner_host_unknown"
        details = {"owner_host": owner.get("host"), "host": current_host}
    else:
        owner_status, owner_reason, details = _pid_liveness(owner)
    heartbeat = owner.get("heartbeat_at") if isinstance(owner, dict) else None
    ttl = owner.get("ttl_seconds") if isinstance(owner, dict) else None
    heartbeat_details = {}
    if (not isinstance(heartbeat, bool)
            and isinstance(heartbeat, (int, float))
            and isinstance(ttl, (int, float)) and ttl > 0):
        age = max(0.0, round(time.time() - float(heartbeat), 3))
        heartbeat_details = {
            "heartbeat_age_seconds": age,
            "heartbeat_fresh": age <= int(ttl),
            "expired": age > int(ttl),
        }
    if owner_status == "alive":
        reasons.append(_reason(owner_reason, "owner process is verified alive",
                               source="owner", **details))
    elif owner_status == "dead":
        reasons.append(_reason(owner_reason, "owner process is not alive",
                               source="owner", **details))
    elif owner_status == "unknown":
        reasons.append(_reason(owner_reason,
                               "owner liveness cannot be determined; stale is "
                               "not proof of death", source="owner", **details))
    if writer is True:
        reasons.append(_reason("writer_lease_held",
                               "the project writer lease is held"))
    elif writer is None:
        reasons.append(_reason("writer_lease_unknown",
                               "the project writer lease cannot be verified"))
    if runner is True:
        reasons.append(_reason("runner_lease_held",
                               "the managed runner lease is held",
                               run_id=run_id))
    elif runner is None:
        reasons.append(_reason("runner_lease_unknown",
                               "the managed runner lease cannot be verified",
                               run_id=run_id))
    leases_held = writer is True or runner is True
    unknown_live = (owner_status == "unknown" or writer is None or runner is None
                    or (leases_held and owner_status != "alive"))
    return {
        "owner": {"status": owner_status, "reason": owner_reason,
                  "alive": owner_status == "alive",
                  "dead": owner_status == "dead",
                  "unknown": owner_status == "unknown",
                  **details, **heartbeat_details},
        "leases": {
            "project_writer": writer,
            "runner": runner,
            "run_id": run_id,
            "held": leases_held,
        },
        "unknown_live": bool(unknown_live),
        "reasons": reasons,
    }


# -- repository and project facts ---------------------------------------
def _run_git(root: Path, args) -> bytes | None:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        process = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, timeout=10,
            env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if process.returncode != 0:
        return None
    return process.stdout


def _excluded_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return (normalized == ".auto-orchestrator"
            or normalized.startswith(".auto-orchestrator/"))


def _content_evidence(root: Path, path_text: str,
                      budget: int) -> tuple[dict, int]:
    """Return per-file evidence and the bytes read for the digest budget."""
    if (not path_text or path_text.startswith("/")
            or ".." in path_text.split("/")):
        return {"content_digest": None, "size": None, "kind": "invalid"}, 0
    path = root / path_text
    try:
        stat_info = path.lstat()
    except OSError:
        return {"content_digest": None, "size": None, "kind": "missing"}, 0
    if stat.S_ISLNK(stat_info.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            return {"content_digest": None, "size": None, "kind": "symlink"}, 0
        encoded = target.encode("utf-8", "replace")
        return {
            "content_digest": hashlib.sha256(encoded).hexdigest(),
            "size": len(encoded),
            "kind": "symlink",
        }, len(encoded)
    if not stat.S_ISREG(stat_info.st_mode):
        return {"content_digest": None, "size": None, "kind": "other"}, 0
    size = int(stat_info.st_size)
    if size > MAX_CONTENT_HASH_BYTES:
        if budget < 131072:
            return {"content_digest": None, "size": size,
                    "kind": "budget"}, 0
        try:
            with path.open("rb") as handle:
                head = handle.read(65536)
                handle.seek(-65536, os.SEEK_END)
                tail = handle.read(65536)
        except OSError:
            return {"content_digest": None, "size": size, "kind": "large"}, 0
        return {
            "content_digest": hashlib.sha256(head + tail).hexdigest(),
            "size": size,
            "kind": "large",
        }, len(head) + len(tail)
    if size > budget:
        return {"content_digest": None, "size": size, "kind": "budget"}, 0
    try:
        payload = path.read_bytes()
    except OSError:
        return {"content_digest": None, "size": None,
                "kind": "unreadable"}, 0
    return {
        "content_digest": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "kind": "file",
    }, len(payload)


def _git_state(root: Path) -> dict:
    head_raw = _run_git(root, ["rev-parse", "--verify", "HEAD"])
    head = head_raw.decode("utf-8", "replace").strip() if head_raw else None
    branch_raw = _run_git(root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    branch = branch_raw.decode("utf-8", "replace").strip() if branch_raw else None
    status_raw = _run_git(root, [
        "status", "--porcelain=v1", "-z", "--untracked-files=all",
    ])
    entries = []
    budget = MAX_DIRTY_CONTENT_BYTES
    if status_raw:
        parts = status_raw.split(b"\0")
        index = 0
        while index < len(parts) and len(entries) < MAX_DISCOVERED_CHANGES:
            token = parts[index]
            index += 1
            if not token:
                continue
            text = token.decode("utf-8", "replace")
            if len(text) < 4:
                continue
            status = text[:2]
            path = text[3:]
            if status[0] in ("R", "C") and index < len(parts):
                index += 1
            path = path.replace("\\", "/")
            if _excluded_path(path):
                continue
            evidence, used = _content_evidence(root, path, budget)
            budget -= used
            entries.append({
                "status": status,
                "path": path,
                **evidence,
            })
    entries.sort(key=lambda item: (item["path"], item["status"]))
    return {
        "branch": branch or None,
        "head": head or None,
        "dirty": bool(entries),
        "dirty_digest": _canonical_digest(entries),
        "changed_files": entries[:MAX_CHANGED_FILES],
    }


def _contract_digest(contract: dict) -> str:
    body = {key: value for key, value in contract.items()
            if key not in CONTRACT_METADATA}
    return _canonical_digest(body)


def _contract_state(root: Path) -> dict:
    result = {
        "contract_id": None,
        "contract_version": None,
        "contract_sha256": None,
        "interview_id": None,
        "valid": True,
        "reasons": [],
    }
    directory = _state_dir(root) / "contracts"
    if directory.is_symlink():
        result["valid"] = False
        result["reasons"].append(_reason("contract_directory_symlink",
                                         "contract directory is a symlink"))
        return result
    pointer_path = directory / "current.json"
    if pointer_path.is_symlink():
        result["valid"] = False
        result["reasons"].append(_reason("contract_pointer_symlink",
                                         "contract pointer is a symlink"))
        return result
    if not pointer_path.is_file():
        return result
    pointer = _read_bounded_json(pointer_path, 8192)
    if not isinstance(pointer, dict):
        result["valid"] = False
        result["reasons"].append(_reason("contract_pointer_invalid",
                                         "contract pointer is invalid"))
        return result
    contract_id = pointer.get("contract_id")
    version = pointer.get("contract_version")
    sha = pointer.get("sha256")
    if (not isinstance(contract_id, str) or not CONTRACT_ID.fullmatch(contract_id)
            or type(version) is not int or version < 1 or version > 10000
            or not isinstance(sha, str) or not SHA256.fullmatch(sha)):
        result["valid"] = False
        result["reasons"].append(_reason("contract_pointer_invalid",
                                         "contract pointer is invalid"))
        return result
    result.update({
        "contract_id": contract_id,
        "contract_version": version,
        "contract_sha256": sha,
    })
    path = directory / f"{contract_id}-v{version}.json"
    try:
        if path.resolve().parent != directory.resolve():
            raise HandoffError("contract path escapes")
    except (OSError, HandoffError):
        result["valid"] = False
        result["reasons"].append(_reason("contract_path_invalid",
                                         "contract path is invalid"))
        return result
    if path.is_symlink():
        result["valid"] = False
        result["reasons"].append(_reason("contract_symlink",
                                         "contract file is a symlink"))
        return result
    if not path.is_file():
        result["valid"] = False
        result["reasons"].append(_reason("contract_missing",
                                         "contract file is missing"))
        return result
    try:
        if path.stat().st_size > 131072:
            raise ValueError("too large")
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result["valid"] = False
        result["reasons"].append(_reason("contract_invalid",
                                         "contract file is invalid"))
        return result
    if not isinstance(contract, dict):
        result["valid"] = False
        result["reasons"].append(_reason("contract_invalid",
                                         "contract file is invalid"))
        return result
    if (contract.get("contract_id") != contract_id
            or contract.get("contract_version") != version):
        result["valid"] = False
        result["reasons"].append(_reason("contract_pointer_mismatch",
                                         "contract file does not match pointer"))
        return result
    actual = _contract_digest(contract)
    if actual != sha or contract.get("sha256") != sha:
        result["valid"] = False
        result["reasons"].append(_reason("contract_digest_mismatch",
                                         "contract digest does not match"))
    interview_id = contract.get("interview_id")
    if isinstance(interview_id, str) and INTERVIEW_ID.fullmatch(interview_id):
        result["interview_id"] = interview_id
    return result


def _open_sqlite_readonly(path: Path):
    if path.is_symlink() or not path.is_file():
        return None
    try:
        uri = path.as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection
    except (sqlite3.Error, OSError, ValueError):
        return None


def _table_names(connection) -> set[str]:
    try:
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


def _interview_digest(fields: dict) -> str:
    return _canonical_digest({
        "interview_id": fields.get("interview_id"),
        "open_question": (fields.get("open_question_id")
                          or fields.get("open_question")),
        "request_hash": fields.get("request_hash"),
        "blocking_questions": list(fields.get("blocking_questions") or []),
    })


def _memory_state(root: Path, project_id: str,
                  preferred_interview_id=None) -> dict:
    result = {
        "available": False,
        "interview_id": None,
        "open_question": None,
        "request_hash": None,
        "blocking_questions": [],
        "confirmed_decision_refs": [],
        "blockers": [],
        "reasons": [],
    }
    path = _state_dir(root) / "memory.sqlite3"
    connection = _open_sqlite_readonly(path)
    if connection is None:
        if path.exists() or path.is_symlink():
            result["reasons"].append(_reason("memory_unavailable",
                                             "project memory is unreadable"))
        return result
    try:
        tables = _table_names(connection)
        if "interviews" not in tables:
            return result
        result["available"] = True
        rows = []
        try:
            rows = [dict(row) for row in connection.execute(
                "SELECT interview_id,request_hash,status,updated_at "
                "FROM interviews WHERE project_id=? "
                "ORDER BY updated_at DESC LIMIT 100", (project_id,))]
        except sqlite3.Error:
            rows = []
        selected = None
        if (isinstance(preferred_interview_id, str)
                and INTERVIEW_ID.fullmatch(preferred_interview_id)):
            selected = next((row for row in rows
                             if row.get("interview_id") == preferred_interview_id),
                            None)
            if selected is None:
                result["interview_id"] = preferred_interview_id
        elif rows:
            selected = rows[0]
        if selected is not None:
            result["interview_id"] = selected.get("interview_id")
            request_hash = selected.get("request_hash")
            if isinstance(request_hash, str) and SHA256.fullmatch(request_hash):
                result["request_hash"] = request_hash
        blocking = []
        if ("interview_questions" in tables
                and isinstance(result["interview_id"], str)
                and result["interview_id"]):
            try:
                questions = list(connection.execute(
                    "SELECT question_id,status,ordinal FROM interview_questions "
                    "WHERE interview_id=? ORDER BY ordinal",
                    (result["interview_id"],)))
            except sqlite3.Error:
                questions = []
            for question in questions:
                status = str(question["status"])
                question_id = question["question_id"]
                if (status in BLOCKING_QUESTION_STATES
                        and isinstance(question_id, str)):
                    blocking.append(question_id)
        result["blocking_questions"] = blocking[:MAX_LIST]
        result["open_question"] = blocking[0] if blocking else None
        if "decisions" in tables:
            try:
                result["confirmed_decision_refs"] = [
                    str(row["decision_id"]) for row in connection.execute(
                        "SELECT decision_id FROM decisions WHERE project_id=? "
                        "AND status='confirmed' ORDER BY updated_at DESC "
                        "LIMIT ?", (project_id, MAX_LIST))
                    if isinstance(row["decision_id"], str)
                    and row["decision_id"]]
            except sqlite3.Error:
                pass
        if "requirements" in tables:
            try:
                result["blockers"] = [
                    "requirement:" + str(row["requirement_id"])
                    for row in connection.execute(
                        "SELECT requirement_id FROM requirements "
                        "WHERE project_id=? AND status='unresolved_blocking' "
                        "ORDER BY updated_at DESC LIMIT ?",
                        (project_id, MAX_LIST))
                    if isinstance(row["requirement_id"], str)]
            except sqlite3.Error:
                pass
        return result
    finally:
        connection.close()


def _choose_task(tasks):
    priority = {
        "running": 0, "cancel_requested": 0, "timed_out": 1,
        "failed": 1, "cancelled": 1, "pending": 2, "completed": 3,
    }
    return min(tasks, key=lambda item: (
        priority.get(str(item.get("state")), 4), item.get("rowid", 0)),
        default=None)


def _choose_milestone(milestones):
    priority = {
        "running": 0, "repairing": 0, "blocked": 1, "planned": 2,
        "failed": 3, "passed": 4,
    }
    return min(milestones, key=lambda item: (
        priority.get(str(item.get("status")), 5), item.get("rowid", 0)),
        default=None)


def _latest_plan_state(root: Path) -> dict:
    directory = _state_dir(root) / "plans"
    if directory.is_symlink() or not directory.is_dir():
        return {}
    candidates = []
    try:
        for path in directory.glob("*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return {}
    for _, path in sorted(candidates, reverse=True)[:20]:
        value = _read_bounded_json(path, 1024 * 1024)
        if not isinstance(value, dict):
            continue
        run_id = value.get("run_id")
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            continue
        return {
            "run_id": run_id,
            "mode": _safe_text(value.get("work_mode"), 40) or None,
            "status": "planned",
        }
    return {}


def _run_state(root: Path, identity: dict) -> dict:
    active = _active_state(root)
    discovered = {}
    run_id = active.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        discovered = _latest_plan_state(root)
        run_id = discovered.get("run_id")
    result = {
        "run_id": run_id,
        "milestone_id": None,
        "task_id": None,
        "run_status": (_safe_text(active.get("status"), 40) or None
                       if run_id == active.get("run_id")
                       else discovered.get("status")),
        "milestone_status": None,
        "task_status": None,
        "mode": (_safe_text(active.get("mode"), 40)
                 if run_id == active.get("run_id")
                 else discovered.get("mode")) or None,
        "last_error": None,
        "completed_tasks": [],
        "pending_tasks": [],
        "failed_tests": [],
        "verified_evidence_refs": [],
        "blockers": [],
    }
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        result["run_id"] = None
        return result
    path = _state_dir(root) / "supervisor.sqlite3"
    connection = _open_sqlite_readonly(path)
    if connection is None:
        return result
    try:
        tables = _table_names(connection)
        if "runs" not in tables or "tasks" not in tables:
            return result
        try:
            run = connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        except sqlite3.Error:
            run = None
        if run is None:
            return result
        try:
            tasks = [dict(row, rowid=index) for index, row in enumerate(
                connection.execute("SELECT * FROM tasks WHERE run_id=? "
                                   "ORDER BY rowid", (run_id,)))]
        except sqlite3.Error:
            tasks = []
        task = _choose_task(tasks)
        if task is not None:
            result["task_id"] = task.get("id")
            result["task_status"] = task.get("state")
        result["completed_tasks"] = [
            str(item.get("id")) for item in tasks
            if item.get("state") == "completed" and item.get("id")]
        result["pending_tasks"] = [
            str(item.get("id")) for item in tasks
            if item.get("state") in ("pending", "running", "cancel_requested")
            and item.get("id")]
        states = [str(item.get("state")) for item in tasks]
        if any(state in ("running", "cancel_requested") for state in states):
            result["run_status"] = "running"
        elif any(state in ("failed", "cancelled", "timed_out")
                 for state in states):
            result["run_status"] = "failed"
        elif states and all(state == "completed" for state in states):
            result["run_status"] = "completed"
        elif states:
            result["run_status"] = "planned"
        elif result["run_status"] is None:
            result["run_status"] = "planned"
        milestones = []
        if "milestones" in tables:
            try:
                milestones = [dict(row, rowid=index) for index, row in enumerate(
                    connection.execute(
                        "SELECT * FROM milestones WHERE run_id=? ORDER BY rowid",
                        (run_id,)))]
            except sqlite3.Error:
                milestones = []
        milestone = _choose_milestone(milestones)
        if milestone is not None:
            result["milestone_id"] = milestone.get("id")
            result["milestone_status"] = milestone.get("status")
        if milestone is not None and milestone.get("last_error"):
            result["last_error"] = _safe_text(milestone["last_error"], 300)
        elif task is not None and task.get("reason"):
            result["last_error"] = _safe_text(task["reason"], 300)
        failed = []
        for item in tasks:
            raw = item.get("result")
            if not isinstance(raw, str):
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            for test in payload.get("tests") or []:
                if (isinstance(test, dict) and test.get("status") == "failed"
                        and isinstance(test.get("name"), str)):
                    name = _safe_text(test["name"], 200)
                    if name and name not in failed:
                        failed.append(name)
        result["failed_tests"] = failed[:MAX_LIST]
        if "teamwork_handoffs" in tables:
            try:
                refs = []
                for row in connection.execute(
                        "SELECT path FROM teamwork_handoffs WHERE run_id=? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (run_id, MAX_LIST)):
                    ref = _relative(root, Path(str(row["path"])))
                    if ref:
                        refs.append(ref)
                result["verified_evidence_refs"] = refs
            except sqlite3.Error:
                pass
        if result["milestone_status"] == "blocked":
            result["blockers"].append(
                f"milestone:{result['milestone_id']}:blocked")
        if result["run_status"] == "failed":
            result["blockers"].append(f"run:{run_id}:failed")
        return result
    finally:
        connection.close()


def _context_refs(root: Path) -> list[str]:
    directory = _state_dir(root) / "context"
    if directory.is_symlink() or not directory.is_dir():
        return []
    refs = []
    try:
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if len(refs) >= 20:
                break
            if path.is_symlink() or not path.is_file():
                continue
            ref = _relative(root, path)
            if ref:
                refs.append(ref)
    except OSError:
        return refs
    return refs


def _handoff_refs(root: Path) -> list[str]:
    refs = []
    for directory in (_state_dir(root) / "handoffs", _snapshots_dir(root)):
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            paths = sorted(directory.glob("*.json"), key=lambda item: item.name)
        except OSError:
            continue
        for path in paths[-10:]:
            if path.is_symlink() or not path.is_file():
                continue
            ref = _relative(root, path)
            if ref and ref not in refs:
                refs.append(ref)
    return refs[:20]


def _project_facts(root: Path, identity: dict, *, git=None) -> dict:
    contract = _contract_state(root)
    memory = _memory_state(root, identity["project_id"],
                           contract.get("interview_id"))
    return {
        "git": git if git is not None else _git_state(root),
        "contract": contract,
        "memory": memory,
        "run": _run_state(root, identity),
        "context_refs": _context_refs(root),
        "handoff_refs": _handoff_refs(root),
    }


def _build_snapshot(identity: dict, owner: dict, target_ide: str, facts: dict,
                    next_action, handoff_refs) -> dict:
    run = facts["run"]
    contract = facts["contract"]
    memory = facts["memory"]
    git = facts["git"]
    blockers = list(run.get("blockers") or []) + list(
        memory.get("blockers") or [])
    snapshot = {
        "version": SNAPSHOT_VERSION,
        "schema_version": SNAPSHOT_VERSION,
        "project_id": identity["project_id"],
        "project_uuid": identity["project_uuid"],
        "source_ide": owner["ide"],
        "target_ide": target_ide,
        "source_session_id": owner.get("session_id"),
        "target_session_id": None,
        "run_id": run.get("run_id"),
        "milestone_id": run.get("milestone_id"),
        "task_id": run.get("task_id"),
        "run_status": run.get("run_status"),
        "milestone_status": run.get("milestone_status"),
        "task_status": run.get("task_status"),
        "mode": run.get("mode"),
        "branch": git.get("branch"),
        "head": git.get("head"),
        "head_sha": git.get("head"),
        "dirty": bool(git.get("dirty")),
        "dirty_digest": git.get("dirty_digest"),
        "changed_files": list(
            git.get("changed_files") or [])[:MAX_CHANGED_FILES],
        "completed_tasks": list(run.get("completed_tasks") or [])[:MAX_LIST],
        "pending_tasks": list(run.get("pending_tasks") or [])[:MAX_LIST],
        "last_error": _safe_text(run.get("last_error"), 300) or None,
        "failed_tests": list(run.get("failed_tests") or [])[:MAX_LIST],
        "verified_evidence_refs": list(
            run.get("verified_evidence_refs") or [])[:MAX_LIST],
        "contract_id": contract.get("contract_id"),
        "contract_version": contract.get("contract_version"),
        "contract_sha256": contract.get("contract_sha256"),
        "sha256": contract.get("contract_sha256"),
        "interview_id": memory.get("interview_id"),
        "open_question": memory.get("open_question"),
        "open_question_id": memory.get("open_question"),
        "request_hash": memory.get("request_hash"),
        "blocking_questions": list(
            memory.get("blocking_questions") or [])[:MAX_LIST],
        "confirmed_decision_refs": list(
            memory.get("confirmed_decision_refs") or [])[:MAX_LIST],
        "blockers": blockers[:MAX_LIST],
        "context_provenance": (
            "project_memory_context" if facts.get("context_refs") else None),
        "context_refs": list(facts.get("context_refs") or [])[:20],
        "next_action": _safe_text(next_action, 2000) or None,
        "handoff_refs": list(handoff_refs or [])[:20],
        "created_at": time.time(),
    }
    snapshot["digest"] = _canonical_digest(snapshot)
    return _validate_snapshot(snapshot)


def _public_owner(owner):
    if not isinstance(owner, dict):
        return None
    value = {key: item for key, item in owner.items() if key != "token"}
    value["token_present"] = bool(owner.get("token"))
    return value


def _public_record(record: dict) -> dict:
    value = dict(record)
    value["owner"] = _public_owner(record.get("owner"))
    value["previous_owner"] = _public_owner(record.get("previous_owner"))
    return value


def _result(record: dict, *, ok: bool, changed: bool = False,
            unchanged: bool = False, status=None, reasons=None,
            owner_token=None, extra=None) -> dict:
    value = {
        "ok": bool(ok),
        "changed": bool(changed),
        "unchanged": bool(unchanged),
        "status": status or record.get("state"),
        "state": record.get("state"),
        "fence": record.get("fence"),
        "project_id": record.get("project_id"),
        "owner": _public_owner(record.get("owner")),
        "handoff": record.get("handoff"),
        "reasons": list(reasons or []),
        "native_cancel_ack": False,
        "stop_ack": False,
        "ownership_path": None,
        "ownership": _public_record(record),
    }
    if owner_token:
        value["owner_token"] = owner_token
        value["token"] = owner_token
    if extra:
        value.update(extra)
    return value


def _blocked(record, code, message, **extra):
    return _result(record, ok=False, status="blocked",
                   reasons=[_reason(code, message, **extra)])


def _conflict(record, code, message, **extra):
    return _result(record, ok=False, status="conflict",
                   reasons=[_reason(code, message, **extra)])


# -- public API ----------------------------------------------------------
def status(root) -> dict:
    """Read ownership and liveness without creating or changing anything."""
    root = _root(root)
    reasons = []
    identity = _identity_readonly(root)
    try:
        record = _load_ownership(root)
    except HandoffError as exc:
        return {
            "ok": False,
            "changed": False,
            "unchanged": False,
            "status": "blocked",
            "state": "blocked",
            "fence": None,
            "project_id": None,
            "project_uuid": None,
            "owner": None,
            "handoff": None,
            "previous_owner": None,
            "last_transition": None,
            "native_cancel_ack": False,
            "stop_ack": False,
            "reasons": [_reason("ownership_invalid", str(exc))],
            "liveness": None,
            "snapshot": None,
            "ownership": None,
            "ownership_path": str(_ownership_path(root)),
            "read_only": True,
        }
    if record is None:
        record = _new_record(
            identity.get("project_id") if isinstance(identity, dict) else None,
            identity.get("project_uuid") if isinstance(identity, dict) else None,
        )
    elif identity is None:
        reasons.append(_reason("project_identity_missing",
                               "project identity is missing or invalid"))
    if identity is not None and not _project_matches(record, identity):
        reasons.append(_reason("wrong_project",
                               "ownership record belongs to another project"))
    live = _liveness(root, record, mutation=False)
    snapshot = None
    if isinstance(record.get("handoff"), dict):
        try:
            loaded = _load_snapshot(root, record["handoff"]["handoff_id"])
            snapshot = {
                "handoff_id": record["handoff"]["handoff_id"],
                "digest": loaded["digest"],
                "source_ide": loaded["source_ide"],
                "target_ide": loaded["target_ide"],
                "created_at": loaded["created_at"],
                # Continuity view for the accepting IDE. These are pointers
                # and bounded summaries from the validated snapshot, not a
                # copy of another IDE's chat or prompts.
                "run_id": loaded.get("run_id"),
                "milestone_id": loaded.get("milestone_id"),
                "task_id": loaded.get("task_id"),
                "task_status": loaded.get("task_status"),
                "branch": loaded.get("branch"),
                "head": loaded.get("head"),
                "dirty": loaded.get("dirty"),
                "completed_tasks": list(loaded.get("completed_tasks") or []),
                "pending_tasks": list(loaded.get("pending_tasks") or []),
                "next_action": loaded.get("next_action"),
                "open_question": loaded.get("open_question"),
                "blocking_questions": list(
                    loaded.get("blocking_questions") or []),
                "failed_tests": list(loaded.get("failed_tests") or []),
                "contract_id": loaded.get("contract_id"),
                "contract_sha256": loaded.get("contract_sha256"),
                "interview_id": loaded.get("interview_id"),
            }
        except HandoffError as exc:
            snapshot = {"error": str(exc)}
    value = _result(
        record,
        ok=not reasons and record["state"] not in (
            "blocked", "recovery_required"),
        status=record["state"],
        reasons=reasons + [
            item for item in (live.get("reasons") or [])
            if item.get("code") not in (
                "pid_verified", "heartbeat_fresh", "no_owner")
        ])
    owner_live = live.get("owner") or {}
    stale_reasons = [
        item.get("code") for item in (live.get("reasons") or [])
        if item.get("code") in (
            "pid_gone", "pid_reused", "pid_zombie", "heartbeat_stale",
            "owner_host_unknown", "writer_lease_held", "runner_lease_held")
    ]
    stale = {
        "is_stale": bool(
            owner_live.get("status") in ("dead", "unknown")
            or owner_live.get("expired") is True),
        "reasons": stale_reasons,
    }
    value.pop("project_id", None)
    value.update({
        "project_id": record.get("project_id"),
        "project_uuid": record.get("project_uuid"),
        "previous_owner": _public_owner(record.get("previous_owner")),
        "last_transition": record.get("last_transition"),
        "native_cancel_ack": False,
        "stop_ack": False,
        "liveness": live,
        "snapshot": snapshot,
        "stale": stale,
        "ownership_path": str(_ownership_path(root)),
        "read_only": True,
    })
    return value


def activate(root, ide, session_id=None, ttl=DEFAULT_TTL_SECONDS,
             pid=None, owner_id=None) -> dict:
    root = _root(root)
    ide = _validate_ide(ide)
    session_id = _validate_session(session_id)
    ttl = _validate_ttl(ttl)
    pid = _validate_pid(pid)
    owner_id = _validate_owner_id(owner_id)
    pid_identity = None
    if pid is not None:
        pid_identity = read_process_identity(pid)
        if pid_identity is None:
            raise HandoffError("pid identity could not be verified")
    _check_state_dir(root)
    with project_lock(root, timeout=10.0):
        identity = _identity_mutation(root)
        record = _current_record(root, identity)
        if not _project_matches(record, identity):
            return _blocked(record, "wrong_project",
                            "ownership record belongs to another project")
        if record["state"] not in ("inactive", "released"):
            return _conflict(record, "state_not_activatable",
                             "ownership is not available for activation",
                             current_state=record["state"])
        if record["state"] == "released" and record.get("handoff") is not None:
            return _conflict(record, "handoff_pending_accept",
                             "a prepared handoff must be accepted")
        live = _liveness(root, record, mutation=True)
        if live["leases"]["held"]:
            return _blocked(record, "lease_held",
                            "a managed writer or runner lease is active")
        if live["unknown_live"]:
            return _blocked(record, "liveness_unknown",
                            "existing ownership liveness cannot be determined")
        now = time.time()
        owner = {
            "owner_id": owner_id,
            "ide": ide,
            "session_id": session_id,
            "pid_identity": pid_identity,
            "host": socket.gethostname(),
            "activated_at": now,
            "heartbeat_at": now,
            "ttl_seconds": ttl,
            "token": secrets.token_hex(24),
        }
        _validate_owner(owner)
        updated = {
            **record,
            "state": "active",
            "fence": record["fence"] + 1,
            "owner": owner,
            "handoff": None,
            "previous_owner": _previous_owner(
                record.get("owner") or record.get("previous_owner"),
                record["fence"]),
            "last_transition": {
                "op": "activate",
                "action": "activate",
                "at": now,
                "by": owner_id,
                "actor": owner_id,
                "fence": record["fence"] + 1,
                "from_state": record["state"],
                "to_state": "active",
                "from_fence": record["fence"],
                "to_fence": record["fence"] + 1,
            },
            "native_cancel_ack": False,
            "stop_ack": False,
        }
        _write_ownership(root, updated)
        result = _result(updated, ok=True, changed=True,
                         owner_token=owner["token"])
        result["ownership_path"] = str(_ownership_path(root))
        return result


def heartbeat(root, owner_token, fence) -> dict:
    root = _root(root)
    owner_token = _validate_token(owner_token)
    fence = _validate_fence(fence)
    _check_state_dir(root)
    with project_lock(root, timeout=10.0):
        identity = _identity_mutation(root)
        record = _current_record(root, identity)
        if not _project_matches(record, identity):
            return _blocked(record, "wrong_project",
                            "ownership record belongs to another project")
        owner = record.get("owner")
        transition = record.get("last_transition") or {}
        if (isinstance(owner, dict) and owner.get("token") == owner_token
                and record["fence"] == fence + 1
                and transition.get("op") == "heartbeat"
                and transition.get("from_fence") == fence):
            return _result(record, ok=True, unchanged=True)
        if (not isinstance(owner, dict) or owner.get("token") != owner_token
                or record["fence"] != fence):
            return _conflict(record, "stale_fence_or_token",
                             "owner token and fence do not match")
        if record["state"] not in ACTIVE_OWNER_STATES:
            return _conflict(record, "state_not_heartbeatable",
                             "ownership is not active",
                             current_state=record["state"])
        live = _liveness(root, record, mutation=True)
        if owner.get("pid_identity") is not None and (
                live["owner"]["status"] != "alive"):
            return _conflict(
                record, "owner_identity_mismatch",
                "the recorded owner process is not verifiably alive")
        now = time.time()
        updated = {
            **record,
            "fence": fence + 1,
            "owner": {**owner, "heartbeat_at": now},
            "last_transition": {
                "op": "heartbeat",
                "action": "heartbeat",
                "at": now,
                "actor": owner.get("owner_id"),
                "fence": fence + 1,
                "from_state": record["state"],
                "to_state": record["state"],
                "from_fence": fence,
                "to_fence": fence + 1,
                "from_owner": owner.get("owner_id"),
            },
            "native_cancel_ack": False,
            "stop_ack": False,
        }
        _write_ownership(root, updated)
        result = _result(updated, ok=True, changed=True)
        result["ownership_path"] = str(_ownership_path(root))
        return result


def prepare_handoff(root, owner_token, fence, target_ide, next_action=None,
                    allow_dirty=False, confirm_idle=False) -> dict:
    root = _root(root)
    owner_token = _validate_token(owner_token)
    fence = _validate_fence(fence)
    target_ide = _validate_ide(target_ide)
    if next_action is not None:
        if not isinstance(next_action, str) or len(next_action) > 4000:
            raise HandoffError("next_action is invalid")
    _check_state_dir(root)
    with project_lock(root, timeout=10.0):
        identity = _identity_mutation(root)
        record = _current_record(root, identity)
        if not _project_matches(record, identity):
            return _blocked(record, "wrong_project",
                            "ownership record belongs to another project")
        owner = record.get("owner")
        handoff = record.get("handoff")
        if (record["state"] == "handoff_prepared"
                and isinstance(owner, dict) and owner.get("token") == owner_token
                and isinstance(handoff, dict)
                and handoff.get("source_fence") == fence
                and handoff.get("target_ide") == target_ide):
            try:
                existing = _load_snapshot(root, handoff["handoff_id"])
            except HandoffError as exc:
                return _conflict(record, "handoff_replay_invalid", str(exc))
            expected_next = _safe_text(next_action, 2000) or None
            if existing.get("next_action") != expected_next:
                return _conflict(
                    record, "handoff_replay_mismatch",
                    "prepare replay does not match the existing snapshot")
            return _result(record, ok=True, unchanged=True)
        if (not isinstance(owner, dict) or owner.get("token") != owner_token
                or record["fence"] != fence):
            return _conflict(record, "stale_fence_or_token",
                             "owner token and fence do not match")
        if record["state"] not in ACTIVE_OWNER_STATES:
            return _conflict(record, "state_not_preparable",
                             "ownership is not active",
                             current_state=record["state"])
        live = _liveness(root, record, mutation=True)
        if live["owner"]["status"] == "dead":
            return _blocked(record, "owner_not_alive",
                            "the owner process is not alive; use recovery",
                            liveness=live["owner"])
        if live["owner"]["status"] != "alive" or live["unknown_live"]:
            return _blocked(record, "liveness_unknown",
                            "owner liveness cannot be determined",
                            liveness=live["owner"])
        if live["leases"]["held"]:
            return _blocked(record, "lease_held",
                            "a managed writer or runner lease is active")
        if not confirm_idle:
            return _blocked(record, "idle_not_confirmed",
                            "prepare requires confirm_idle=True")
        git = _git_state(root)
        if git["dirty"] and not allow_dirty:
            return _blocked(record, "dirty_worktree",
                            "the worktree is dirty; pass allow_dirty=True")
        facts = _project_facts(root, identity, git=git)
        try:
            snapshot = _build_snapshot(
                identity, owner, target_ide, facts, next_action,
                facts.get("handoff_refs") or [])
        except HandoffError as exc:
            return _blocked(record, "snapshot_invalid", str(exc))
        snapshot_digest = snapshot["digest"]
        snapshot_text = json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
        if len(snapshot_text.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            return _blocked(record, "snapshot_too_large",
                            "handoff snapshot exceeds the 64 KiB limit")
        material = "|".join((
            identity["project_id"], owner_token, str(fence), target_ide,
            snapshot_digest,
        ))
        handoff_id = "ho_" + hashlib.sha256(
            material.encode("utf-8")).hexdigest()[:24]
        snapshot_path = _snapshot_path(root, handoff_id)
        relative = _relative(root, snapshot_path)
        updated = {
            **record,
            "state": "handoff_prepared",
            "fence": fence + 1,
            "handoff": {
                "handoff_id": handoff_id,
                "digest": snapshot_digest,
                "path": relative,
                "source_fence": fence,
                "target_ide": target_ide,
            },
            "last_transition": {
                "op": "prepare_handoff",
                "action": "prepare_handoff",
                "at": time.time(),
                "by": owner.get("owner_id"),
                "actor": owner.get("owner_id"),
                "fence": fence + 1,
                "from_state": record["state"],
                "to_state": "handoff_prepared",
                "from_fence": fence,
                "to_fence": fence + 1,
                "target_ide": target_ide,
                "handoff_id": handoff_id,
                "dirty": bool(git["dirty"]),
            },
            "native_cancel_ack": False,
            "stop_ack": False,
        }
        # Validate the record before publishing either artifact.
        _validate_record(updated)
        existed = snapshot_path.exists()
        try:
            _write_snapshot(root, handoff_id, snapshot)
        except HandoffError as exc:
            return _blocked(record, "snapshot_write_failed", str(exc))
        try:
            _write_ownership(root, updated)
        except Exception:
            if not existed:
                try:
                    snapshot_path.unlink()
                except OSError:
                    pass
            raise
        result = _result(updated, ok=True, changed=True, extra={
            "handoff_path": relative,
            "snapshot_digest": snapshot_digest,
            "dirty": bool(git["dirty"]),
        })
        result["ownership_path"] = str(_ownership_path(root))
        return result


def release(root, owner_token, fence, handoff_id=None) -> dict:
    root = _root(root)
    owner_token = _validate_token(owner_token)
    fence = _validate_fence(fence)
    if handoff_id is not None:
        handoff_id = _validate_handoff_id(handoff_id)
    _check_state_dir(root)
    with project_lock(root, timeout=10.0):
        identity = _identity_mutation(root)
        record = _current_record(root, identity)
        if not _project_matches(record, identity):
            return _blocked(record, "wrong_project",
                            "ownership record belongs to another project")
        owner = record.get("owner")
        previous = record.get("previous_owner")
        transition = record.get("last_transition") or {}
        current_handoff = record.get("handoff")
        current_handoff_id = (current_handoff.get("handoff_id")
                              if isinstance(current_handoff, dict) else None)
        if (record["state"] == "released"
                and isinstance(previous, dict)
                and previous.get("token") == owner_token
                and transition.get("op") == "release"
                and transition.get("from_fence") == fence
                and (handoff_id is None or current_handoff_id == handoff_id)):
            return _result(record, ok=True, unchanged=True)
        if (not isinstance(owner, dict) or owner.get("token") != owner_token
                or record["fence"] != fence):
            return _conflict(record, "stale_fence_or_token",
                             "owner token and fence do not match")
        if record["state"] not in ("active", "transferred", "handoff_prepared"):
            return _conflict(record, "state_not_releasable",
                             "ownership cannot be released in this state",
                             current_state=record["state"])
        if handoff_id is not None and current_handoff_id != handoff_id:
            return _conflict(record, "handoff_mismatch",
                             "handoff id does not match the prepared handoff")
        live = _liveness(root, record, mutation=True)
        if (live["leases"]["held"]
                and live["owner"]["status"] != "alive"):
            return _blocked(record, "lease_held",
                            "an unrelated managed writer lease is active")
        now = time.time()
        updated = {
            **record,
            "state": "released",
            "fence": fence + 1,
            "owner": None,
            "previous_owner": _previous_owner(owner, fence),
            "last_transition": {
                "op": "release",
                "action": "release",
                "at": now,
                "by": owner.get("owner_id"),
                "actor": owner.get("owner_id"),
                "fence": fence + 1,
                "from_state": record["state"],
                "to_state": "released",
                "from_fence": fence,
                "to_fence": fence + 1,
                "handoff_id": current_handoff_id,
            },
            "native_cancel_ack": False,
            "stop_ack": False,
        }
        _write_ownership(root, updated)
        result = _result(updated, ok=True, changed=True)
        result["ownership_path"] = str(_ownership_path(root))
        return result


def _accept_mismatch_reasons(snapshot: dict, identity, git: dict,
                             contract: dict, memory: dict,
                             record: dict) -> list[dict]:
    reasons = []
    if identity is None or not _project_matches(snapshot, identity):
        reasons.append(_reason("project_mismatch",
                               "project identity changed since the handoff"))
    if snapshot["branch"] != git["branch"]:
        reasons.append(_reason("branch_mismatch",
                               "git branch changed since the handoff",
                               expected=snapshot["branch"],
                               actual=git["branch"]))
    expected_head = snapshot.get("head_sha") or snapshot.get("head")
    if expected_head != git["head"]:
        reasons.append(_reason("head_mismatch",
                               "git HEAD changed since the handoff",
                               expected=expected_head, actual=git["head"]))
    if snapshot["dirty_digest"] != git["dirty_digest"]:
        reasons.append(_reason("dirty_digest_mismatch",
                               "worktree changes since the handoff",
                               expected=snapshot["dirty_digest"],
                               actual=git["dirty_digest"]))
    expected_contract = (
        snapshot["contract_id"], snapshot["contract_version"],
        snapshot.get("sha256") or snapshot.get("contract_sha256"))
    actual_contract = (
        contract.get("contract_id"), contract.get("contract_version"),
        contract.get("contract_sha256"))
    if expected_contract != actual_contract:
        reasons.append(_reason("contract_mismatch",
                               "requirements contract changed since the handoff"))
    elif not contract.get("valid", True):
        reasons.append(_reason("contract_integrity",
                               "requirements contract is no longer valid"))
    expected_interview = _interview_digest(snapshot)
    actual_interview = _interview_digest(memory)
    if expected_interview != actual_interview:
        reasons.append(_reason("interview_mismatch",
                               "interview questions changed since the handoff"))
    handoff = record.get("handoff") or {}
    source_fence = handoff.get("source_fence")
    transition = record.get("last_transition") or {}
    if record["state"] == "handoff_prepared":
        valid_generation = record["fence"] == source_fence + 1
    elif record["state"] == "released":
        valid_generation = (
            record["fence"] >= source_fence + 2
            and transition.get("to_fence") == record["fence"]
            and transition.get("op") in ("release", "recover_apply"))
    else:
        valid_generation = False
    if not valid_generation:
        reasons.append(_reason("ownership_generation_mismatch",
                               "ownership generation does not match the "
                               "handoff fence",
                               expected=None if source_fence is None
                               else source_fence + 1,
                               actual=record["fence"]))
    return reasons


def accept(root, handoff_id, digest, ide, session_id=None) -> dict:
    root = _root(root)
    handoff_id = _validate_handoff_id(handoff_id)
    digest = _validate_digest(digest)
    ide = _validate_ide(ide)
    session_id = _validate_session(session_id)
    identity_now = _identity_readonly(root)
    if identity_now is None:
        return _blocked(
            _new_record(None, None), "project_identity_missing",
            "project identity is missing or invalid")
    _check_state_dir(root)
    with project_lock(root, timeout=10.0):
        identity = _identity_mutation(root)
        record = _current_record(root, identity)
        if not _project_matches(record, identity):
            return _blocked(record, "wrong_project",
                            "ownership record belongs to another project")
        owner = record.get("owner")
        current_handoff = record.get("handoff")
        current_handoff_id = (current_handoff.get("handoff_id")
                              if isinstance(current_handoff, dict) else None)
        if record["state"] == "transferred":
            same = (current_handoff_id == handoff_id
                    and isinstance(current_handoff, dict)
                    and current_handoff.get("digest") == digest
                    and isinstance(owner, dict)
                    and owner.get("ide") == ide
                    and (session_id is None
                         or owner.get("session_id") == session_id))
            if same:
                return _result(record, ok=True, unchanged=True)
            return _conflict(record, "already_transferred",
                             "ownership was already transferred")
        if record["state"] not in ACCEPT_STATES:
            return _blocked(record, "accept_not_allowed",
                            "handoff cannot be accepted in this state",
                            current_state=record["state"])
        if not isinstance(current_handoff, dict):
            return _blocked(record, "handoff_missing",
                            "no prepared handoff is recorded")
        if (current_handoff_id != handoff_id
                or current_handoff.get("digest") != digest):
            return _blocked(record, "handoff_mismatch",
                            "handoff id or digest does not match ownership")
        try:
            snapshot = _load_snapshot(root, handoff_id)
        except HandoffError as exc:
            return _blocked(record, "snapshot_rejected", str(exc))
        if snapshot["digest"] != digest:
            return _blocked(record, "snapshot_digest_mismatch",
                            "snapshot digest does not match the requested digest")
        if (snapshot["project_id"] != identity["project_id"]
                or snapshot["project_uuid"].lower()
                != identity["project_uuid"].lower()):
            return _blocked(record, "wrong_project",
                            "handoff snapshot belongs to another project")
        if snapshot["target_ide"] != ide:
            return _blocked(record, "target_ide_mismatch",
                            "handoff target does not match the accepting IDE")
        expected_path = _relative(root, _snapshot_path(root, handoff_id))
        if current_handoff.get("path") != expected_path:
            return _blocked(record, "handoff_path_mismatch",
                            "handoff path does not match its id")
        live = _liveness(root, record, mutation=True)
        if live["leases"]["held"]:
            return _blocked(record, "lease_held",
                            "a managed writer or runner lease is active")
        if live["owner"]["status"] == "unknown" or live["unknown_live"]:
            return _blocked(record, "liveness_unknown",
                            "source owner liveness cannot be determined",
                            liveness=live["owner"])
        facts = _project_facts(root, identity)
        reasons = _accept_mismatch_reasons(
            snapshot, identity, facts["git"], facts["contract"],
            facts["memory"], record)
        if reasons:
            return _result(record, ok=False, status="blocked",
                           reasons=reasons)
        now = time.time()
        ttl = DEFAULT_TTL_SECONDS
        if isinstance(owner, dict) and isinstance(owner.get("ttl_seconds"), int):
            ttl = owner["ttl_seconds"]
        elif isinstance(record.get("previous_owner"), dict):
            previous_ttl = record["previous_owner"].get("ttl_seconds")
            if isinstance(previous_ttl, int):
                ttl = previous_ttl
        new_owner = {
            "owner_id": _validate_owner_id(None),
            "ide": ide,
            "session_id": session_id,
            "pid_identity": None,
            "host": socket.gethostname(),
            "activated_at": now,
            "heartbeat_at": now,
            "ttl_seconds": ttl,
            "token": secrets.token_hex(24),
        }
        _validate_owner(new_owner)
        previous_owner = owner if isinstance(owner, dict) else record.get(
            "previous_owner")
        updated = {
            **record,
            "state": "transferred",
            "fence": record["fence"] + 1,
            "owner": new_owner,
            "previous_owner": _previous_owner(previous_owner, record["fence"]),
            "last_transition": {
                "op": "accept",
                "action": "accept",
                "at": now,
                "by": new_owner["owner_id"],
                "actor": new_owner["owner_id"],
                "fence": record["fence"] + 1,
                "from_state": record["state"],
                "to_state": "transferred",
                "from_fence": record["fence"],
                "to_fence": record["fence"] + 1,
                "handoff_id": handoff_id,
                "target_ide": ide,
            },
            "native_cancel_ack": False,
            "stop_ack": False,
        }
        _write_ownership(root, updated)
        result = _result(updated, ok=True, changed=True,
                         owner_token=new_owner["token"])
        result["ownership_path"] = str(_ownership_path(root))
        return result


def _normalise_evidence(evidence):
    if evidence is None:
        return None
    try:
        encoded = json.dumps(evidence, sort_keys=True, ensure_ascii=False,
                             default=str)
    except (TypeError, ValueError) as exc:
        raise HandoffError("evidence is not serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise HandoffError("evidence is too large")
    if isinstance(evidence, str):
        text = _safe_text(evidence, 500)
        if not text:
            return None
        refs = [text]
    elif isinstance(evidence, list):
        refs = []
        for item in evidence:
            if isinstance(item, str):
                text = _safe_text(item, 500)
                if text:
                    refs.append(text)
            elif isinstance(item, dict):
                for key in ("ref", "refs", "kind", "path"):
                    value = item.get(key)
                    if isinstance(value, str):
                        text = _safe_text(value, 500)
                        if text:
                            refs.append(text)
                    elif isinstance(value, list):
                        refs.extend(_safe_string_list(
                            value, "evidence refs", item_limit=500))
        refs = list(dict.fromkeys(refs))[:20]
        if not refs:
            return None
    elif isinstance(evidence, dict):
        refs = []
        for key in ("ref", "refs", "evidence_refs", "kind", "path"):
            item = evidence.get(key)
            if isinstance(item, str):
                text = _safe_text(item, 500)
                if text:
                    refs.append(text)
            elif isinstance(item, list):
                refs.extend(_safe_string_list(
                    item, "evidence refs", item_limit=500))
        if not refs:
            meaningful = any(
                value not in (None, "", [], {}) for value in evidence.values())
            if not meaningful:
                return None
            refs = ["evidence_sha256:" + hashlib.sha256(
                encoded.encode("utf-8")).hexdigest()[:16]]
    else:
        text = _safe_text(str(evidence), 500)
        if not text:
            return None
        refs = [text]
    return {
        "refs": list(dict.fromkeys(refs))[:20],
        "digest": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def recover(root, actor, reason, evidence=None, apply=False,
            discard_handoff=False) -> dict:
    root = _root(root)
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise HandoffError("actor is invalid")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise HandoffError("reason is invalid")
    if not isinstance(discard_handoff, bool):
        raise HandoffError("discard_handoff must be a boolean")
    actor = _safe_text(actor, 200)
    reason = _safe_text(reason, 1000)
    evidence_value = _normalise_evidence(evidence)
    _check_state_dir(root)
    with project_lock(root, timeout=10.0):
        identity = _identity_mutation(root)
        record = _current_record(root, identity)
        if not _project_matches(record, identity):
            return _blocked(record, "wrong_project",
                            "ownership record belongs to another project")
        state = record["state"]
        if state == "inactive":
            return _result(record, ok=True, unchanged=True,
                           status="inactive")
        if state == "released" and not isinstance(record.get("owner"), dict):
            if not apply:
                return _result(record, ok=True, unchanged=True)
            if evidence_value is None:
                raise HandoffError(
                    "explicit recovery evidence is required for apply")
            if record.get("handoff") is None:
                return _result(record, ok=True, unchanged=True)
            transition = record.get("last_transition") or {}
            if (transition.get("op") == "recover_apply"
                    and not discard_handoff):
                return _result(record, ok=True, unchanged=True)
            live = _liveness(root, record, mutation=True)
            if live["leases"]["held"]:
                return _blocked(record, "lease_held",
                                "a managed writer or runner lease is active")
            now = time.time()
            updated = {
                **record,
                "state": "inactive",
                "fence": record["fence"] + 1,
                "owner": None,
                "handoff": None,
                "previous_owner": record.get("previous_owner"),
                "last_transition": {
                    "op": "recover_apply",
                    "action": "recover_apply",
                    "at": now,
                    "actor": actor,
                    "reason": reason,
                    "fence": record["fence"] + 1,
                    "from_state": "released",
                    "to_state": "inactive",
                    "from_fence": record["fence"],
                    "to_fence": record["fence"] + 1,
                    "evidence": evidence_value,
                    "discarded_handoff_id": (
                        record["handoff"].get("handoff_id")
                        if isinstance(record.get("handoff"), dict) else None),
                    "liveness": "unknown",
                    "stop_ack": False,
                    "native_cancel_ack": False,
                },
                "native_cancel_ack": False,
                "stop_ack": False,
            }
            _write_ownership(root, updated)
            result = _result(updated, ok=True, changed=True)
            result["ownership_path"] = str(_ownership_path(root))
            return result
        live = _liveness(root, record, mutation=True)
        if apply:
            if evidence_value is None:
                raise HandoffError(
                    "explicit recovery evidence is required for apply")
            if live["owner"]["status"] == "alive" and state != "recovery_required":
                return _conflict(record, "owner_alive",
                                 "owner is alive; recovery is not required")
            now = time.time()
            discarded = record.get("handoff")
            new_state = ("inactive" if discard_handoff
                         else ("released" if discarded else "inactive"))
            previous_owner = record.get("owner") or record.get("previous_owner")
            updated = {
                **record,
                "state": new_state,
                "fence": record["fence"] + 1,
                "owner": None,
                "handoff": None if discard_handoff else record.get("handoff"),
                "previous_owner": _previous_owner(previous_owner, record["fence"]),
                "last_transition": {
                    "op": "recover_apply",
                    "action": "recover_apply",
                    "at": now,
                    "actor": actor,
                    "reason": reason,
                    "fence": record["fence"] + 1,
                    "from_state": state,
                    "to_state": new_state,
                    "from_fence": record["fence"],
                    "to_fence": record["fence"] + 1,
                    "evidence": evidence_value,
                    "discarded_handoff_id": (
                        discarded.get("handoff_id")
                        if discard_handoff and isinstance(discarded, dict)
                        else None),
                    "liveness": "unknown",
                    "stop_ack": False,
                    "native_cancel_ack": False,
                },
                "native_cancel_ack": False,
                "stop_ack": False,
            }
            _write_ownership(root, updated)
            result = _result(updated, ok=True, changed=True)
            result["ownership_path"] = str(_ownership_path(root))
            return result
        if state == "recovery_required":
            return _result(record, ok=True, unchanged=True,
                           status="recovery_required")
        if live["owner"]["status"] == "alive":
            return _conflict(record, "owner_alive",
                             "owner is alive; recovery is not required")
        now = time.time()
        transition = {
            "op": "recover_required",
            "action": "recover_required",
            "at": now,
            "actor": actor,
            "reason": reason,
            "fence": record["fence"] + 1,
            "from_state": state,
            "to_state": "recovery_required",
            "from_fence": record["fence"],
            "to_fence": record["fence"] + 1,
            "liveness": live["owner"]["status"],
            "stop_ack": False,
            "native_cancel_ack": False,
        }
        if evidence_value is not None:
            transition["evidence"] = evidence_value
        updated = {
            **record,
            "state": "recovery_required",
            "fence": record["fence"] + 1,
            "last_transition": transition,
            "native_cancel_ack": False,
            "stop_ack": False,
        }
        _write_ownership(root, updated)
        result = _result(updated, ok=True, changed=True)
        result["ownership_path"] = str(_ownership_path(root))
        return result
