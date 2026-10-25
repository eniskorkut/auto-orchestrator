from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
