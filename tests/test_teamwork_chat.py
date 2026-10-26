from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from tools import teamwork_chat as chat
from tools.teamwork import TeamworkEngine


def minimal_plan(run_id="chat-test"):
    return {
        "run_id": run_id, "max_parallel": 1, "work_mode": "fast",
        "pattern": "iterative_coding",
        "milestones": [{
            "id": "M1", "objective": "Small change", "depends_on": [],
            "acceptance": ["done"], "verification_policy": {
                "require_roles": [], "max_repair": 1}},
        ],
        "tasks": [{
            "id": "build", "role": "implementer", "milestone": "M1",
            "phase": "build", "objective": "Build", "acceptance": ["done"],
            "read_scope": [], "write_scope": ["src/"], "depends_on": [],
            "timeout_seconds": 60, "max_retries": 1, "verify_commands": [],
            "department": "backend", "provider": "opencode",
        }],
    }


class ChatBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        engine = TeamworkEngine(self.root)
        engine.init(minimal_plan())
        engine.close()
        self.active = {
            "run_id": "chat-test", "mode": "fast", "pid": 0,
            "plan": str(self.root / ".auto-orchestrator/plans/chat-test.json"),
            "log": "log", "model": None,
        }
        path = self.root / ".auto-orchestrator" / "teamwork" / "active.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.active), encoding="utf-8")
        plan_path = self.root / ".auto-orchestrator" / "plans" / "chat-test.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(minimal_plan()), encoding="utf-8")

    def _args(self, command):
        return argparse.Namespace(root=str(self.root), run_id=None,
                                  command=command)

    def test_status_agents_and_milestones_read_persisted_state(self):
        status = chat.status(self._args("status"))
        self.assertEqual(status["run_id"], "chat-test")
        self.assertEqual(status["mode"], "fast")
        self.assertEqual(status["milestones"][0]["id"], "M1")
        self.assertFalse(status["pid_alive"])
        agents = chat.agents(self._args("agents"))
        self.assertEqual(agents["agents"], [])
        milestones = chat.milestones(self._args("milestones"))
        self.assertEqual(milestones["work_mode"], "fast")

    def test_cancel_is_local_only_and_writes_flag(self):
        result = chat.cancel(self._args("cancel"))
        self.assertTrue(result["local_signal_only"])
        self.assertFalse(result["native_cancel_ack"])
        path = self.root / ".auto-orchestrator" / "teamwork" / "cancel-chat-test.json"
        self.assertTrue(path.is_file())

    def test_cancel_flag_reports_cancelled_when_owner_is_gone(self):
        chat.cancel(self._args("cancel"))
        status = chat.status(self._args("status"))
        self.assertEqual(status["status"], "cancelled")
        self.assertEqual(status["engine_status"], "ready")
        self.assertFalse(status["native_cancel_ack"] if "native_cancel_ack" in status else False)

    def test_start_refuses_a_live_duplicate_run(self):
        self.active["pid"] = __import__("os").getpid()
        path = self.root / ".auto-orchestrator" / "teamwork" / "active.json"
        path.write_text(json.dumps(self.active), encoding="utf-8")
        args = argparse.Namespace(root=str(self.root), request="x", mode="fast",
                                  run_id="new", model="fake", max_seconds=10)
        with self.assertRaises(chat.ChatError):
            chat.start(args)

    def test_resume_clears_a_stale_cancel_flag(self):
        cancel = self.root / ".auto-orchestrator" / "teamwork" / "cancel-chat-test.json"
        cancel.write_text("{}", encoding="utf-8")
        calls = []
        original_spawn = chat._spawn

        class FakeProcess:
            pid = 12345

        def fake_spawn(root, log, plan_path, *, recover, owner_token, model,
                       max_seconds):
            calls.append({"recover": recover, "plan": str(plan_path),
                          "token": owner_token})
            return FakeProcess()

        chat._spawn = fake_spawn
        original_owner = chat.owner_record
        chat.owner_record = lambda pid: {
            "pid": pid, "start_token": "fake-start", "uid": None,
            "host": "test", "platform": "test",
        }
        try:
            result = chat.resume(self._args("resume"))
        finally:
            chat._spawn = original_spawn
            chat.owner_record = original_owner
        self.assertEqual(result["status"], "resumed")
        self.assertFalse(cancel.exists())
        self.assertTrue(calls[0]["recover"])

    def test_command_file_uses_arguments_and_real_bridge(self):
        command = (Path(__file__).resolve().parents[1] / ".opencode" /
                   "commands" / "teamwork.md").read_text(encoding="utf-8")
        self.assertIn("$ARGUMENTS", command)
        self.assertIn("tools/teamwork_chat.py", command)
        self.assertIn("status", command)
        self.assertIn("cancel", command)
        self.assertIn("--request-file", command)
        self.assertNotIn('--request "', command)
        self.assertIn("untrusted", command.lower())


if __name__ == "__main__":
    unittest.main()


class ChatSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        self.requests = self.root / ".auto-orchestrator" / "teamwork" / "requests"
        self.requests.mkdir(parents=True)
        from tools.run_owner import read_process_identity
        self.identity = read_process_identity(__import__("os").getpid())
        self.assertIsNotNone(self.identity)
        self.original_spawn = chat._spawn
        self.original_owner = chat.owner_record
        self.spawn_calls = []

        class FakeProcess:
            pid = __import__("os").getpid()

            def terminate(self):
                return None

        def fake_spawn(root, log, plan_path, *, recover, owner_token, model,
                       max_seconds):
            self.spawn_calls.append({
                "root": str(root), "log": str(log), "plan": str(plan_path),
                "recover": recover, "token": owner_token, "model": model,
                "max_seconds": max_seconds,
            })
            return FakeProcess()

        self.FakeProcess = FakeProcess
        chat._spawn = fake_spawn
        chat.owner_record = lambda pid: dict(self.identity)

    def tearDown(self):
        chat._spawn = self.original_spawn
        chat.owner_record = self.original_owner

    def _start_args(self, **overrides):
        values = {
            "root": str(self.root),
            "request": None,
            "request_file": None,
            "request_stdin": False,
            "mode": "fast",
            "run_id": "secure-run",
            "model": "fake-model",
            "max_seconds": 30,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_request_file_preserves_shell_metacharacters_without_shell_use(self):
        marker = self.root / "SHOULD_NOT_EXIST"
        request = (
            "Add a page with quotes ' \" and `backticks`; $(touch "
            f"{marker}); Unicode: ĞüşiÖÇ — line one\nline two"
        )
        path = self.requests / "request-safe.md"
        # Preserve LF bytes on Windows; text-mode write would translate to CRLF.
        path.write_bytes(request.encode("utf-8"))
        result = chat.start(self._start_args(request_file=str(path)))
        self.assertEqual(result["status"], "started")
        plan = json.loads(Path(result["plan"]).read_text(encoding="utf-8"))
        self.assertEqual(plan["request"], request)
        self.assertFalse(marker.exists())
        self.assertEqual(len(self.spawn_calls), 1)
        launch = self.spawn_calls[0]
        self.assertFalse(any("SHOULD_NOT_EXIST" in str(value) or "touch" in str(value)
                             for value in launch.values()))
        self.assertTrue(launch["plan"].endswith("secure-run.json"))
        self.assertIn("token", launch)
        active = json.loads((self.root / ".auto-orchestrator" / "teamwork" /
                             "active.json").read_text(encoding="utf-8"))
        self.assertNotIn("request", active)
        self.assertNotIn("profile", active)
        self.assertTrue(active["owner"]["start_token"])

    def test_stdin_request_preserves_unicode_and_json(self):
        import io
        import sys
        request = '{"task": "öğrenci — login", "note": "```json\\n{}\n```; $(noop)"}'
        original = sys.stdin
        sys.stdin = io.StringIO(request)
        try:
            result = chat.start(self._start_args(request_stdin=True))
        finally:
            sys.stdin = original
        plan = json.loads(Path(result["plan"]).read_text(encoding="utf-8"))
        self.assertEqual(plan["request"], request)
        self.assertEqual(len(self.spawn_calls), 1)

    def test_request_file_outside_private_directory_is_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("hello", encoding="utf-8")
        with self.assertRaises(chat.ChatError):
            chat.start(self._start_args(request_file=str(outside)))

    def test_request_file_symlink_and_invalid_utf8_are_rejected(self):
        target = self.requests / "target.md"
        target.write_text("hello", encoding="utf-8")
        link = self.requests / "link.md"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks are not available on this platform")
        with self.assertRaises(chat.ChatError):
            chat.start(self._start_args(request_file=str(link)))
        bad = self.requests / "bad.md"
        bad.write_bytes(b"\xff\xfe")
        with self.assertRaises(chat.ChatError):
            chat.start(self._start_args(request_file=str(bad)))

    def test_empty_nul_and_too_long_requests_are_rejected(self):
        empty = self.requests / "empty.md"
        empty.write_text("", encoding="utf-8")
        with self.assertRaises(chat.ChatError):
            chat.start(self._start_args(request_file=str(empty)))
        with self.assertRaises(chat.ChatError):
            chat.start(self._start_args(request="a\x00b"))
        too_long = self.requests / "long.md"
        too_long.write_text("x" * (chat.MAX_REQUEST + 1), encoding="utf-8")
        with self.assertRaises(chat.ChatError):
            chat.start(self._start_args(request_file=str(too_long)))

    def test_run_id_path_traversal_is_rejected(self):
        for run_id in ("../escape", "a/b", "..", "x" * 81):
            with self.assertRaises(chat.ChatError):
                chat.start(self._start_args(request="x", run_id=run_id))

    def test_concurrent_start_allows_only_one_live_owner(self):
        import threading

        results = []
        errors = []

        def attempt(run_id):
            try:
                results.append(chat.start(self._start_args(
                    request="build a tiny page", run_id=run_id)))
            except Exception as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=attempt, args=(f"run-{index}",))
                   for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], chat.ChatError)
        self.assertIn("duplicate", str(errors[0]))

    def test_resume_refuses_live_owner_and_pid_reuse_is_disconnected(self):
        path = self.root / ".auto-orchestrator" / "teamwork" / "active.json"
        plan = self.root / ".auto-orchestrator" / "plans" / "secure-run.json"
        plan.parent.mkdir(parents=True)
        plan.write_text(json.dumps(minimal_plan("secure-run")), encoding="utf-8")
        engine = TeamworkEngine(self.root)
        try:
            engine.init(minimal_plan("secure-run"))
        finally:
            engine.close()
        active = {
            "version": 2, "run_id": "secure-run", "mode": "fast",
            "plan": str(plan), "log": "log", "model": None,
            "owner": dict(self.identity), "pid": self.identity["pid"],
            "heartbeat_at": __import__("time").time(),
        }
        path.write_text(json.dumps(active), encoding="utf-8")
        result = chat.resume(argparse.Namespace(root=str(self.root),
                                                run_id=None))
        self.assertEqual(result["status"], "already_running")

        active["owner"] = {**self.identity, "start_token": "reused"}
        path.write_text(json.dumps(active), encoding="utf-8")
        status = chat.status(argparse.Namespace(root=str(self.root),
                                                run_id=None))
        self.assertFalse(status["owner_connected"])
        self.assertFalse(status["owner_process_alive"])
        self.assertEqual(status["recovery_state"], "runner_disconnected")

        active["owner"] = dict(self.identity)
        active["heartbeat_at"] = __import__("time").time() - 10000
        path.write_text(json.dumps(active), encoding="utf-8")
        status = chat.status(argparse.Namespace(root=str(self.root),
                                                run_id=None))
        self.assertFalse(status["owner_connected"])
        self.assertTrue(status["owner_process_alive"])
        self.assertEqual(status["recovery_state"], "runner_disconnected")
        resumed = chat.resume(argparse.Namespace(root=str(self.root),
                                                 run_id=None))
        self.assertEqual(resumed["status"], "already_running")
        self.assertTrue(resumed["heartbeat_stale"])
