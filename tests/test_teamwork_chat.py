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
        original = chat.subprocess.Popen

        class FakeProcess:
            pid = 12345

        def fake_popen(command, **kwargs):
            calls.append(command)
            return FakeProcess()

        chat.subprocess.Popen = fake_popen
        try:
            result = chat.resume(self._args("resume"))
        finally:
            chat.subprocess.Popen = original
        self.assertEqual(result["status"], "resumed")
        self.assertFalse(cancel.exists())
        self.assertIn("--recover", calls[0])

    def test_command_file_uses_arguments_and_real_bridge(self):
        command = (Path(__file__).resolve().parents[1] / ".opencode" /
                   "commands" / "teamwork.md").read_text(encoding="utf-8")
        self.assertIn("$ARGUMENTS", command)
        self.assertIn("tools/teamwork_chat.py", command)
        self.assertIn("status", command)
        self.assertIn("cancel", command)


if __name__ == "__main__":
    unittest.main()
