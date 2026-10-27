from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from tools import assumption_guard, context_selector
from tools.project_memory import ProjectMemory


class ContextSelectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_includes_blockers_and_excludes_irrelevant_decisions(self):
        with ProjectMemory(self.root) as memory:
            memory.add_decision(
                topic="auth_session", decision="JWT", rationale="login tokens",
                source="user", status="confirmed", confirmed_by="user")
            memory.add_decision(
                topic="payments", decision="Stripe", rationale="checkout billing",
                source="user", status="confirmed", confirmed_by="user")
            memory.add_requirement(
                "Resolve login session expiry",
                category="unresolved_blocking",
                status="unresolved_blocking", source="user")
            selection = context_selector.select_context(
                memory, "Fix login authentication", budget_tokens=500)
        refs = selection["refs"]
        self.assertTrue(any(ref.startswith("requirement:") for ref in refs))
        self.assertTrue(any(ref.startswith("decision:") for ref in refs))
        self.assertFalse(any(item["kind"] == "decision"
                             and item["provenance"].get("topic") == "payments"
                             for item in selection["items"]))
        for item in selection["items"]:
            self.assertTrue(item["ref"])
            self.assertTrue(item["provenance"])
            self.assertGreater(item["tokens"], 0)
        self.assertLessEqual(selection["token_estimate"],
                             selection["budget_tokens"])
        self.assertTrue(selection["truncated"] is False)

    def test_failed_tests_and_contract_have_provenance(self):
        with ProjectMemory(self.root) as memory:
            memory.record_observation(
                kind="verification", files=["backend/auth.py"],
                test_evidence="auth tests failed: 2 errors")
            contracts = self.root / ".auto-orchestrator" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "current.json").write_text(json.dumps({
                "contract_id": "rc_" + "a" * 16,
                "contract_version": 3,
                "sha256": "b" * 64,
                "path": "rc_" + "a" * 16 + "-v3.json",
                "updated_at": time.time(),
            }), encoding="utf-8")
            selection = context_selector.select_context(
                memory, "unrelated typo fix", budget_tokens=400)
        kinds = selection["counts"]
        self.assertEqual(kinds.get("failed_test"), 1)
        self.assertEqual(kinds.get("contract"), 1)
        contract = next(item for item in selection["items"]
                        if item["kind"] == "contract")
        self.assertEqual(contract["provenance"]["contract_version"], 3)
        failed = next(item for item in selection["items"]
                      if item["kind"] == "failed_test")
        self.assertEqual(failed["provenance"]["kind"], "verification")

    def test_relevant_handoff_and_changes_are_selected(self):
        with ProjectMemory(self.root) as memory:
            memory.record_handoff(
                run_id="run-1", milestone_id="M1", platform="codex",
                payload={"milestone_id": "M1",
                         "context_summary": "login JWT work passed",
                         "changed_files": ["backend/auth.py"],
                         "verified_tests": ["test_login"]})
            memory.record_observation(
                kind="git_commit", commit_sha="c" * 40,
                files=["backend/auth.py", "README.md"],
                test_evidence="11 tests passed")
            selection = context_selector.select_context(
                memory, "update backend auth", budget_tokens=600)
        self.assertTrue(any(item["kind"] == "handoff"
                            for item in selection["items"]))
        self.assertTrue(any(item["kind"] == "recent_change"
                            for item in selection["items"]))
        handoff = next(item for item in selection["items"]
                       if item["kind"] == "handoff")
        self.assertTrue(handoff["provenance"]["verified"])

    def test_runner_helper_renders_bounded_prompt_block(self):
        with ProjectMemory(self.root) as memory:
            memory.add_requirement(
                "Turkish blocked item: oturum süresi çözülmeli",
                category="unresolved_blocking",
                status="unresolved_blocking", source="user")
            prompt = context_selector.runner_context(
                memory, "oturum süresi", budget_tokens=300)
        self.assertIn("Selected project context", prompt)
        self.assertIn("requirement:", prompt)
        self.assertIn("çözülmeli", prompt)

    def test_selection_does_not_change_guard_receipt(self):
        request = "Fix a typo in README"
        first = assumption_guard.evaluate(self.root, request, platform="codex")
        with ProjectMemory(self.root) as memory:
            context_selector.select_context(memory, request)
        second = assumption_guard.evaluate(self.root, request, platform="codex")
        self.assertEqual(first["receipt"], second["receipt"])
        self.assertEqual(first["context_digest"], second["context_digest"])

    def test_utf8_turkish_decision_is_preserved(self):
        with ProjectMemory(self.root) as memory:
            memory.add_decision(
                topic="data_retention", decision="Kalıcı silme onayı",
                rationale="Kullanıcı silme politikası", source="user",
                status="confirmed", confirmed_by="user")
            selection = context_selector.select_context(
                memory, "kullanıcı silme politikası")
        decision = next(item for item in selection["items"]
                        if item["kind"] == "decision")
        self.assertIn("Kalıcı silme onayı", decision["text"])
        self.assertIn("politikası", decision["text"])


if __name__ == "__main__":
    unittest.main()
