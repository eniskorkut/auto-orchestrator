from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.project_planner import (
    PlannerError,
    initialize,
    load_plan,
    supervisor_plan,
    validate_rich_plan,
)
from tools.supervisor import Supervisor


def base_plan():
    return {
        "run_id": "feature-1",
        "max_parallel": 2,
        "tasks": [
            {
                "id": "schema", "role": "explorer", "objective": "Map the contract",
                "acceptance": ["A dependency map exists"],
                "read_scope": ["db/"], "write_scope": [],
            },
            {
                "id": "api", "role": "implementer", "objective": "Implement the API",
                "depends_on": ["schema"], "acceptance": ["API tests pass", "Contract is documented"],
                "read_scope": ["db/"], "write_scope": ["src/"],
            },
            {
                "id": "tests", "role": "tester", "objective": "Add regression coverage",
                "depends_on": ["api"], "acceptance": ["Regression suite passes"],
                "read_scope": ["src/"], "write_scope": ["tests/"],
            },
            {
                "id": "docs", "role": "implementer", "objective": "Document the endpoint",
                "depends_on": ["api"], "acceptance": ["Docs cover the endpoint"],
                "read_scope": ["src/"], "write_scope": ["docs/"],
            },
        ],
    }


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_plan_preserves_acceptance(self):
        rich = validate_rich_plan(base_plan())
        self.assertEqual(rich["run_id"], "feature-1")
        self.assertEqual(len(rich["tasks"]), 4)
        api = next(task for task in rich["tasks"] if task["id"] == "api")
        self.assertEqual(api["acceptance"], ["API tests pass", "Contract is documented"])
        sup = supervisor_plan(rich)
        self.assertNotIn("acceptance", sup["tasks"][0])

    def test_rejects_missing_acceptance(self):
        plan = base_plan()
        plan["tasks"][0].pop("acceptance")
        with self.assertRaises(PlannerError):
            validate_rich_plan(plan)

    def test_rejects_unordered_write_overlap(self):
        plan = base_plan()
        plan["tasks"][3]["write_scope"] = ["src/"]
        with self.assertRaises(PlannerError):
            validate_rich_plan(plan)

    def test_rejects_cycle(self):
        plan = base_plan()
        plan["tasks"][0]["depends_on"] = ["tests"]
        with self.assertRaises(PlannerError):
            validate_rich_plan(plan)

    def test_rejects_unsafe_scope(self):
        plan = base_plan()
        plan["tasks"][1]["write_scope"] = ["../escape"]
        with self.assertRaises(ValueError):
            validate_rich_plan(plan)

    def test_initialize_persists_plan_and_supervisor_run(self):
        output = initialize(self.root, base_plan())
        self.assertTrue(Path(output["planner_artifact"]).is_file())
        stored = load_plan(self.root, "feature-1")
        self.assertEqual(stored["tasks"][0]["acceptance"], ["A dependency map exists"])
        supervisor = Supervisor(self.root)
        try:
            status = supervisor.status("feature-1")
        finally:
            supervisor.close()
        self.assertEqual(len(status["tasks"]), 4)
        self.assertFalse(status["native_supervision"])

    def test_duplicate_run_does_not_overwrite(self):
        initialize(self.root, base_plan())
        with self.assertRaises(ValueError):
            initialize(self.root, base_plan())

    def test_json_serializable(self):
        json.dumps(validate_rich_plan(base_plan()))

    def test_department_round_trip_through_planner_and_supervisor(self):
        plan = base_plan()
        plan["tasks"][0]["department"] = "Research"
        plan["tasks"][1]["assigned_room"] = "backend"
        plan["tasks"][2]["department"] = "qa"
        rich = validate_rich_plan(plan)
        by_id = {task["id"]: task for task in rich["tasks"]}
        self.assertEqual(by_id["schema"]["department"], "research")
        self.assertEqual(by_id["api"]["department"], "backend")
        self.assertEqual(by_id["tests"]["department"], "qa")
        self.assertIsNone(by_id["docs"]["department"])
        sup = supervisor_plan(rich)
        sup_by_id = {task["id"]: task for task in sup["tasks"]}
        self.assertEqual(sup_by_id["schema"]["department"], "research")
        self.assertEqual(sup_by_id["tests"]["department"], "qa")

        initialize(self.root, plan)
        stored = load_plan(self.root, "feature-1")
        stored_by_id = {task["id"]: task for task in stored["tasks"]}
        self.assertEqual(stored_by_id["api"]["department"], "backend")
        supervisor = Supervisor(self.root)
        try:
            status = supervisor.status("feature-1")
        finally:
            supervisor.close()
        status_by_id = {task["id"]: task for task in status["tasks"]}
        self.assertEqual(status_by_id["schema"]["department"], "research")
        self.assertEqual(status_by_id["api"]["department"], "backend")
        self.assertEqual(status_by_id["tests"]["department"], "qa")
        self.assertIsNone(status_by_id["docs"]["department"])

    def test_invalid_department_is_rejected(self):
        for value in ("backend team", "1backend", "backend_team", "x" * 41, ""):
            plan = base_plan()
            plan["tasks"][0]["department"] = value
            with self.assertRaises(PlannerError, msg=value):
                validate_rich_plan(plan)


class SupervisorDepartmentTests(unittest.TestCase):
    def test_retry_preserves_department(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = Supervisor(Path(temp))
            try:
                supervisor.init("retry-run", {
                    "max_parallel": 1,
                    "tasks": [{"id": "work", "role": "implementer",
                               "objective": "Retryable work",
                               "department": "backend",
                               "read_scope": [], "write_scope": []}],
                })
                lease = supervisor.claim("retry-run")["claimed"][0]["lease"]
                supervisor.change("retry-run", "work", lease, "failed",
                                  reason="Transient failure")
                supervisor.retry("retry-run", "work", "Retry after transient failure")
                task = supervisor.status("retry-run")["tasks"][0]
            finally:
                supervisor.close()
            self.assertEqual(task["state"], "pending")
            self.assertEqual(task["department"], "backend")


class SupervisorMigrationTests(unittest.TestCase):
    def test_old_task_table_gains_nullable_department(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / ".auto-orchestrator"
            directory.mkdir()
            database = directory / "supervisor.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.executescript("""
                CREATE TABLE runs (
                  id TEXT PRIMARY KEY, created REAL NOT NULL, max_parallel INTEGER NOT NULL);
                CREATE TABLE tasks (
                  run_id TEXT NOT NULL, id TEXT NOT NULL, role TEXT NOT NULL,
                  objective TEXT NOT NULL, dependencies TEXT NOT NULL,
                  read_scope TEXT NOT NULL, write_scope TEXT NOT NULL,
                  timeout_seconds REAL NOT NULL, max_retries INTEGER NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  state TEXT NOT NULL DEFAULT 'pending', lease TEXT, started REAL,
                  finished REAL, termination_confirmed INTEGER NOT NULL DEFAULT 0,
                  result TEXT, reason TEXT,
                  PRIMARY KEY(run_id,id), FOREIGN KEY(run_id) REFERENCES runs(id));
                """)
                connection.execute("INSERT INTO runs VALUES ('old-run', 1.0, 1)")
                connection.execute(
                    "INSERT INTO tasks (run_id,id,role,objective,dependencies,"
                    "read_scope,write_scope,timeout_seconds,max_retries,state) "
                    "VALUES ('old-run','legacy','explorer','Preserved objective',"
                    "'[]','[]','[]',900,2,'pending')")
                connection.commit()
            finally:
                connection.close()

            supervisor = Supervisor(root)
            try:
                status = supervisor.status("old-run")
                self.assertEqual(len(status["tasks"]), 1)
                self.assertEqual(status["tasks"][0]["id"], "legacy")
                self.assertIsNone(status["tasks"][0]["department"])
                claimed = supervisor.claim("old-run")["claimed"]
                self.assertEqual(claimed[0]["id"], "legacy")
                self.assertEqual(claimed[0]["objective"], "Preserved objective")
                self.assertIsNone(claimed[0]["department"])
                supervisor.init("new-run", {
                    "max_parallel": 1,
                    "tasks": [{"id": "roomed", "role": "implementer",
                               "objective": "Roomed work", "department": "Backend",
                               "read_scope": [], "write_scope": []}],
                })
                new_status = supervisor.status("new-run")
            finally:
                supervisor.close()
            self.assertEqual(new_status["tasks"][0]["department"], "backend")

            # Re-opening the migrated database is idempotent.
            reopened = Supervisor(root)
            try:
                self.assertIsNone(reopened.status("old-run")["tasks"][0]["department"])
                self.assertEqual(reopened.status("new-run")["tasks"][0]["department"],
                                 "backend")
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
