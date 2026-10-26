#!/usr/bin/env python3
"""Managed execution adapters for the V7.3 teamwork engine.

Two providers are supported:

* :class:`OpenCodeAdapter` is the primary, operational adapter. It launches the
  installed ``opencode run --format json`` CLI, streams its JSONL event stream,
  keeps a sanitized evidence file and exposes completion, usage and tool
  metadata. Cancellation is a *local* process signal: the CLI has no native
  acknowledgement contract, so the adapter never reports one.
* :class:`CodexAdapter` is a small JSON-RPC-over-stdio client for
  ``codex app-server``. It is contract-tested with a fake server and marked
  unavailable when the binary is absent. It never fabricates a completed turn.

Security rules enforced here:

* prompts are passed to the child through argv/stdin but are never written to
  the evidence file or returned from :meth:`session_state`;
* tool input/output, command text and provider metadata are stripped before an
  event is persisted;
* the child receives an allow-listed environment (plus explicit caller
  overrides), never the full ambient environment;
* event files are created with mode ``0600`` on POSIX;
* output reading, message sizes and retained text/error lists are bounded;
* a prompt echoed by a provider error or stderr is redacted before retention.

Only the Python standard library is used. Tests use fake provider executables;
the live OpenCode test is opt-in via ``AO_LIVE_OPENCODE=1``.
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

__all__ = [
    "ManagedError",
    "OpenCodeAdapter",
    "CodexAdapter",
    "adapter_for",
]

MAX_EVENT_BYTES = 8 * 1024 * 1024
MAX_RPC_BYTES = 1024 * 1024
MAX_TEXT = 8000
MAX_TOOLS = 256
MAX_ERRORS = 16
MAX_STDERR = 2000
MAX_SESSION_ID = 200
MAX_TITLE = 200
MAX_NOTIFICATIONS = 1024
MAX_UNKNOWN_METHODS = 128
DRAIN_SECONDS = 2.0
KILL_SECONDS = 5.0
START_TIMEOUT = 15.0
REDACTED = "[redacted-prompt]"

# Ambient variables that are safe (and usually necessary) for a native CLI.
# Everything else is dropped unless the caller explicitly supplies it.
_SAFE_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TERM",
    "NO_COLOR",
    "CI",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_DISABLE_AUTOUPDATE",
)

# Explicit overrides are trusted, apart from loader/interpreter injection.
_BLOCKED_ENV_KEYS = {
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
}

_CACHE_KEY = "cache"
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|authorization)\b\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|"
        r"github_pat_[A-Za-z0-9_]{8,}|xox[abprs]-[A-Za-z0-9-]{8,})\b"
    ),
)


def _redact_secrets(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[redacted-secret]", value)
    return value


class ManagedError(ValueError):
    """Raised when a managed execution request cannot be honoured."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _finite_number(value, *, integer: bool = False):
    """Return a finite, non-negative number (0 for malformed input)."""
    if isinstance(value, bool):
        return 0 if integer else 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0 if integer else 0.0
    if not math.isfinite(number) or number < 0:
        return 0 if integer else 0.0
    return int(number) if integer else number


def _text(
    value,
    *,
    limit: int,
    prompt: str | None = None,
    tail: bool = False,
    secrets: bool = False,
):
    """Bound and redact an untrusted string. Returns None for non-strings."""
    if not isinstance(value, str):
        return None
    if prompt:
        value = value.replace(prompt, REDACTED)
    if secrets:
        value = _redact_secrets(value)
    value = value.replace("\x00", "")
    if len(value) > limit:
        value = value[-limit:] if tail else value[:limit]
    return value


def _safe_component(value, limit: int = 80) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._-")
    return (text or "task")[:limit]


def _positive_timeout(value, *, name: str = "timeout") -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ManagedError(f"{name} must be a number") from exc
    if not math.isfinite(number):
        raise ManagedError(f"{name} must be finite")
    return max(0.0, number)


def _require_prompt(prompt) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ManagedError("prompt must be a non-empty string")
    if "\x00" in prompt:
        raise ManagedError("prompt must not contain NUL")
    return prompt


def _require_task_id(task_id) -> str:
    if not isinstance(task_id, str) or not task_id.strip():
        raise ManagedError("task_id must be a non-empty string")
    if "\x00" in task_id:
        raise ManagedError("task_id must not contain NUL")
    return task_id


def _require_cwd(cwd) -> Path:
    try:
        path = Path(cwd)
        is_dir = path.is_dir()
    except (TypeError, ValueError, OSError) as exc:
        raise ManagedError("cwd must be a path") from exc
    if not is_dir:
        raise ManagedError(f"cwd is not a directory: {path}")
    return path


def _optional_argument(value, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManagedError(f"{field} must be a string")
    if "\x00" in value:
        raise ManagedError(f"{field} must not contain NUL")
    return value


def sanitized_environment(overrides: dict | None = None) -> dict[str, str]:
    """Build the allow-listed environment for a managed child process."""
    env: dict[str, str] = {}
    for key in _SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    for key, value in (overrides or {}).items():
        if not isinstance(key, str) or not key or "=" in key or "\x00" in key:
            continue
        if key in _BLOCKED_ENV_KEYS or value is None:
            continue
        env[key] = str(value)
    env.setdefault("PATH", os.defpath)
    return env


def _open_private_stream(path: Path):
    """Open ``path`` exclusively with POSIX mode 0600."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return os.fdopen(fd, "w", encoding="utf-8", newline="\n")


def _close_quietly(stream) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _terminate_process(process, *, grace: float = 1.0) -> bool:
    """Terminate then kill a child. Returns True when it is no longer alive."""
    if process is None:
        return True
    if process.poll() is not None:
        return True
    try:
        process.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=max(0.05, grace))
        return True
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=KILL_SECONDS)
        return True
    except subprocess.TimeoutExpired:
        return False


def _signal_process(process, sig) -> bool:
    """Signal a child, preferring its own process group on POSIX."""
    if process is None or process.poll() is not None:
        return False
    try:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), sig)
            except (ProcessLookupError, PermissionError):
                process.send_signal(sig)
        elif sig == _SIGKILL:
            process.kill()
        else:
            process.terminate()
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


class _Run:
    """Mutable runtime state for one managed process."""

    def __init__(
        self,
        *,
        provider: str,
        task_id: str,
        model: str | None,
        events_path: Path,
        stream,
        resume: str | None,
    ):
        self.provider = provider
        self.run_id = f"run_{uuid.uuid4().hex}"
        self.task_id = task_id
        self.model = model
        self.events_path = events_path
        self.stream = stream
        self.resume_of = resume
        self.session_id = resume
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stdout_eof = threading.Event()
        self.process = None
        self.threads: list[threading.Thread] = []
        self.started_at = time.time()
        self.updated_at = self.started_at
        self.finished_at = None
        self.status = "running"
        self.exit_code = None
        self.finished = False
        self.error_flag = False
        self.protocol_error = False
        self.errors: list[dict] = []
        self.warnings: list[str] = []
        self.cancel_requested = False
        self.cancel_state = "none"
        self.cancel_signal_sent = False
        self.native_cancel_ack = False
        self.local_signal_only = True
        self.text = ""
        self.tools: list[dict] = []
        self.tokens = {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
        }
        self.cost = 0.0
        self.steps = 0
        self.stderr_tail = ""
        # Prompt is held only for exact redaction while the run is alive.
        self.redaction_prompt: str | None = None
        # Codex-specific protocol state.
        self.rpc = None
        self.thread_id = None
        self.turn_id = None
        self.turn_done = False
        self.turn_status = None


# ---------------------------------------------------------------------------
# Shared adapter plumbing
# ---------------------------------------------------------------------------


class _ManagedAdapter:
    provider = ""

    def __init__(
        self,
        root: Path,
        *,
        model: str | None = None,
        binary: str | None = None,
        env: dict | None = None,
    ):
        try:
            self.root = Path(root)
        except (TypeError, ValueError, OSError) as exc:
            raise ManagedError("root must be a path") from exc
        if self.root.exists() and not self.root.is_dir():
            raise ManagedError(f"root is not a directory: {self.root}")
        self.model = model
        self.binary = binary
        self.env = dict(env or {})
        self._runs: dict[str, _Run] = {}
        self._runs_lock = threading.RLock()

    # -- capability / binary discovery ------------------------------------

    def _resolve_binary(self, default: str) -> str | None:
        candidate = self.binary or default
        if isinstance(candidate, os.PathLike):
            candidate = os.fspath(candidate)
        if not isinstance(candidate, str) or not candidate:
            return None
        if os.sep in candidate or (os.altsep and os.altsep in candidate):
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
            return None
        search_path = self.env.get("PATH") if isinstance(self.env, dict) else None
        return shutil.which(candidate, path=str(search_path) if search_path else None)

    def available(self) -> bool:
        raise NotImplementedError

    def capabilities(self) -> dict:
        raise NotImplementedError

    # -- files / registry -------------------------------------------------

    def _new_run(
        self,
        *,
        task_id: str,
        model: str | None,
        resume: str | None,
    ) -> _Run:
        base = (
            self.root
            / ".auto-orchestrator"
            / "managed"
            / _safe_component(task_id)
        )
        try:
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix":
                try:
                    os.chmod(base, 0o700)
                except OSError:
                    pass
            path = base / f"{uuid.uuid4().hex}.jsonl"
            stream = _open_private_stream(path)
        except OSError as exc:
            raise ManagedError(f"cannot create managed evidence file: {exc}") from exc
        return _Run(
            provider=self.provider,
            task_id=task_id,
            model=model,
            events_path=path,
            stream=stream,
            resume=resume,
        )

    def _register(self, run: _Run) -> None:
        with self._runs_lock:
            self._runs[run.run_id] = run

    def _run_for(self, handle) -> _Run:
        if not isinstance(handle, dict):
            raise ManagedError("handle must be a managed run dict")
        run_id = handle.get("run_id")
        with self._runs_lock:
            run = self._runs.get(run_id)
        if run is None:
            raise ManagedError("unknown managed run handle")
        return run

    @staticmethod
    def _warn(run: _Run, message: str) -> None:
        if len(run.warnings) < MAX_UNKNOWN_METHODS:
            run.warnings.append(message)

    def _write_record(self, run: _Run, record: dict) -> None:
        with run.lock:
            if run.stream is None:
                return
            try:
                encoded = json.dumps(
                    record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                if len(encoded.encode("utf-8")) > MAX_RPC_BYTES:
                    self._warn(run, "event record truncated")
                    return
                run.stream.write(encoded + "\n")
                run.stream.flush()
            except (OSError, ValueError):
                run.stream = None

    # -- process lifecycle ------------------------------------------------

    def _spawn(self, run: _Run, argv: list[str], cwd: Path, *, stdin):
        kwargs = {
            "cwd": str(cwd),
            "env": sanitized_environment(self.env),
            "stdin": stdin,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "bufsize": 0,
            "close_fds": True,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        try:
            run.process = subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            _close_quietly(run.stream)
            run.stream = None
            try:
                run.events_path.unlink()
            except OSError:
                pass
            raise ManagedError(f"cannot start {self.provider}: {exc}") from exc
        return run.process

    def _abandon(self, run: _Run) -> None:
        _terminate_process(run.process, grace=1.0)
        _close_quietly(run.stream)
        run.stream = None
        run.redaction_prompt = None

    def _append_error(self, run: _Run, error_type, message) -> None:
        with run.lock:
            self._append_error_locked(run, error_type, message)

    def _append_error_locked(self, run: _Run, error_type, message) -> None:
        prompt = run.redaction_prompt
        safe_type = _text(
            str(error_type or "error"), limit=80, prompt=prompt, secrets=True
        )
        safe_message = _text(
            str(message or ""), limit=500, prompt=prompt, secrets=True
        )
        if len(run.errors) < MAX_ERRORS:
            run.errors.append(
                {"type": safe_type or "error", "message": safe_message or ""}
            )
        else:
            self._warn(run, "error limit reached")
        run.error_flag = True
        run.updated_at = time.time()

    def _error_text(self, run: _Run) -> str | None:
        with run.lock:
            if run.errors:
                first = run.errors[0]
                kind = first.get("type") or "error"
                message = first.get("message") or ""
                text = f"{kind}: {message}" if message else kind
                return _text(text, limit=500, prompt=run.redaction_prompt)
            if run.protocol_error:
                return "malformed provider event stream"
            if run.exit_code not in (None, 0):
                return f"exit code {run.exit_code}"
            return None

    def _finish_common(self, run: _Run, code) -> None:
        """Close evidence and release the prompt. Caller holds ``run.lock``."""
        run.exit_code = code
        run.finished = True
        run.finished_at = time.time()
        run.updated_at = run.finished_at
        run.text = _text(
            run.text, limit=MAX_TEXT, prompt=run.redaction_prompt
        ) or ""
        _close_quietly(run.stream)
        run.stream = None
        if run.process is not None and not run.stdout_eof.is_set():
            _close_quietly(getattr(run.process, "stdout", None))
        run.redaction_prompt = None
        run.wake.set()

    def _wait_stdout_eof(self, run: _Run, timeout: float = DRAIN_SECONDS) -> bool:
        return run.stdout_eof.wait(max(0.0, timeout))


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------


class OpenCodeAdapter(_ManagedAdapter):
    """Managed ``opencode run --format json`` execution."""

    provider = "opencode"

    def available(self) -> bool:
        return self._resolve_binary("opencode") is not None

    def capabilities(self) -> dict:
        return {
            "provider": self.provider,
            "managed_session": True,
            "event_stream": True,
            "tool_activity": True,
            "completion": True,
            "resume": True,
            "native_cancel_ack": False,
            "exact_background_stop": False,
            "terminal_stream": False,
        }

    # -- start ------------------------------------------------------------

    def start(
        self,
        *,
        task_id: str,
        prompt: str,
        cwd: Path,
        title: str | None = None,
        model: str | None = None,
        agent: str | None = None,
        resume: str | None = None,
    ) -> dict:
        task_id = _require_task_id(task_id)
        prompt = _require_prompt(prompt)
        cwd = _require_cwd(cwd)
        binary = self._resolve_binary("opencode")
        if binary is None:
            raise ManagedError("opencode binary is not available")
        title = _optional_argument(title, "title")
        model = _optional_argument(model, "model")
        agent = _optional_argument(agent, "agent")
        resume = _optional_argument(resume, "resume")
        if resume is not None and (not resume.strip() or "\n" in resume):
            raise ManagedError("resume must be a non-empty session id")
        selected_model = model if model is not None else self.model
        selected_model = _optional_argument(selected_model, "model")
        selected_title = title if title is not None else task_id
        if not isinstance(selected_title, str) or not selected_title.strip():
            selected_title = task_id
        selected_title = _text(selected_title, limit=MAX_TITLE) or task_id

        run = self._new_run(task_id=task_id, model=selected_model, resume=resume)
        run.redaction_prompt = prompt
        argv = [binary, "run", "--format", "json", "--standalone"]
        argv.extend(["--title", selected_title])
        if selected_model:
            argv.extend(["--model", str(selected_model)])
        if agent:
            argv.extend(["--agent", str(agent)])
        if resume:
            argv.extend(["--session", resume])
        argv.append(prompt)

        self._spawn(run, argv, cwd, stdin=subprocess.DEVNULL)
        self._register(run)
        self._start_readers(run)
        return self._handle(run)

    def _start_readers(self, run: _Run) -> None:
        stdout_thread = threading.Thread(
            target=self._stdout_loop,
            args=(run, run.process.stdout),
            name=f"managed-opencode-stdout-{run.run_id}",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._stderr_loop,
            args=(run, run.process.stderr),
            name=f"managed-opencode-stderr-{run.run_id}",
            daemon=True,
        )
        run.threads = [stdout_thread, stderr_thread]
        for thread in run.threads:
            thread.start()

    def _stdout_loop(self, run: _Run, stream) -> None:
        try:
            while True:
                raw = stream.readline(MAX_EVENT_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_EVENT_BYTES and not raw.endswith(b"\n"):
                    with run.lock:
                        run.protocol_error = True
                        self._warn(run, "oversized provider event")
                    _signal_process(run.process, signal.SIGTERM)
                    break
                try:
                    line = raw.decode("utf-8", "replace").strip()
                except Exception:  # pragma: no cover - decode never raises here
                    line = ""
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    with run.lock:
                        run.protocol_error = True
                        self._append_error_locked(
                            run, "protocol_error", "non-JSON event from provider"
                        )
                        self._write_record(
                            run, {"type": "managed_protocol_error"}
                        )
                    _signal_process(run.process, signal.SIGTERM)
                    break
                if not isinstance(event, dict):
                    with run.lock:
                        run.protocol_error = True
                        self._append_error_locked(
                            run, "protocol_error", "non-object event from provider"
                        )
                    _signal_process(run.process, signal.SIGTERM)
                    break
                self._consume_event(run, event)
        except (OSError, ValueError):
            pass
        finally:
            _close_quietly(stream)
            run.stdout_eof.set()
            run.wake.set()

    def _stderr_loop(self, run: _Run, stream) -> None:
        tail = ""
        # Capture the prompt locally: finalisation may clear the shared value
        # before this thread sees EOF, and stderr must still be redacted.
        prompt = run.redaction_prompt
        try:
            while True:
                raw = stream.readline(65536)
                if not raw:
                    break
                chunk = raw.decode("utf-8", "replace")
                tail = (tail + chunk)[-MAX_STDERR:]
        except (OSError, ValueError):
            pass
        finally:
            _close_quietly(stream)
            with run.lock:
                run.stderr_tail = _text(
                    tail,
                    limit=MAX_STDERR,
                    prompt=prompt,
                    tail=True,
                    secrets=True,
                ) or ""
                run.updated_at = time.time()
            run.wake.set()

    # -- event handling ---------------------------------------------------

    def _consume_event(self, run: _Run, event: dict) -> None:
        with run.lock:
            if run.finished:
                return
            prompt = run.redaction_prompt
            part = event.get("part")
            part_dict = part if isinstance(part, dict) else {}
            session_id = event.get("sessionID") or part_dict.get("sessionID")
            if isinstance(session_id, str) and session_id:
                run.session_id = _text(
                    session_id, limit=MAX_SESSION_ID, prompt=prompt
                )
            event_type = event.get("type")
            if event_type == "text":
                text = part_dict.get("text")
                if isinstance(text, str) and text:
                    text = _text(text, limit=MAX_TEXT, prompt=prompt)
                    if text:
                        run.text = _text(
                            (run.text + text)[-MAX_TEXT:],
                            limit=MAX_TEXT,
                            prompt=prompt,
                        )
            elif event_type == "tool_use":
                self._record_tool(run, part_dict)
            elif event_type == "step_finish":
                self._record_usage(run, part_dict)
            elif event_type == "error":
                error = event.get("error")
                if isinstance(error, dict):
                    kind = error.get("type") or error.get("code") or "error"
                    message = error.get("message") or ""
                else:
                    kind = "error"
                    message = error if isinstance(error, str) else ""
                self._append_error_locked(run, kind, message)
            self._write_record(run, self._sanitized_event(run, event, part_dict))
            run.updated_at = time.time()
        run.wake.set()

    def _record_tool(self, run: _Run, part: dict) -> None:
        prompt = run.redaction_prompt
        name = part.get("tool") or part.get("name") or "unknown"
        name = _text(str(name), limit=64, prompt=prompt) or "unknown"
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = state.get("status")
        status = _text(str(status), limit=32, prompt=prompt) if status else "unknown"
        call_id = part.get("id") or part.get("callID") or part.get("call_id")
        call_id = _text(str(call_id), limit=80, prompt=prompt) if call_id else None
        duration = None
        timing = state.get("time") if isinstance(state.get("time"), dict) else {}
        if timing:
            start = _finite_number(timing.get("start"))
            end = _finite_number(timing.get("end"))
            if start and end and end >= start:
                duration = int(end - start)
        record = {"name": name, "tool": name, "status": status or "unknown"}
        if call_id:
            record["call_id"] = call_id
        if duration is not None:
            record["duration_ms"] = duration
        for exit_key in ("exitCode", "exit_code"):
            exit_value = state.get(exit_key)
            if isinstance(exit_value, int) and not isinstance(exit_value, bool):
                record["exit_code"] = exit_value
                break
        if "exit_code" not in record:
            metadata = state.get("metadata")
            if isinstance(metadata, dict):
                for exit_key in ("exit", "exitCode", "exit_code"):
                    exit_value = metadata.get(exit_key)
                    if isinstance(exit_value, int) and not isinstance(
                        exit_value, bool
                    ):
                        record["exit_code"] = exit_value
                        break
        if call_id:
            for index, existing in enumerate(run.tools):
                if existing.get("call_id") == call_id:
                    run.tools[index] = record
                    return
        if len(run.tools) < MAX_TOOLS:
            run.tools.append(record)
        else:
            self._warn(run, "tool limit reached")

    def _record_usage(self, run: _Run, part: dict) -> None:
        run.steps += 1
        run.cost += _finite_number(part.get("cost"))
        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        run.tokens["input"] += _finite_number(tokens.get("input"), integer=True)
        run.tokens["output"] += _finite_number(tokens.get("output"), integer=True)
        run.tokens["reasoning"] += _finite_number(
            tokens.get("reasoning"), integer=True
        )
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        run.tokens["cache_read"] += _finite_number(
            cache.get("read", tokens.get("cache_read")), integer=True
        )
        run.tokens["cache_write"] += _finite_number(
            cache.get("write", tokens.get("cache_write")), integer=True
        )

    def _sanitized_event(self, run: _Run, event: dict, part: dict) -> dict:
        prompt = run.redaction_prompt
        record: dict = {}
        event_type = _text(str(event.get("type") or "unknown"), limit=64, prompt=prompt)
        record["type"] = event_type or "unknown"
        session_id = event.get("sessionID") or part.get("sessionID")
        if isinstance(session_id, str) and session_id:
            record["sessionID"] = _text(
                session_id, limit=MAX_SESSION_ID, prompt=prompt
            )
        timestamp = event.get("timestamp")
        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
            record["timestamp"] = timestamp
        if part:
            clean_part: dict = {}
            for key in ("type", "sessionID", "messageID", "id"):
                value = part.get(key)
                if isinstance(value, str) and value:
                    clean_part[key] = _text(value, limit=MAX_SESSION_ID, prompt=prompt)
            if event_type == "tool_use":
                clean_part["tool"] = _text(
                    str(part.get("tool") or part.get("name") or "unknown"),
                    limit=64,
                    prompt=prompt,
                )
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                clean_part["state"] = {
                    "status": _text(
                        str(state.get("status") or "unknown"),
                        limit=32,
                        prompt=prompt,
                    )
                }
            elif event_type == "step_finish":
                clean_part["cost"] = _finite_number(part.get("cost"))
                tokens = (
                    part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
                )
                cache = (
                    tokens.get("cache")
                    if isinstance(tokens.get("cache"), dict)
                    else {}
                )
                clean_part["tokens"] = {
                    "input": _finite_number(tokens.get("input"), integer=True),
                    "output": _finite_number(tokens.get("output"), integer=True),
                    "reasoning": _finite_number(
                        tokens.get("reasoning"), integer=True
                    ),
                    _CACHE_KEY: {
                        "read": _finite_number(
                            cache.get("read", tokens.get("cache_read")),
                            integer=True,
                        ),
                        "write": _finite_number(
                            cache.get("write", tokens.get("cache_write")),
                            integer=True,
                        ),
                    },
                }
            elif event_type == "text":
                clean_part["text"] = _text(
                    part.get("text") or "", limit=MAX_TEXT, prompt=prompt
                )
            if clean_part:
                record["part"] = clean_part
        error = event.get("error")
        if isinstance(error, dict):
            record["error"] = {
                "type": _text(
                    str(error.get("type") or error.get("code") or "error"),
                    limit=80,
                    prompt=prompt,
                    secrets=True,
                ),
                "message": _text(
                    str(error.get("message") or ""),
                    limit=500,
                    prompt=prompt,
                    secrets=True,
                ),
            }
        elif isinstance(error, str):
            record["error"] = {
                "type": "error",
                "message": _text(
                    error, limit=500, prompt=prompt, secrets=True
                ),
            }
        return record

    # -- polling / cancel -------------------------------------------------

    def _handle(self, run: _Run) -> dict:
        with run.lock:
            return {
                "provider": self.provider,
                "run_id": run.run_id,
                "task_id": run.task_id,
                "status": run.status,
                "session_id": run.session_id,
                "resume_of": run.resume_of,
                "events_path": str(run.events_path),
                "pid": run.process.pid if run.process else None,
                "started_at": run.started_at,
                "cancel_state": run.cancel_state,
                "native_cancel_ack": False,
                "local_signal_only": True,
            }

    def _finalize(self, run: _Run, code) -> None:
        with run.lock:
            if run.finished:
                return
            if run.cancel_requested:
                run.status = "cancelled"
            elif run.error_flag or run.protocol_error or code != 0:
                run.status = "failed"
            else:
                run.status = "completed"
            if run.cancel_requested and run.cancel_state == "requested":
                run.cancel_state = "terminated"
            self._finish_common(run, code)

    def poll(self, handle: dict, *, timeout: float = 0) -> dict:
        run = self._run_for(handle)
        timeout = _positive_timeout(timeout)
        deadline = time.monotonic() + timeout
        while True:
            with run.lock:
                if run.finished:
                    return self._snapshot(run)
            code = run.process.poll() if run.process else None
            if code is not None:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._wait_stdout_eof(run, remaining)
                if run.stdout_eof.is_set():
                    self._finalize(run, code)
                    return self._snapshot(run)
                # The reader has not drained the pipe yet. Report the current
                # (still running) state and let the next poll finalise it.
                return self._snapshot(run)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._snapshot(run)
            run.wake.wait(remaining)
            run.wake.clear()

    def cancel(self, handle: dict, *, grace: float = 5.0) -> dict:
        run = self._run_for(handle)
        grace = _positive_timeout(grace, name="grace")
        with run.lock:
            if run.finished or run.process is None or run.process.poll() is not None:
                code = run.process.poll() if run.process else None
                if code is not None:
                    self._wait_stdout_eof(run)
                    self._finalize(run, code)
                return {
                    "requested": False,
                    "terminated": bool(run.process and run.process.poll() is not None),
                    "native_cancel_ack": False,
                    "local_signal_only": True,
                }
            run.cancel_requested = True
            run.cancel_state = "requested"
            run.local_signal_only = True

        sent = _signal_process(run.process, signal.SIGTERM)
        with run.lock:
            if sent:
                run.cancel_signal_sent = True
            already_sent = run.cancel_signal_sent
        terminated = False
        if not sent and not already_sent and run.process.poll() is not None:
            # The process exited between the liveness check and the signal.
            # We did not cancel it, so never label it cancelled.
            with run.lock:
                run.cancel_requested = False
                run.cancel_state = "none"
        if sent:
            try:
                run.process.wait(timeout=grace)
                terminated = True
            except subprocess.TimeoutExpired:
                with run.lock:
                    run.cancel_state = "killed"
                _signal_process(run.process, _SIGKILL)
                try:
                    run.process.wait(timeout=KILL_SECONDS)
                    terminated = True
                except subprocess.TimeoutExpired:
                    terminated = False
        self._wait_stdout_eof(run)
        code = run.process.poll()
        if code is None:
            with run.lock:
                run.cancel_state = "terminate_failed"
            return {
                "requested": bool(sent),
                "terminated": False,
                "native_cancel_ack": False,
                "local_signal_only": True,
            }
        terminated = True
        self._finalize(run, code)
        return {
            "requested": bool(sent),
            "terminated": terminated,
            "native_cancel_ack": False,
            "local_signal_only": True,
        }

    def session_state(self, handle: dict) -> dict:
        return self._snapshot(self._run_for(handle))

    def _snapshot(self, run: _Run) -> dict:
        with run.lock:
            tokens = dict(run.tokens)
            tokens[_CACHE_KEY] = {
                "read": run.tokens["cache_read"],
                "write": run.tokens["cache_write"],
            }
            return {
                "provider": self.provider,
                "run_id": run.run_id,
                "task_id": run.task_id,
                "model": run.model,
                "session_id": run.session_id,
                "resume_of": run.resume_of,
                "status": run.status,
                "pid": run.process.pid if run.process else None,
                "started_at": run.started_at,
                "updated_at": run.updated_at,
                "finished_at": run.finished_at,
                "events_path": str(run.events_path),
                "exit_code": run.exit_code,
                "error": self._error_text(run),
                "cancel_state": run.cancel_state,
                "native_cancel_ack": False,
                "local_signal_only": True,
                "text": run.text,
                "final_text": run.text,
                "tools": [dict(tool) for tool in run.tools],
                "tool_count": len(run.tools),
                "tokens": tokens,
                "cost": round(run.cost, 9),
                "steps": run.steps,
                "errors": [dict(error) for error in run.errors],
                "stderr": run.stderr_tail,
                "warnings": list(run.warnings),
            }

    def close(self, handle: dict | None = None) -> None:
        if handle is not None:
            runs = [self._run_for(handle)]
        else:
            with self._runs_lock:
                runs = list(self._runs.values())
        for run in runs:
            try:
                if not run.finished:
                    self.cancel(self._handle(run), grace=1.0)
            except (ManagedError, OSError):
                _terminate_process(run.process, grace=1.0)


# ---------------------------------------------------------------------------
# Codex app-server (JSON-RPC over stdio)
# ---------------------------------------------------------------------------


class _JsonRpcConnection:
    """Bounded, thread-safe JSON-RPC client over a child's stdin/stdout."""

    def __init__(self, process, *, prompt: str | None = None):
        self.process = process
        self._redaction_prompt = prompt
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, queue.Queue] = {}
        self._next_id = 0
        self._condition = threading.Condition()
        self._notifications: deque = deque(maxlen=MAX_NOTIFICATIONS)
        self._closed = False
        self._fatal: str | None = None
        self._unknown = 0
        self.stderr_tail = ""
        self._threads = [
            threading.Thread(target=self._read_loop, daemon=True),
            threading.Thread(target=self._stderr_loop, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    # -- outbound ---------------------------------------------------------

    def request(self, method: str, params: dict, *, timeout: float):
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            pending: queue.Queue = queue.Queue(maxsize=1)
            key = str(request_id)
            self._pending[key] = pending
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            self._write(payload)
            try:
                message = pending.get(timeout=max(0.05, timeout))
            except queue.Empty as exc:
                raise ManagedError(f"codex {method} timed out") from exc
            if message.get("_closed"):
                raise ManagedError("codex app-server closed before replying")
            if "error" in message:
                error = message.get("error")
                if isinstance(error, dict):
                    code = error.get("code")
                    detail = error.get("message") or "unknown error"
                    raise ManagedError(f"codex {method} failed ({code}): {detail}")
                raise ManagedError(f"codex {method} failed")
            return message.get("result")
        finally:
            with self._pending_lock:
                self._pending.pop(key, None)

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, message: dict) -> None:
        try:
            payload = (
                json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ManagedError("cannot encode JSON-RPC message") from exc
        if len(payload) > MAX_RPC_BYTES:
            raise ManagedError("JSON-RPC message exceeds bounded size")
        with self._write_lock:
            if self.process.stdin is None or self.process.poll() is not None:
                raise ManagedError("codex app-server is not running")
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise ManagedError("codex app-server is not accepting input") from exc

    # -- inbound ----------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            while True:
                raw = self.process.stdout.readline(MAX_RPC_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_RPC_BYTES and not raw.endswith(b"\n"):
                    self._fail("oversized JSON-RPC message")
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line.decode("utf-8", "replace"))
                except (ValueError, TypeError):
                    self._fail("malformed JSON-RPC message")
                    break
                if not isinstance(message, dict):
                    self._fail("non-object JSON-RPC message")
                    break
                if "id" in message and ("result" in message or "error" in message):
                    with self._pending_lock:
                        pending = self._pending.pop(str(message["id"]), None)
                    if pending is not None:
                        try:
                            pending.put_nowait(message)
                        except queue.Full:  # pragma: no cover - defensive
                            pass
                    else:
                        with self._condition:
                            self._unknown += 1
                            self._condition.notify_all()
                elif "method" in message:
                    with self._condition:
                        self._notifications.append(message)
                        self._condition.notify_all()
                else:
                    with self._condition:
                        self._unknown += 1
                        self._condition.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            _close_quietly(self.process.stdout)
            with self._condition:
                self._closed = True
                self._condition.notify_all()
            with self._pending_lock:
                for pending in list(self._pending.values()):
                    try:
                        pending.put_nowait({"_closed": True})
                    except queue.Full:  # pragma: no cover
                        pass
                self._pending.clear()
            self._close_stdin()

    def _stderr_loop(self) -> None:
        tail = ""
        try:
            while True:
                raw = self.process.stderr.readline(65536)
                if not raw:
                    break
                tail = (tail + raw.decode("utf-8", "replace"))[-MAX_STDERR:]
        except (OSError, ValueError):
            pass
        finally:
            _close_quietly(self.process.stderr)
            self.stderr_tail = _text(
                tail,
                limit=MAX_STDERR,
                prompt=self._redaction_prompt,
                tail=True,
                secrets=True,
            ) or ""

    def _fail(self, message: str) -> None:
        with self._condition:
            self._fatal = message
            self._closed = True
            self._condition.notify_all()
        try:
            self.process.kill()
        except (OSError, ProcessLookupError):
            pass

    def _close_stdin(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except (OSError, ValueError):
            pass

    # -- consumer API -----------------------------------------------------

    def drain(self) -> tuple[list[dict], str | None, bool]:
        with self._condition:
            items = list(self._notifications)
            self._notifications.clear()
            return items, self._fatal, self._closed

    def wait_activity(self, timeout: float) -> None:
        with self._condition:
            if self._notifications or self._closed or self._fatal is not None:
                return
            self._condition.wait(max(0.0, timeout))

    def unknown_count(self) -> int:
        with self._condition:
            return self._unknown

    def close_stdin(self) -> None:
        self._close_stdin()

    def close(self) -> None:
        self._close_stdin()
        try:
            self.process.terminate()
        except (OSError, ProcessLookupError):
            pass


class CodexAdapter(_ManagedAdapter):
    """JSON-RPC adapter for ``codex app-server``.

    The adapter is contract-tested with a fake server and has a local V7.5 live
    smoke against Codex CLI 0.164.0. It still treats provider availability as
    runtime state: :meth:`available` is false when the binary is absent.
    """

    provider = "codex"

    def __init__(self, root: Path, *, sandbox: str | None = None,
                 approval_policy: str = "never", **kwargs):
        super().__init__(root, **kwargs)
        if sandbox not in (None, "read-only", "workspace-write"):
            raise ManagedError(
                "Codex sandbox must be read-only or workspace-write; "
                "danger-full-access is not accepted by Teamwork"
            )
        if approval_policy != "never":
            raise ManagedError(
                "Codex approval bridging is not implemented; only 'never' is accepted"
            )
        self.sandbox = sandbox
        self.approval_policy = approval_policy

    def available(self) -> bool:
        return self._resolve_binary("codex") is not None

    def capabilities(self) -> dict:
        return {
            "provider": self.provider,
            "managed_session": True,
            "event_stream": True,
            "tool_activity": True,
            "completion": True,
            "resume": True,
            "native_cancel_ack": False,
            "exact_background_stop": False,
            "terminal_stream": False,
        }

    def start(
        self,
        *,
        task_id: str,
        prompt: str,
        cwd: Path,
        title: str | None = None,
        model: str | None = None,
        agent: str | None = None,
        resume: str | None = None,
    ) -> dict:
        task_id = _require_task_id(task_id)
        prompt = _require_prompt(prompt)
        cwd = _require_cwd(cwd)
        binary = self._resolve_binary("codex")
        if binary is None:
            raise ManagedError("codex binary is not available")
        title = _optional_argument(title, "title")
        model = _optional_argument(model, "model")
        agent = _optional_argument(agent, "agent")
        resume = _optional_argument(resume, "resume")
        if resume is not None and (not resume.strip() or "\n" in resume):
            raise ManagedError("resume must be a non-empty thread id")
        selected_model = model if model is not None else self.model
        selected_model = _optional_argument(selected_model, "model")

        run = self._new_run(task_id=task_id, model=selected_model, resume=resume)
        run.redaction_prompt = prompt
        self._spawn(run, [binary, "app-server"], cwd, stdin=subprocess.PIPE)
        self._register(run)
        run.rpc = _JsonRpcConnection(run.process, prompt=prompt)
        try:
            run.rpc.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "auto-orchestrator",
                        "version": "7.3",
                    }
                },
                timeout=START_TIMEOUT,
            )
            run.rpc.notify("initialized", {})
            if resume:
                resume_params = {"threadId": resume, "cwd": str(cwd)}
                if self.sandbox is not None:
                    resume_params["sandbox"] = self.sandbox
                if self.approval_policy is not None:
                    resume_params["approvalPolicy"] = self.approval_policy
                result = run.rpc.request(
                    "thread/resume", resume_params, timeout=START_TIMEOUT
                )
            else:
                params = {"cwd": str(cwd)}
                if selected_model:
                    params["model"] = str(selected_model)
                if self.sandbox is not None:
                    params["sandbox"] = self.sandbox
                if self.approval_policy is not None:
                    params["approvalPolicy"] = self.approval_policy
                result = run.rpc.request(
                    "thread/start", params, timeout=START_TIMEOUT
                )
            thread_id = self._extract_id(result, ("thread", "threadId", "id"))
            if not thread_id:
                raise ManagedError("codex did not return a thread id")
            with run.lock:
                run.thread_id = _text(
                    thread_id, limit=MAX_SESSION_ID, prompt=run.redaction_prompt
                )
                run.session_id = run.thread_id
            turn_params = {
                "threadId": run.thread_id,
                "input": [{"type": "text", "text": prompt}],
            }
            if selected_model:
                turn_params["model"] = str(selected_model)
            result = run.rpc.request(
                "turn/start", turn_params, timeout=START_TIMEOUT
            )
            turn_id = self._extract_id(result, ("turn", "turnId", "id"))
            with run.lock:
                run.turn_id = (
                    _text(turn_id, limit=MAX_SESSION_ID, prompt=run.redaction_prompt)
                    if turn_id
                    else None
                )
                self._write_record(
                    run,
                    {
                        "type": "managed_start",
                        "method": "thread/resume" if resume else "thread/start",
                        "thread_id": run.thread_id,
                        "turn_id": run.turn_id,
                    },
                )
                run.updated_at = time.time()
        except ManagedError as exc:
            with run.lock:
                self._append_error_locked(run, "startup", str(exc))
                self._write_record(
                    run,
                    {
                        "type": "managed_start_failed",
                        "error": self._error_text(run),
                    },
                )
            _terminate_process(run.process, grace=1.0)
            self._wait_for_rpc_threads(run)
            with run.lock:
                run.status = "failed"
                self._finish_common(run, run.process.poll() if run.process else None)
            raise
        return self._handle(run)

    @staticmethod
    def _extract_id(result, keys: tuple[str, ...]) -> str | None:
        if not isinstance(result, dict):
            return None
        for key in keys:
            if key == "thread" and isinstance(result.get("thread"), dict):
                value = result["thread"].get("id") or result["thread"].get("threadId")
                if isinstance(value, str) and value:
                    return value
            if key == "turn" and isinstance(result.get("turn"), dict):
                value = result["turn"].get("id") or result["turn"].get("turnId")
                if isinstance(value, str) and value:
                    return value
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _handle(self, run: _Run) -> dict:
        with run.lock:
            return {
                "provider": self.provider,
                "run_id": run.run_id,
                "task_id": run.task_id,
                "status": run.status,
                "session_id": run.session_id,
                "thread_id": run.thread_id,
                "turn_id": run.turn_id,
                "resume_of": run.resume_of,
                "events_path": str(run.events_path),
                "pid": run.process.pid if run.process else None,
                "started_at": run.started_at,
                "cancel_state": run.cancel_state,
                "native_cancel_ack": False,
                "local_signal_only": False,
            }

    # -- notifications ----------------------------------------------------

    def _drain(self, run: _Run) -> None:
        if run.rpc is None:
            return
        notifications, fatal, _closed = run.rpc.drain()
        for message in notifications:
            self._apply_notification(run, message)
        if fatal:
            with run.lock:
                if not run.error_flag:
                    self._append_error_locked(run, "protocol", fatal)
                    run.protocol_error = True
                    self._write_record(
                        run, {"type": "managed_protocol_error", "error": fatal}
                    )
            _terminate_process(run.process, grace=0.5)

    def _apply_notification(self, run: _Run, message: dict) -> None:
        method = message.get("method")
        method = _text(str(method or ""), limit=80, prompt=run.redaction_prompt)
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        with run.lock:
            if run.finished:
                return
            record: dict = {"type": "notification", "method": method}
            status = None
            if method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                status = (
                    turn.get("status")
                    or params.get("status")
                    or "completed"
                )
                status = _text(str(status), limit=32, prompt=run.redaction_prompt)
                turn_id = (
                    turn.get("id")
                    or params.get("turnId")
                    or params.get("turnID")
                    or turn.get("turnId")
                )
                if isinstance(turn_id, str) and turn_id:
                    run.turn_id = _text(
                        turn_id, limit=MAX_SESSION_ID, prompt=run.redaction_prompt
                    )
                text = self._extract_text(params)
                if text:
                    text = _text(
                        text, limit=MAX_TEXT, prompt=run.redaction_prompt
                    )
                    if text:
                        run.text = _text(
                            (run.text + text)[-MAX_TEXT:],
                            limit=MAX_TEXT,
                            prompt=run.redaction_prompt,
                        )
                run.turn_done = True
                run.turn_status = status
                record["status"] = status
                if run.turn_id:
                    record["turn_id"] = run.turn_id
                lowered = (status or "").lower()
                if lowered in ("failed", "error"):
                    self._append_error_locked(
                        run, "turn", self._extract_error(params) or "turn failed"
                    )
                elif lowered in ("cancelled", "canceled", "interrupted"):
                    if not run.cancel_requested:
                        self._append_error_locked(
                            run, "turn", "turn cancelled by provider"
                        )
                elif lowered not in ("", "completed", "complete", "succeeded", "success"):
                    self._append_error_locked(
                        run, "turn", f"unknown turn status: {status}"
                    )
            elif method in ("turn/failed", "turn/error", "error"):
                status = "failed"
                run.turn_done = True
                run.turn_status = "failed"
                self._append_error_locked(
                    run,
                    "turn" if method != "error" else "provider",
                    self._extract_error(params) or "provider returned an error",
                )
                record["status"] = "failed"
            elif method == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage")
                usage = usage if isinstance(usage, dict) else {}
                total = usage.get("total")
                total = total if isinstance(total, dict) else {}
                values = {
                    "input": _finite_number(total.get("inputTokens"), integer=True),
                    "output": _finite_number(total.get("outputTokens"), integer=True),
                    "reasoning": _finite_number(
                        total.get("reasoningOutputTokens"), integer=True),
                    "cache_read": _finite_number(
                        total.get("cachedInputTokens"), integer=True),
                    "cache_write": _finite_number(
                        total.get("cacheWriteInputTokens"), integer=True),
                }
                if any(values.values()):
                    run.tokens.update(values)
                    run.tokens["total"] = _finite_number(
                        total.get("totalTokens"), integer=True)
                    record["token_usage"] = dict(run.tokens)
                record["status"] = "observed"
            elif method in ("item/completed", "item/started", "turn/started"):
                text = self._extract_text(params)
                if text:
                    text = _text(
                        text, limit=MAX_TEXT, prompt=run.redaction_prompt
                    )
                    if text:
                        run.text = _text(
                            (run.text + text)[-MAX_TEXT:],
                            limit=MAX_TEXT,
                            prompt=run.redaction_prompt,
                        )
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                item_type = item.get("type")
                if (method == "item/started"
                        and item_type in ("commandExecution", "fileChange",
                                          "mcpToolCall", "webSearch")):
                    call_id = item.get("id")
                    call_id = (_text(str(call_id), limit=80,
                                     prompt=run.redaction_prompt, secrets=True)
                               if call_id is not None else None)
                    if (len(run.tools) < MAX_TOOLS
                            and (call_id is None or all(
                                tool.get("call_id") != call_id
                                for tool in run.tools))):
                        run.tools.append({
                            "name": str(item_type)[:64],
                            "tool": str(item_type)[:64],
                            "status": "started",
                            "call_id": call_id,
                        })
                record["status"] = "observed"
            elif method is not None:
                self._warn(run, f"unknown notification: {method}")
            self._write_record(run, record)
            run.updated_at = time.time()
        run.wake.set()

    @staticmethod
    def _extract_text(params: dict) -> str | None:
        candidates = []
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        candidates.extend(
            [
                params.get("text"),
                params.get("output"),
                turn.get("text"),
                turn.get("output"),
                item.get("text"),
                item.get("output"),
            ]
        )
        for container in (params.get("items"), turn.get("items")):
            if isinstance(container, list):
                for entry in container[-16:]:
                    if isinstance(entry, dict):
                        candidates.append(entry.get("text"))
                        candidates.append(entry.get("output"))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    @staticmethod
    def _extract_error(params: dict) -> str | None:
        error = params.get("error") if isinstance(params.get("error"), dict) else {}
        for candidate in (
            error.get("message"),
            params.get("message"),
            params.get("reason"),
        ):
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    # -- lifecycle --------------------------------------------------------

    def _shutdown_process(
        self, run: _Run, grace: float = 1.0, *, graceful: bool = True
    ) -> bool:
        if run.rpc is not None:
            run.rpc.close_stdin()
        if graceful and run.process is not None:
            try:
                run.process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        if run.rpc is not None:
            run.rpc.close()
        terminated = _terminate_process(
            run.process, grace=grace if not graceful else 0.2
        )
        self._wait_for_rpc_threads(run)
        if run.process is not None:
            code = run.process.poll()
            if code is not None:
                with run.lock:
                    run.exit_code = code
        return terminated

    def _wait_for_rpc_threads(self, run: _Run) -> None:
        if run.rpc is None:
            return
        for thread in run.rpc._threads:
            thread.join(timeout=DRAIN_SECONDS)

    def _finalize(self, run: _Run, *, reason: str | None = None) -> None:
        with run.lock:
            if run.finished:
                return
            status = None
            if run.turn_done:
                lowered = (run.turn_status or "").lower()
                if run.error_flag:
                    status = "failed"
                elif lowered in ("interrupted", "cancelled", "canceled"):
                    status = "cancelled" if run.cancel_requested else "failed"
                elif lowered in ("failed", "error"):
                    status = "failed"
                else:
                    status = "completed"
            elif run.cancel_requested:
                status = "cancelled"
            else:
                status = "failed"
                if not run.error_flag:
                    detail = reason or "app-server ended before turn/completed"
                    self._append_error_locked(run, "protocol", detail)
                run.protocol_error = True
            run.status = status
            self._finish_common(
                run, run.process.poll() if run.process else None
            )

    def poll(self, handle: dict, *, timeout: float = 0) -> dict:
        run = self._run_for(handle)
        timeout = _positive_timeout(timeout)
        deadline = time.monotonic() + timeout
        while True:
            self._drain(run)
            with run.lock:
                if run.finished:
                    return self._snapshot(run)
                if run.turn_done or run.error_flag or run.protocol_error:
                    finalize = True
                elif run.process is not None and run.process.poll() is not None:
                    finalize = True
                else:
                    finalize = False
            if finalize:
                self._finalize(run)
                self._shutdown_process(run)
                return self._snapshot(run)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._snapshot(run)
            if run.rpc is not None:
                run.rpc.wait_activity(remaining)

    def cancel(self, handle: dict, *, grace: float = 5.0) -> dict:
        run = self._run_for(handle)
        grace = _positive_timeout(grace, name="grace")
        with run.lock:
            if run.finished or (
                run.process is not None and run.process.poll() is not None
            ):
                self._drain(run)
                self._finalize(run)
                return {
                    "requested": False,
                    "terminated": bool(
                        run.process is not None and run.process.poll() is not None
                    ),
                    "native_cancel_ack": False,
                    "local_signal_only": False,
                }
            run.cancel_requested = True
            run.cancel_state = "interrupt_requested"
        params = {}
        if run.thread_id:
            params["threadId"] = run.thread_id
        if run.turn_id:
            params["turnId"] = run.turn_id
        try:
            if run.rpc is not None:
                run.rpc.request(
                    "turn/interrupt",
                    params,
                    timeout=max(0.1, min(grace, 10.0)),
                )
                with run.lock:
                    run.cancel_state = "interrupt_sent"
        except ManagedError as exc:
            with run.lock:
                self._append_error_locked(run, "cancel", str(exc))
                run.cancel_state = "interrupt_failed"

        deadline = time.monotonic() + grace
        while not run.turn_done:
            self._drain(run)
            if run.turn_done:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if run.rpc is not None:
                run.rpc.wait_activity(remaining)

        if not run.turn_done:
            with run.lock:
                run.cancel_state = "interrupt_no_ack"
            self._shutdown_process(
                run, grace=min(1.0, max(0.1, grace)), graceful=False
            )
        else:
            self._drain(run)
        self._finalize(run)
        if run.turn_done:
            self._shutdown_process(run)
        return {
            "requested": True,
            "terminated": bool(
                run.process is not None and run.process.poll() is not None
            ),
            "native_cancel_ack": False,
            "local_signal_only": False,
        }

    def session_state(self, handle: dict) -> dict:
        return self._snapshot(self._run_for(handle))

    def _snapshot(self, run: _Run) -> dict:
        with run.lock:
            return {
                "provider": self.provider,
                "run_id": run.run_id,
                "task_id": run.task_id,
                "model": run.model,
                "session_id": run.session_id,
                "thread_id": run.thread_id,
                "turn_id": run.turn_id,
                "turn_status": run.turn_status,
                "resume_of": run.resume_of,
                "status": run.status,
                "pid": run.process.pid if run.process else None,
                "started_at": run.started_at,
                "updated_at": run.updated_at,
                "finished_at": run.finished_at,
                "events_path": str(run.events_path),
                "exit_code": run.exit_code,
                "error": self._error_text(run),
                "cancel_state": run.cancel_state,
                "native_cancel_ack": False,
                "local_signal_only": False,
                "text": run.text,
                "final_text": run.text,
                "tools": [dict(tool) for tool in run.tools],
                "tool_count": len(run.tools),
                "tokens": {
                    **dict(run.tokens),
                    _CACHE_KEY: {
                        "read": run.tokens["cache_read"],
                        "write": run.tokens["cache_write"],
                    },
                },
                "cost": None,
                "unknown_notifications": (
                    run.rpc.unknown_count() if run.rpc is not None else 0
                ),
                "errors": [dict(error) for error in run.errors],
                "stderr": _text(
                    run.rpc.stderr_tail if run.rpc is not None else "",
                    limit=MAX_STDERR,
                    prompt=run.redaction_prompt,
                    tail=True,
                    secrets=True,
                )
                or "",
                "warnings": list(run.warnings),
            }

    def close(self, handle: dict | None = None) -> None:
        if handle is not None:
            runs = [self._run_for(handle)]
        else:
            with self._runs_lock:
                runs = list(self._runs.values())
        for run in runs:
            if run.finished:
                continue
            try:
                if run.turn_done:
                    self._finalize(run)
                else:
                    self._shutdown_process(run, grace=1.0)
                    with run.lock:
                        run.cancel_state = "closed"
                    self._finalize(run)
                if run.process is not None and run.process.poll() is None:
                    self._shutdown_process(run, grace=1.0)
            except (ManagedError, OSError):
                self._shutdown_process(run, grace=1.0)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def adapter_for(provider: str, root: Path, **kwargs):
    """Return a managed adapter for ``provider`` (``opencode`` or ``codex``)."""
    normalized = (provider or "").strip().lower() if isinstance(provider, str) else ""
    if normalized == "opencode":
        return OpenCodeAdapter(root, **kwargs)
    if normalized == "codex":
        return CodexAdapter(root, **kwargs)
    raise ManagedError(f"unsupported managed provider: {provider!r}")
