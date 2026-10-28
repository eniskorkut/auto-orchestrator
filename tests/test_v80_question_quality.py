"""V8.0 question-quality scoring and Grill-Me summary integration."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import question_quality
from tools.grill_me import GrillMeEngine


class QuestionQualityTests(unittest.TestCase):
    def test_critical_risk_question_scores_higher_than_optional(self):
        critical = question_quality.score_question({
            "question_key": "auth_session", "critical": True,
            "options": [{"id": "jwt"}], "depends_on": [],
        })
        optional = question_quality.score_question({
            "question_key": "naming", "critical": False,
            "options": [], "depends_on": [],
        })
        self.assertGreater(critical["score"], optional["score"])
        self.assertEqual(critical["risk_coverage"], 1.0)
        self.assertTrue(critical["eligible"])

    def test_answered_question_is_ineligible_and_redundant(self):
        value = question_quality.score_question(
            {"question_key": "auth_session", "critical": True,
             "depends_on": []},
            answered_keys={"auth_session"})
        self.assertEqual(value["redundancy"], 0.0)
        self.assertFalse(value["eligible"])

    def test_unmet_dependency_lowers_relevance(self):
        base = {"question_key": "jwt_refresh", "critical": True,
                "depends_on": ["auth_session"]}
        unmet = question_quality.score_question(base)
        met = question_quality.score_question(base,
                                              answered_keys={"auth_session"})
        self.assertLess(unmet["relevance"], met["relevance"])

    def test_rank_keeps_critical_and_filters_low_optional(self):
        questions = [
            {"question_key": "auth_session", "critical": True},
            {"question_key": "naming", "critical": False},
        ]
        ranked = question_quality.rank_questions(
            questions, repo_facts={"naming": "already documented"},
            threshold=0.45)
        keys = [item["question_key"] for item in ranked]
        self.assertIn("auth_session", keys)

    def test_engine_summary_exposes_quality(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text("x\n", encoding="utf-8")
            with GrillMeEngine(root) as engine:
                engine.start("Kullanıcı girişi ve token yenileme ekle",
                             origin_platform="codex", mode="auto")
                summary = engine.summary()
        question = summary["questions"][0]
        self.assertIn("quality", question)
        self.assertIsNotNone(question["quality"])
        self.assertIn("score", question["quality"])


if __name__ == "__main__":
    unittest.main()
