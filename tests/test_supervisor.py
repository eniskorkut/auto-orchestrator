from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.supervisor import ROLES, Supervisor, SupervisorError

RESULT = {
    "summary": "Completed the task",
    "changed_files": ["src/app.py"],
    "tests": [{"name": "unit", "status": "passed"}],
    "findings": [],
}


def task(task_id, role="implementer", *, depends_on=None, write_scope=None,
         timeout=900):
    return {
        "id": task_id,
        "role": role,
        "objective": f"Complete {task_id}",
        "depends_on": list(depends_on or []),
        "read_scope": [],
        "write_scope": list(write_scope or ["src/"]),
        "timeout_seconds": timeout,
    }


class SupervisorRoleTests(unittest.TestCase):
    def test_v73_roles_extend_existing_roles(self):
        self.assertTrue({"explorer", "implementer", "tester", "reviewer"}
                        <= ROLES)
        self.assertTrue({"critic", "challenger", "auditor", "success-auditor"}
                        <= ROLES)

    def test_new_roles_initialize(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = Supervisor(Path(temp))
            try:
                supervisor.init("roles-run", {
                    "max_parallel": 3,
                    "tasks": [
                        task("critic", "critic", write_scope=[]),
                        task("challenger", "challenger", write_scope=[]),
                        task("auditor", "auditor", write_scope=[]),
                        task("success", "success-auditor", write_scope=[]),
                    ],
                })
                status = supervisor.status("roles-run")
            finally:
                supervisor.close()
            self.assertEqual({item["role"] for item in status["tasks"]},
                             {"critic", "challenger", "auditor",
                              "success-auditor"})
            self.assertFalse(status["complete"])

    def test_unknown_role_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = Supervisor(Path(temp))
            try:
                with self.assertRaises(SupervisorError):
                    supervisor.init("bad-role", {
                        "tasks": [task("work", "wizard")],
                    })
            finally:
                supervisor.close()


class SupervisorReopenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.supervisor = Supervisor(Path(self.temp.name))
        self.addCleanup(self.supervisor.close)

    def test_reopen_resets_completed_task_and_allows_a_new_attempt(self):
        self.supervisor.init("run", {"tasks": [task("work")]})
        first = self.supervisor.claim("run")["claimed"][0]
        self.supervisor.change("run", "work", first["lease"], "completed",
                               result=RESULT)
        output = self.supervisor.reopen("run", "work", "Repair missing evidence")
        self.assertTrue(output["reopened"])
        self.assertEqual(output["state"], "pending")
        self.assertFalse(output["native_cancellation_required"])
        row = self.supervisor.conn.execute(
            "SELECT state,lease,result,attempts,termination_confirmed "
            "FROM tasks WHERE run_id=? AND id=?", ("run", "work")).fetchone()
        self.assertEqual(row["state"], "pending")
        self.assertIsNone(row["lease"])
        self.assertIsNone(row["result"])
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["termination_confirmed"], 0)
        with self.assertRaises(SupervisorError):
            self.supervisor.reopen("run", "work", "Already pending")

        second = self.supervisor.claim("run")["claimed"][0]
        self.assertEqual(second["id"], "work")
        self.assertEqual(second["attempt"], 2)
        self.assertNotEqual(second["lease"], first["lease"])
        with self.assertRaises(SupervisorError):
            self.supervisor.change("run", "work", first["lease"], "completed",
                                   result=RESULT)
        with self.assertRaises(SupervisorError):
            self.supervisor.reopen("run", "work", "Running task")
        self.supervisor.change("run", "work", second["lease"], "completed",
                               result=RESULT)
        self.assertTrue(self.supervisor.status("run")["complete"])

    def test_reopen_failed_task_and_validate_reason(self):
        self.supervisor.init("run", {"tasks": [task("work")]})
        lease = self.supervisor.claim("run")["claimed"][0]["lease"]
        self.supervisor.change("run", "work", lease, "failed",
                               reason="Observed failure")
        output = self.supervisor.reopen("run", "work", "Retry after diagnosis")
        self.assertEqual(output["state"], "pending")
        for reason in ("", "   ", None, "x" * 1001):
            with self.subTest(reason=reason), self.assertRaises(SupervisorError):
                self.supervisor.reopen("run", "work", reason)

    def test_reopen_preserves_native_evidence_requirements(self):
        self.supervisor.init("native-run", {
            "native_supervision": True,
            "tasks": [task("work")],
        })
        first = self.supervisor.claim("native-run")["claimed"][0]
        self.supervisor.bind_native("native-run", "work", first["lease"],
                                    "codex", "native-attempt-1")
        self.supervisor.observe_native("codex", "native-attempt-1", "start")
        self.supervisor.observe_native("codex", "native-attempt-1", "stop")
        self.supervisor.change("native-run", "work", first["lease"],
                               "completed", result=RESULT)
        self.supervisor.reopen("native-run", "work", "Repair native evidence")
        second = self.supervisor.claim("native-run")["claimed"][0]
        self.assertEqual(second["attempt"], 2)
        self.supervisor.bind_native("native-run", "work", second["lease"],
                                    "codex", "native-attempt-2")
        with self.assertRaises(SupervisorError):
            self.supervisor.change("native-run", "work", second["lease"],
                                   "completed", result=RESULT)
        self.supervisor.observe_native("codex", "native-attempt-2", "start")
        self.supervisor.observe_native("codex", "native-attempt-2", "stop")
        self.supervisor.change("native-run", "work", second["lease"],
                               "completed", result=RESULT)
        status = self.supervisor.status("native-run")
        self.assertTrue(status["complete"])
        self.assertTrue(status["tasks"][0]["native_stop_observed"])


if __name__ == "__main__":
    unittest.main()
