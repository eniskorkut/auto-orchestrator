#!/usr/bin/env python3
"""Process ownership and project-level locking for durable Teamwork runs.

The bridge and runner must never trust a bare PID. A PID can be recycled by an
unrelated process after a crash. Every owner record therefore includes a
provider-independent start token and, on POSIX, the owning UID. Signals are only
sent after the live process identity matches the recorded identity.

No native cancellation acknowledgement is produced by this module. It only
reports local process signals and whether the owned process exited.
"""
from __future__ import annotations

import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path


class OwnershipError(RuntimeError):
    pass


_SELF_IDENTITY = None
_SELF_LOCK = __import__("threading").Lock()


def _posix_identity(pid: int) -> dict | None:
    try:
        process = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=", "-o", "uid=",
             "-o", "stat=", "-o", "lstart="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if process.returncode != 0:
        return None
    parts = process.stdout.strip().split(None, 3)
    if len(parts) != 4:
        return None
    try:
        found_pid = int(parts[0])
        uid = int(parts[1])
    except ValueError:
        return None
    if found_pid != pid:
        return None
    return {
        "pid": pid,
        "uid": uid,
        "state": parts[2].strip(),
        "start_token": parts[3].strip(),
        "platform": "posix",
    }


def _windows_identity(pid: int) -> dict | None:
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int,
                                         ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
        if not handle:
            return None
        try:
            exit_code = ctypes.c_ulong(0)
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value != 259:
                return None
            created = ctypes.c_ulonglong()
            exited = ctypes.c_ulonglong()
            kernel = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            # 100ns intervals since 1601-01-01.
            start_token = f"filetime:{created.value}"
            return {
                "pid": int(pid),
                "uid": None,
                "start_token": start_token,
                "platform": "windows",
            }
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return None


def read_process_identity(pid) -> dict | None:
    """Return a process identity, or None when it cannot be verified."""
    if type(pid) is not int or pid <= 0 or pid > 2**31 - 1:
        return None
    if os.name == "nt":
        # Never use os.kill(pid, 0) on Windows: Python maps non-console
        # signals to TerminateProcess. OpenProcess is query-only here.
        identity = _windows_identity(pid)
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            # Exists but is owned by another user; identity is not ours to trust.
            return None
        except OSError:
            return None
        identity = _posix_identity(pid)
    if identity is None:
        return None
    identity["host"] = socket.gethostname()
    return identity


def identity_matches(record, live=None) -> bool:
    """Compare a stored identity with a live process without sending signals."""
    if not isinstance(record, dict):
        return False
    pid = record.get("pid")
    start_token = record.get("start_token")
    if type(pid) is not int or pid <= 0 or not isinstance(start_token, str):
        return False
    live = live if live is not None else read_process_identity(pid)
    if not isinstance(live, dict):
        return False
    if live.get("pid") != pid or live.get("start_token") != start_token:
        return False
    if str(live.get("state") or "").upper().startswith("Z"):
        return False
    stored_uid = record.get("uid")
    if stored_uid is not None and live.get("uid") != stored_uid:
        return False
    stored_host = record.get("host")
    if stored_host and live.get("host") and stored_host != live["host"]:
        return False
    return True


def owner_alive(record) -> bool:
    return identity_matches(record)


def wait_for_exit(record, timeout: float = 5.0) -> bool:
    timeout = max(0.0, float(timeout))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not identity_matches(record):
            return True
        time.sleep(0.1)
    return not identity_matches(record)


def terminate_owned(record, *, grace: float = 5.0) -> dict:
    """Terminate only a process whose live identity matches ``record``.

    Returns a local-signal result. This is deliberately not a native provider
    cancellation acknowledgement.
    """
    if not identity_matches(record):
        return {
            "requested": False,
            "signaled": False,
            "terminated": not identity_matches(record),
            "native_cancel_ack": False,
            "local_signal_only": True,
            "reason": "process ownership could not be verified; no signal sent",
        }
    pid = record["pid"]
    signaled = False
    try:
        if os.name == "nt":
            # os.kill supports SIGTERM on modern Python/Windows; it is only
            # issued after the creation-time identity check above.
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
        signaled = True
    except (OSError, ProcessLookupError):
        signaled = False
    if signaled and not wait_for_exit(record, grace):
        try:
            if os.name != "nt":
                os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    return {
        "requested": True,
        "signaled": signaled,
        "terminated": wait_for_exit(record, grace),
        "native_cancel_ack": False,
        "local_signal_only": True,
        "pid": pid,
    }


@contextmanager
def project_lock(root: Path, *, timeout: float = 5.0):
    """Cross-process atomic lock for bridge/runner state mutations.

    An exclusive create is used instead of an OS-specific advisory lock so the
    behavior is identical on POSIX and Windows. The lock file records the same
    PID + start-token ownership as the run itself. A crash leaves a stale file
    that is reclaimed only after its owner identity no longer matches.
    """
    path = Path(root) / ".auto-orchestrator" / "teamwork" / "state.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    global _SELF_IDENTITY
    with _SELF_LOCK:
        if _SELF_IDENTITY is None:
            _SELF_IDENTITY = read_process_identity(os.getpid())
        identity = _SELF_IDENTITY
    if identity is None:
        raise OwnershipError("cannot verify the lock owner process identity")
    token = os.urandom(8).hex()
    payload = {**identity, "lock_token": token,
               "acquired_at": time.time()}
    deadline = time.monotonic() + max(0.0, float(timeout))
    acquired = False
    while True:
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, (json.dumps(payload, separators=(",", ":")) + "\n").encode())
                try:
                    os.fsync(fd)
                except OSError:
                    pass
            finally:
                os.close(fd)
            acquired = True
            break
        except FileExistsError:
            holder = read_json(path, None)
            stale = False
            if isinstance(holder, dict):
                stale = not identity_matches(holder)
            else:
                try:
                    age = time.time() - path.stat().st_mtime
                except OSError:
                    age = 0.0
                stale = age > 2.0
            if stale:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise OwnershipError(
                    "another Teamwork state operation is in progress"
                )
            time.sleep(0.05)
        except OSError as exc:
            raise OwnershipError(f"could not acquire project lock: {exc}") from exc
    try:
        yield path
    finally:
        if acquired:
            holder = read_json(path, None)
            if isinstance(holder, dict) and holder.get("lock_token") == token:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


def owner_record(pid) -> dict | None:
    """Identity record for a newly started child, with short startup retries."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        identity = read_process_identity(pid)
        if identity is not None:
            return identity
        time.sleep(0.05)
    return None


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def teamwork_dir(root) -> Path:
    return Path(root) / ".auto-orchestrator" / "teamwork"


def atomic_write_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(temporary, path)


def update_active(root, run_id: str, **fields) -> dict:
    """Atomically update the active record if it still belongs to ``run_id``."""
    path = teamwork_dir(root) / "active.json"
    with project_lock(root, timeout=5.0):
        current = read_json(path, {})
        if not isinstance(current, dict) or current.get("run_id") != run_id:
            raise OwnershipError("active run record changed or does not belong to this run")
        current.update(fields)
        atomic_write_json(path, current)
        return current


def recovery_state_path(root, run_id: str) -> Path:
    return teamwork_dir(root) / f"recovery-{run_id}.json"


def write_recovery_state(root, run_id: str, state: str, **fields) -> dict:
    allowed = {
        "active", "runner_disconnected", "reconciling", "orphaned",
        "retry_ready", "resuming", "verifying", "recovered",
        "recovery_failed",
    }
    if state not in allowed:
        raise OwnershipError(f"unknown recovery state: {state}")
    payload = {
        "version": 1,
        "run_id": run_id,
        "state": state,
        "updated_at": time.time(),
        **fields,
    }
    atomic_write_json(recovery_state_path(root, run_id), payload)
    return payload


def read_recovery_state(root, run_id: str) -> dict | None:
    value = read_json(recovery_state_path(root, run_id), None)
    return value if isinstance(value, dict) else None
