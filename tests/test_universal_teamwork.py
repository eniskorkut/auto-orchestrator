from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import teamwork_chat
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

    def test_approved_interview_requires_the_original_full_request(self):
        from tools.grill_me import GrillMeEngine
        request = ("Kullanıcı giriş sistemini geliştir\n"
                   "Ayrıca denetim kaydı ve oturum süresi istiyorum.")
        with GrillMeEngine(self.root) as engine:
            start = engine.start(request, origin_platform="codex", mode="auto")
            question = start["question"]
            nxt = engine.answer(question["question_id"], option="jwt")["next"]
            engine.answer(nxt["question_id"], option="refresh")
            approved = engine.approve()
        captured = {}
        def fake_start(args):
            captured["request"] = args.request
            captured["provider"] = args.provider
            return {"status": "started", "run_id": "fake"}
        with mock.patch.object(universal.teamwork_chat, "start", fake_start):
            result = universal.start(self._args(
                request=request, request_file=None, request_stdin=False,
                mode="auto", run_id=None, model=None, max_seconds=10,
                grill=False, grill_mode="auto", provider="opencode",
                interview_id=approved["interview_id"]))
        self.assertEqual(result["status"], "started")
        self.assertEqual(captured["request"], request)
        self.assertEqual(captured["provider"], "opencode")

    def test_approved_interview_rejects_a_different_request(self):
        from tools.grill_me import GrillMeEngine
        request = "Kullanıcı giriş sistemini geliştir"
        with GrillMeEngine(self.root) as engine:
            start = engine.start(request, origin_platform="codex", mode="auto")
            nxt = engine.answer(start["question"]["question_id"], option="jwt")["next"]
            engine.answer(nxt["question_id"], option="refresh")
            approved = engine.approve()
        with self.assertRaises(universal.UniversalError):
            universal.start(self._args(
                request="Başka bir görev", request_file=None,
                request_stdin=False, mode="auto", run_id=None, model=None,
                max_seconds=10, grill=False, grill_mode="auto",
                provider="opencode", interview_id=approved["interview_id"]))

    def test_pause_rejects_path_traversal_run_id(self):
        victim = self.root.parent / "victim.json"
        with self.assertRaises(teamwork_chat.ChatError):
            universal.pause(self._args(run_id="../../victim"))
        self.assertFalse(victim.exists())

    def test_codex_start_requires_explicit_provider(self):
        (self.root / ".git").mkdir()
        with self.assertRaises(teamwork_chat.ChatError) as raised:
            universal.start(self._args(
                request="Küçük bir değişiklik yap", request_file=None,
                request_stdin=False, mode="auto", run_id=None, model=None,
                max_seconds=10, grill=False, grill_mode="auto",
                interview_id=None, provider=None))
        self.assertIn("no managed execution adapter", str(raised.exception))
        self.assertFalse((self.root / ".auto-orchestrator" / "plans").exists())

    def test_start_requires_request_without_approved_interview(self):
        with self.assertRaises(universal.UniversalError):
            universal.start(self._args(
                request=None, request_file=None, request_stdin=False,
                mode="auto", run_id=None, model=None, max_seconds=10,
                grill=False, grill_mode="auto", interview_id=None))


if __name__ == "__main__":
    unittest.main()
