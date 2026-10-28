"""Tamper-evident receipts for native verification.

`native-verify` is the only command that issues a receipt. A dispatch cannot be
promoted by pointing at an arbitrary project JSON file; it must present a
`verification_run_id` whose receipt exists in the protected registry, matches
the same project/run/task/commit/contract/plan, and carries a valid HMAC.

Trust boundary: the key lives in `.auto-orchestrator/ide/verification.key`
(mode 0600). This protects against accidental or file-only forgery by an agent
that cannot execute code in this project. A same-user process that can read the
key and import this module can forge a receipt; that is an OS trust boundary,
not a cryptographic one, and is documented rather than hidden.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

try:
    from tools.run_owner import atomic_write_json
except ModuleNotFoundError:  # pragma: no cover
    from run_owner import atomic_write_json  # type: ignore

RECEIPT_ID = re.compile(r"^vr_[0-9a-f]{16}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_BYTES = 64 * 1024
KEY_BYTES = 32


class ReceiptError(ValueError):
    pass


def _root(value) -> Path:
    root = Path(value)
    if not root.is_dir():
        raise ReceiptError("project root must be a directory")
    return root


def _key_path(root: Path) -> Path:
    return root / ".auto-orchestrator" / "ide" / "verification.key"


def _receipt_path(root: Path, verification_run_id: str) -> Path:
    if not isinstance(verification_run_id, str) or not RECEIPT_ID.fullmatch(
            verification_run_id):
        raise ReceiptError("verification_run_id is invalid")
    return (root / ".auto-orchestrator" / "verification" / "receipts" /
            f"{verification_run_id}.json")


def _read_key(root: Path, *, create: bool) -> bytes:
    path = _key_path(root)
    if path.is_symlink():
        raise ReceiptError("verification key must not be a symlink")
    if path.is_file():
        try:
            if path.stat().st_size > 256:
                raise ReceiptError("verification key is invalid")
            value = path.read_bytes().strip()
        except OSError as exc:
            raise ReceiptError("verification key is unreadable") from exc
        if len(value) != KEY_BYTES * 2:
            raise ReceiptError("verification key is invalid")
        try:
            return bytes.fromhex(value.decode("ascii"))
        except (UnicodeError, ValueError) as exc:
            raise ReceiptError("verification key is invalid") from exc
    if not create:
        raise ReceiptError("verification key is missing")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = secrets.token_bytes(KEY_BYTES)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key.hex().encode("ascii") + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
    except FileExistsError:
        return _read_key(root, create=False)
    except OSError as exc:
        raise ReceiptError("verification key could not be created") from exc
    return key


def _canonical(payload) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _mac(key: bytes, payload) -> str:
    return hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()


def issue(root, payload: dict) -> Path:
    """Issue a receipt for a completed native-verify run."""
    root = _root(root)
    if not isinstance(payload, dict):
        raise ReceiptError("receipt payload must be an object")
    verification_run_id = payload.get("verification_run_id")
    path = _receipt_path(root, verification_run_id)
    if path.exists() or path.is_symlink():
        raise ReceiptError("verification receipt already exists")
    safe = {}
    for key in ("verification_run_id", "project_id", "run_id", "task_id",
                "dispatch_id", "commit", "contract_sha256", "plan_sha256",
                "verified", "exit_code", "test_results_sha256",
                "test_output_sha256", "dispatch_artifact_sha256",
                "dispatch_artifact_size", "verification_record_sha256",
                "record_ref", "issued_at"):
        value = payload.get(key)
        if value is not None:
            safe[key] = value
    key = _read_key(root, create=True)
    receipt = {
        "version": 1,
        "kind": "native-verification-receipt",
        "payload": safe,
        "mac": _mac(key, safe),
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, receipt)
    return path


def read(root, verification_run_id: str) -> dict:
    root = _root(root)
    path = _receipt_path(root, verification_run_id)
    if path.is_symlink() or not path.is_file():
        raise ReceiptError("verification receipt is missing")
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ReceiptError("verification receipt is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ReceiptError:
        raise
    except (OSError, ValueError) as exc:
        raise ReceiptError("verification receipt is invalid") from exc
    if (not isinstance(value, dict) or value.get("kind")
            != "native-verification-receipt"):
        raise ReceiptError("verification receipt is invalid")
    payload = value.get("payload")
    mac = value.get("mac")
    if not isinstance(payload, dict) or not isinstance(mac, str):
        raise ReceiptError("verification receipt is invalid")
    key = _read_key(root, create=False)
    if not hmac.compare_digest(mac, _mac(key, payload)):
        raise ReceiptError("verification receipt MAC does not match")
    if payload.get("verification_run_id") != verification_run_id:
        raise ReceiptError("verification receipt id does not match")
    return payload


def require(root, verification_run_id: str, *, run_id, task_id, commit,
            contract_sha256=None, plan_sha256=None, project_id=None,
            dispatch_id=None, dispatch_artifact_sha256=None,
            dispatch_artifact_size=None) -> dict:
    payload = read(root, verification_run_id)
    if project_id and payload.get("project_id") != project_id:
        raise ReceiptError("verification receipt belongs to another project")
    if payload.get("run_id") != run_id or payload.get("task_id") != task_id:
        raise ReceiptError("verification receipt belongs to another run/task")
    if dispatch_id is not None:
        if payload.get("dispatch_id") != dispatch_id:
            raise ReceiptError(
                "verification receipt belongs to another dispatch")
        if payload.get("dispatch_artifact_sha256") != dispatch_artifact_sha256:
            raise ReceiptError(
                "verification receipt belongs to another artifact")
        if payload.get("dispatch_artifact_size") != dispatch_artifact_size:
            raise ReceiptError(
                "verification receipt artifact size differs")
    if payload.get("commit") != commit:
        raise ReceiptError("verification receipt is stale for this commit")
    if contract_sha256 and payload.get("contract_sha256") != contract_sha256:
        raise ReceiptError("verification receipt contract digest differs")
    if plan_sha256 and payload.get("plan_sha256") != plan_sha256:
        raise ReceiptError("verification receipt plan digest differs")
    if payload.get("verified") is not True:
        raise ReceiptError("verification receipt is not verified")
    if payload.get("exit_code") != 0:
        raise ReceiptError("verification receipt has a non-zero exit code")
    for field in ("test_results_sha256", "test_output_sha256",
                  "verification_record_sha256"):
        value = payload.get(field)
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            raise ReceiptError(f"verification receipt {field} is invalid")
    if dispatch_id is not None:
        size = payload.get("dispatch_artifact_size")
        digest = payload.get("dispatch_artifact_sha256")
        if (type(size) is not int or size < 0
                or not isinstance(digest, str) or not SHA256.fullmatch(digest)):
            raise ReceiptError(
                "verification receipt dispatch artifact binding is invalid")
    record_ref = payload.get("record_ref")
    if record_ref is not None:
        if (not isinstance(record_ref, str) or not record_ref
                or record_ref.startswith("/") or ".." in record_ref.split("/")):
            raise ReceiptError("verification receipt record_ref is invalid")
    return payload


def consume(root, verification_run_id: str, dispatch_id: str) -> bool:
    """Atomically mark a receipt consumed by one dispatch.

    Returns False for an idempotent replay by the same dispatch. A different
    dispatch is rejected even if it somehow presents the same receipt.
    """
    root = _root(root)
    if not isinstance(dispatch_id, str) or not dispatch_id.startswith("nd_"):
        raise ReceiptError("dispatch_id is invalid")
    receipt = _receipt_path(root, verification_run_id)
    marker = receipt.with_suffix(".used")
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if marker.is_symlink():
        raise ReceiptError("receipt consumption marker is a symlink")
    if marker.exists():
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReceiptError("receipt consumption marker is invalid") from exc
        if isinstance(value, dict) and value.get("dispatch_id") == dispatch_id:
            return False
        raise ReceiptError("verification receipt was consumed by another dispatch")
    try:
        fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, json.dumps({
                "verification_run_id": verification_run_id,
                "dispatch_id": dispatch_id,
                "consumed_at": time.time(),
            }).encode("utf-8") + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
    except FileExistsError:
        value = json.loads(marker.read_text(encoding="utf-8"))
        if value.get("dispatch_id") == dispatch_id:
            return False
        raise ReceiptError("verification receipt was consumed by another dispatch")
    return True
