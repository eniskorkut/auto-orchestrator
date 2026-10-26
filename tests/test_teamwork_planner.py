from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.project_planner import validate_rich_plan
from tools.teamwork import PATTERNS, TeamworkEngine, validate_teamwork_plan
from tools.teamwork_planner import (
    TeamworkPlannerError,
    plan_request,
)

PROJECT_ID = "p_" + "a" * 16


def by_milestone(plan):
    result = {}
    for task in plan["tasks"]:
        result.setdefault(task["milestone"], []).append(task)
    return result


class AdaptivePlannerTests(unittest.TestCase):
    request = "Kullanıcı giriş sistemini backend ve frontend ile geliştir, testlerini yaz."

    def _plan(self, mode, profile=None):
        return plan_request(
            self.request, run_id="adaptive", model="test-model", mode=mode,
            repo_profile=profile,
        )

    def test_fast_has_one_milestone_and_no_separate_reviewers(self):
        plan = self._plan("fast", {"files": 3, "modules": 1, "tests": 2,
                                   "backend": True, "languages": ["python"]})
        self.assertEqual(plan["work_mode"], "fast")
        self.assertEqual(len(plan["milestones"]), 1)
        roles = {task["role"] for task in plan["tasks"]}
        self.assertNotIn("reviewer", roles)
        self.assertNotIn("challenger", roles)
        self.assertIn("tester", roles)

    def test_balanced_has_three_milestones_and_risk_reviewer(self):
        plan = self._plan("balanced", {"files": 80, "modules": 5, "tests": 20,
                                       "backend": True, "frontend": True})
        self.assertEqual(plan["work_mode"], "balanced")
        self.assertEqual(len(plan["milestones"]), 3)
        roles = {task["role"] for task in plan["tasks"]}
        self.assertIn("reviewer", roles)
        self.assertIn("auditor", roles)
        self.assertNotIn("challenger", roles)

    def test_rigorous_keeps_all_verification_roles(self):
        plan = self._plan("rigorous")
        self.assertEqual(plan["work_mode"], "rigorous")
        self.assertEqual(len(plan["milestones"]), 5)
        roles = {task["role"] for task in plan["tasks"]}
        self.assertTrue({"reviewer", "challenger", "auditor",
                         "success-auditor"}.issubset(roles))

    def test_auto_uses_profile_facts_and_safe_fallback(self):
        small = {"files": 2, "modules": 1, "tests": 2, "backend": True,
                 "languages": ["python"]}
        fast = plan_request(
            "fix a small backend typo", run_id="adaptive-fast",
            model="test-model", mode="auto", repo_profile=small,
        )
        self.assertEqual(fast["work_mode"], "fast")
        self.assertIn("risk_assessment", fast)
        self.assertEqual(fast["risk_assessment"]["confidence"], "high")

        missing = self._plan("auto", None)
        self.assertEqual(missing["work_mode"], "balanced")
        self.assertEqual(missing["risk_assessment"]["confidence"], "low")
        self.assertEqual(missing["risk_assessment"]["fallback_policy"],
                         "BALANCED when repository facts are missing")

    def test_high_risk_auto_goes_rigorous_and_records_factors(self):
        plan = self._plan("auto", {
            "files": 2000, "modules": 30, "tests": 300,
            "languages": ["python", "javascript", "sql"],
            "backend": True, "frontend": True,
        })
        self.assertEqual(plan["work_mode"], "rigorous")
        factors = plan["risk_assessment"]["factors"]
        self.assertTrue(factors["profile_available"])
        self.assertGreaterEqual(plan["risk_assessment"]["score"], 50)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(TeamworkPlannerError):
            self._plan("turbo")

    def test_fast_is_safety_upgraded_for_critical_risk(self):
        plan = plan_request(
            "Add an auth migration with a breaking API change",
            run_id="safety", model="test-model", mode="fast",
            repo_profile={"files": 500, "modules": 12, "tests": 50,
                          "backend": True, "frontend": True},
        )
        self.assertEqual(plan["work_mode"], "rigorous")
        self.assertTrue(plan["risk_assessment"]["safety_upgrade"])
        self.assertEqual(plan["verification_budget"]["review_depth"], "full")

    def test_detected_test_command_replaces_planner_default(self):
        plan = plan_request(
            "Build the backend", run_id="command", model="test-model",
            mode="balanced",
            repo_profile={
                "files": 10, "modules": 2, "tests": 3,
                "backend": True, "languages": ["python"],
                "test_commands": ["python3.11 -m pytest -q"],
                "test_command_sources": [{
                    "command": "python3.11 -m pytest -q",
                    "source": "pyproject.toml pytest configuration",
                }],
            },
        )
        self.assertEqual(plan["test_command"], "python3.11 -m pytest -q")
        self.assertEqual(plan["test_command_source"],
                         "pyproject.toml pytest configuration")
        self.assertTrue(all(
            "python3.11 -m pytest -q" in task["verify_commands"]
            for task in plan["tasks"]
            if task["role"] == "tester"
        ))
        self.assertEqual(plan["verification_budget"]["mode"], "balanced")
        self.assertIsNone(plan["verification_budget"]["token_budget"])


class PlannerTests(unittest.TestCase):
    def test_full_deterministic_plan_has_m1_to_m5(self):
        plan = plan_request(
            "Build a backend API and a frontend UI",
            run_id="plan-1", project_id=PROJECT_ID, max_parallel=3,
            model="opencode-go/deepseek-v4.1-flash")
        self.assertEqual(plan["run_id"], "plan-1")
        self.assertEqual(plan["project_id"], PROJECT_ID)
        self.assertEqual(plan["pattern"], "distributed_coding")
        self.assertEqual([m["id"] for m in plan["milestones"]],
                         ["M1", "M2", "M3", "M4", "M5"])
        groups = by_milestone(plan)
        self.assertEqual([task["role"] for task in groups["M1"]], ["explorer"])
        self.assertEqual([task["role"] for task in groups["M2"]],
                         ["implementer", "implementer"])
        self.assertEqual({task["department"] for task in groups["M2"]},
                         {"backend", "frontend"})
        self.assertEqual({task["id"] for task in groups["M2"]},
                         {"build-backend", "build-frontend"})
        self.assertEqual(groups["M2"][0]["depends_on"],
                         groups["M2"][1]["depends_on"])
        self.assertEqual([task["role"] for task in groups["M3"]], ["tester"])
        self.assertEqual([task["role"] for task in groups["M4"]],
                         ["reviewer", "challenger", "auditor"])
        self.assertEqual([task["role"] for task in groups["M5"]],
                         ["success-auditor"])
        self.assertTrue(all(task["phase"] == "verify" for task in
                            groups["M3"] + groups["M4"] + groups["M5"]))
        self.assertEqual(plan["milestones"][0]["verification_policy"]["require_roles"], [])
        self.assertEqual(plan["milestones"][2]["verification_policy"]["require_roles"],
                         ["tester"])
        self.assertEqual(plan["milestones"][3]["verification_policy"]["require_roles"],
                         ["reviewer", "challenger", "auditor"])
        self.assertEqual(plan["milestones"][4]["verification_policy"]["require_roles"],
                         ["success-auditor"])
        self.assertEqual([m["depends_on"] for m in plan["milestones"]],
                         [[], ["M1"], ["M2"], ["M3"], ["M4"]])
        for task in plan["tasks"]:
            self.assertIn(task["milestone"], {"M1", "M2", "M3", "M4", "M5"})
            self.assertIn(task["phase"], ("build", "verify", "repair"))
            self.assertTrue(task["acceptance"])
            self.assertTrue(task["verify_commands"])
            self.assertEqual(task["provider"], "opencode")
            self.assertEqual(task["model"], "opencode-go/deepseek-v4.1-flash")
        self.assertEqual(plan_request(
            "Build a backend API and a frontend UI",
            run_id="plan-1", project_id=PROJECT_ID, max_parallel=3,
            model="opencode-go/deepseek-v4.1-flash"), plan)

    def test_full_plan_validates_and_initializes(self):
        plan = plan_request("Build a backend API and a frontend UI",
                            run_id="plan-init")
        validate_teamwork_plan(plan)
        rich = validate_rich_plan(plan)
        self.assertEqual(len(rich["tasks"]), 8)
        with tempfile.TemporaryDirectory() as temp:
            engine = TeamworkEngine(Path(temp))
            try:
                state = engine.init(plan)
                self.assertEqual(state["milestone_count"], 5)
                self.assertEqual(state["task_count"], 8)
                self.assertEqual(state["verified_progress"], 0.0)
                self.assertIsNone(state["estimated_progress"])
                self.assertEqual(state["milestones"][0]["status"], "ready")
                self.assertEqual(state["milestones"][1]["status"], "planned")
            finally:
                engine.close()

    def test_research_uses_one_milestone(self):
        plan = plan_request("Research the current architecture and compare options",
                            run_id="research-1")
        self.assertEqual(plan["pattern"], "research")
        self.assertEqual(len(plan["milestones"]), 1)
        self.assertEqual(plan["milestones"][0]["id"], "M1")
        self.assertEqual([task["role"] for task in plan["tasks"]], ["explorer"])
        self.assertEqual(plan["tasks"][0]["milestone"], "M1")
        validate_rich_plan(plan)

    def test_single_backend_uses_three_milestones(self):
        plan = plan_request("Implement a backend endpoint for authentication",
                            run_id="backend-1")
        self.assertEqual([m["id"] for m in plan["milestones"]],
                         ["M1", "M2", "M3"])
        self.assertEqual([task["role"] for task in plan["tasks"]],
                         ["implementer", "tester", "auditor"])
        self.assertEqual(plan["tasks"][0]["id"], "build-backend")
        self.assertEqual(plan["tasks"][0]["milestone"], "M1")
        self.assertEqual(plan["tasks"][1]["milestone"], "M2")
        self.assertEqual(plan["tasks"][2]["milestone"], "M3")
        self.assertNotIn("build-frontend", {task["id"] for task in plan["tasks"]})
        self.assertEqual([m["depends_on"] for m in plan["milestones"]],
                         [[], ["M1"], ["M2"]])
        validate_rich_plan(plan)

    def test_fix_request_selects_review_and_repair_without_extra_milestones(self):
        plan = plan_request("Fix a bug in the backend API", run_id="repair-1")
        self.assertEqual(plan["pattern"], "review_and_repair")
        self.assertEqual(len(plan["milestones"]), 3)
        self.assertIn("Repair", plan["tasks"][0]["objective"])

    def test_explicit_patterns_are_honoured(self):
        for pattern in PATTERNS:
            with self.subTest(pattern=pattern):
                plan = plan_request("do the work", run_id="pattern-1",
                                    pattern=pattern)
                self.assertEqual(plan["pattern"], pattern)
                validate_teamwork_plan(plan)

    def test_repository_dependent_fields_are_explicit(self):
        plan = plan_request("Build a backend API and a frontend UI",
                            run_id="markers-1")
        self.assertTrue(plan["requires_repo"])
        self.assertTrue(plan["repo_inputs"])
        self.assertIn("tasks[].verify_commands", plan["repo_dependent_fields"])
        self.assertTrue(any("no LLM" in note for note in plan["notes"]))
        for task in plan["tasks"]:
            self.assertTrue(task["requires_repo"])
            self.assertIn("verify_commands", task["repo_dependent_fields"])
            self.assertTrue(task["acceptance"])

    def test_invalid_arguments_are_rejected(self):
        cases = [
            dict(request="x", run_id=""),
            dict(request="x", run_id="../escape"),
            dict(request="x", run_id="ok", project_id="not-a-project"),
            dict(request="x", run_id="ok", max_parallel=0),
            dict(request="x", run_id="ok", max_parallel=4),
            dict(request="x", run_id="ok", max_parallel=True),
            dict(request="x", run_id="ok", model=""),
            dict(request="x", run_id="ok", model="x" * 201),
            dict(request="x", run_id="ok", pattern="made_up"),
        ]
        for case in cases:
            request = case.pop("request")
            with self.subTest(case=case), self.assertRaises(TeamworkPlannerError):
                plan_request(request, **case)
        for request in ("", "   ", None, 42, "x" * 20001):
            with self.subTest(request=request), self.assertRaises(TeamworkPlannerError):
                plan_request(request, run_id="ok")

    def test_object_request_is_supported_deterministically(self):
        first = plan_request({"text": "Research two options"}, run_id="object-1")
        second = plan_request({"text": "Research two options"}, run_id="object-1")
        self.assertEqual(first, second)
        self.assertEqual(first["pattern"], "research")


if __name__ == "__main__":
    unittest.main()
