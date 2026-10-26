from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
