from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tools import assumption_guard
from tools.grill_me import GrillMeEngine
from tools.project_memory import ProjectMemory


class AssumptionGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _interview_count(self):
        with ProjectMemory(self.root) as memory:
            return memory.conn.execute(
                "SELECT COUNT(*) FROM interviews WHERE project_id=?",
                (memory.project_id,)).fetchone()[0]

    def _no_plan_or_engine_artifacts(self):
        self.assertFalse((self.root / ".auto-orchestrator" / "plans").exists())
        self.assertFalse(
            (self.root / ".auto-orchestrator" / "teamwork" / "active.json").exists())

    def test_small_clear_tasks_pass_without_an_interview(self):
        for request in ("Fix a typo in README",
                        "Rename the author column",
                        "Update docs and tests for the parser"):
            result = assumption_guard.evaluate(self.root, request,
                                               platform="codex")
            self.assertEqual(result["status"], "allowed")
            self.assertTrue(result["allowed"])
            self.assertIsNone(result["interview"])
            self.assertIsNone(result["question"])
        self.assertEqual(self._interview_count(), 0)
        self._no_plan_or_engine_artifacts()

    def test_auth_is_critical_in_turkish_english_and_dotted_i(self):
        request_hash = hashlib.sha256(
            "Kullanıcı giriş sistemini geliştir".encode()).hexdigest()
        for request in ("Kullanıcı giriş sistemini geliştir",
                        "Add login to the app",
                        "KİMLİK DOĞRULAMA ekle"):
            result = assumption_guard.evaluate(self.root, request,
                                               platform="codex")
            self.assertEqual(result["status"], "interview_required")
            self.assertEqual(result["question"]["question_key"], "auth_session")
            self.assertTrue(result["reasons"])
            self.assertEqual(result["receipt"]["status"], "interview_required")
            self.assertEqual(result["receipt"]["request_hash"],
                             hashlib.sha256(request.encode()).hexdigest())
            self.assertIn("context_digest", result["receipt"])
            self.assertIn("head_sha", result["receipt"])
            self.assertIn("assumptions", result["receipt"])
        self.assertEqual(
            assumption_guard.evaluate(
                self.root, "Kullanıcı giriş sistemini geliştir",
                platform="codex")["request_hash"], request_hash)
        self._no_plan_or_engine_artifacts()

    def test_off_still_blocks_critical_but_allows_clear_tasks(self):
        blocked = assumption_guard.evaluate(
            self.root, "Add authentication", platform="codex", mode="off")
        self.assertEqual(blocked["status"], "interview_required")
        self.assertEqual(blocked["interview"]["mode"], "off")
        self.assertEqual(blocked["interview"]["status"], "blocked")
        allowed = assumption_guard.evaluate(
            self.root, "Fix a typo in README", platform="codex", mode="off")
        self.assertEqual(allowed["status"], "allowed")

    def test_required_always_interviews_even_clear_tasks(self):
        result = assumption_guard.evaluate(
            self.root, "Fix a typo in README", platform="codex",
            mode="required")
        self.assertEqual(result["status"], "interview_required")
        self.assertEqual(result["question"]["question_key"], "scope")

    def test_approved_interview_with_matching_hash_is_allowed(self):
        request = "Kullanıcı giriş sistemini geliştir"
        with GrillMeEngine(self.root) as engine:
            started = engine.start(request, origin_platform="codex",
                                   mode="auto")
            first = started["question"]
            nxt = engine.answer(first["question_id"], option="jwt")["next"]
            engine.answer(nxt["question_id"], option="refresh")
            approved = engine.approve()
        result = assumption_guard.evaluate(
            self.root, request, platform="opencode",
            interview_id=approved["interview_id"])
        self.assertEqual(result["status"], "allowed")
        self.assertEqual(result["interview_id"], approved["interview_id"])
        self.assertEqual(result["receipt"]["status"], "allowed")
        self.assertEqual(result["receipt"]["interview_id"],
                         approved["interview_id"])
        with self.assertRaises(assumption_guard.AssumptionGuardError):
            assumption_guard.evaluate(
                self.root, "Başka bir görev", platform="opencode",
                interview_id=approved["interview_id"])

    def test_approved_interview_matches_hash_after_request_trimming(self):
        request = "Kullanıcı giriş sistemini geliştir\n"
        with GrillMeEngine(self.root) as engine:
            started = engine.start(request, origin_platform="codex",
                                   mode="auto")
            nxt = engine.answer(started["question"]["question_id"],
                                option="jwt")["next"]
            engine.answer(nxt["question_id"], option="refresh")
            approved = engine.approve()
        result = assumption_guard.evaluate(
            self.root, "  " + request + "  ", platform="codex",
            interview_id=approved["interview_id"])
        self.assertEqual(result["status"], "allowed")

    def test_docs_only_evidence_never_suppresses_a_question(self):
        (self.root / "README.md").write_text(
            "This project uses JWT and PostgreSQL.\n", encoding="utf-8")
        result = assumption_guard.evaluate(
            self.root, "Kullanıcı giriş sistemini geliştir", platform="codex")
        self.assertEqual(result["status"], "interview_required")
        self.assertEqual(result["question"]["question_key"], "auth_session")

    def test_contradictory_choice_against_code_is_detected(self):
        (self.root / "backend").mkdir()
        (self.root / "backend" / "auth.py").write_text(
            "import jwt\n", encoding="utf-8")
        result = assumption_guard.evaluate(
            self.root, "Use session cookies for authentication",
            platform="codex")
        self.assertEqual(result["status"], "interview_required")
        self.assertEqual(result["question"]["question_key"], "auth_session")
        self.assertEqual(result["question"]["recommended_option"], "jwt")
        self.assertTrue(any(item["topic"] == "auth_session"
                            for item in result["contradictions"]))
        self.assertTrue(any("Contradiction" in reason
                            for reason in result["reasons"]))

    def test_contradictory_choice_against_confirmed_decision_is_detected(self):
        with ProjectMemory(self.root) as memory:
            memory.add_decision(
                topic="auth_session", decision="session cookie",
                source="user", source_ref="seed:confirmed", status="confirmed",
                confirmed_by="user")
        result = assumption_guard.evaluate(
            self.root, "Use JWT for authentication", platform="codex")
        self.assertEqual(result["status"], "interview_required")
        self.assertEqual(result["question"]["recommended_option"], "cookie")
        self.assertEqual(result["contradictions"][0]["source"],
                         "confirmed decision")

    def test_token_boundaries_do_not_match_author(self):
        self.assertEqual(
            assumption_guard.evaluate(
                self.root, "Update the author biography", platform="codex"
            )["status"], "allowed")
        self.assertEqual(
            assumption_guard.evaluate(
                self.root, "Add auth middleware", platform="codex"
            )["status"], "interview_required")

    def test_risk_order_puts_financial_and_destructive_topics_first(self):
        result = assumption_guard.evaluate(
            self.root,
            "Ödeme, giriş, veri silme ve migrasyon ekle",
            platform="codex")
        self.assertEqual(result["question"]["question_key"], "payments")
        topics = result["topics"]
        self.assertLess(topics.index("data_retention"), topics.index("auth_session"))

    def test_critical_topic_matrix(self):
        expected = {
            "Kullanıcı paylaşımı ekle": "file_access",
            "Dosya yükleme depolaması ekle": "file_storage",
            "Veritabanı migrasyonu ekle": "database",
            "Kalıcı silme özelliği ekle": "data_retention",
            "Veriyi üçüncü tarafa aktar": "external_transfer",
            "Breaking API değişikliği yap": "api_compat",
            "Kredi kartı ödemesi ekle": "payments",
            "Rol ve yetki modeli ekle": "roles",
        }
        for request, key in expected.items():
            result = assumption_guard.evaluate(self.root, request,
                                               platform="codex")
            self.assertEqual(result["status"], "interview_required", request)
            self.assertIn(result["question"]["question_key"],
                          (key, "file_access", "roles"), request)

    def test_admin_accounts_email_and_response_shape_are_critical(self):
        for request in ("Add an admin panel", "Remove user accounts",
                        "Send welcome emails",
                        "Change the /api/users response shape"):
            result = assumption_guard.evaluate(self.root, request,
                                               platform="codex")
            self.assertEqual(result["status"], "interview_required", request)

    def test_json_schema_validator_is_not_a_database_choice(self):
        result = assumption_guard.evaluate(
            self.root, "Add a JSON schema validator", platform="codex")
        self.assertEqual(result["status"], "allowed")

    def test_receipt_is_deterministic_for_the_same_context(self):
        first = assumption_guard.evaluate(
            self.root, "Fix a typo in README", platform="codex")
        second = assumption_guard.evaluate(
            self.root, "Fix a typo in README", platform="codex")
        self.assertEqual(first["context_digest"], second["context_digest"])
        self.assertEqual(first["receipt"], second["receipt"])


    def test_deterministic_questions_carry_decision_metadata(self):
        questions = assumption_guard.critical_questions(
            "Ödeme, giriş ve veri silme ekle", [], {}, mode="auto")
        self.assertEqual([item["key"] for item in questions],
                         ["payments", "data_retention", "auth_session"])
        for question in questions:
            self.assertTrue(question["why"])
            self.assertTrue(question["rationale"])
            self.assertTrue(question["alternatives"])
            self.assertTrue(question["affected_requirements"])
            self.assertTrue(question["critical"])
            self.assertIsNone(question["dependency"])
            for option in question["options"]:
                self.assertEqual(set(option), {"id", "label", "recommended"})
            self.assertEqual(
                question["recommended_option"],
                next(option["id"] for option in question["options"]
                     if option["recommended"]))

    def test_followup_and_scope_metadata_are_deterministic(self):
        followup = assumption_guard.followup_questions("auth_session", "jwt")[0]
        self.assertEqual(followup["key"], "jwt_refresh")
        self.assertEqual(followup["dependency"], "auth_session")
        self.assertTrue(followup["why"])
        self.assertTrue(followup["rationale"])
        scope = assumption_guard.critical_questions(
            "Fix a typo in README", [], {}, mode="required")[0]
        self.assertEqual(scope["key"], "scope")
        self.assertTrue(scope["why"])
        self.assertTrue(scope["affected_requirements"])

    def test_receipt_digest_is_unaffected_by_question_metadata(self):
        first = assumption_guard.evaluate(
            self.root, "Fix a typo in README", platform="codex")
        second = assumption_guard.evaluate(
            self.root, "Fix a typo in README", platform="codex")
        self.assertEqual(first["context_digest"], second["context_digest"])
        self.assertEqual(first["guard_receipt"], second["guard_receipt"])


if __name__ == "__main__":
    unittest.main()
