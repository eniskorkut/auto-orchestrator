from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.executor import Executor, ExecutorError
from tools.project_planner import initialize
from tools.supervisor import Supervisor


def plan():
    return {
        "run_id": "exec-1",
        "max_parallel": 2,
        "tasks": [
            {
                "id": "schema", "role": "explorer", "objective": "Map contract",
                "department": "research",
                "acceptance": ["Contract map exists"],
                "read_scope": ["db/"], "write_scope": [],
            },
            {
                "id": "api", "role": "implementer", "objective": "Implement API",
                "department": "backend",
                "depends_on": ["schema"], "acceptance": ["API test passes"],
                "read_scope": ["db/"], "write_scope": ["src/"],
            },
            {
                "id": "tests", "role": "tester", "objective": "Add tests",
                "department": "qa",
                "depends_on": ["api"], "acceptance": ["Regression suite passes"],
                "read_scope": ["src/"], "write_scope": ["tests/"],
            },
            {
                "id": "docs", "role": "implementer", "objective": "Write docs",
                "department": "frontend",
                "depends_on": ["api"], "acceptance": ["Docs cover the API"],
                "read_scope": ["src/"], "write_scope": ["docs/"],
            },
        ],
    }


def result(criterion, changed, test_name):
    return {
        "summary": f"Completed {criterion}",
        "changed_files": changed,
        "tests": [{"name": test_name, "status": "passed"}],
        "findings": [],
        "acceptance": [{"criterion": criterion, "status": "passed",
                        "evidence": f"Observed {test_name} passing"}],
    }


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        initialize(self.root, plan())
        self.executor = Executor(self.root)

    def tearDown(self):
        self.executor.close()
        self.temp.cleanup()

    def claim(self):
        output = self.executor.next("exec-1")
        self.assertTrue(output["claimed"])
        return output

    def test_next_manifest_keeps_lease_out_of_task_prompt(self):
        output = self.claim()
        self.assertTrue(Path(output["manifest"]).is_file())
        task = output["tasks"][0]
        self.assertNotIn("lease", task["task"])
        self.assertTrue(task["control"]["lease"].startswith("ao_"))
        manifest = json.loads(Path(output["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["run_id"], "exec-1")
        self.assertEqual(manifest["claimed"], ["schema"])

    def test_manifest_and_status_preserve_department(self):
        output = self.claim()
        task = output["tasks"][0]
        self.assertEqual(task["task"]["department"], "research")
        manifest = json.loads(Path(output["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["tasks"][0]["task"]["department"], "research")
        self.executor.complete("exec-1", "schema", task["control"]["lease"],
                               result("Contract map exists", [], "contract-map"))
        second = self.claim()
        self.assertEqual(second["tasks"][0]["task"]["department"], "backend")
        status = self.executor.status("exec-1")
        departments = {item["id"]: item["department"] for item in status["tasks"]}
        self.assertEqual(departments, {"schema": "research", "api": "backend",
                                       "tests": "qa", "docs": "frontend"})

    def test_completion_requires_acceptance_evidence(self):
        output = self.claim()
        task = output["tasks"][0]
        bad = result("Contract map exists", [], "explore")
        bad.pop("acceptance")
        with self.assertRaises(ExecutorError):
            self.executor.complete("exec-1", task["task"]["id"], task["control"]["lease"], bad)

    def test_implementer_needs_test_evidence(self):
        first = self.claim()
        schema = first["tasks"][0]
        self.executor.complete("exec-1", "schema", schema["control"]["lease"],
                               result("Contract map exists", [], "contract-map"))
        second = self.claim()
        api = second["tasks"][0]
        payload = result("API test passes", ["src/api.py"], "api-unit")
        payload["tests"] = []
        with self.assertRaises(ExecutorError):
            self.executor.complete("exec-1", "api", api["control"]["lease"], payload)

    def test_full_dag_completes_and_audits(self):
        first = self.claim()
        schema_lease = first["tasks"][0]["control"]["lease"]
        self.executor.complete("exec-1", "schema", schema_lease,
                               result("Contract map exists", [], "contract-map"))
        second = self.claim()
        self.assertEqual({item["task"]["id"] for item in second["tasks"]}, {"api"})
        api_lease = second["tasks"][0]["control"]["lease"]
        self.executor.complete("exec-1", "api", api_lease,
                               result("API test passes", ["src/api.py"], "api-unit"))
        third = self.claim()
        self.assertEqual({item["task"]["id"] for item in third["tasks"]}, {"tests", "docs"})
        for item in third["tasks"]:
            task_id = item["task"]["id"]
            criterion = item["task"]["acceptance"][0]
            changed = ["tests/test_api.py"] if task_id == "tests" else ["docs/api.md"]
            self.executor.complete("exec-1", task_id, item["control"]["lease"],
                                   result(criterion, changed, f"{task_id}-check"))
        status = self.executor.status("exec-1")
        self.assertTrue(status["complete"])
        self.assertTrue(all(task["acceptance_reported"] for task in status["tasks"]))
        audit = self.executor.audit("exec-1")
        self.assertTrue(audit["ok"], audit["issues"])

    def test_stale_lease_is_rejected(self):
        first = self.claim()
        task = first["tasks"][0]
        lease = task["control"]["lease"]
        self.executor.complete("exec-1", "schema", lease,
                               result("Contract map exists", [], "contract-map"))
        with self.assertRaises(ValueError):
            self.executor.complete("exec-1", "schema", lease,
                                   result("Contract map exists", [], "contract-map"))

    def test_preview_never_merges(self):
        output = self.executor.preview("exec-1")
        self.assertIn("never merges", output["note"])
        self.assertEqual(output["previews"], [])

    def test_plan_artifact_missing_is_reported(self):
        with self.assertRaises(ValueError):
            self.executor.next("missing-run")

    def test_supervisor_plan_has_no_acceptance_field(self):
        supervisor = Supervisor(self.root)
        try:
            status = supervisor.status("exec-1")
        finally:
            supervisor.close()
        self.assertEqual(len(status["tasks"]), 4)
        self.assertFalse(status["native_supervision"])


if __name__ == "__main__":
    unittest.main()
