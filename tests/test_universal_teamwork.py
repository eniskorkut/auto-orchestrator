from __future__ import annotations

import json
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
            captured["interview_id"] = args.interview_id
            captured["guard_receipt"] = args.guard_receipt
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
        self.assertEqual(captured["interview_id"], approved["interview_id"])
        self.assertEqual(captured["guard_receipt"]["status"], "allowed")
        self.assertEqual(captured["guard_receipt"]["interview_id"],
                         approved["interview_id"])

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

    def test_ambiguous_start_returns_interview_without_starting_code(self):
        (self.root / ".git").mkdir()
        with mock.patch.object(universal.teamwork_chat, "start") as start:
            result = universal.start(self._args(
                request="Çok kullanıcılı dosya paylaşım sistemi ekle",
                request_file=None, request_stdin=False, mode="auto",
                run_id=None, model=None, max_seconds=10, grill=False,
                grill_mode="auto", provider=None, interview_id=None))
        start.assert_not_called()
        self.assertEqual(result["status"], "interview_required")
        self.assertEqual(result["question"]["question_key"], "file_access")
        self.assertTrue(result["interview"]["interview_id"].startswith("iv_"))
        self.assertEqual(result["assumption_guard"]["status"],
                         "interview_required")
        self.assertFalse((self.root / ".auto-orchestrator" / "plans").exists())
        self.assertFalse((self.root / ".auto-orchestrator" / "teamwork" /
                          "active.json").exists())

    def test_clear_start_passes_guard_receipt_to_chat(self):
        captured = {}

        def fake_start(args):
            captured["guard_receipt"] = args.guard_receipt
            captured["interview_id"] = args.interview_id
            return {"status": "started", "run_id": "fake"}

        with mock.patch.object(universal.teamwork_chat, "start", fake_start):
            result = universal.start(self._args(
                request="Küçük bir metin düzeltmesi yap", request_file=None,
                request_stdin=False, mode="auto", run_id=None, model=None,
                max_seconds=10, grill=False, grill_mode="auto",
                provider="opencode", interview_id=None))
        self.assertEqual(result["status"], "started")
        self.assertEqual(captured["guard_receipt"]["status"], "allowed")
        self.assertIsNone(captured["interview_id"])

    def test_off_mode_still_returns_a_critical_interview(self):
        with mock.patch.object(universal.teamwork_chat, "start") as start:
            result = universal.start(self._args(
                request="Add login", request_file=None, request_stdin=False,
                mode="auto", run_id=None, model=None, max_seconds=10,
                grill=False, grill_mode="off", provider="opencode",
                interview_id=None))
        start.assert_not_called()
        self.assertEqual(result["status"], "interview_required")
        self.assertEqual(result["interview"]["mode"], "off")
        self.assertEqual(result["question"]["question_key"], "auth_session")

    def test_pause_rejects_path_traversal_run_id(self):
        victim = self.root.parent / "victim.json"
        with self.assertRaises(teamwork_chat.ChatError):
            universal.pause(self._args(run_id="../../victim"))
        self.assertFalse(victim.exists())

    def test_global_platform_is_preserved_for_grill_subcommands(self):
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = universal.main([
                "--root", str(self.root), "--platform", "codex",
                "grill", "start", "--request",
                "Kullanıcı giriş sistemini geliştir"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["interview"]["origin_platform"], "codex")

    def test_codex_workspace_write_is_explicit_and_planned(self):
        (self.root / ".git").mkdir()
        result = universal.start(self._args(
            request="Küçük bir değişiklik yap", request_file=None,
            request_stdin=False, mode="auto", run_id="codex-write-plan",
            model="gpt-6.1-sol", max_seconds=10, grill=False,
            grill_mode="plan-only", interview_id=None, provider="codex",
            platform="codex", codex_sandbox="workspace-write"))
        self.assertEqual(result["status"], "planned")
        plan = json.loads(Path(result["plan"]).read_text(encoding="utf-8"))
        for task in plan["tasks"]:
            self.assertEqual(task["provider"], "codex")
            self.assertEqual(task["sandbox"], "workspace-write")
            self.assertEqual(task["approval_policy"], "never")

    def test_plan_only_creates_a_plan_without_spawning_a_runner(self):
        (self.root / ".git").mkdir()
        result = universal.start(self._args(
            request="Küçük bir değişiklik yap", request_file=None,
            request_stdin=False, mode="auto", run_id="plan-only-test",
            model="fake", max_seconds=10, grill=False, grill_mode="plan-only",
            interview_id=None, provider="opencode"))
        self.assertEqual(result["status"], "planned")
        self.assertTrue(result["plan_only"])
        self.assertTrue(Path(result["plan"]).is_file())
        self.assertFalse((self.root / ".auto-orchestrator" / "teamwork"
                          / "active.json").exists())

    def test_codex_start_requires_explicit_provider(self):
        (self.root / ".git").mkdir()
        with self.assertRaises(teamwork_chat.ChatError) as raised:
            universal.start(self._args(
                request="Küçük bir değişiklik yap", request_file=None,
                request_stdin=False, mode="auto", run_id=None, model=None,
                max_seconds=10, grill=False, grill_mode="auto",
                interview_id=None, provider=None))
        self.assertIn("no managed execution adapter", str(raised.exception))
        self.assertIn("--native", str(raised.exception))
        self.assertIn("managed", str(raised.exception))
        self.assertFalse((self.root / ".auto-orchestrator" / "plans").exists())

    def test_start_requires_request_without_approved_interview(self):
        with self.assertRaises(universal.UniversalError):
            universal.start(self._args(
                request=None, request_file=None, request_stdin=False,
                mode="auto", run_id=None, model=None, max_seconds=10,
                grill=False, grill_mode="auto", interview_id=None))


    def test_context_includes_bounded_selection_block(self):
        with ProjectMemory(self.root) as memory:
            memory.add_requirement(
                "Resolve login session expiry",
                category="unresolved_blocking",
                status="unresolved_blocking", source="user")
            memory.add_decision(
                topic="auth_session", decision="JWT", rationale="login tokens",
                source="user", status="confirmed", confirmed_by="user")
        context = universal.context(self._args(
            limit=1200, request="Fix login session", selection_budget=500,
            module=None))
        self.assertIn("selection", context)
        selection = context["selection"]
        self.assertEqual(selection, context["selected_context"])
        self.assertTrue(any(ref.startswith("requirement:")
                            for ref in selection["refs"]))
        self.assertLessEqual(selection["token_estimate"],
                             selection["budget_tokens"])
        for item in selection["items"]:
            self.assertTrue(item["provenance"])
            self.assertTrue(item["ref"])


if __name__ == "__main__":
    unittest.main()
