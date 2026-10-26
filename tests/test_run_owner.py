from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tools.run_owner import (
    OwnershipError,
    atomic_write_json,
    identity_matches,
    lease_held,
    owner_alive,
    project_lock,
    read_process_identity,
    read_recovery_state,
    runner_lease,
    terminate_owned,
    update_active,
    write_recovery_state,
)


class ProcessIdentityTests(unittest.TestCase):
    def test_current_process_identity_is_verifiable(self):
        identity = read_process_identity(os_getpid())
        self.assertIsNotNone(identity)
        self.assertEqual(identity["pid"], os_getpid())
        self.assertTrue(identity_matches(identity))
        self.assertTrue(owner_alive(identity))

    def test_dead_or_reused_pid_does_not_match(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process.wait(timeout=10)
        self.assertFalse(identity_matches({
            "pid": process.pid, "start_token": "not-the-owner", "uid": None,
            "host": None,
        }))
        self.assertFalse(owner_alive({"pid": 0, "start_token": "x"}))
        self.assertFalse(owner_alive({"pid": os_getpid(), "start_token": "wrong"}))

    def test_owned_process_can_be_terminated_but_reused_identity_is_refused(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        identity = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            identity = read_process_identity(process.pid)
            if identity is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(identity)
        wrong = dict(identity)
        wrong["start_token"] = "reused-pid"
        refused = terminate_owned(wrong, grace=0.2)
        self.assertFalse(refused["signaled"])
        self.assertIsNone(process.poll())
        result = terminate_owned(identity, grace=5)
        self.assertTrue(result["signaled"])
        self.assertTrue(result["terminated"])
        self.assertFalse(result["native_cancel_ack"])
        process.wait(timeout=10)
        self.assertIsNotNone(process.returncode)


class ProcessGroupTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX process-group semantics only")
    def test_owned_process_tree_is_terminated_as_a_group(self):
        code = (
            "import subprocess,sys,time;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            "print(p.pid,flush=True);time.sleep(60)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        grandchild = int(child.stdout.readline().strip())
        identity = read_process_identity(child.pid)
        self.assertIsNotNone(identity)
        result = terminate_owned(identity, grace=3)
        self.assertTrue(result["signaled"])
        self.assertTrue(result["terminated"])
        self.assertTrue(result["group_terminated"])
        child.wait(timeout=10)
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            pass
        else:
            self.fail("grandchild survived owned process-group termination")


class ProjectLockTests(unittest.TestCase):
    def test_lock_is_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entered = threading.Event()
            release = threading.Event()

            def holder():
                with project_lock(root, timeout=2):
                    entered.set()
                    release.wait(5)

            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(entered.wait(2))
            with self.assertRaises(OwnershipError):
                with project_lock(root, timeout=0.2):
                    pass
            release.set()
            thread.join(5)
            with project_lock(root, timeout=2):
                pass


class RunnerLeaseTests(unittest.TestCase):
    def test_lease_distinguishes_live_and_released_runner(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertFalse(lease_held(root, "run-1"))
            with runner_lease(root, "run-1"):
                self.assertTrue(lease_held(root, "run-1"))
            self.assertFalse(lease_held(root, "run-1"))
            with self.assertRaises(OwnershipError):
                with runner_lease(root, "../escape"):
                    pass


class ActiveStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _write(self, run_id="run-1"):
        path = self.root / ".auto-orchestrator" / "teamwork" / "active.json"
        atomic_write_json(path, {"run_id": run_id, "pid": os_getpid()})
        return path

    def test_update_requires_matching_run_identity(self):
        path = self._write()
        updated = update_active(self.root, "run-1", heartbeat_at=1.0)
        self.assertEqual(updated["pid"], os_getpid())
        self.assertEqual(json.loads(path.read_text())["heartbeat_at"], 1.0)
        with self.assertRaises(OwnershipError):
            update_active(self.root, "other", heartbeat_at=2.0)
        with self.assertRaises(OwnershipError):
            update_active(self.root, "run-1", expected_token="stale",
                          heartbeat_at=3.0)

    def test_recovery_states_are_validated_and_persisted(self):
        result = write_recovery_state(self.root, "run-1", "reconciling",
                                      orphaned_tasks=["backend"])
        self.assertEqual(result["state"], "reconciling")
        stored = read_recovery_state(self.root, "run-1")
        self.assertEqual(stored["orphaned_tasks"], ["backend"])
        with self.assertRaises(OwnershipError):
            write_recovery_state(self.root, "run-1", "not-a-state")


def os_getpid():
    import os
    return os.getpid()


if __name__ == "__main__":
    unittest.main()
