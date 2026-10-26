"""Contract tests for the V7.3 managed execution adapters.

OpenCode tests use a fake ``opencode`` executable (a Python script) that emits
the verified JSONL shapes. Codex tests use a fake ``app-server`` speaking
bounded JSON-RPC over stdin/stdout. Neither claims live provider coverage; the
single live OpenCode test is skipped unless ``AO_LIVE_OPENCODE=1``.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools.managed_exec import (
    CodexAdapter,
    ManagedError,
    OpenCodeAdapter,
    adapter_for,
)


FAKE_OPENCODE = r'''#!{python}
import json
import os
import sys
import time


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


mode = os.environ.get("FAKE_OPENCODE_MODE", "success")
log_path = os.environ.get("FAKE_OPENCODE_ARGV_LOG")
argv = sys.argv[1:]
if log_path:
    with open(log_path, "w", encoding="utf-8") as handle:
        json.dump(argv, handle)
prompt = argv[-1] if argv else ""
session = "ses_fake_new"
if "--session" in argv:
    index = argv.index("--session")
    if index + 1 < len(argv):
        session = argv[index + 1]
if mode == "resume":
    session = "ses_fake_resumed"

# Deliberately echo the prompt to stderr: the adapter must redact it.
sys.stderr.write("fake stderr echo: " + prompt + "\n")
sys.stderr.flush()

if mode == "nonzero":
    emit({{"type": "step_start", "sessionID": session,
          "part": {{"type": "step-start", "sessionID": session}}}})
    emit({{"type": "text", "sessionID": session,
          "part": {{"type": "text", "text": "partial"}}}})
    emit({{"type": "step_finish", "sessionID": session,
          "part": {{"cost": 0.0001,
                    "tokens": {{"input": 1, "output": 1}}}}}})
    sys.exit(7)

if mode == "error":
    emit({{"type": "step_start", "sessionID": session,
          "part": {{"type": "step-start", "sessionID": session}}}})
    emit({{"type": "error", "sessionID": session,
          "error": {{"type": "provider_error",
                     "message": "provider rejected: " + prompt}}}})
    sys.exit(0)

if mode == "cancel":
    emit({{"type": "step_start", "sessionID": session,
          "part": {{"type": "step-start", "sessionID": session}}}})
    time.sleep(60)
    sys.exit(0)

emit({{"type": "step_start", "sessionID": session,
      "part": {{"type": "step-start", "sessionID": session}}}})
emit({{"type": "tool_use", "sessionID": session,
      "part": {{"id": "call_1", "sessionID": session, "type": "tool",
               "tool": "write",
               "state": {{"status": "completed",
                         "input": {{"path": "/tmp/secret", "content": prompt}},
                         "output": "wrote " + prompt,
                         "metadata": {{"rawInput": prompt}},
                         "time": {{"start": 1000, "end": 1250}}}}}}}})
emit({{"type": "tool_use", "sessionID": session,
      "part": {{"id": "call_2", "sessionID": session, "type": "tool",
               "tool": "read",
               "state": {{"status": "completed",
                         "input": {{"command": "cat SECRET_COMMAND"}},
                         "time": {{"start": 1300, "end": 1320}}}}}}}})
emit({{"type": "step_finish", "sessionID": session,
      "part": {{"cost": 0.0014,
                "tokens": {{"input": 100, "output": 20, "reasoning": 5,
                          "cache": {{"read": 10, "write": 2}}}}}}}})
emit({{"type": "step_finish", "sessionID": session,
      "part": {{"cost": 0.0006,
                "tokens": {{"input": 50, "output": 10, "reasoning": 0,
                          "cache": {{"read": 4, "write": 0}}}}}}}})
final_text = "FINAL_OK " + prompt if mode == "echo_text" else "FINAL_OK"
emit({{"type": "text", "sessionID": session,
      "part": {{"type": "text", "text": final_text}}}})
time.sleep(0.02)
'''


FAKE_CODEX = r'''#!{python}
import json
import os
import sys


mode = os.environ.get("FAKE_CODEX_MODE", "success")
log_path = os.environ.get("FAKE_CODEX_METHOD_LOG")
thread_id = "th_fake_1"
turn_id = "tu_fake_1"


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def log_method(method):
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(str(method) + "\n")


def respond(message_id, result):
    emit({{"jsonrpc": "2.0", "id": message_id, "result": result}})


def notify(method, params):
    emit({{"jsonrpc": "2.0", "method": method, "params": params}})


for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    message = json.loads(raw)
    method = message.get("method")
    message_id = message.get("id")
    if message_id is None:
        continue
    log_method(method)
    if method == "initialize":
        if mode == "init_error":
            emit({{"jsonrpc": "2.0", "id": message_id,
                  "error": {{"code": -32000, "message": "init boom"}}}})
        else:
            respond(message_id, {{"userAgent": "fake-codex/0.0"}})
    elif method == "thread/start":
        respond(message_id, {{"thread": {{"id": thread_id}}}})
    elif method == "thread/resume":
        params = message.get("params") or {{}}
        respond(message_id,
                {{"thread": {{"id": params.get("threadId", thread_id)}}}})
    elif method == "turn/start":
        respond(message_id, {{"turn": {{"id": turn_id}}}})
        if mode == "success":
            notify("item/started", {{
                "threadId": thread_id, "turnId": turn_id,
                "item": {{"id": "cmd_fake_1", "type": "commandExecution"}}}})
            notify("thread/tokenUsage/updated", {{
                "threadId": thread_id, "turnId": turn_id,
                "tokenUsage": {{"total": {{
                    "inputTokens": 100, "outputTokens": 20,
                    "reasoningOutputTokens": 5, "cachedInputTokens": 7,
                    "cacheWriteInputTokens": 3, "totalTokens": 135
                }}}}}})
            notify("turn/completed", {{
                "threadId": thread_id, "turnId": turn_id,
                "turn": {{"id": turn_id, "status": "completed",
                          "output": "DONE_CODEX"}}}})
        elif mode == "unknown":
            notify("vendor/unknown", {{"note": "ignored"}})
            notify("turn/completed", {{
                "threadId": thread_id, "turnId": turn_id,
                "turn": {{"id": turn_id, "status": "completed",
                          "output": "DONE_CODEX"}}}})
        elif mode == "crash":
            sys.exit(9)
        elif mode == "huge":
            sys.stdout.write("X" * (1024 * 1024 + 2048) + "\n")
            sys.stdout.flush()
        elif mode == "cancel":
            pass
    elif method == "turn/interrupt":
        respond(message_id, {{"ok": True}})
        if mode == "cancel":
            notify("turn/completed", {{
                "threadId": thread_id, "turnId": turn_id,
                "turn": {{"id": turn_id, "status": "interrupted"}}}})
    else:
        emit({{"jsonrpc": "2.0", "id": message_id,
              "error": {{"code": -32601, "message": "unknown method"}}}})
'''


def _write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


@unittest.skipIf(os.name == 'nt', 'fake CLI fixtures use POSIX shebang execution; Windows product binaries are .exe')
class ManagedExecTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.opencode = self.root / "bin" / "opencode"
        self.codex = self.root / "bin" / "codex"
        _write_executable(
            self.opencode, FAKE_OPENCODE.format(python=sys.executable)
        )
        _write_executable(self.codex, FAKE_CODEX.format(python=sys.executable))
        self.argv_log = self.root / "opencode-argv.json"
        self.method_log = self.root / "codex-methods.txt"
        self.prompt = "PROMPT_SECRET_MARKER_93741 please do the thing"

    def _adapter(self, mode="success", *, extra_env=None):
        env = {
            "FAKE_OPENCODE_MODE": mode,
            "FAKE_OPENCODE_ARGV_LOG": str(self.argv_log),
            "HARMLESS_TEST_ENV": "ENV_SECRET_MARKER_55221",
        }
        env.update(extra_env or {})
        return OpenCodeAdapter(
            self.root,
            binary=str(self.opencode),
            model="fake/model",
            env=env,
        )

    def _codex_adapter(self, mode="success"):
        return CodexAdapter(
            self.root,
            binary=str(self.codex),
            env={
                "FAKE_CODEX_MODE": mode,
                "FAKE_CODEX_METHOD_LOG": str(self.method_log),
            },
        )

    # -- OpenCode ---------------------------------------------------------

    def test_opencode_capabilities_are_conservative(self):
        caps = self._adapter().capabilities()
        self.assertEqual(caps["provider"], "opencode")
        self.assertIs(caps["managed_session"], True)
        self.assertIs(caps["event_stream"], True)
        self.assertIs(caps["tool_activity"], True)
        self.assertIs(caps["completion"], True)
        self.assertIs(caps["resume"], True)
        self.assertIs(caps["native_cancel_ack"], False)
        self.assertIs(caps["exact_background_stop"], False)
        self.assertIs(caps["terminal_stream"], False)

    def test_available_requires_an_executable(self):
        self.assertTrue(self._adapter().available())
        missing = OpenCodeAdapter(
            self.root, binary=str(self.root / "missing" / "opencode")
        )
        self.assertFalse(missing.available())
        with self.assertRaises(ManagedError):
            missing.start(
                task_id="task", prompt=self.prompt, cwd=self.work
            )

    def test_success_streams_events_usage_and_session(self):
        adapter = self._adapter()
        handle = adapter.start(
            task_id="task-success",
            prompt=self.prompt,
            cwd=self.work,
            title="Success run",
            agent="build",
        )
        state = adapter.poll(handle, timeout=10)
        self.assertEqual(state["status"], "completed", state)
        self.assertEqual(state["exit_code"], 0)
        self.assertEqual(state["session_id"], "ses_fake_new")
        self.assertEqual(state["text"], "FINAL_OK")
        self.assertEqual(state["tokens"]["input"], 150)
        self.assertEqual(state["tokens"]["output"], 30)
        self.assertEqual(state["tokens"]["reasoning"], 5)
        self.assertEqual(state["tokens"]["cache_read"], 14)
        self.assertEqual(state["tokens"]["cache_write"], 2)
        self.assertEqual(state["tokens"]["cache"], {"read": 14, "write": 2})
        self.assertAlmostEqual(state["cost"], 0.002, places=9)
        self.assertEqual(state["steps"], 2)
        self.assertEqual(state["tool_count"], 2)
        self.assertIs(state["native_cancel_ack"], False)
        self.assertIs(state["local_signal_only"], True)
        self.assertNotIn(self.prompt, json.dumps(handle))
        self.assertNotIn("ENV_SECRET_MARKER_55221", json.dumps(handle))

        args = json.loads(self.argv_log.read_text(encoding="utf-8"))
        self.assertEqual(args[0], "run")
        self.assertEqual(args[1:3], ["--format", "json"])
        self.assertIn("--standalone", args)
        self.assertEqual(args[args.index("--title") + 1], "Success run")
        self.assertEqual(args[args.index("--model") + 1], "fake/model")
        self.assertEqual(args[args.index("--agent") + 1], "build")
        self.assertNotIn("--session", args)
        self.assertEqual(args[-1], self.prompt)

    def test_tool_metadata_has_no_command_or_content(self):
        adapter = self._adapter()
        handle = adapter.start(
            task_id="task-tools", prompt=self.prompt, cwd=self.work
        )
        state = adapter.poll(handle, timeout=10)
        self.assertEqual(
            [(tool["name"], tool["status"]) for tool in state["tools"]],
            [("write", "completed"), ("read", "completed")],
        )
        self.assertEqual(state["tools"][0]["call_id"], "call_1")
        self.assertEqual(state["tools"][0]["duration_ms"], 250)
        for tool in state["tools"]:
            for forbidden in ("input", "output", "command", "content", "metadata"):
                self.assertNotIn(forbidden, tool)
        persisted = state["events_path"]
        payload = Path(persisted).read_text(encoding="utf-8")
        self.assertNotIn(self.prompt, payload)
        self.assertNotIn("ENV_SECRET_MARKER_55221", payload)
        self.assertNotIn("SECRET_COMMAND", payload)
        self.assertNotIn('"rawInput"', payload)
        self.assertNotIn('"metadata"', payload)
        for line in payload.splitlines():
            record = json.loads(line)
            if record.get("type") != "tool_use":
                continue
            state = record["part"].get("state", {})
            self.assertEqual(set(state), {"status"})
            self.assertNotIn("input", state)
            self.assertNotIn("output", state)

    def test_nonzero_exit_is_failed(self):
        adapter = self._adapter("nonzero")
        handle = adapter.start(
            task_id="task-nonzero", prompt=self.prompt, cwd=self.work
        )
        state = adapter.poll(handle, timeout=10)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["exit_code"], 7)
        self.assertEqual(state["text"], "partial")
        self.assertIn("exit code 7", state["error"])
        self.assertNotIn(self.prompt, json.dumps(state))

    def test_error_event_fails_even_with_zero_exit(self):
        adapter = self._adapter("error")
        handle = adapter.start(
            task_id="task-error", prompt=self.prompt, cwd=self.work
        )
        state = adapter.poll(handle, timeout=10)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["exit_code"], 0)
        self.assertEqual(state["errors"][0]["type"], "provider_error")
        self.assertNotIn(self.prompt, state["errors"][0]["message"])
        payload = Path(state["events_path"]).read_text(encoding="utf-8")
        self.assertNotIn(self.prompt, payload)
        self.assertNotIn(self.prompt, json.dumps(state))

    def test_resume_passes_session_flag_and_parses_new_session(self):
        adapter = self._adapter("resume")
        handle = adapter.start(
            task_id="task-resume",
            prompt=self.prompt,
            cwd=self.work,
            resume="ses_existing123",
        )
        state = adapter.poll(handle, timeout=10)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["resume_of"], "ses_existing123")
        self.assertEqual(state["session_id"], "ses_fake_resumed")
        args = json.loads(self.argv_log.read_text(encoding="utf-8"))
        self.assertEqual(args[args.index("--session") + 1], "ses_existing123")
        self.assertEqual(args[-1], self.prompt)

    def test_cancel_is_local_only_and_bounded(self):
        adapter = self._adapter("cancel")
        handle = adapter.start(
            task_id="task-cancel", prompt=self.prompt, cwd=self.work
        )
        started = time.monotonic()
        state = adapter.poll(handle, timeout=0.3)
        elapsed = time.monotonic() - started
        self.assertEqual(state["status"], "running")
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 5.0)
        result = adapter.cancel(handle, grace=2.0)
        self.assertEqual(
            set(result),
            {"requested", "terminated", "native_cancel_ack", "local_signal_only"},
        )
        self.assertIs(result["requested"], True)
        self.assertIs(result["terminated"], True)
        self.assertIs(result["native_cancel_ack"], False)
        self.assertIs(result["local_signal_only"], True)
        final = adapter.session_state(handle)
        self.assertEqual(final["status"], "cancelled")
        self.assertNotEqual(final["cancel_state"], "none")
        self.assertNotIn(self.prompt, json.dumps(final))
        self.assertNotIn(self.prompt, Path(final["events_path"]).read_text("utf-8"))

    def test_evidence_file_is_private_jsonl_without_prompt(self):
        adapter = self._adapter()
        handle = adapter.start(
            task_id="task-evidence", prompt=self.prompt, cwd=self.work
        )
        state = adapter.poll(handle, timeout=10)
        path = Path(state["events_path"])
        self.assertTrue(path.is_file())
        self.assertIn(".auto-orchestrator", path.parts)
        self.assertIn("managed", path.parts)
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertGreater(len(lines), 0)
        for line in lines:
            record = json.loads(line)
            self.assertIsInstance(record, dict)
            if record.get("type") == "tool_use":
                self.assertEqual(set(record["part"].get("state", {})), {"status"})
        self.assertNotIn(self.prompt, json.dumps(state))
        self.assertNotIn("ENV_SECRET_MARKER_55221", json.dumps(state))

    def test_poll_zero_converges_without_busy_wait(self):
        adapter = self._adapter()
        handle = adapter.start(
            task_id="task-poll-zero", prompt=self.prompt, cwd=self.work
        )
        deadline = time.monotonic() + 5
        state = adapter.poll(handle, timeout=0)
        while state["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            state = adapter.poll(handle, timeout=0)
        self.assertEqual(state["status"], "completed", state)

    def test_final_text_echoing_prompt_is_redacted(self):
        adapter = self._adapter("echo_text")
        handle = adapter.start(
            task_id="task-echo", prompt=self.prompt, cwd=self.work
        )
        state = adapter.poll(handle, timeout=10)
        self.assertEqual(state["status"], "completed")
        self.assertNotIn(self.prompt, state["text"])
        self.assertIn("[redacted-prompt]", state["text"])
        payload = Path(state["events_path"]).read_text(encoding="utf-8")
        self.assertNotIn(self.prompt, payload)

    def test_hostile_task_id_cannot_escape_managed_directory(self):
        adapter = self._adapter()
        handle = adapter.start(
            task_id="../../etc/passwd", prompt="tiny request", cwd=self.work
        )
        state = adapter.poll(handle, timeout=10)
        path = Path(state["events_path"]).resolve()
        managed = (self.root / ".auto-orchestrator" / "managed").resolve()
        self.assertEqual(path.parent.parent, managed)
        self.assertNotIn("..", path.parts)

    def test_unknown_handle_is_rejected(self):
        adapter = self._adapter()
        with self.assertRaises(ManagedError):
            adapter.poll({"run_id": "run_missing"}, timeout=0)

    def test_adapter_factory(self):
        adapter = adapter_for(
            "opencode", self.root, binary=str(self.opencode)
        )
        self.assertIsInstance(adapter, OpenCodeAdapter)
        codex = adapter_for("codex", self.root, binary=str(self.codex))
        self.assertIsInstance(codex, CodexAdapter)
        with self.assertRaises(ManagedError):
            adapter_for("antigravity", self.root)

    # -- Codex ------------------------------------------------------------

    def test_codex_unavailable_without_binary(self):
        adapter = CodexAdapter(
            self.root, binary=str(self.root / "missing" / "codex")
        )
        self.assertFalse(adapter.available())
        with self.assertRaises(ManagedError):
            adapter.start(task_id="codex", prompt=self.prompt, cwd=self.work)

    def test_codex_rejects_invalid_sandbox_and_approval_values(self):
        with self.assertRaises(ManagedError):
            CodexAdapter(self.root, sandbox="write-everything")
        with self.assertRaises(ManagedError):
            CodexAdapter(self.root, sandbox="danger-full-access")
        with self.assertRaises(ManagedError):
            CodexAdapter(self.root, approval_policy="always")
        with self.assertRaises(ManagedError):
            CodexAdapter(self.root, approval_policy="on-request")

    def test_codex_capabilities_include_live_tool_activity(self):
        caps = self._codex_adapter().capabilities()
        self.assertEqual(caps["provider"], "codex")
        self.assertIs(caps["managed_session"], True)
        self.assertIs(caps["tool_activity"], True)
        self.assertIs(caps["completion"], True)
        self.assertIs(caps["resume"], True)
        self.assertIs(caps["native_cancel_ack"], False)
        self.assertIs(caps["exact_background_stop"], False)
        self.assertIs(caps["terminal_stream"], False)

    def test_codex_turn_completed_and_method_order(self):
        adapter = self._codex_adapter("success")
        handle = adapter.start(
            task_id="codex-success", prompt=self.prompt, cwd=self.work
        )
        state = adapter.poll(handle, timeout=10)
        self.assertEqual(state["status"], "completed", state)
        self.assertEqual(state["exit_code"], 0)
        self.assertEqual(state["thread_id"], "th_fake_1")
        self.assertEqual(state["turn_id"], "tu_fake_1")
        self.assertEqual(state["session_id"], "th_fake_1")
        self.assertEqual(state["text"], "DONE_CODEX")
        self.assertEqual(state["tool_count"], 1)
        self.assertEqual(state["tokens"]["input"], 100)
        self.assertEqual(state["tokens"]["output"], 20)
        self.assertEqual(state["tokens"]["cache_read"], 7)
        self.assertEqual(state["tokens"]["cache"]["write"], 3)
        methods = self.method_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(methods[:3], ["initialize", "thread/start", "turn/start"])
        self.assertNotIn(self.prompt, json.dumps(state))
        self.assertNotIn(
            self.prompt, Path(state["events_path"]).read_text(encoding="utf-8")
        )

    def test_codex_resume_uses_thread_resume(self):
        adapter = self._codex_adapter("success")
        handle = adapter.start(
            task_id="codex-resume",
            prompt=self.prompt,
            cwd=self.work,
            resume="th_existing",
        )
        state = adapter.poll(handle, timeout=10)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["thread_id"], "th_existing")
        self.assertEqual(state["resume_of"], "th_existing")
        methods = self.method_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(methods[:3], ["initialize", "thread/resume", "turn/start"])
        self.assertNotIn("thread/start", methods)

    def test_codex_cancel_sends_interrupt_without_native_ack(self):
        adapter = self._codex_adapter("cancel")
        handle = adapter.start(
            task_id="codex-cancel", prompt=self.prompt, cwd=self.work
        )
        state = adapter.poll(handle, timeout=0.3)
        self.assertEqual(state["status"], "running")
        result = adapter.cancel(handle, grace=2.0)
        self.assertIs(result["requested"], True)
        self.assertIs(result["native_cancel_ack"], False)
        methods = self.method_log.read_text(encoding="utf-8").splitlines()
        self.assertIn("turn/interrupt", methods)
        final = adapter.session_state(handle)
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(final["turn_status"], "interrupted")
        self.assertNotIn(self.prompt, json.dumps(final))

    def test_codex_unknown_notification_does_not_complete_early(self):
        adapter = self._codex_adapter("unknown")
        handle = adapter.start(
            task_id="codex-unknown", prompt=self.prompt, cwd=self.work
        )
        state = adapter.poll(handle, timeout=10)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["text"], "DONE_CODEX")
        self.assertTrue(
            any("vendor/unknown" in warning for warning in state["warnings"])
        )

    def test_codex_jsonrpc_error_on_initialize_is_managed_error(self):
        adapter = self._codex_adapter("init_error")
        with self.assertRaises(ManagedError) as caught:
            adapter.start(
                task_id="codex-init-error", prompt=self.prompt, cwd=self.work
            )
        self.assertIn("initialize failed", str(caught.exception))

    def test_codex_exit_without_completion_never_fabricates_success(self):
        adapter = self._codex_adapter("crash")
        handle = adapter.start(
            task_id="codex-crash", prompt=self.prompt, cwd=self.work
        )
        state = adapter.poll(handle, timeout=10)
        self.assertEqual(state["status"], "failed")
        self.assertIn("ended before turn/completed", state["error"])
        self.assertNotIn(self.prompt, json.dumps(state))

    def test_codex_oversized_message_is_bounded(self):
        adapter = self._codex_adapter("huge")
        handle = adapter.start(
            task_id="codex-huge", prompt=self.prompt, cwd=self.work
        )
        state = adapter.poll(handle, timeout=10)
        self.assertEqual(state["status"], "failed")
        self.assertIn("oversized JSON-RPC message", state["error"])

    # -- Live (opt-in only) ----------------------------------------------

    @unittest.skipUnless(
        os.environ.get("AO_LIVE_OPENCODE") == "1",
        "set AO_LIVE_OPENCODE=1 to run the real OpenCode CLI",
    )
    def test_live_opencode_reply_ok(self):
        binary = shutil.which("opencode")
        if not binary:
            self.skipTest("opencode binary not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            adapter = OpenCodeAdapter(root, binary=binary)
            model = os.environ.get(
                "AO_LIVE_OPENCODE_MODEL", "opencode-go/deepseek-v4.1-flash"
            )
            handle = adapter.start(
                task_id="live-opencode",
                prompt="Reply OK",
                cwd=work,
                title="AO live smoke",
                model=model,
            )
            state = adapter.poll(handle, timeout=120)
            self.assertEqual(state["status"], "completed", state)
            self.assertIn("OK", state["text"].upper())


if __name__ == "__main__":
    unittest.main()
