"""V7.9 review evidence: task/criterion/commit/contract binding and independence."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tools import completion_engine


def contract():
    value = {
        "schema_version": 2,
        "contract_id": "rc_" + hashlib.sha256(b"review").hexdigest()[:16],
        "contract_version": 1,
        "project_id": "p_" + hashlib.sha256(b"project").hexdigest()[:16],
        "request_hash": hashlib.sha256(b"request").hexdigest(),
        "goal": "Review binding",
        "source": {"kind": "user", "ref": "test:review"},
        "verification_command": "python3 -m unittest",
        "acceptance_criteria": [{
            "id": "ac_review",
            "text": "review is independent",
            "applies_to": ["build", "verify"],
            "review_required": True,
            "evidence": {"kind": "command",
                         "command": "python3 -m unittest"},
        }],
    }
    normalized = completion_engine.validate_contract(value)
    return normalized


COMMIT = "a" * 40


def review(**overrides):
    value = {
        "reviewed_task_id": "build",
        "role": "reviewer",
        "verdict": "pass",
        "commit": COMMIT,
        "criterion_ids": ["ac_review"],
        "reviewer_agent_id": "reviewer-1",
        "independence_level": "accepted",
        "independent": True,
    }
    value.update(overrides)
    return value


class ReviewBindingTests(unittest.TestCase):
    def evaluate(self, reviews, **overrides):
        value = contract()
        kwargs = dict(
            changed_files=["src/app.py"], commits=[COMMIT],
            test_results=[{"command": "python3 -m unittest",
                           "exit_code": 0, "signals": []}],
            reviews=reviews, commit_sha=COMMIT, task_id="build",
            check_state=False, check_commit=False, scan_code=False,
            implementer_identity="implementer-1",
        )
        kwargs.update(overrides)
        result = completion_engine.evaluate_completion(self.root, value,
                                                       **kwargs)
        return result, value

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_matching_accepted_review_verifies_the_criterion(self):
        value = contract()
        result, _ = self.evaluate([review(contract_sha256=value["sha256"])])
        criterion = result["criteria"][0]
        self.assertEqual(criterion["state"], "verified", criterion)
        self.assertTrue(result["verified_completion"])

    def test_declared_review_is_not_independence(self):
        value = contract()
        result, _ = self.evaluate([review(
            contract_sha256=value["sha256"], independence_level="declared",
            independent=True)])
        self.assertEqual(result["criteria"][0]["state"], "needs_review")

    def test_wrong_task_commit_contract_and_self_review_are_rejected(self):
        value = contract()
        cases = {
            "wrong_task": review(reviewed_task_id="other",
                                 contract_sha256=value["sha256"]),
            "wrong_commit": review(commit="b" * 40,
                                   contract_sha256=value["sha256"]),
            "wrong_contract": review(contract_sha256="c" * 64),
            "self": review(reviewer_agent_id="implementer-1",
                           contract_sha256=value["sha256"]),
        }
        for name, item in cases.items():
            with self.subTest(name=name):
                result, _ = self.evaluate([item])
                self.assertEqual(result["criteria"][0]["state"],
                                 "needs_review")

    def test_failed_review_marks_the_criterion_failed(self):
        value = contract()
        result, _ = self.evaluate([review(
            verdict="fail", contract_sha256=value["sha256"])])
        self.assertEqual(result["criteria"][0]["state"], "failed")

    def test_legacy_review_without_level_is_declared_only(self):
        value = contract()
        legacy = {
            "task_id": "build", "role": "reviewer", "verdict": "pass",
            "commit": COMMIT, "independent": True,
            "contract_sha256": value["sha256"],
        }
        result, _ = self.evaluate([legacy])
        self.assertEqual(result["criteria"][0]["state"], "needs_review")

    def test_criterion_specific_review_does_not_cover_another_criterion(self):
        value = contract()
        item = review(contract_sha256=value["sha256"],
                      criterion_ids=["ac_other"])
        result, _ = self.evaluate([item])
        self.assertEqual(result["criteria"][0]["state"], "needs_review")

    def test_no_review_requires_review(self):
        result, _ = self.evaluate([])
        self.assertEqual(result["criteria"][0]["state"], "needs_review")


if __name__ == "__main__":
    unittest.main()
