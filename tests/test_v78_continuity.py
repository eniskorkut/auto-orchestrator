"""V7.8 continuity tests: approvals, native run state and handoff recovery.

These exercise the real CLI functions and the real ``tools/ide_handoff.py``
state machine in one process (no mocks), so the ownership fence, the snapshot
and the native plan pointers are tested together.
"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools import universal_teamwork as universal

REQUEST = ("Add a short contributor note to the README and one small unit "
           "test. Keep the change documentation-only.")


class V78ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "proje"
        self.root.mkdir()
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "tests@example.invalid"),
                     ("config", "user.name", "V78 Tests")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True,
                           capture_output=True)
        (self.root / ".gitignore").write_text(".auto-orchestrator/\n",
                                              encoding="utf-8")
        (self.root / "README.md").write_text("# demo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "init"],
                       check=True, capture_output=True)

    # -- helpers ---------------------------------------------------------
    def cli(self, platform: str, *args, expect: int = 0) -> dict:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = universal.main(["--root", str(self.root), "--platform",
                                   platform, *map(str, args)])
        self.assertEqual(code, expect,
                         f"{args} -> {code}: {stderr.getvalue()[:400]}")
        text = stdout.getvalue().strip()
        return json.loads(text) if text else {}

    def request_path(self) -> str:
        from tools import teamwork_chat
        value = teamwork_chat.request_path(
            type("A", (), {"root": str(self.root)})())
        target = Path(value["absolute_path"])
        target.write_text(REQUEST, encoding="utf-8")
        return str(target)

    def native_plan(self, platform: str = "codex") -> dict:
        relative = self.request_path()
        self.cli(platform, "ide", "activate")
        plan = self.cli(platform, "plan", "--request-file", relative,
                        "--run-id", "run-cont-1", "--grill-mode", "off")
        self.assertEqual(plan["status"], "plan_ready")
        approved = self.cli(platform, "approve-plan", "--run-id",
                            "run-cont-1", "--approver", "tests")
        self.assertEqual(approved["status"], "approved")
        started = self.cli(platform, "start", "--plan", "run-cont-1")
        self.assertEqual(started["status"], "native_plan_ready")
        return started

    def transfer(self, source: str, target: str, *, next_action: str,
                 allow_dirty: bool = False) -> dict:
        args = ["ide", "prepare", "--target-platform", target,
                "--next-action", next_action, "--confirm-idle", "user"]
        if allow_dirty:
            args.append("--allow-dirty")
        prepared = self.cli(source, *args)
        handoff = prepared["result"]["handoff"]
        self.cli(source, "ide", "release", "--handoff", handoff["handoff_id"])
        accepted = self.cli(target, "ide", "accept", "--handoff",
                            handoff["handoff_id"], "--digest",
                            handoff["digest"])
        self.assertTrue(accepted["ok"])
        return handoff

    def ownership(self) -> dict:
        return json.loads(
            (self.root / ".auto-orchestrator" / "ide" /
             "ownership.json").read_text(encoding="utf-8"))

    # -- tests -----------------------------------------------------------
    def test_approved_plan_continues_in_the_new_owner(self):
        self.native_plan("codex")
        self.transfer("codex", "opencode",
                      next_action="Finish the README note and run the test")
        snapshot = self.cli("opencode", "ide", "status")["result"]["snapshot"]
        self.assertEqual(snapshot["run_id"], "run-cont-1")
        self.assertEqual(snapshot["next_action"],
                         "Finish the README note and run the test")
        self.assertTrue(snapshot["pending_tasks"],
                        "snapshot has no pending tasks for the new owner")
        continued = self.cli("opencode", "start", "--plan", "run-cont-1")
        self.assertEqual(continued["status"], "native_plan_ready")
        self.assertEqual(continued["plan_authored_by"], "codex")
        self.assertTrue(continued["handoff_continuation"])
        self.assertEqual(self.ownership()["owner"]["ide"], "opencode")

    def test_native_report_from_the_new_owner_is_evidence_only(self):
        started = self.native_plan("codex")
        self.transfer("codex", "cursor",
                      next_action="Continue the remaining task")
        plan = json.loads(Path(started["plan"]).read_text(encoding="utf-8"))
        task_id = plan["tasks"][0]["id"]
        tests = Path(self.temp.name) / "tests.json"
        tests.write_text(json.dumps([
            {"name": "unit", "status": "passed", "exit_code": 0}]),
            encoding="utf-8")
        # cursor has no CLI agent in this test; it is only the owner label.
        self.cli("cursor", "ide", "activate", expect=0)
        report = self.cli("cursor", "native-report", "--run-id", "run-cont-1",
                          "--task-id", task_id, "--status", "passed",
                          "--verdict", "passed", "--tests", str(tests))
        self.assertFalse(report["verified"])
        self.assertFalse(report["completion_recorded"])
        self.assertIsNone(report["native_session_id"])

    def test_recovery_can_discard_an_unacceptable_handoff(self):
        self.native_plan("codex")
        prepared = self.cli("codex", "ide", "prepare", "--target-platform",
                            "opencode", "--next-action", "continue",
                            "--confirm-idle", "user")
        handoff = prepared["result"]["handoff"]
        record = self.ownership()
        record["owner"]["heartbeat_at"] = (
            time.time() - record["owner"]["ttl_seconds"] - 300)
        path = self.root / ".auto-orchestrator" / "ide" / "ownership.json"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        evidence = Path(self.temp.name) / "evidence.json"
        evidence.write_text(json.dumps({"ref": "manual:lost-owner"}),
                            encoding="utf-8")
        recovered = self.cli("opencode", "ide", "recover", "--reason",
                             "owner heartbeat expired", "--evidence",
                             str(evidence), "--apply", "--discard-handoff")
        self.assertTrue(recovered["ok"])
        record = self.ownership()
        self.assertEqual(record["state"], "inactive")
        self.assertIsNone(record["handoff"])
        transition = record["last_transition"]
        self.assertEqual(transition["discarded_handoff_id"],
                         handoff["handoff_id"])
        self.assertFalse(transition["stop_ack"])
        activated = self.cli("opencode", "ide", "activate")
        self.assertTrue(activated["ok"])

    def test_recovery_without_evidence_still_fails(self):
        self.native_plan("codex")
        self.cli("codex", "ide", "prepare", "--target-platform", "opencode",
                 "--confirm-idle", "user")
        result = self.cli("cursor", "ide", "recover", "--apply",
                          expect=1)
        self.assertEqual(result, {})

    def test_accept_refusal_is_non_zero_and_names_the_drift(self):
        self.native_plan("codex")
        prepared = self.cli("codex", "ide", "prepare", "--target-platform",
                            "opencode", "--next-action", "continue",
                            "--confirm-idle", "user")
        handoff = prepared["result"]["handoff"]
        self.cli("codex", "ide", "release", "--handoff",
                 handoff["handoff_id"])
        (self.root / "drift.txt").write_text("drift\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "drift.txt"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "drift"],
                       check=True, capture_output=True)
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = universal.main(["--root", str(self.root), "--platform",
                                   "opencode", "ide", "accept", "--handoff",
                                   handoff["handoff_id"], "--digest",
                                   handoff["digest"]])
        self.assertEqual(code, 1)
        self.assertIn("HEAD", stderr.getvalue())

    def test_status_never_exposes_the_owner_token(self):
        activated = self.cli("codex", "ide", "activate")
        self.assertNotIn("owner_token", activated["result"])
        self.assertTrue(activated["ownership"]["credential_present"])
        token = universal._load_credential(self.root, "codex")["owner_token"]
        status = self.cli("cursor", "ide", "status")
        self.assertNotIn(token, json.dumps(status))
        self.assertTrue(status["result"]["owner"]["token_present"])

    def test_snapshot_carries_failed_tests_and_last_error(self):
        started = self.native_plan("codex")
        plan = json.loads(Path(started["plan"]).read_text(encoding="utf-8"))
        task_id = plan["tasks"][0]["id"]
        database = (self.root / ".auto-orchestrator" / "supervisor.sqlite3")
        with contextlib.closing(sqlite3.connect(database)) as connection:
            with connection:
                connection.execute(
                    "UPDATE tasks SET state='failed', reason=?, result=? "
                    "WHERE run_id=? AND id=?",
                    ("pytest failed", json.dumps({"tests": [
                        {"name": "tests/test_auth.py::test_refresh",
                         "status": "failed"},
                        {"name": "tests/test_auth.py::test_login",
                         "status": "passed"},
                    ]}), "run-cont-1", task_id))
        prepared = self.cli("codex", "ide", "prepare", "--target-platform",
                            "opencode", "--next-action",
                            "Fix the failing refresh test",
                            "--confirm-idle", "user")
        handoff = prepared["result"]["handoff"]
        path = (self.root / ".auto-orchestrator" / "ide" / "handoffs" /
                f"{handoff['handoff_id']}.json")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["failed_tests"],
                         ["tests/test_auth.py::test_refresh"])
        self.assertEqual(snapshot["last_error"], "pytest failed")
        self.assertEqual(snapshot["run_status"], "failed")
        self.assertIn("run:run-cont-1:failed", snapshot["blockers"])
        self.assertEqual(snapshot["pending_tasks"],
                         [task["id"] for task in plan["tasks"]
                          if task["id"] != task_id])


if __name__ == "__main__":
    unittest.main()
