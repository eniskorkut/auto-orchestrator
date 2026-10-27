from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools import assumption_guard
from tools.grill_llm import GrillLLM, enrich_questions, record_rejected
from tools.grill_me import GrillMeEngine
from tools.project_memory import ProjectMemory


def _payload(*, quote="Add login", option="cookie", topic="auth_session",
             extra_topic=None):
    topic_value = {
        "topic": topic,
        "why": "The request names login and needs an explicit session model.",
        "evidence": [{"kind": "request", "ref": "request", "quote": quote}],
        "options": [
            {"id": "cookie", "label": "session cookie", "recommended":
             option == "cookie"},
            {"id": "jwt", "label": "JWT", "recommended": option == "jwt"},
        ],
        "recommended_option": option,
    }
    topics = [topic_value]
    if extra_topic is not None:
        topics.append(extra_topic)
    return {"topics": topics}


class GrillLLMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _llm(self, value, **kwargs):
        if not isinstance(value, str):
            value = json.dumps(value)
        return GrillLLM(lambda prompt: value, enabled=True, **kwargs)

    def test_disabled_by_default_and_provider_is_not_called(self):
        calls = []

        def provider(prompt):
            calls.append(prompt)
            return json.dumps(_payload())

        llm = GrillLLM(provider)
        result = llm.propose("Add login", topics=["auth_session"])
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(calls, [])

    def test_valid_proposal_is_evidence_bound(self):
        llm = self._llm(_payload())
        result = llm.propose("Add login with a session", topics=["auth_session"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["topics"][0]["topic"], "auth_session")
        self.assertEqual(result["topics"][0]["evidence"][0]["quote"], "Add login")
        self.assertEqual(result["topics"][0]["recommended_option"], "cookie")

    def test_malformed_or_injected_json_falls_back(self):
        cases = [
            "```json\n" + json.dumps(_payload()) + "\n```",
            json.dumps({"topics": [], "extra": True}),
            json.dumps({"topics": [dict(_payload()["topics"][0],
                                        ignored="value")]}),
        ]
        for raw in cases:
            with self.subTest(raw=raw[:40]):
                result = self._llm(raw).propose(
                    "Add login", topics=["auth_session"])
                self.assertEqual(result["status"], "invalid")
                self.assertEqual(result["topics"], [])
                self.assertTrue(result["rejected"])

    def test_unknown_topic_bad_option_and_bad_quote_are_rejected(self):
        unknown = {"topics": [dict(_payload()["topics"][0],
                                   topic="unknown_topic")]}
        result = self._llm(unknown).propose(
            "Add login", topics=["auth_session"])
        self.assertEqual(result["status"], "invalid")

        bad_option = _payload()
        bad_option["topics"][0]["options"][0]["id"] = "bogus"
        result = self._llm(bad_option).propose(
            "Add login", topics=["auth_session"])
        self.assertEqual(result["status"], "invalid")

        result = self._llm(_payload(quote="fabricated quote")).propose(
            "Add login", topics=["auth_session"])
        self.assertEqual(result["status"], "invalid")

    def test_more_than_six_topics_and_oversized_output_are_rejected(self):
        topic = _payload()["topics"][0]
        many = {"topics": [dict(topic, topic=name) for name in (
            "auth_session", "database", "payments", "roles", "file_access",
            "file_storage", "data_retention")]}
        result = self._llm(many).propose(
            "Add login", topics=list(assumption_guard.RULES))
        self.assertEqual(result["status"], "invalid")

        huge = json.dumps({"topics": []}) + " " * 5000
        result = GrillLLM(lambda prompt: huge, enabled=True,
                          max_output_bytes=1024).propose(
            "Add login", topics=["auth_session"])
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["error"], "oversized_output")

    def test_timeout_and_provider_error_fall_back(self):
        def slow(prompt):
            time.sleep(0.5)
            return json.dumps({"topics": []})

        result = GrillLLM(slow, enabled=True, timeout=0.05).propose(
            "Add login", topics=["auth_session"])
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["topics"], [])

        def broken(prompt):
            raise RuntimeError("provider failed")

        result = GrillLLM(broken, enabled=True).propose(
            "Add login", topics=["auth_session"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["topics"], [])

    def test_local_command_provider(self):
        command = [sys.executable, "-c",
                   "import sys; sys.stdin.read(); print('{\"topics\": []}')"]
        result = GrillLLM(command, enabled=True).propose(
            "Add login", topics=["auth_session"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["topics"], [])

    def test_decision_and_code_evidence_provenance(self):
        decisions = [{"decision_id": "d_" + "a" * 16,
                      "topic": "auth_session", "decision": "JWT",
                      "rationale": "existing verified tokens",
                      "status": "confirmed"}]
        payload = _payload(quote="existing verified tokens")
        payload["topics"][0]["evidence"] = [{
            "kind": "decision", "ref": "d_" + "a" * 16,
            "quote": "existing verified tokens"}]
        result = self._llm(payload).propose(
            "Add login", decisions=decisions, topics=["auth_session"])
        self.assertEqual(result["status"], "ok")

        code_payload = _payload(quote="auth")
        code_payload["topics"][0]["evidence"] = [{
            "kind": "code", "ref": "backend/auth.py", "quote": "auth"}]
        result = self._llm(code_payload).propose(
            "Add login", facts={"code_evidence": {"jwt": ["backend/auth.py"]}},
            topics=["auth_session"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["topics"][0]["evidence"][0]["kind"], "code")

    def test_enrichment_changes_only_why_and_rationale(self):
        question = assumption_guard.critical_questions(
            "Add login", [], {}, mode="auto")[0]
        enriched = enrich_questions([question], [{
            "topic": "auth_session",
            "why": "Extra provider context.",
            "rationale": "Provider rationale.",
        }])[0]
        self.assertIn("Extra provider context", enriched["why"])
        self.assertIn("Provider rationale", enriched["rationale"])
        for field in ("options", "recommended_option", "depends_on",
                      "critical", "alternatives", "affected_requirements",
                      "dependency"):
            self.assertEqual(enriched[field], question[field])
        self.assertNotEqual(question["why"], enriched["why"])

    def test_rejected_proposals_are_observations_not_decisions(self):
        invalid = _payload()
        invalid["topics"][0]["options"][0]["id"] = "bogus"
        with GrillMeEngine(
                self.root,
                llm=self._llm(invalid)) as engine:
            started = engine.start("Add login", origin_platform="codex",
                                   mode="auto")
            question = started["question"]
            self.assertEqual(question["question_key"], "auth_session")
            self.assertEqual(question["recommended_option"], "cookie")
        with ProjectMemory(self.root) as memory:
            self.assertEqual(memory.list_decisions(status="confirmed"), [])
            rows = memory.conn.execute(
                "SELECT kind FROM observations WHERE project_id=?",
                (memory.project_id,)).fetchall()
            self.assertTrue(any(row["kind"] == "grill_llm_rejected"
                                for row in rows))

    def test_valid_proposal_enriches_question_but_never_confirms_it(self):
        with GrillMeEngine(
                self.root, llm=self._llm(_payload())) as engine:
            started = engine.start("Add login to the app",
                                   origin_platform="codex", mode="auto")
            question = started["question"]
            self.assertIn("Proposal context", question["why"])
            self.assertEqual(question["recommended_option"], "cookie")
            self.assertEqual(question["selected_option"], None)
        with ProjectMemory(self.root) as memory:
            self.assertEqual(memory.list_decisions(status="confirmed"), [])

    def test_record_rejected_is_bounded_and_provenanced(self):
        with ProjectMemory(self.root) as memory:
            observed = record_rejected(memory, {
                "status": "invalid",
                "rejected": [{"topic": "auth_session",
                              "reason": "quote is not in the request"}],
            })
            self.assertEqual(observed["kind"], "grill_llm_rejected")
            self.assertEqual(memory.list_decisions(), [])


if __name__ == "__main__":
    unittest.main()
