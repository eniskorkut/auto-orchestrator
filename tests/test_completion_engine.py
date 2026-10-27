from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.completion_engine import (
    CRITERION_STATES,
    evaluate_completion,
    parse_passed_test_signals,
    parse_test_signals,
    requirements_projection,
)
from tools.project_identity import ensure_project_identity
from tools.project_memory import ProjectMemory
from tools.requirements_contract import (
    DEFAULT_VERIFICATION_COMMAND,
    save_contract,
    validate_contract,
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", check=True)
    return result.stdout


def make_repo(root: Path) -> str:
    root.mkdir(parents=True)
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    (root / "auth").mkdir()
    (root / "auth" / "session.py").write_text("JWT = True\n", encoding="utf-8")
    git(root, "init")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return git(root, "rev-parse", "HEAD").strip()


class CompletionContractMixin:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.head = make_repo(self.root)
        self.identity = ensure_project_identity(self.root)
        self.decision = None
        with ProjectMemory(self.root, identity=self.identity) as memory:
            self.decision = memory.add_decision(
                topic="auth_session", decision="JWT", source="user",
                status="confirmed", confirmed_by="tester")

    def contract(self, *, schema_version=2, test_cases=None, critical=True,
                 review_required=True, evidence=None, implementation_files=None,
                 criteria=None, version=1, decisions=None):
        if criteria is None:
            criteria = [{
                "id": "ac_01_auth",
                "text": "JWT authentication is implemented",
                "applies_to": ["build", "verify"],
                "decision_ref": self.decision["decision_id"],
                "evidence": evidence or {"kind": "not_automated"},
                "requirement_id": "req_01_auth",
                "critical": critical,
                "review_required": review_required,
                "implementation_files": (
                    ["auth/session.py"] if implementation_files is None
                    else implementation_files),
                "test_cases": test_cases if test_cases is not None else [{
                    "test_id": "test_jwt_access_issuance",
                    "expected_behavior": "A valid login issues an access token.",
                    "negative": False,
                    "required": True,
                }, {
                    "test_id": "test_invalid_refresh_rejected",
                    "expected_behavior": "An invalid refresh token is rejected.",
                    "negative": True,
                    "required": True,
                }],
            }]
        value = {
            "contract_id": "rc_" + "1" * 16,
            "contract_version": version,
            "schema_version": schema_version,
            "status": "confirmed",
            "project_id": self.identity["project_id"],
            "interview_id": None,
            "goal": "Verify completion honestly",
            "source": {"kind": "user", "ref": "completion-test"},
            "confirmed_decisions": decisions if decisions is not None else [{
                "decision_ref": self.decision["decision_id"],
                "topic": "auth_session",
                "decision": "JWT",
                "rationale": "",
                "confirmed_by": "tester",
            }],
            "constraints": [],
            "out_of_scope": [],
            "acceptance_criteria": criteria,
            "verification_strategy": {"commands": [],
                                      "required_roles": ["tester", "reviewer"]},
            "blockers": [],
            "tasks": [],
            "created_at": 1.0,
            "approved_at": 1.0,
        }
        if schema_version == 1:
            for item in value["acceptance_criteria"]:
                for key in ("requirement_id", "critical", "review_required",
                            "implementation_files", "test_cases"):
                    item.pop(key, None)
            value.pop("schema_version")
            value.pop("verification_command", None)
        return validate_contract(value)

    def signals(self, *names, exit_code=0, command=None):
        return [{
            "command": command or DEFAULT_VERIFICATION_COMMAND,
            "exit_code": exit_code,
            "signals": list(names),
            "source": "runner",
        }]

    def evaluate(self, contract, **kwargs):
        kwargs.setdefault("changed_files", ["auth/session.py"])
        kwargs.setdefault("commit_sha", self.head)
        kwargs.setdefault("reviews", [{"role": "reviewer", "verdict": "pass",
                                       "task_id": "review"}])
        return evaluate_completion(self.root, contract, **kwargs)


class CompletionEngineTests(CompletionContractMixin, unittest.TestCase):
    def test_v2_all_three_levels_verify(self):
        contract = self.contract()
        result = self.evaluate(
            contract,
            test_results=self.signals("test_jwt_access_issuance",
                                      "test_invalid_refresh_rejected"))
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["fully_verified"])
        self.assertTrue(result["verified_completion"])
        self.assertEqual(result["levels"]["implementation"]["status"],
                         "verified")
        self.assertEqual(result["levels"]["behavioral"]["status"], "verified")
        self.assertEqual(result["levels"]["conformance"]["status"], "verified")
        self.assertEqual(result["criteria"][0]["state"], "verified")
        self.assertIn(result["criteria"][0]["state"], CRITERION_STATES)
        self.assertEqual(result["binding"]["contract_sha256"],
                         contract["sha256"])
        self.assertEqual(result["binding"]["commit_sha"], self.head)
        self.assertIn("test_jwt_access_issuance",
                      result["binding"]["test_ids"])

    def test_positive_and_negative_signals_are_both_required(self):
        contract = self.contract()
        result = self.evaluate(
            contract, test_results=self.signals("test_jwt_access_issuance"))
        self.assertEqual(result["status"], "partially_verified")
        self.assertFalse(result["verified_completion"])
        self.assertEqual(result["criteria"][0]["state"], "partially_verified")
        self.assertEqual(result["criteria"][0]["missing_test_ids"],
                         ["test_invalid_refresh_rejected"])

        result = self.evaluate(
            contract,
            test_results=self.signals("test_jwt_access_issuance",
                                      "test_invalid_refresh_rejected"))
        self.assertTrue(result["verified_completion"])

    def test_missing_tests_are_needs_review_or_implemented_unverified(self):
        review_contract = self.contract()
        result = self.evaluate(review_contract)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["criteria"][0]["state"], "needs_review")
        self.assertFalse(result["verified_completion"])

        unreviewed = self.contract(critical=False, review_required=False)
        result = self.evaluate(unreviewed, reviews=[])
        self.assertEqual(result["status"], "implemented_unverified")
        self.assertEqual(result["criteria"][0]["state"],
                         "implemented_unverified")
        self.assertFalse(result["verified_completion"])

    def test_failed_real_command_is_failed(self):
        contract = self.contract()
        result = self.evaluate(
            contract,
            test_results=self.signals("test_jwt_access_issuance",
                                      "test_invalid_refresh_rejected",
                                      exit_code=1))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["fully_verified"])
        self.assertEqual(result["criteria"][0]["state"], "failed")
        self.assertEqual(result["test_results"]["failed"], 1)

    def test_claimed_only_evidence_is_rejected(self):
        contract = self.contract()
        result = self.evaluate(
            contract,
            test_results=[{"status": "passed", "agent_says": "all green"}],
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["rejected_test_results"][0]["reason"],
                         "missing_real_exit_code")
        self.assertEqual(result["criteria"][0]["test_ids"], [])
        self.assertFalse(result["verified_completion"])

    def test_implementation_scope_violation_fails(self):
        contract = self.contract()
        result = self.evaluate(
            contract,
            plan={"tasks": [{"id": "build", "role": "implementer",
                             "write_scope": ["backend/"]}]},
            changed_files=["auth/session.py"],
            test_results=self.signals("test_jwt_access_issuance",
                                      "test_invalid_refresh_rejected"),
        )
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["scope_violation"])
        self.assertEqual(result["levels"]["implementation"]["status"],
                         "failed")

    def test_stale_commit_is_blocked(self):
        contract = self.contract()
        result = self.evaluate(contract, commit_sha="0" * 40,
                               test_results=self.signals(
                                   "test_jwt_access_issuance",
                                   "test_invalid_refresh_rejected"))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["stale"])
        self.assertIn("stale_commit", result["stale_reason"])
        self.assertFalse(result["verified_completion"])

    def test_stale_contract_version_is_blocked(self):
        first = self.contract()
        save_contract(self.root, first)
        second = self.contract(version=2)
        second.pop("sha256")
        second["supersedes"] = {
            "contract_id": first["contract_id"],
            "contract_version": 1,
        }
        second = validate_contract(second)
        save_contract(self.root, second)
        result = self.evaluate(
            first,
            test_results=self.signals("test_jwt_access_issuance",
                                      "test_invalid_refresh_rejected"))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("newer contract version", result["stale_reason"])

    def test_stale_decision_is_blocked(self):
        contract = self.contract()
        with ProjectMemory(self.root, identity=self.identity) as memory:
            memory.conn.execute(
                "UPDATE decisions SET status='superseded',updated_at=? "
                "WHERE decision_id=?", (2.0, self.decision["decision_id"]))
            memory.conn.commit()
        result = self.evaluate(
            contract,
            test_results=self.signals("test_jwt_access_issuance",
                                      "test_invalid_refresh_rejected"))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["stale"])
        self.assertIn("stale_decision", result["stale_reason"])

    def test_code_evidence_failure_is_a_critical_failure(self):
        contract = self.contract(
            evidence={"kind": "code_evidence", "topic": "database",
                      "expected": "postgres"},
            test_cases=[])
        result = self.evaluate(contract, changed_files=["auth/session.py"],
                               test_results=self.signals(
                                   DEFAULT_VERIFICATION_COMMAND))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["criteria"][0]["conformance_status"], "failed")
        self.assertEqual(len(result["critical_failures"]), 1)

    def test_v1_contract_uses_legacy_verify_contract_fallback(self):
        contract = self.contract(
            schema_version=1,
            criteria=[{
                "id": "ac_01_tests",
                "text": "The test command passes",
                "applies_to": ["verify"],
                "decision_ref": None,
                "evidence": {"kind": "command",
                             "command": DEFAULT_VERIFICATION_COMMAND},
            }],
            decisions=[],
        )
        result = self.evaluate(
            contract,
            test_results=self.signals(DEFAULT_VERIFICATION_COMMAND))
        self.assertTrue(result["legacy"])
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["fully_verified"])
        self.assertEqual(result["criteria"][0]["state"], "verified")

        partial = self.contract(
            schema_version=1,
            criteria=[{
                "id": "ac_01_review",
                "text": "A human review is recorded",
                "applies_to": ["review"],
                "decision_ref": None,
                "evidence": {"kind": "not_automated"},
            }],
            decisions=[],
        )
        result = self.evaluate(partial)
        self.assertTrue(result["legacy"])
        self.assertEqual(result["status"], "partially_verified")
        self.assertFalse(result["fully_verified"])
        self.assertEqual(result["criteria"][0]["state"], "needs_review")

    def test_critical_without_behavioral_tests_cannot_use_generic_green_command(self):
        import copy
        value = copy.deepcopy(self.contract())
        for criterion in value["acceptance_criteria"]:
            criterion["critical"] = True
            criterion["test_cases"] = []
            criterion["evidence"] = {"kind": "not_automated"}
        result = self.evaluate(
            value,
            test_results=self.signals("some_unrelated_test"),
            reviews=[{"role": "reviewer", "verdict": "pass",
                      "task_id": "review"}])
        self.assertFalse(result["verified_completion"])
        self.assertFalse(result["fully_verified"])
        self.assertTrue(all(item["state"] != "verified"
                            for item in result["criteria"]))

    def test_test_signal_parser_handles_unittest_and_pytest(self):
        output = "\n".join([
            "test_jwt_access_issuance (auth.tests.SessionTests.test_jwt_access_issuance) ... ok",
            "tests/test_auth.py::test_invalid_refresh_rejected PASSED",
            "FAIL: test_expired (auth.tests.SessionTests.test_expired)",
        ])
        signals = parse_test_signals(output)
        self.assertIn("test_jwt_access_issuance", signals)
        self.assertIn("test_invalid_refresh_rejected", signals)
        self.assertIn("test_expired", signals)
        passed = parse_passed_test_signals(output)
        self.assertIn("test_jwt_access_issuance", passed)
        self.assertIn("test_invalid_refresh_rejected", passed)
        self.assertNotIn("test_expired", passed)
        skipped = "tests/test_auth.py::test_jwt_expiry_enforced SKIPPED"
        self.assertNotIn("test_jwt_expiry_enforced",
                         parse_passed_test_signals(skipped))

    def test_projection_is_additive_and_never_claims_full_verification(self):
        contract = self.contract()
        projection = requirements_projection(contract, state={"tasks": [{
            "id": "build", "role": "implementer",
            "result": {"changed_files": ["auth/session.py"],
                       "tests": self.signals("test_jwt_access_issuance"),
                       "verdict": "pass"},
        }]})
        self.assertTrue(projection["projection"])
        self.assertFalse(projection["verified_completion"])
        self.assertIn(projection["status"], CRITERION_STATES)


class CompletionContractValidationTests(CompletionContractMixin,
                                        unittest.TestCase):
    def test_verification_command_must_be_an_allowed_runner(self):
        contract = self.contract()
        contract["verification_command"] = "python -c 'print(1)'"
        contract.pop("sha256")
        with self.assertRaises(ValueError):
            validate_contract(contract)

    def test_contract_digest_is_stable(self):
        contract = self.contract()
        self.assertEqual(contract["sha256"], validate_contract(contract)["sha256"])
        self.assertEqual(contract["sha256"],
                         validate_contract(json.loads(json.dumps(contract)))["sha256"])


if __name__ == "__main__":
    unittest.main()
