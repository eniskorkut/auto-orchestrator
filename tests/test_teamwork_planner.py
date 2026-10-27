from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.project_planner import validate_rich_plan
from tools.requirements_contract import validate_for_plan
from tools.teamwork import PATTERNS, TeamworkEngine, validate_teamwork_plan
from tools.teamwork_planner import (
    TeamworkPlannerError,
    plan_request,
)

PROJECT_ID = "p_" + "a" * 16
CONTRACT_ID = "rc_" + "4" * 16
DECISION_ID = "d_" + "5" * 16
CONTRACT_COMMAND = "python3.11 -m unittest tests.test_teamwork_planner -q"


def requirements_contract(**overrides):
    value = {
        "contract_id": CONTRACT_ID,
        "contract_version": 1,
        "status": "confirmed",
        "project_id": PROJECT_ID,
        "interview_id": "iv_" + "6" * 16,
        "goal": "Build a verified backend API",
        "source": {"kind": "grill-me", "ref": "iv_" + "6" * 16},
        "confirmed_decisions": [{
            "decision_ref": DECISION_ID,
            "topic": "database",
            "decision": "Use SQLite",
            "rationale": "single-user local deployment",
            "confirmed_by": "user",
        }],
        "constraints": ["Use SQLite only for local persistence"],
        "out_of_scope": ["Mobile application"],
        "acceptance_criteria": [
            {
                "id": "ac_01_database",
                "text": "SQLite is used for persistence",
                "applies_to": ["build", "verify"],
                "decision_ref": DECISION_ID,
                "evidence": {"kind": "code_evidence", "topic": "database",
                             "expected": "sqlite"},
            },
            {
                "id": "ac_02_tests",
                "text": "The targeted test command passes",
                "applies_to": ["verify"],
                "decision_ref": None,
                "evidence": {"kind": "command", "command": CONTRACT_COMMAND},
            },
            {
                "id": "ac_03_review",
                "text": "Reviewer signs off on the evidence",
                "applies_to": ["review"],
                "decision_ref": None,
                "evidence": {"kind": "not_automated"},
            },
            {
                "id": "ac_04_audit",
                "text": "Auditor reconciles every claim",
                "applies_to": ["audit"],
                "decision_ref": None,
                "evidence": {"kind": "not_automated"},
            },
        ],
        "verification_strategy": {
            "commands": [],
            "required_roles": ["tester", "reviewer"],
        },
        "blockers": [],
        "tasks": [],
        "created_at": 1000.0,
        "approved_at": 1001.0,
    }
    value.update(overrides)
    return value


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

    def test_polyglot_test_commands_are_all_added(self):
        plan = plan_request(
            "Build the backend and frontend", run_id="polyglot",
            model="test-model", mode="balanced",
            repo_profile={
                "files": 20, "modules": 3, "tests": 5,
                "backend": True, "frontend": True,
                "languages": ["python", "javascript"],
                "test_commands": ["npm test", "python3.11 -m pytest -q"],
                "test_command_sources": [
                    {"command": "npm test", "source": "package.json"},
                    {"command": "python3.11 -m pytest -q",
                     "source": "pyproject.toml"},
                ],
            },
        )
        self.assertEqual(plan["test_commands"],
                         ["npm test", "python3.11 -m pytest -q"])
        tester = next(task for task in plan["tasks"]
                      if task["role"] == "tester")
        self.assertIn("npm test", tester["verify_commands"])
        self.assertIn("python3.11 -m pytest -q", tester["verify_commands"])

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


class ExecutionProviderTests(unittest.TestCase):
    def test_codex_provider_is_read_only_and_never_silently_opencode(self):
        plan = plan_request("Küçük bir hata düzelt", run_id="provider-test",
                            mode="auto", provider="codex")
        self.assertTrue(plan["tasks"])
        for task in plan["tasks"]:
            self.assertEqual(task["provider"], "codex")
            self.assertEqual(task["sandbox"], "read-only")
            self.assertEqual(task["approval_policy"], "never")

    def test_codex_write_requires_explicit_sandbox(self):
        plan = plan_request("Küçük bir hata düzelt", run_id="provider-write",
                            mode="auto", provider="codex",
                            codex_sandbox="workspace-write")
        for task in plan["tasks"]:
            self.assertEqual(task["sandbox"], "workspace-write")
            self.assertEqual(task["approval_policy"], "never")
        with self.assertRaises(TeamworkPlannerError):
            plan_request("Küçük bir hata düzelt", run_id="provider-bad",
                         provider="codex", codex_sandbox="danger-full-access")

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(TeamworkPlannerError):
            plan_request("Küçük bir hata düzelt", run_id="provider-test",
                         provider="cursor")


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
            from tools.assumption_guard import evaluate
            guard = evaluate(Path(temp), plan["request"],
                             platform="opencode", mode="auto")
            plan["assumption_guard"] = guard["receipt"]
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


class RequirementsContractPlannerTests(unittest.TestCase):
    request = "Build a backend API"

    def _plan(self, contract=None, *, project_id=PROJECT_ID):
        kwargs = {
            "run_id": "contract-plan",
            "project_id": project_id,
            "model": "test-model",
            "mode": "balanced",
        }
        if contract is not None:
            kwargs["requirements_contract"] = contract
        return plan_request(self.request, **kwargs)

    def test_no_contract_keeps_legacy_plan_shape(self):
        plan = self._plan()
        self.assertNotIn("requirements_contract", plan)
        self.assertNotIn("contract_ref", plan)
        self.assertTrue(all("Contract constraints:" not in task["objective"]
                            for task in plan["tasks"]))
        self.assertTrue(all(not any(criterion.startswith("AC ")
                                    for criterion in task["acceptance"])
                            for task in plan["tasks"]))
        validate_teamwork_plan(plan)
        validate_rich_plan(plan)

    def test_constraints_and_acceptance_are_injected_without_scope_change(self):
        contract = requirements_contract()
        base = self._plan()
        plan = self._plan(contract)
        normalized = validate_for_plan(contract, project_id=PROJECT_ID)
        self.assertEqual(plan["requirements_contract"], normalized)
        self.assertEqual(plan["contract_ref"], {
            "contract_id": CONTRACT_ID,
            "contract_version": 1,
            "sha256": normalized["sha256"],
        })
        for task in plan["tasks"]:
            original = next(item for item in base["tasks"]
                            if item["id"] == task["id"])
            self.assertEqual(task["read_scope"], original["read_scope"])
            self.assertEqual(task["write_scope"], original["write_scope"])

        builders = [task for task in plan["tasks"]
                    if task["role"] == "implementer"]
        self.assertTrue(builders)
        for task in builders:
            self.assertIn(
                "Contract constraints: Use SQLite only for local persistence",
                task["objective"])
            self.assertIn("Confirmed decisions: database=Use SQLite",
                          task["objective"])

        tester = next(task for task in plan["tasks"]
                      if task["role"] == "tester")
        self.assertIn("AC ac_01_database: SQLite is used for persistence",
                      tester["acceptance"])
        self.assertIn("AC ac_02_tests: The targeted test command passes",
                      tester["acceptance"])
        self.assertNotIn("AC ac_03_review: Reviewer signs off on the evidence",
                         tester["acceptance"])
        self.assertIn(CONTRACT_COMMAND, tester["verify_commands"])

        reviewer = next(task for task in plan["tasks"]
                        if task["role"] == "reviewer")
        self.assertIn("AC ac_03_review: Reviewer signs off on the evidence",
                      reviewer["acceptance"])
        self.assertNotIn("AC ac_04_audit: Auditor reconciles every claim",
                         reviewer["acceptance"])
        auditor = next(task for task in plan["tasks"]
                       if task["role"] == "auditor")
        self.assertIn("AC ac_04_audit: Auditor reconciles every claim",
                      auditor["acceptance"])

        self.assertEqual(validate_teamwork_plan(plan)["requirements_contract"],
                         normalized)
        rich = validate_rich_plan(plan)
        self.assertEqual(rich["requirements_contract"], normalized)
        self.assertEqual(rich["contract_ref"], plan["contract_ref"])

    def test_assumption_guard_metadata_is_preserved(self):
        guard = {"assumptions": [{"id": "a1", "status": "guarded"}]}
        plan = self._plan(requirements_contract())
        plan["assumption_guard"] = guard
        self.assertEqual(validate_teamwork_plan(plan)["assumption_guard"], guard)
        self.assertEqual(validate_rich_plan(plan)["assumption_guard"], guard)

    def test_command_evidence_is_added_once_and_must_be_validated(self):
        plan = self._plan(requirements_contract())
        tester = next(task for task in plan["tasks"]
                      if task["role"] == "tester")
        self.assertEqual(tester["verify_commands"].count(CONTRACT_COMMAND), 1)
        unsafe = requirements_contract()
        unsafe["acceptance_criteria"][1]["evidence"]["command"] = "rm -rf /"
        with self.assertRaises(TeamworkPlannerError):
            self._plan(unsafe)

    def test_stale_blocker_and_wrong_project_contracts_are_rejected(self):
        for bad in (
            requirements_contract(status="superseded"),
            requirements_contract(status="invalidated"),
            requirements_contract(blockers=[{
                "id": "Q1", "text": "Deployment target is unknown",
                "blocking": True,
            }]),
            requirements_contract(project_id="p_" + "b" * 16),
        ):
            with self.subTest(contract=bad):
                with self.assertRaises(TeamworkPlannerError):
                    self._plan(bad)
        with self.assertRaises(TeamworkPlannerError):
            plan_request(self.request, run_id="no-project", mode="balanced",
                         requirements_contract=requirements_contract())

    def test_confirmed_contract_with_digest_mismatch_is_rejected(self):
        bad = requirements_contract()
        bad["goal"] = "Changed after approval"
        bad["sha256"] = "0" * 64
        with self.assertRaises(TeamworkPlannerError):
            self._plan(bad)


if __name__ == "__main__":
    unittest.main()
