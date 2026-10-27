from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.grill_me import GrillError, GrillMeEngine
from tools.project_memory import ProjectMemory


class GrillMeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_one_question_and_recommendation_is_not_confirmed(self):
        with GrillMeEngine(self.root) as engine:
            result = engine.start("Kullanıcı giriş sistemini geliştir",
                                  origin_platform="codex", mode="auto")
            question = result["question"]
            self.assertEqual(question["question_key"], "auth_session")
            self.assertEqual(question["options"][0]["recommended"], True)
            self.assertIsNone(question["selected_option"])
            memory = ProjectMemory(self.root)
            try:
                self.assertEqual(memory.list_decisions(status="confirmed"), [])
                self.assertEqual(memory.list_decisions(status="code_derived"), [])
            finally:
                memory.close()

    def test_answer_creates_confirmed_decision_and_skip_blocks(self):
        with GrillMeEngine(self.root) as engine:
            start = engine.start("Kullanıcı giriş sistemini geliştir",
                                 origin_platform="codex", mode="auto")
            q = start["question"]
            answered = engine.answer(q["question_id"], option="jwt")
            self.assertEqual(answered["next"]["question_key"], "jwt_refresh")
            summary = engine.summary()
            self.assertFalse(summary["ready"])
            self.assertTrue(any(item["status"] == "confirmed"
                                for item in summary["decisions"]))
            skipped = engine.skip(answered["next"]["question_id"])
            self.assertIsNone(skipped["next"])
            blocked = engine.summary()
            self.assertFalse(blocked["ready"])
            with self.assertRaises(GrillError):
                engine.approve()

    def test_back_invalidates_dependent_questions(self):
        with GrillMeEngine(self.root) as engine:
            start = engine.start("Kullanıcı giriş sistemini geliştir",
                                 origin_platform="codex", mode="auto")
            first = start["question"]
            engine.answer(first["question_id"], option="jwt")
            second = engine.next_question(start["interview"]["interview_id"])
            back = engine.back(second["question_id"])
            self.assertEqual(back["question"]["status"], "open")
            self.assertIsNone(back["question"]["selected_option"])
            with ProjectMemory(self.root) as memory:
                decisions = memory.list_decisions(topic="jwt_refresh")
                self.assertTrue(all(item["status"] == "superseded" for item in decisions))

    def test_skipped_critical_question_can_be_resumed_and_answered(self):
        with GrillMeEngine(self.root) as engine:
            start = engine.start("Kullanıcı giriş sistemini geliştir",
                                 origin_platform="codex", mode="auto")
            skipped = engine.skip(start["question"]["question_id"])
            self.assertFalse(engine.summary()["ready"])
            with ProjectMemory(self.root) as memory:
                self.assertEqual(len(memory.list_requirements(
                    status="unresolved_blocking")), 1)
            resumed = engine.resume()
            self.assertEqual(resumed["question"]["question_key"], "auth_session")
            answered = engine.answer(resumed["question"]["question_id"],
                                     option="jwt")
            self.assertEqual(answered["next"]["question_key"], "jwt_refresh")
            engine.answer(answered["next"]["question_id"], option="refresh")
            self.assertTrue(engine.summary()["ready"])
            with ProjectMemory(self.root) as memory:
                self.assertEqual(memory.list_requirements(
                    status="unresolved_blocking"), [])
            self.assertTrue(engine.approve()["approved"])

    def test_back_invalidates_answered_dependent_and_does_not_approve_stale_answer(self):
        with GrillMeEngine(self.root) as engine:
            start = engine.start("Kullanıcı giriş sistemini geliştir",
                                 origin_platform="codex", mode="auto")
            first = start["question"]
            answered = engine.answer(first["question_id"], option="jwt")
            second = answered["next"]
            engine.back(first["question_id"])
            engine.answer(first["question_id"], option="cookie")
            expiry = engine.next_question(start["interview"]["interview_id"])
            self.assertEqual(expiry["question_key"], "session_expiry")
            engine.answer(expiry["question_id"], option="idle30")
            summary = engine.summary()
            self.assertTrue(summary["ready"])
            self.assertEqual(summary["blocking_questions"], [])
            with ProjectMemory(self.root) as memory:
                stale = [item for item in memory.list_decisions(topic="jwt_refresh")]
                self.assertTrue(all(item["status"] == "superseded" for item in stale))
            with self.assertRaises(GrillError):
                engine.answer(second["question_id"], option="refresh")

    def test_new_request_does_not_silently_reuse_open_interview(self):
        with GrillMeEngine(self.root) as engine:
            engine.start("Kullanıcı giriş sistemini geliştir",
                         origin_platform="codex", mode="auto")
            with self.assertRaises(GrillError):
                engine.start("Çok kullanıcılı dosya paylaşımı geliştir",
                             origin_platform="opencode", mode="auto")

    def test_code_derived_facts_suppress_questions(self):
        (self.root / "backend").mkdir()
        (self.root / "backend" / "auth.py").write_text(
            "import jwt\n# postgresql connection\n", encoding="utf-8")
        with GrillMeEngine(self.root) as engine:
            result = engine.start("Kullanıcı giriş sistemini geliştir",
                                  origin_platform="codex", mode="auto")
            self.assertIsNone(result["question"])
            self.assertEqual(result["interview"]["status"], "ready")
            summary = engine.summary()
            self.assertTrue(summary["ready"])
            with ProjectMemory(self.root) as memory:
                decisions = memory.list_decisions()
                self.assertTrue(any(item["topic"] == "auth_session"
                                    and item["status"] == "code_derived"
                                    for item in decisions))

    def test_cross_ide_resume_keeps_answers(self):
        with GrillMeEngine(self.root) as codex:
            start = codex.start("Kullanıcı giriş sistemini geliştir",
                                origin_platform="codex", mode="auto")
            codex.answer(start["question"]["question_id"], option="jwt")
        with GrillMeEngine(self.root) as opencode:
            resumed = opencode.resume()
            self.assertEqual(resumed["interview"]["origin_platform"], "codex")
            self.assertEqual(resumed["question"]["question_key"], "jwt_refresh")
            self.assertEqual(resumed["interview"]["status"], "open")
            answered = opencode.answer(resumed["question"]["question_id"], option="refresh")
            self.assertIsNone(answered["next"])
            self.assertTrue(opencode.approve()["approved"])

    def test_off_blocks_critical_and_required_forces_scope(self):
        with GrillMeEngine(self.root) as engine:
            blocked = engine.start("Kullanıcı giriş sistemini geliştir",
                                   origin_platform="codex", mode="off")
            self.assertEqual(blocked["interview"]["status"], "blocked")
            engine.cancel()
        with GrillMeEngine(self.root) as engine:
            required = engine.start("Küçük bir yardımcı metin ekle",
                                    origin_platform="codex", mode="required")
            self.assertEqual(required["question"]["question_key"], "scope")

    def test_superseded_decision_does_not_suppress_a_question(self):
        with ProjectMemory(self.root) as memory:
            memory.add_decision(
                topic="auth_session", decision="JWT", source="user",
                source_ref="seed:superseded", status="confirmed",
                confirmed_by="user")
            memory.mark_decisions_superseded(source_ref="seed:superseded")
        with GrillMeEngine(self.root) as engine:
            result = engine.start("Kullanıcı giriş sistemini geliştir",
                                  origin_platform="codex", mode="auto")
            self.assertEqual(result["question"]["question_key"], "auth_session")

    def test_docs_comments_and_tests_do_not_prove_a_technology(self):
        (self.root / "README.md").write_text(
            "This project uses JWT, PostgreSQL and S3.\n", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text(
            "# session cookie\n# postgresql\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_auth.py").write_text(
            "import jwt\n", encoding="utf-8")
        with GrillMeEngine(self.root) as engine:
            result = engine.start("Kullanıcı giriş sistemini geliştir",
                                  origin_platform="codex", mode="auto")
            self.assertEqual(result["question"]["question_key"], "auth_session")
        from tools.repo_facts import scan_repository
        facts = scan_repository(self.root)
        self.assertFalse(facts["code_detected"]["jwt"])
        self.assertFalse(facts["code_detected"]["session"])
        self.assertTrue(facts["docs_detected"]["jwt"])
        self.assertEqual(facts["docs_evidence"]["jwt"], ["README.md"])
        self.assertNotIn(str(self.root), json.dumps(facts))

    def test_contradiction_question_compares_request_with_code(self):
        (self.root / "backend").mkdir()
        (self.root / "backend" / "auth.py").write_text(
            "import jwt\n", encoding="utf-8")
        with GrillMeEngine(self.root) as engine:
            result = engine.start("Use session cookies for authentication",
                                  origin_platform="codex", mode="auto")
            question = result["question"]
            self.assertEqual(question["question_key"], "auth_session")
            self.assertIn("JWT", question["question"])
            self.assertEqual(question["recommended_option"], "jwt")

    def test_dynamic_followups_are_risk_relevant(self):
        with GrillMeEngine(self.root) as engine:
            started = engine.start("Oturum yönetimi ekle",
                                   origin_platform="codex", mode="auto")
            answered = engine.answer(started["question"]["question_id"],
                                     option="cookie")
            self.assertEqual(answered["next"]["question_key"], "session_expiry")
        with GrillMeEngine(self.root) as engine:
            started = engine.start("Veritabanı migrasyonu ekle",
                                   origin_platform="codex", mode="auto",
                                   allow_new=True)
            answered = engine.answer(started["question"]["question_id"],
                                     option="postgres")
            self.assertEqual(answered["next"]["question_key"], "db_rollback")
        with GrillMeEngine(self.root) as engine:
            started = engine.start("Breaking API değişikliği yap",
                                   origin_platform="codex", mode="auto",
                                   allow_new=True)
            answered = engine.answer(started["question"]["question_id"],
                                     option="versioned")
            self.assertEqual(answered["next"]["question_key"], "api_deprecation")
        with GrillMeEngine(self.root) as engine:
            started = engine.start("Dosya paylaşım sistemi ekle",
                                   origin_platform="codex", mode="auto",
                                   allow_new=True)
            answered = engine.answer(started["question"]["question_id"],
                                     option="team")
            self.assertEqual(answered["next"]["question_key"], "file_delete")

    def test_dependent_question_waits_for_its_parent(self):
        with GrillMeEngine(self.root) as engine:
            start = engine.start("Dosya paylaşımı ve depolama ekle",
                                 origin_platform="codex", mode="auto")
            first = start["question"]
            self.assertEqual(first["question_key"], "file_access")
            interview_id = start["interview"]["interview_id"]
            rows = {row["question_key"]: row for row in engine.conn.execute(
                "SELECT * FROM interview_questions WHERE interview_id=?",
                (interview_id,))}
            self.assertEqual(json.loads(rows["file_storage"]["depends_on"]),
                             ["file_access"])
            self.assertEqual(engine.next_question(interview_id)["question_key"],
                             "file_access")
            with self.assertRaises(GrillError):
                engine.answer(rows["file_storage"]["question_id"], option="local")

    def test_initial_questions_are_risk_ordered(self):
        with GrillMeEngine(self.root) as engine:
            start = engine.start("Ödeme, giriş ve veri silme ekle",
                                 origin_platform="codex", mode="auto")
            self.assertEqual(start["question"]["question_key"], "payments")

    def test_approve_builds_contract_and_preserves_last_platform(self):
        with ProjectMemory(self.root) as memory:
            memory.update_meta(last_platform="opencode", last_run_id="run-7")
        with GrillMeEngine(self.root) as engine:
            started = engine.start("Küçük bir metin düzeltmesi yap",
                                   origin_platform="codex", mode="required")
            question = started["question"]
            self.assertEqual(question["question_key"], "scope")
            engine.answer(question["question_id"], option="none")
            approved = engine.approve()
        self.assertTrue(approved["approved"])
        contract = approved["requirements_contract"]
        self.assertTrue(contract["contract_id"].startswith("rc_"))
        self.assertEqual(approved["summary"]["requirements_contract"], contract)
        path = (self.root / ".auto-orchestrator" / "contracts" /
                f"{contract['contract_id']}-v{contract['contract_version']}.json")
        self.assertTrue(path.is_file())
        with ProjectMemory(self.root) as memory:
            state = memory.context_state()
        self.assertEqual(state["last_platform"], "opencode")
        self.assertEqual(state["last_run_id"], "run-7")

    def test_cookie_approval_contract_round_trips(self):
        from tools.requirements_contract import load_current

        with GrillMeEngine(self.root) as engine:
            started = engine.start("Oturum yönetimi ekle",
                                   origin_platform="codex", mode="auto")
            answered = engine.answer(started["question"]["question_id"],
                                     option="cookie")
            self.assertEqual(answered["next"]["question_key"], "session_expiry")
            engine.answer(answered["next"]["question_id"], option="idle30")
            approved = engine.approve()
        self.assertTrue(approved["approved"])
        contract = load_current(self.root)
        self.assertEqual(contract["interview_id"],
                         approved["interview_id"])
        self.assertTrue(any(
            item.get("evidence", {}).get("expected") == "session"
            for item in contract["acceptance_criteria"]))


if __name__ == "__main__":
    unittest.main()
