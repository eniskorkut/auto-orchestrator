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
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path


class OwnershipError(RuntimeError):
    pass


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


def _process_group(pid: int) -> int | None:
    if os.name == "nt":
        return None
    try:
        group = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return None
    return group if group == pid else None


def _group_alive(group: int | None) -> bool:
    if group is None:
        return False
    # A zombie group leader would make killpg(0) look alive forever; inspect
    # the group states and ignore zombies.
    try:
        process = subprocess.run(
            ["ps", "-o", "stat=", "-g", str(group)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        if process.returncode == 0:
            return any(not state.upper().startswith("Z")
                       for state in process.stdout.split())
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def owned_group_alive(pid: int) -> bool:
    """Check the session group led by an owned managed PID (POSIX)."""
    if os.name == "nt" or type(pid) is not int or pid <= 0:
        return False
    return _group_alive(pid)


def _signal_owned(pid: int, group: int | None, sig) -> bool:
    try:
        if group is not None:
            os.killpg(group, sig)
        else:
            os.kill(pid, sig)
        return True
    except (OSError, ProcessLookupError):
        return False


def terminate_owned(record, *, grace: float = 5.0) -> dict:
    """Terminate only a process whose live identity matches ``record``.

    The managed provider starts a new session, so its direct PID is normally
    the process-group leader. On POSIX the whole owned group is signalled; a
    surviving grandchild would otherwise keep writing to the worktree.
    """
    if not identity_matches(record):
        return {
            "requested": False,
            "signaled": False,
            "terminated": not identity_matches(record),
            "group_terminated": True,
            "native_cancel_ack": False,
            "local_signal_only": True,
            "reason": "process ownership could not be verified; no signal sent",
        }
    pid = record["pid"]
    group = _process_group(pid)
    signaled = _signal_owned(pid, group, signal.SIGTERM)
    parent_exited = wait_for_exit(record, grace) if signaled else False
    group_alive = _group_alive(group)
    if signaled and group_alive and group is not None:
        _signal_owned(pid, group, signal.SIGKILL)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and _group_alive(group):
            time.sleep(0.1)
    group_alive = _group_alive(group)
    return {
        "requested": True,
        "signaled": signaled,
        "terminated": bool(parent_exited and not group_alive),
        "group_terminated": not group_alive,
        "process_group": group,
        "native_cancel_ack": False,
        "local_signal_only": True,
        "pid": pid,
    }


_PROJECT_LOCKS: dict[str, "threading.Lock"] = {}
_PROJECT_LOCKS_GUARD = __import__("threading").Lock()
_ACTIVE_LEASES: set[str] = set()


def _thread_lock(path: Path):
    key = str(path)
    with _PROJECT_LOCKS_GUARD:
        lock = _PROJECT_LOCKS.get(key)
        if lock is None:
            lock = __import__("threading").Lock()
            _PROJECT_LOCKS[key] = lock
        return lock


def _try_lock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass


@contextmanager
def project_lock(root: Path, *, timeout: float = 5.0):
    """Cross-process advisory lock for bridge/runner state mutations.

    The OS releases the lock when the holder exits, so a crash never leaves a
    permanently held lock. A per-process thread lock also serializes two
    threads that open the same file, which is needed on Windows because file
    locks are process-scoped there.
    """
    path = Path(root) / ".auto-orchestrator" / "teamwork" / "state.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    thread_lock = _thread_lock(path)
    if not thread_lock.acquire(timeout=max(0.0, float(timeout))):
        raise OwnershipError("another Teamwork state operation is in progress")
    handle = None
    acquired = False
    try:
        handle = open(path, "a+b")
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                _try_lock(handle)
                acquired = True
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise OwnershipError(
                        "another Teamwork state operation is in progress"
                    )
                time.sleep(0.05)
        yield path
    finally:
        if handle is not None:
            if acquired:
                _unlock(handle)
            handle.close()
        thread_lock.release()


def _runner_lock_path(root, run_id: str) -> Path:
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_id):
        raise OwnershipError("invalid runner id for lease")
    return teamwork_dir(root) / f"runner-{run_id}.lock"


@contextmanager
def runner_lease(root, run_id: str, *, timeout: float = 0.0):
    """Hold an OS lease for the lifetime of one runner process.

    The lease is independent of PID identity, so a bridge can distinguish a
    live (even hung) runner from a dead one after a crash. The OS releases it
    when the process exits; the lock file may remain on disk.
    """
    path = _runner_lock_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = str(path.resolve())
    with _PROJECT_LOCKS_GUARD:
        if resolved in _ACTIVE_LEASES:
            raise OwnershipError("runner lease is already held")
        _ACTIVE_LEASES.add(resolved)
    thread_lock = _thread_lock(path)
    if not thread_lock.acquire(timeout=max(0.0, float(timeout))):
        with _PROJECT_LOCKS_GUARD:
            _ACTIVE_LEASES.discard(resolved)
        raise OwnershipError("runner lease is already held")
    handle = None
    acquired = False
    try:
        handle = open(path, "a+b")
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                _try_lock(handle)
                acquired = True
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise OwnershipError("runner lease is already held")
                time.sleep(0.05)
        yield path
    finally:
        if handle is not None:
            if acquired:
                _unlock(handle)
            handle.close()
        thread_lock.release()
        with _PROJECT_LOCKS_GUARD:
            _ACTIVE_LEASES.discard(resolved)


def lease_held(root, run_id: str) -> bool:
    path = _runner_lock_path(root, run_id)
    resolved = str(path.resolve())
    with _PROJECT_LOCKS_GUARD:
        if resolved in _ACTIVE_LEASES:
            return True
    if not path.exists():
        return False
    try:
        with runner_lease(root, run_id, timeout=0):
            return False
    except (OwnershipError, OSError, ValueError):
        return True


def _project_writer_path(root) -> Path:
    return teamwork_dir(root) / "project-writer.lock"


@contextmanager
def project_writer_lease(root, *, timeout: float = 0.0):
    """Hold a project-level writer lease across all runs and IDEs."""
    path = _project_writer_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = str(path.resolve())
    with _PROJECT_LOCKS_GUARD:
        if resolved in _ACTIVE_LEASES:
            raise OwnershipError("another IDE already owns the project writer lease")
        _ACTIVE_LEASES.add(resolved)
    thread_lock = _thread_lock(path)
    if not thread_lock.acquire(timeout=max(0.0, float(timeout))):
        with _PROJECT_LOCKS_GUARD:
            _ACTIVE_LEASES.discard(resolved)
        raise OwnershipError("another IDE already owns the project writer lease")
    handle = None
    acquired = False
    try:
        handle = open(path, "a+b")
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                _try_lock(handle)
                acquired = True
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise OwnershipError(
                        "another IDE already owns the project writer lease"
                    )
                time.sleep(0.05)
        yield path
    finally:
        if handle is not None:
            if acquired:
                _unlock(handle)
            handle.close()
        thread_lock.release()
        with _PROJECT_LOCKS_GUARD:
            _ACTIVE_LEASES.discard(resolved)


def project_writer_held(root) -> bool:
    path = _project_writer_path(root)
    resolved = str(path.resolve())
    with _PROJECT_LOCKS_GUARD:
        if resolved in _ACTIVE_LEASES:
            return True
    if not path.exists():
        return False
    try:
        with project_writer_lease(root, timeout=0):
            return False
    except (OwnershipError, OSError, ValueError):
        return True


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


def update_active(root, run_id: str, *, expected_token: str | None = None,
                  **fields) -> dict:
    """Atomically update the active record if it still belongs to ``run_id``."""
    path = teamwork_dir(root) / "active.json"
    with project_lock(root, timeout=5.0):
        current = read_json(path, {})
        if not isinstance(current, dict) or current.get("run_id") != run_id:
            raise OwnershipError("active run record changed or does not belong to this run")
        if expected_token is not None and current.get("runner_token") != expected_token:
            raise OwnershipError("active run record is owned by a different runner token")
        current.update(fields)
        atomic_write_json(path, current)
        return current


def recovery_state_path(root, run_id: str) -> Path:
    return teamwork_dir(root) / f"recovery-{run_id}.json"


def write_recovery_state(root, run_id: str, state: str, **fields) -> dict:
    allowed = {
        "active", "runner_disconnected", "reconciling", "orphaned",
        "retry_ready", "resuming", "verifying", "recovered",
        "recovery_failed", "cancelled",
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
