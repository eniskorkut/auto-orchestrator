"""Regression tests for native OpenCode evidence arriving before binding."""
import hashlib
import tempfile
import unittest

from tools.supervisor import Supervisor, SupervisorError


def plan():
    return {
        "native_supervision": True, "max_parallel": 1,
        "tasks": [{"id": "check", "role": "tester", "objective": "Run tests",
                   "read_scope": [], "write_scope": [], "max_retries": 0}]
    }


def result():
    return {"summary": "Verified", "changed_files": [],
            "tests": [{"name": "unit", "status": "passed"}], "findings": []}


class NativeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.supervisor = Supervisor(self.temp.name)
        self.addCleanup(self.supervisor.close)
        self.supervisor.init("run-1", plan())
        self.lease = self.supervisor.claim("run-1")["claimed"][0]["lease"]
        self.digest = hashlib.sha256(b"opencode|tool-call-123").hexdigest()

    def test_stop_before_bind_is_retained_only_after_matching_start(self):
        s = self.supervisor
        self.assertFalse(s.observe_hash("opencode", self.digest, "start")["matched"])
        self.assertFalse(s.observe_hash("opencode", self.digest, "stop")["matched"])
        s.bind_native("run-1", "check", self.lease, "opencode", native_hash=self.digest)
        self.assertTrue(s.status("run-1")["tasks"][0]["native_stop_observed"])
        self.assertEqual(s.change("run-1", "check", self.lease, "completed", result=result())["state"],
                         "completed")

    def test_unpaired_stop_never_qualifies(self):
        s = self.supervisor
        s.observe_hash("opencode", self.digest, "stop")
        s.bind_native("run-1", "check", self.lease, "opencode", native_hash=self.digest)
        self.assertFalse(s.status("run-1")["tasks"][0]["native_stop_observed"])
        with self.assertRaisesRegex(SupervisorError, "Native termination"):
            s.change("run-1", "check", self.lease, "completed", result=result())

    def test_start_without_stop_still_fails_closed(self):
        s = self.supervisor
        s.observe_hash("opencode", self.digest, "start")
        s.bind_native("run-1", "check", self.lease, "opencode", native_hash=self.digest)
        self.assertFalse(s.status("run-1")["tasks"][0]["native_stop_observed"])
        with self.assertRaisesRegex(SupervisorError, "Native termination"):
            s.change("run-1", "check", self.lease, "completed", result=result())
        self.assertTrue(s.observe_hash("opencode", self.digest, "stop")["native_stop_observed"])
        self.assertEqual(s.change("run-1", "check", self.lease, "completed", result=result())["state"],
                         "completed")

    def test_hash_validation_and_no_unsupervised_buffering(self):
        s = self.supervisor
        for bad in ("", "123abc", "F" * 64):
            with self.assertRaises(SupervisorError):
                s.observe_hash("opencode", bad, "start")
        with self.assertRaises(SupervisorError):
            s.bind_native("run-1", "check", self.lease, "opencode",
                          native_id="tool-call-123", native_hash=self.digest)
        # No replays are allowed for a random event observed before a claim.
        other = hashlib.sha256(b"opencode|other-native").hexdigest()
        s.observe_hash("opencode", other, "start")
        self.assertTrue(s.conn.execute(
            "SELECT 1 FROM native_pending_starts WHERE native_hash=?", (other,)
        ).fetchone())


if __name__ == "__main__":
    unittest.main()
