from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools import ide_handoff as handoff
from tools.run_owner import (
    atomic_write_json,
    project_lock,
    project_writer_lease,
    read_process_identity,
    runner_lease,
)


def _git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        check=check, timeout=30)


def _make_repository(root: Path):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "handoff@example.invalid")
    _git(root, "config", "user.name", "Handoff Test")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-q", "-m", "initial")


class RecoveryTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        _make_repository(self.root)

    def ownership(self):
        return json.loads(
            handoff._ownership_path(self.root).read_text(encoding="utf-8"))

    def ownership_bytes(self):
        return handoff._ownership_path(self.root).read_bytes()

    def write_ownership(self, record):
        with project_lock(self.root):
            atomic_write_json(handoff._ownership_path(self.root), record)

    def sleeping_child(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def cleanup():
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.SubprocessError:
                pass

        self.addCleanup(cleanup)
        deadline = time.monotonic() + 10
        identity = None
        while time.monotonic() < deadline:
            identity = read_process_identity(process.pid)
            if identity is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(identity)
        return process

    def activate(self, **kwargs):
        kwargs.setdefault("session_id", "source")
        result = handoff.activate(self.root, "opencode", **kwargs)
        self.assertTrue(result["ok"], result)
        return result

    def prepare(self, active, **kwargs):
        result = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            idle_confirmation="user_confirmed", **kwargs)
        self.assertTrue(result["ok"], result)
        return result


class CrashRecoveryTests(RecoveryTestBase):
    def test_crash_after_prepare_requires_explicit_evidence(self):
        child = self.sleeping_child()
        active = self.activate(pid=child.pid, session_id="managed")
        prepared = self.prepare(active)
        child.kill()
        child.wait(timeout=10)

        status = handoff.status(self.root)
        self.assertEqual(status["liveness"]["owner"]["status"], "dead")

        escalated = handoff.recover(
            self.root, "operator", "runner process exited", apply=False)
        self.assertTrue(escalated["ok"], escalated)
        self.assertEqual(escalated["state"], "recovery_required")
        self.assertTrue(escalated["changed"])
        before = self.ownership_bytes()

        restarted = handoff.status(self.root)
        self.assertEqual(restarted["state"], "recovery_required")
        self.assertEqual(restarted["liveness"]["owner"]["status"], "dead")

        replay = handoff.recover(
            self.root, "operator", "runner process exited", apply=False)
        self.assertTrue(replay["ok"])
        self.assertTrue(replay["unchanged"])
        self.assertEqual(self.ownership_bytes(), before)

        refused_before = self.ownership_bytes()
        with self.assertRaises(handoff.HandoffError):
            handoff.recover(
                self.root, "operator", "runner process exited", apply=True)
        self.assertEqual(self.ownership_bytes(), refused_before)

        applied = handoff.recover(
            self.root, "operator", "verified crash evidence",
            evidence={"ref": "evidence:crash-42", "stop_ack": True,
                      "native_cancel_ack": True},
            apply=True)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(applied["state"], "released")
        self.assertIsNone(applied["owner"])
        record = self.ownership()
        transition = record["last_transition"]
        self.assertEqual(transition["op"], "recover_apply")
        self.assertEqual(transition["liveness"], "unknown")
        self.assertFalse(transition["stop_ack"])
        self.assertFalse(transition["native_cancel_ack"])
        self.assertIn("evidence:crash-42", transition["evidence"]["refs"])
        self.assertNotIn("stop_ack", transition["evidence"])
        self.assertFalse(record["native_cancel_ack"])
        self.assertFalse(record["stop_ack"])
        self.assertEqual(record["previous_owner"]["session_id"], "managed")
        self.assertEqual(record["handoff"]["handoff_id"],
                         prepared["handoff"]["handoff_id"])

        replay_apply = handoff.recover(
            self.root, "operator", "verified crash evidence",
            evidence={"ref": "evidence:crash-42", "stop_ack": True,
                      "native_cancel_ack": True},
            apply=True)
        self.assertTrue(replay_apply["ok"])
        self.assertTrue(replay_apply["unchanged"])
        self.assertEqual(replay_apply["fence"], applied["fence"])

        accepted = handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "codex", session_id="target")
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["state"], "transferred")

    def test_direct_apply_from_dead_active_owner(self):
        child = self.sleeping_child()
        active = self.activate(pid=child.pid)
        child.kill()
        child.wait(timeout=10)
        applied = handoff.recover(
            self.root, "operator", "crash", evidence=["log:crash"], apply=True)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(applied["state"], "inactive")
        self.assertIsNone(applied["owner"])
        self.assertEqual(handoff.status(self.root)["state"], "inactive")
        replacement = handoff.activate(self.root, "codex", session_id="new")
        self.assertTrue(replacement["ok"], replacement)

    def test_alive_owner_cannot_be_recovered(self):
        active = self.activate()
        before = self.ownership_bytes()
        result = handoff.recover(
            self.root, "operator", "not a crash", evidence=["x"], apply=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["reasons"][0]["code"], "owner_alive")
        self.assertEqual(self.ownership_bytes(), before)
        self.assertFalse(handoff.recover(
            self.root, "operator", "not a crash", apply=False)["ok"])


class UnknownLivenessTests(RecoveryTestBase):
    def test_stale_native_owner_blocks_accept_until_recovery(self):
        active = self.activate()
        prepared = self.prepare(active)
        record = self.ownership()
        record["owner"]["heartbeat_at"] = (
            time.time() - record["owner"]["ttl_seconds"] - 120)
        self.write_ownership(record)

        blocked = handoff.release(
            self.root, active["owner_token"], prepared["fence"],
            prepared["handoff"]["handoff_id"],
            idle_confirmation="user_confirmed")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reasons"][0]["code"], "liveness_unknown")

        escalated = handoff.recover(
            self.root, "operator", "native heartbeat expired", apply=False)
        self.assertTrue(escalated["ok"], escalated)
        self.assertEqual(escalated["state"], "recovery_required")
        self.assertEqual(self.ownership()["last_transition"]["liveness"],
                         "heartbeat_stale")

        applied = handoff.recover(
            self.root, "operator", "operator confirmed stopped",
            evidence={"ref": "evidence:native-stop-observation"},
            apply=True)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(applied["state"], "released")
        self.assertEqual(applied["fence"], escalated["fence"] + 1)
        self.assertIsNotNone(self.ownership()["handoff"])

        accepted = handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "codex")
        self.assertTrue(accepted["ok"], accepted)

    def test_recovery_can_explicitly_discard_unaccepted_handoff(self):
        active = self.activate()
        prepared = self.prepare(active)
        record = self.ownership()
        record["owner"]["heartbeat_at"] = (
            time.time() - record["owner"]["ttl_seconds"] - 120)
        self.write_ownership(record)
        escalated = handoff.recover(
            self.root, "operator", "native heartbeat expired", apply=False)
        self.assertEqual(escalated["state"], "recovery_required")
        applied = handoff.recover(
            self.root, "operator", "operator chose a fresh activation",
            evidence={"ref": "evidence:discard"}, apply=True,
            discard_handoff=True)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(applied["state"], "inactive")
        self.assertIsNone(self.ownership()["handoff"])
        self.assertEqual(
            self.ownership()["last_transition"]["discarded_handoff_id"],
            prepared["handoff"]["handoff_id"])
        self.assertFalse(handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "codex")["ok"])
        activated = handoff.activate(self.root, "codex", session_id="target")
        self.assertTrue(activated["ok"], activated)

    def test_pid_reuse_is_dead_but_not_permission_to_take_over(self):
        first = self.sleeping_child()
        active = self.activate(pid=first.pid)
        first.kill()
        first.wait(timeout=10)
        second = self.sleeping_child()
        record = self.ownership()
        record["owner"]["pid_identity"]["pid"] = second.pid
        record["owner"]["pid_identity"]["start_token"] = "reused-start-token"
        self.write_ownership(record)

        status = handoff.status(self.root)
        self.assertEqual(status["liveness"]["owner"]["status"], "dead")
        self.assertEqual(status["liveness"]["owner"]["reason"], "pid_reused")

        takeover = handoff.activate(self.root, "cursor")
        self.assertFalse(takeover["ok"])
        self.assertEqual(takeover["status"], "conflict")
        beat = handoff.heartbeat(
            self.root, active["owner_token"], active["fence"])
        self.assertFalse(beat["ok"])

        escalated = handoff.recover(
            self.root, "operator", "pid reuse detected", apply=False)
        self.assertTrue(escalated["ok"], escalated)
        self.assertEqual(escalated["state"], "recovery_required")
        applied = handoff.recover(
            self.root, "operator", "explicit operator decision",
            evidence={"ref": "evidence:pid-reuse"}, apply=True)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(applied["state"], "inactive")


class LeaseEvidenceTests(RecoveryTestBase):
    def test_project_writer_lease_blocks_prepare_and_accept(self):
        active = self.activate()
        with project_writer_lease(self.root):
            blocked = handoff.prepare_handoff(
                self.root, active["owner_token"], active["fence"], "codex",
                idle_confirmation="user_confirmed")
            self.assertFalse(blocked["ok"])
            self.assertIn("lease_held",
                          [item["code"] for item in blocked["reasons"]])

        prepared = self.prepare(active)
        released = handoff.release(
            self.root, active["owner_token"], prepared["fence"],
            prepared["handoff"]["handoff_id"],
            idle_confirmation="user_confirmed")
        self.assertTrue(released["ok"], released)
        with project_writer_lease(self.root):
            blocked_accept = handoff.accept(
                self.root, prepared["handoff"]["handoff_id"],
                prepared["handoff"]["digest"], "codex")
            self.assertFalse(blocked_accept["ok"])
            self.assertIn("lease_held",
                          [item["code"] for item in blocked_accept["reasons"]])

        accepted = handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "codex")
        self.assertTrue(accepted["ok"], accepted)

    def test_runner_lease_blocks_prepare(self):
        active = self.activate()
        atomic_write_json(
            self.root / ".auto-orchestrator" / "teamwork" / "active.json",
            {"run_id": "run-1", "mode": "fast"})
        with runner_lease(self.root, "run-1"):
            blocked = handoff.prepare_handoff(
                self.root, active["owner_token"], active["fence"], "codex",
                idle_confirmation="user_confirmed")
            self.assertFalse(blocked["ok"])
            self.assertIn("lease_held",
                          [item["code"] for item in blocked["reasons"]])
        prepared = self.prepare(active)
        self.assertTrue(prepared["ok"], prepared)


if __name__ == "__main__":
    unittest.main()
