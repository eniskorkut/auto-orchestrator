"""V7.9 Teamwork 3.0 plan artifact, ownership manifest and integrity policy."""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools import teamwork
from tools import teamwork_planner
from tools.teamwork_planner import TeamworkPlannerError, plan_request
from tools import teamwork_chat
from tools import universal_teamwork as universal

REQUEST = ("Refactor the backend API and the frontend client so the new "
           "pagination parameters stay compatible.")


class PlanArtifactTests(unittest.TestCase):
    def plan(self, **kwargs):
        values = dict(run_id="run-v79", model="test-model", mode="balanced")
        values.update(kwargs)
        return plan_request(REQUEST, **values)

    def test_plan_carries_scope_ownership_and_integrity(self):
        plan = self.plan(integrity_policy="demo")
        self.assertEqual(plan["plan_version"], 2)
        self.assertEqual(plan["integrity_policy"], "demo")
        scope = plan["scope_contract"]
        for key in ("objective", "in_scope", "non_goals", "requirements",
                    "deliverables", "rollback", "constraints"):
            self.assertIn(key, scope)
        self.assertTrue(scope["non_goals"])
        self.assertTrue(scope["rollback"])
        self.assertEqual(plan["ownership_conflicts"], [])
        ids = {task["id"] for task in plan["tasks"]}
        manifest = {item["task_id"] for item in plan["file_ownership"]}
        self.assertEqual(manifest, ids)
        adaptive = plan["adaptive_decision"]
        self.assertEqual(adaptive["requested_mode"], "balanced")
        self.assertIn(adaptive["chosen_mode"],
                      ("solo", "fast", "balanced", "rigorous"))
        self.assertTrue(adaptive["reason"])

    def test_unknown_integrity_policy_is_rejected(self):
        with self.assertRaises(TeamworkPlannerError):
            self.plan(integrity_policy="production")

    def test_validation_preserves_v79_fields_and_rejects_bad_policy(self):
        plan = self.plan()
        normalized = teamwork.validate_teamwork_plan(plan)
        self.assertEqual(normalized["plan_version"], 2)
        self.assertEqual(normalized["integrity_policy"], "development")
        self.assertEqual(normalized["scope_contract"]["objective"],
                         plan["scope_contract"]["objective"])
        self.assertEqual(normalized["file_ownership"], plan["file_ownership"])
        broken = dict(plan)
        broken["integrity_policy"] = "production"
        with self.assertRaises(teamwork.TeamworkError):
            teamwork.validate_teamwork_plan(broken)

    def test_scope_overlap_detects_files_and_directories(self):
        self.assertTrue(teamwork_planner._scopes_overlap(
            ["src/api.py"], ["src/"]))
        self.assertTrue(teamwork_planner._scopes_overlap(
            ["src"], ["src/api.py"]))
        self.assertFalse(teamwork_planner._scopes_overlap(
            ["src/api.py"], ["tests/"]))
        self.assertFalse(teamwork_planner._scopes_overlap([], ["src/"]))

    def test_concurrent_overlapping_tasks_are_reported(self):
        plan = {
            "tasks": [
                {"id": "a", "milestone": "M2", "depends_on": [],
                 "write_scope": ["src/api.py"], "role": "implementer"},
                {"id": "b", "milestone": "M2", "depends_on": [],
                 "write_scope": ["src/"], "role": "implementer"},
            ]
        }
        conflicts = teamwork_planner._ownership_conflicts(plan)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["left"], "a")
        self.assertEqual(conflicts[0]["right"], "b")

    def test_serialized_overlap_is_not_a_conflict(self):
        plan = {
            "tasks": [
                {"id": "a", "milestone": "M2", "depends_on": [],
                 "write_scope": ["src/"], "role": "implementer"},
                {"id": "b", "milestone": "M2", "depends_on": ["a"],
                 "write_scope": ["src/"], "role": "tester"},
            ]
        }
        self.assertEqual(teamwork_planner._ownership_conflicts(plan), [])


class PlanCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "p"
        self.root.mkdir()
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "a@b.c"),
                     ("config", "user.name", "t")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True,
                           capture_output=True)
        (self.root / ".gitignore").write_text(".auto-orchestrator/\n")
        (self.root / "README.md").write_text("x\n")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "i"],
                       check=True, capture_output=True)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = universal.main(["--root", str(self.root), "--platform",
                                   "codex", *args])
        self.assertEqual(code, 0, err.getvalue()[:400])
        return json.loads(out.getvalue())

    def test_cli_plan_records_the_integrity_policy(self):
        value = teamwork_chat.request_path(
            type("A", (), {"root": str(self.root)})())
        target = Path(value["absolute_path"])
        target.write_text(REQUEST, encoding="utf-8")
        self.cli("ide", "activate")
        result = self.cli("plan", "--request-file", str(target), "--run-id",
                          "run-cli-v79", "--grill-mode", "off",
                          "--integrity-policy", "demo")
        plan = json.loads(Path(result["plan"]).read_text(encoding="utf-8"))
        self.assertEqual(plan["integrity_policy"], "demo")
        self.assertEqual(plan["plan_version"], 2)
        self.assertTrue(plan["scope_contract"]["rollback"])


if __name__ == "__main__":
    unittest.main()
