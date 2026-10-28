"""V8.0 typed native dispatch and agent lifecycle records.

The engine does not spawn a native agent. The owning IDE's parent session does
that through its own native tools. This module records the dispatch contract,
the lifecycle events the parent can actually observe, and the evidence needed
before any task may be considered complete.

A dispatch never enters ``running`` without a native child reference, and a
parent-supplied reference is recorded as ``child_provenance="declared"`` unless
a trusted adapter observes the lifecycle. Missing lifecycle evidence ends in
``unknown``/``recovery_required``, not in a fabricated ``completed`` state.

Python access to this module is an internal API, not an authorization
boundary. The CLI calls ``ide_handoff`` ownership checks before every mutation;
embedding code must do the same.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

try:
    from tools.run_owner import atomic_write_json, project_lock, read_json
except ModuleNotFoundError:  # pragma: no cover - flat installation
    from run_owner import atomic_write_json, project_lock, read_json  # type: ignore

VERSION = 1
ROLES = frozenset({
    "coordinator", "planner", "explorer", "implementer", "tester", "reviewer",
    "critic", "challenger", "auditor", "success-auditor",
})
IDES = frozenset({"codex", "opencode", "cursor", "antigravity"})
STATES = (
    "queued", "dispatch_requested", "dispatch_confirmed", "running",
    "waiting_for_permission", "completed_claimed", "output_collected",
    "verification_pending", "verified", "failed", "cancelled", "unknown",
    "recovery_required",
)
ACTIVE_STATES = frozenset({
    "queued", "dispatch_requested", "dispatch_confirmed", "running",
    "waiting_for_permission", "completed_claimed", "output_collected",
    "verification_pending", "recovery_required",
})
TERMINAL_STATES = frozenset({"verified", "failed", "cancelled"})
MAX_SCOPE = 40
MAX_TEXT = 500
MAX_ACTIVE = 3


class DispatchError(ValueError):
    pass


def _root(value) -> Path:
    root = Path(value)
    if not root.is_dir():
        raise DispatchError("project root must be a directory")
    return root


def _dir(root: Path, run_id: str) -> Path:
    normalized = _text(run_id, "run_id", 100)
    if "/" in normalized or "\\" in normalized or normalized in (".", ".."):
        raise DispatchError("run_id is invalid")
    return root / ".auto-orchestrator" / "native-dispatches" / normalized


def _text(value, field, limit=MAX_TEXT, *, required=True):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise DispatchError(f"{field} must be a non-empty string")
    if len(value) > limit or "\x00" in value:
        raise DispatchError(f"{field} is invalid")
    return value.strip()


def _scopes(value, field):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_SCOPE:
        raise DispatchError(f"{field} must be a bounded list")
    result = []
    for item in value:
        item = _text(item, field, 300)
        unified = item.replace("\\", "/")
        if (unified.startswith("/") or unified.startswith("//")
                or item.startswith("\\\\")
                or (len(unified) >= 2 and unified[1] == ":")
                or unified.startswith("~")):
            raise DispatchError(f"{field} must be workspace-relative")
        normalized = unified.strip("/")
        if (not normalized or ".." in normalized.split("/")):
            raise DispatchError(f"{field} contains an unsafe path")
        if normalized not in result:
            result.append(normalized)
    return result


def _overlap(left, right):
    for a in left:
        for b in right:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False


def _dispatch_path(root: Path, run_id: str, dispatch_id: str) -> Path:
    if not dispatch_id.startswith("nd_") or len(dispatch_id) > 40:
        raise DispatchError("dispatch id is invalid")
    return _dir(root, run_id) / f"{dispatch_id}.json"


def _load(root: Path, run_id: str, dispatch_id: str) -> dict:
    path = _dispatch_path(root, run_id, dispatch_id)
    if path.is_symlink() or not path.is_file():
        raise DispatchError("dispatch does not exist")
    try:
        value = read_json(path, {})
    except (OSError, ValueError) as exc:
        raise DispatchError("dispatch record is unreadable") from exc
    if not isinstance(value, dict):
        raise DispatchError("dispatch record is invalid")
    return value


def _history(record, action, **fields):
    entry = {"action": action, "at": time.time(),
             "from": record.get("state"), "to": fields.pop("to_state",
                                                          record.get("state"))}
    entry.update({key: item for key, item in fields.items()
                  if item is not None})
    record["history"] = (list(record.get("history") or []) + [entry])[-100:]
    return record


def _write(root: Path, record: dict) -> dict:
    path = _dispatch_path(root, record["run_id"], record["dispatch_id"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    record["updated_at"] = time.time()
    atomic_write_json(path, record)
    return record


def _public(record: dict) -> dict:
    value = dict(record)
    value["history"] = list(record.get("history") or [])[-20:]
    return value


def list_dispatches(root, run_id=None) -> list[dict]:
    root = _root(root)
    base = root / ".auto-orchestrator" / "native-dispatches"
    if base.is_symlink() or not base.is_dir():
        return []
    runs = [run_id] if run_id else [
        path.name for path in base.iterdir() if path.is_dir()]
    result = []
    for run in runs:
        directory = base / run
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            value = read_json(path, {})
            if isinstance(value, dict):
                result.append(value)
    return result



def _head(root: Path):
    import subprocess
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _is_ancestor(root: Path, ancestor, descendant) -> bool:
    if not ancestor or not descendant:
        return True
    if ancestor == descendant:
        return True
    import subprocess
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor",
             ancestor, descendant], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def create(root, *, run_id, task_id, role, ide, owner_session_id,
           read_scope=None, write_scope=None, dependencies=None,
           sandbox_policy=None, permission_policy=None,
           parent_agent_id=None, commit=None, contract_sha256=None,
           plan_sha256=None) -> dict:
    root = _root(root)
    run_id = _text(run_id, "run_id", 100)
    task_id = _text(task_id, "task_id", 100)
    role = _text(role, "role", 40)
    ide = _text(ide, "ide", 40)
    if role not in ROLES:
        raise DispatchError("role is not supported")
    if ide not in IDES:
        raise DispatchError("ide is not supported")
    owner = _text(owner_session_id, "owner_session_id", 120)
    try:
        from tools.project_identity import ensure_project_identity
    except ModuleNotFoundError:
        from project_identity import ensure_project_identity  # type: ignore
    identity = ensure_project_identity(root)
    if commit is None:
        try:
            import subprocess
            resolved = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10)
            commit = resolved.stdout.strip() if resolved.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            commit = None
    for label, value in (("commit", commit),
                         ("contract_sha256", contract_sha256),
                         ("plan_sha256", plan_sha256)):
        if value is not None and (not isinstance(value, str)
                                  or len(value) > 80):
            raise DispatchError(f"{label} is invalid")
    read_scope = _scopes(read_scope, "read_scope")
    write_scope = _scopes(write_scope, "write_scope")
    dependencies = _scopes(dependencies, "dependencies")
    with project_lock(root, timeout=10.0):
        existing = list_dispatches(root, run_id)
        active = [item for item in existing if item.get("state") in ACTIVE_STATES]
        if any(item.get("task_id") == task_id and item.get("state") in ACTIVE_STATES
               for item in existing):
            raise DispatchError("an active dispatch already exists for this task")
        if len(active) >= MAX_ACTIVE:
            raise DispatchError("native dispatch concurrency limit reached")
        for item in active:
            if item.get("state") in ("failed", "cancelled", "unknown"):
                continue
            if _overlap(write_scope, item.get("write_scope") or []):
                raise DispatchError(
                    "write scope overlaps another active dispatch")
        for dependency in dependencies:
            matches = [item for item in existing
                       if item.get("task_id") == dependency]
            if not matches:
                raise DispatchError(
                    f"dependency {dependency} has no dispatch record")
            latest = max(matches, key=lambda item: item.get("created_at") or 0)
            if latest.get("state") != "verified":
                raise DispatchError(
                    f"dependency {dependency} is not verified")
        dispatch_id = "nd_" + secrets.token_hex(8)
        now = time.time()
        record = {
            "version": VERSION,
            "dispatch_id": dispatch_id,
            "project_id": identity["project_id"],
            "commit": commit,
            "contract_sha256": contract_sha256,
            "plan_sha256": plan_sha256,
            "run_id": run_id,
            "task_id": task_id,
            "role": role,
            "native_ide": ide,
            "owner_session_id": owner,
            "parent_agent_id": parent_agent_id,
            "child_agent_id": None,
            "native_child_ref": None,
            "child_provenance": None,
            "native_lifecycle_observed": False,
            "read_scope": read_scope,
            "write_scope": write_scope,
            "dependencies": dependencies,
            "sandbox_policy": sandbox_policy or "native-parent",
            "permission_policy": permission_policy or "native-parent",
            "state": "queued",
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "lifecycle_status": "queued",
            "result_ref": None,
            "evidence_ref": None,
            "verification_run_id": None,
            "retry_count": 0,
            "exit_reason": None,
            "history": [],
        }
        _history(record, "create", to_state="queued")
        return _public(_write(root, record))


def _transition(root, dispatch_id, *, expected, to_state, action, **fields):
    root = _root(root)
    run_id = fields.pop("run_id")
    with project_lock(root, timeout=10.0):
        record = _load(root, run_id, dispatch_id)
        if record.get("state") not in expected:
            raise DispatchError(
                f"dispatch state {record.get('state')} does not allow {action}")
        record.update({key: value for key, value in fields.items()
                       if value is not None})
        record["state"] = to_state
        record["lifecycle_status"] = to_state
        _history(record, action, to_state=to_state,
                 **{key: value for key, value in fields.items()
                    if key in ("native_child_ref", "result_ref",
                               "evidence_ref", "verification_run_id",
                               "exit_reason")})
        return _public(_write(root, record))


def request(root, dispatch_id, *, run_id) -> dict:
    return _transition(root, dispatch_id, run_id=run_id,
                       expected={"queued"}, to_state="dispatch_requested",
                       action="request")


def confirm(root, dispatch_id, *, run_id, native_child_ref=None) -> dict:
    ref = _text(native_child_ref, "native_child_ref", 200, required=False)
    root = _root(root)
    with project_lock(root, timeout=10.0):
        record = _load(root, run_id, dispatch_id)
        if record.get("state") == "dispatch_confirmed" and ref:
            record["native_child_ref"] = ref
            _history(record, "attach_child",
                     to_state="dispatch_confirmed", native_child_ref=ref)
            return _public(_write(root, record))
        if record.get("state") != "dispatch_requested":
            raise DispatchError("dispatch is not requested")
        record["state"] = "dispatch_confirmed"
        record["lifecycle_status"] = "dispatch_confirmed"
        if ref:
            record["native_child_ref"] = ref
        _history(record, "confirm", to_state="dispatch_confirmed",
                 native_child_ref=ref or None)
        return _public(_write(root, record))


def start(root, dispatch_id, *, run_id, native_child_ref=None) -> dict:
    ref = _text(native_child_ref, "native_child_ref", 200, required=False)
    with project_lock(root, timeout=10.0):
        record = _load(root, run_id, dispatch_id)
        if record.get("state") != "dispatch_confirmed":
            raise DispatchError("dispatch is not confirmed")
        child = ref or record.get("native_child_ref")
        if not child:
            # No observed native child: stay confirmed, never fake running.
            raise DispatchError(
                "native child identity is required before running; a dispatch "
                "request is not a running child")
        record["started_at"] = time.time()
        record["state"] = "running"
        record["lifecycle_status"] = "running"
        record["native_child_ref"] = child
        # A reference supplied by the parent is a declaration, not proof of a
        # vendor lifecycle event. Adapters may raise this only with trusted
        # evidence; no such adapter is claimed here.
        record["child_provenance"] = "declared"
        record["native_lifecycle_observed"] = False
        _history(record, "start", to_state="running",
                 native_child_ref=child)
        return _public(_write(root, record))


def permission(root, dispatch_id, *, run_id, waiting=True) -> dict:
    if waiting:
        return _transition(root, dispatch_id, run_id=run_id,
                           expected={"running"},
                           to_state="waiting_for_permission",
                           action="permission_wait")
    return _transition(root, dispatch_id, run_id=run_id,
                       expected={"waiting_for_permission"},
                       to_state="running", action="permission_resume")


def complete(root, dispatch_id, *, run_id, result_ref) -> dict:
    ref = _text(result_ref, "result_ref", 300)
    return _transition(root, dispatch_id, run_id=run_id,
                       expected={"running", "waiting_for_permission"},
                       to_state="completed_claimed", action="complete_claim",
                       result_ref=ref)


def _workspace_path(root, base, relative):
    base = Path(base).resolve()
    if base != root and root not in base.parents:
        raise DispatchError("output directory escapes the workspace")
    candidate = base / relative
    if candidate.is_symlink():
        raise DispatchError("evidence artifact must not be a symlink")
    current = candidate
    while current != base and current.parent != current:
        if current.parent.is_symlink():
            raise DispatchError("evidence path contains a symlink")
        current = current.parent
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise DispatchError("evidence path escapes the workspace")
    if not resolved.is_file():
        raise DispatchError("evidence artifact is missing")
    return resolved


def _hash_path(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def collect(root, dispatch_id, *, run_id, evidence_ref, output_dir=None) -> dict:
    root = _root(root).resolve()
    ref = _text(evidence_ref, "evidence_ref", 300)
    base = Path(output_dir) if output_dir else root
    path = _workspace_path(root, base, ref)
    digest, size = _hash_path(path)
    with project_lock(root, timeout=10.0):
        record = _load(root, run_id, dispatch_id)
        if record.get("state") != "completed_claimed":
            raise DispatchError("output is not claimed complete")
        record["output_digest"] = digest
        record["output_size"] = size
        record["state"] = "output_collected"
        record["lifecycle_status"] = "output_collected"
        record["evidence_ref"] = ref
        _history(record, "collect_output", to_state="output_collected",
                 evidence_ref=ref, output_digest=digest)
        return _public(_write(root, record))


def verify(root, dispatch_id, *, run_id, verification_run_id) -> dict:
    root = _root(root).resolve()
    ref = _text(verification_run_id, "verification_run_id", 120)
    try:
        from tools import verification_receipt
    except ModuleNotFoundError:
        import verification_receipt  # type: ignore
    with project_lock(root, timeout=10.0):
        record = _load(root, run_id, dispatch_id)
        if record.get("state") not in ("output_collected",
                                       "verification_pending"):
            raise DispatchError("dispatch output is not collected")
        evidence_ref = record.get("evidence_ref")
        if not isinstance(evidence_ref, str) or not evidence_ref:
            raise DispatchError("dispatch has no collected artifact")
        path = _workspace_path(root, root, evidence_ref)
        digest, size = _hash_path(path)
        if digest != record.get("output_digest") or size != record.get("output_size"):
            raise DispatchError(
                "collected artifact changed after collection; re-collect it")
        current_head = _head(root)
        if not current_head:
            raise DispatchError("dispatch verification requires a git commit")
        if (record.get("commit")
                and not _is_ancestor(root, record.get("commit"), current_head)):
            raise DispatchError(
                "dispatch base commit is not an ancestor of HEAD")
        payload = verification_receipt.require(
            root, ref, run_id=run_id, task_id=record.get("task_id"),
            commit=current_head,
            contract_sha256=record.get("contract_sha256"),
            plan_sha256=record.get("plan_sha256"))
        record_ref = payload.get("record_ref")
        record_path = _workspace_path(root, root, record_ref)
        try:
            if record_path.stat().st_size > 262_144:
                raise DispatchError("verification record is too large")
            record_value = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DispatchError("verification record is unreadable") from exc
        record_digest = hashlib.sha256(json.dumps(
            record_value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()
        if record_digest != payload.get("record_sha256"):
            raise DispatchError(
                "verification record changed after the receipt was issued")
        record["verification_run_id"] = ref
        record["verification_receipt_id"] = ref
        record["verification_record_ref"] = record_ref
        record["verification_record_sha256"] = record_digest
        record["verified_commit"] = current_head
        record["state"] = "verified"
        record["lifecycle_status"] = "verified"
        record["completed_at"] = time.time()
        _history(record, "verify", to_state="verified",
                 verification_run_id=ref)
        return _public(_write(root, record))


def fail(root, dispatch_id, *, run_id, reason) -> dict:
    return _transition(root, dispatch_id, run_id=run_id,
                       expected=set(ACTIVE_STATES), to_state="failed",
                       action="fail", exit_reason=_text(reason, "reason", 500))


def cancel(root, dispatch_id, *, run_id, reason) -> dict:
    return _transition(root, dispatch_id, run_id=run_id,
                       expected=set(ACTIVE_STATES), to_state="cancelled",
                       action="cancel", exit_reason=_text(reason, "reason", 500))


def recover(root, dispatch_id, *, run_id, reason) -> dict:
    """Mark an unobservable lifecycle as unknown; never as completed."""
    with project_lock(root, timeout=10.0):
        record = _load(root, run_id, dispatch_id)
        if record.get("state") in TERMINAL_STATES:
            raise DispatchError("terminal dispatch cannot be recovered")
        if record.get("state") == "recovery_required":
            return _public(record)
        record["state"] = "recovery_required"
        record["lifecycle_status"] = "recovery_required"
        record["exit_reason"] = _text(reason, "reason", 500)
        record["stop_ack"] = False
        record["native_cancel_ack"] = False
        record["verification_run_id"] = None
        record["verification_receipt_id"] = None
        record["verification_record_ref"] = None
        _history(record, "recover", to_state="recovery_required",
                 exit_reason=record["exit_reason"], stop_ack=False)
        return _public(_write(root, record))


def status(root, *, run_id=None) -> dict:
    values = list_dispatches(root, run_id)
    counts = {}
    for item in values:
        state = str(item.get("state"))
        counts[state] = counts.get(state, 0) + 1
    return {
        "run_id": run_id,
        "total": len(values),
        "counts": counts,
        "active": [item for item in values
                   if item.get("state") in ACTIVE_STATES],
        "unknown": [item for item in values
                    if item.get("state") in ("unknown", "recovery_required")],
        "verified": [item for item in values if item.get("state") == "verified"],
        "dispatches": values,
        "note": ("A dispatch record is not proof that a native child ran. "
                 "Running requires an observed child reference."),
    }
