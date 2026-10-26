from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools import universal_teamwork as universal
from tools.project_memory import ProjectMemory


class UniversalTeamworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _args(self, **values):
        defaults = {"root": str(self.root), "platform": "codex"}
        defaults.update(values)
        return SimpleNamespace(**defaults)

    def test_context_and_grill_commands_share_project_memory(self):
        context = universal.context(self._args(limit=1200))
        self.assertEqual(context["platform"], "codex")
        self.assertTrue(context["identity"]["project_id"].startswith("p_"))
        start = universal.grill(self._args(
            grill_command="start", request="Kullanıcı giriş sistemini geliştir",
            request_file=None, request_stdin=False, grill_mode="auto",
            interview_id=None))
        question = start["question"]
        self.assertEqual(question["question_key"], "auth_session")
        answered = universal.grill(self._args(
            grill_command="answer", question_id=question["question_id"],
            option="jwt", text=None, interview_id=None))
        self.assertEqual(answered["next"]["question_key"], "jwt_refresh")
        resumed = universal.grill(self._args(
            grill_command="resume", interview_id=None, question_id=None,
            option=None, text=None))
        self.assertEqual(resumed["interview"]["origin_platform"], "codex")
        with ProjectMemory(self.root) as memory:
            self.assertTrue(any(item["status"] == "confirmed"
                                for item in memory.list_decisions()))

    def test_start_requires_request_without_approved_interview(self):
        with self.assertRaises(universal.UniversalError):
            universal.start(self._args(
                request=None, request_file=None, request_stdin=False,
                mode="auto", run_id=None, model=None, max_seconds=10,
                grill=False, grill_mode="auto", interview_id=None))


if __name__ == "__main__":
    unittest.main()
