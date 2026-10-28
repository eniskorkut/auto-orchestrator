from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools import ide_handoff as handoff
from tools.project_identity import ensure_project_identity
from tools.run_owner import atomic_write_json, project_lock, read_process_identity

REPO_ROOT = Path(__file__).resolve().parents[1]


class OwnershipTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()

    def identity(self):
        return ensure_project_identity(self.root)

    def activate(self, **kwargs):
        result = handoff.activate(self.root, "opencode", **kwargs)
        self.assertTrue(result["ok"], result)
        return result

    def ownership(self):
        return json.loads(
            handoff._ownership_path(self.root).read_text(encoding="utf-8"))

    def write_ownership(self, record):
        with project_lock(self.root):
            atomic_write_json(handoff._ownership_path(self.root), record)

    def snapshot_files(self):
        directory = handoff._snapshots_dir(self.root)
        if not directory.is_dir():
            return []
        return sorted(path.name for path in directory.glob("*.json"))


class StateMatrixTests(OwnershipTestBase):
    def test_inactive_activation_and_release_without_handoff(self):
        unknown = handoff.heartbeat(self.root, "a" * 48, 0)
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["status"], "conflict")
        self.assertEqual(handoff.status(self.root)["state"], "inactive")

        active = self.activate(session_id="source")
        self.assertEqual(active["state"], "active")
        self.assertEqual(active["fence"], 1)

        second = handoff.activate(self.root, "codex", session_id="loser")
        self.assertFalse(second["ok"])
        self.assertEqual(second["status"], "conflict")
        self.assertEqual(self.ownership()["owner"]["ide"], "opencode")

        released = handoff.release(
            self.root, active["owner_token"], active["fence"],
            idle_confirmation="user_confirmed")
        self.assertTrue(released["ok"])
        self.assertEqual(released["state"], "released")
        self.assertIsNone(released["owner"])

        again = handoff.activate(self.root, "codex", session_id="next")
        self.assertTrue(again["ok"])
        self.assertEqual(again["state"], "active")

    def test_prepared_handoff_requires_explicit_release_before_accept(self):
        active = self.activate(session_id="source")
        prepared = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            idle_confirmation="user_confirmed")
        self.assertTrue(prepared["ok"], prepared)
        self.assertEqual(prepared["state"], "handoff_prepared")
        self.assertEqual(prepared["handoff"]["source_fence"], active["fence"])

        blocked = handoff.activate(self.root, "cursor", session_id="thief")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["status"], "conflict")

        # Prepared is not released: the target must not be able to accept yet.
        premature = handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "codex", session_id="target")
        self.assertFalse(premature["ok"], premature)
        self.assertEqual(premature["status"], "blocked")
        self.assertEqual(premature["reasons"][0]["code"], "release_required")

        # The source still owns the project and can heartbeat at this fence.
        still_owner = handoff.heartbeat(
            self.root, active["owner_token"], prepared["fence"])
        self.assertTrue(still_owner["ok"], still_owner)

        released = handoff.release(
            self.root, active["owner_token"], still_owner["fence"],
            prepared["handoff"]["handoff_id"],
            idle_confirmation="user_confirmed")
        self.assertTrue(released["ok"], released)

        accepted = handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "codex", session_id="target")
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["state"], "transferred")

        heartbeat = handoff.heartbeat(
            self.root, accepted["owner_token"], accepted["fence"])
        self.assertTrue(heartbeat["ok"], heartbeat)
        self.assertEqual(heartbeat["fence"], accepted["fence"] + 1)

        old_owner = handoff.heartbeat(
            self.root, active["owner_token"], released["fence"] - 1)
        self.assertFalse(old_owner["ok"])
        self.assertEqual(old_owner["status"], "conflict")

    def test_released_handoff_accepts_and_recovery_state_is_closed(self):
        active = self.activate(session_id="source")
        prepared = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            idle_confirmation="user_confirmed")
        released = handoff.release(
            self.root, active["owner_token"], prepared["fence"],
            prepared["handoff"]["handoff_id"])
        self.assertTrue(released["ok"])
        self.assertEqual(released["state"], "released")
        self.assertIsNone(released["owner"])

        refused = handoff.activate(self.root, "cursor")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["status"], "conflict")

        accepted = handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "codex")
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["state"], "transferred")

    def test_recovery_required_blocks_all_owner_operations(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        crashed = self.activate(session_id="crash", pid=child.pid)
        prepared = handoff.prepare_handoff(
            self.root, crashed["owner_token"], crashed["fence"], "codex",
            idle_confirmation="user_confirmed")
        child.kill()
        child.wait(timeout=10)
        escalated = handoff.recover(
            self.root, "tester", "simulated crash", apply=False)
        self.assertTrue(escalated["ok"], escalated)
        self.assertEqual(escalated["state"], "recovery_required")

        self.assertFalse(handoff.activate(self.root, "codex")["ok"])
        self.assertFalse(handoff.heartbeat(
            self.root, crashed["owner_token"], prepared["fence"])["ok"])
        self.assertFalse(handoff.prepare_handoff(
            self.root, crashed["owner_token"], prepared["fence"], "codex",
            idle_confirmation="user_confirmed")["ok"])
        self.assertFalse(handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "codex")["ok"])


class CompareAndSwapTests(OwnershipTestBase):
    def test_token_and_fence_are_both_required(self):
        active = self.activate(session_id="source")
        before = handoff._ownership_path(self.root).read_bytes()

        for token, fence in (("f" * 48, active["fence"]),
                             (active["owner_token"], active["fence"] + 1)):
            result = handoff.heartbeat(self.root, token, fence)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "conflict")
            self.assertEqual(
                handoff._ownership_path(self.root).read_bytes(), before)

        beat = handoff.heartbeat(
            self.root, active["owner_token"], active["fence"])
        self.assertTrue(beat["ok"])
        self.assertEqual(beat["fence"], active["fence"] + 1)

        stale = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            idle_confirmation="user_confirmed")
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["status"], "conflict")

        prepared = handoff.prepare_handoff(
            self.root, active["owner_token"], beat["fence"], "codex",
            idle_confirmation="user_confirmed")
        self.assertTrue(prepared["ok"], prepared)

        wrong = handoff.release(
            self.root, "e" * 48, prepared["fence"])
        self.assertFalse(wrong["ok"])
        self.assertEqual(wrong["status"], "conflict")

    def test_double_prepare_release_and_accept_are_idempotent(self):
        active = self.activate(session_id="source")
        prepared = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            next_action="continue", idle_confirmation="user_confirmed")
        self.assertTrue(prepared["ok"], prepared)
        files = self.snapshot_files()
        self.assertEqual(len(files), 1)

        replay = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            next_action="continue", idle_confirmation="user_confirmed")
        self.assertTrue(replay["ok"])
        self.assertTrue(replay["unchanged"])
        self.assertEqual(replay["fence"], prepared["fence"])
        self.assertEqual(self.snapshot_files(), files)

        released = handoff.release(
            self.root, active["owner_token"], prepared["fence"],
            prepared["handoff"]["handoff_id"])
        self.assertTrue(released["ok"])
        release_replay = handoff.release(
            self.root, active["owner_token"], prepared["fence"],
            prepared["handoff"]["handoff_id"])
        self.assertTrue(release_replay["ok"])
        self.assertTrue(release_replay["unchanged"])
        self.assertEqual(release_replay["fence"], released["fence"])

        accepted = handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "codex", session_id="target")
        self.assertTrue(accepted["ok"], accepted)
        accept_replay = handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "codex", session_id="target")
        self.assertTrue(accept_replay["ok"])
        self.assertTrue(accept_replay["unchanged"])
        self.assertEqual(accept_replay["fence"], accepted["fence"])


class ConcurrencyTests(OwnershipTestBase):
    def test_concurrent_subprocess_activate_has_one_winner_and_no_artifacts(self):
        script = (
            "import json,sys\n"
            "from tools.ide_handoff import activate\n"
            "result = activate(sys.argv[1], 'opencode', session_id=sys.argv[2])\n"
            "print(json.dumps({'ok': result['ok'], 'status': result['status'],\n"
            "                  'fence': result['fence']}))\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.root), f"session-{index}"],
                cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            for index in range(2)
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(sum(1 for item in results if item["ok"]), 1)
        self.assertEqual(sum(1 for item in results if not item["ok"]), 1)
        record = self.ownership()
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["fence"], 1)
        self.assertEqual(self.snapshot_files(), [])
        ide_dir = handoff._ide_dir(self.root)
        self.assertEqual(
            [path.name for path in ide_dir.glob("*.tmp-*")], [])


class LivenessTests(OwnershipTestBase):
    def _sleeping_child(self):
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

    def test_pid_identity_detects_death_and_reuse(self):
        process = self._sleeping_child()
        active = self.activate(pid=process.pid, session_id="managed")
        live = handoff.status(self.root)["liveness"]["owner"]
        self.assertEqual(live["status"], "pid_verified")
        self.assertTrue(live["process_verified"])
        self.assertEqual(live["reason"], "pid_verified")

        process.kill()
        process.wait(timeout=10)
        dead = handoff.status(self.root)["liveness"]["owner"]
        self.assertEqual(dead["status"], "dead")

        blocked = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            idle_confirmation="user_confirmed")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reasons"][0]["code"], "owner_not_alive")

        other = self._sleeping_child()
        record = self.ownership()
        record["owner"]["pid_identity"]["pid"] = other.pid
        record["owner"]["pid_identity"]["start_token"] = "reused-pid-token"
        self.write_ownership(record)
        reused = handoff.status(self.root)["liveness"]["owner"]
        self.assertEqual(reused["status"], "dead")
        self.assertEqual(reused["reason"], "pid_reused")
        beat = handoff.heartbeat(
            self.root, active["owner_token"], active["fence"])
        self.assertFalse(beat["ok"])
        self.assertEqual(beat["reasons"][0]["code"], "owner_identity_mismatch")

    def test_native_stale_heartbeat_is_unknown_not_dead(self):
        active = self.activate(session_id="native")
        record = self.ownership()
        record["owner"]["heartbeat_at"] = (
            time.time() - record["owner"]["ttl_seconds"] - 60)
        self.write_ownership(record)

        status = handoff.status(self.root)
        self.assertEqual(status["liveness"]["owner"]["status"],
                         "heartbeat_stale")
        self.assertTrue(status["liveness"]["owner"]["unknown"])
        self.assertTrue(status["liveness"]["unknown_live"])
        self.assertEqual(status["liveness"]["owner"]["reason"], "heartbeat_stale")

        blocked = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            idle_confirmation="user_confirmed")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reasons"][0]["code"], "liveness_unknown")

        takeover = handoff.activate(self.root, "cursor")
        self.assertFalse(takeover["ok"])
        self.assertEqual(takeover["status"], "conflict")

        beat = handoff.heartbeat(
            self.root, active["owner_token"], active["fence"])
        self.assertTrue(beat["ok"], beat)
        prepared = handoff.prepare_handoff(
            self.root, active["owner_token"], beat["fence"], "codex",
            idle_confirmation="user_confirmed")
        self.assertTrue(prepared["ok"], prepared)


if __name__ == "__main__":
    unittest.main()
