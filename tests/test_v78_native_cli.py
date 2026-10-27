from __future__ import annotations

import io
import json
import secrets
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import teamwork_chat
from tools import universal_teamwork as universal
from tools.project_memory import ProjectMemory

try:
    from tools import ide_handoff as real_handoff
except ModuleNotFoundError:  # pragma: no cover - optional sibling module
    real_handoff = None


class FakeHandoff:
    """Small stand-in for the documented ide_handoff public API."""

    def __init__(self, owner=None):
        self.owner = owner
        self.fence = 0
        self.state = "active" if owner else "inactive"
        self.token = None
        self.handoff = None
        self.calls = []

    def _result(self, *, ok=True, status=None, **extra):
        value = {
            "ok": ok,
            "status": status or self.state,
            "state": self.state,
            "fence": self.fence,
            "owner": self.owner,
            "handoff": self.handoff,
            "reasons": [],
        }
        value.update(extra)
        return value

    def status(self, root):
        self.calls.append(("status",))
        return self._result()

    def activate(self, root, ide=None, session_id=None, ttl=900,
                 pid=None, owner_id=None, **kwargs):
        self.calls.append(("activate", ide))
        if self.owner:
            return self._result(ok=False, status="conflict")
        self.fence += 1
        self.token = secrets.token_hex(24)
        self.owner = {"owner_id": owner_id or ide, "ide": ide,
                      "session_id": session_id, "token_present": True}
        self.state = "active"
        return self._result(owner_token=self.token)

    def heartbeat(self, root, owner_token, fence):
        self.calls.append(("heartbeat", fence))
        if owner_token != self.token or fence != self.fence:
            return self._result(ok=False, status="conflict")
        return self._result()

    def prepare_handoff(self, root, owner_token, fence, target_ide,
                        next_action=None, allow_dirty=False, confirm_idle=False):
        self.calls.append(("prepare", target_ide))
        if owner_token != self.token or fence != self.fence:
            return self._result(ok=False, status="conflict")
        if not confirm_idle:
            return self._result(ok=False, status="blocked")
        self.fence += 1
        self.state = "handoff_prepared"
        self.handoff = {
            "handoff_id": "ho_" + secrets.token_hex(12),
            "digest": secrets.token_hex(32),
            "source_fence": fence,
            "target_ide": target_ide,
        }
        return self._result()

    def release(self, root, owner_token, fence, handoff_id=None):
        self.calls.append(("release", fence))
        if owner_token != self.token or fence != self.fence:
            return self._result(ok=False, status="conflict")
        self.fence += 1
        self.owner = None
        self.state = "released"
        return self._result()

    def accept(self, root, handoff_id, digest, ide, session_id=None):
        self.calls.append(("accept", ide))
        if self.handoff is None or self.handoff["handoff_id"] != handoff_id:
            return self._result(ok=False, status="blocked")
        self.fence += 1
        self.token = secrets.token_hex(24)
        self.owner = {"owner_id": ide, "ide": ide, "session_id": session_id,
                      "token_present": True}
        self.state = "transferred"
        self.handoff = None
        return self._result(owner_token=self.token)

    def recover(self, root, actor, reason, evidence=None, apply=False):
        self.calls.append(("recover", actor))
        return self._result()


def _args(root, **values):
    defaults = {
        "root": str(root), "platform": "codex", "request": None,
        "request_file": None, "request_stdin": False, "mode": "auto",
        "run_id": None, "model": "fake-model", "max_seconds": 10,
        "grill": False, "grill_mode": "auto", "provider": None,
        "interview_id": None, "native": False, "plan": None,
        "codex_sandbox": "read-only",
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _clear_request(text):
    return _args(None, request=text)


class NativeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()

    def _fake(self, **kwargs):
        return FakeHandoff(**kwargs)

    def _start_native(self, fake, *, platform="codex", run_id="native-1",
                      request="Küçük bir metin düzeltmesi yap", mode="solo"):
        args = _args(self.root, platform=platform, request=request,
                     mode=mode, run_id=run_id, native=True)
        with mock.patch.object(universal, "_ide_handoff_module",
                               return_value=fake):
            return universal.start(args)

    def test_native_start_writes_plan_and_engine_without_a_process(self):
        fake = self._fake()
        with mock.patch.object(teamwork_chat, "_spawn",
                               side_effect=AssertionError("spawned")):
            result = self._start_native(fake)
        self.assertEqual(result["status"], "native_plan_ready")
        self.assertEqual(result["execution"], "native")
        self.assertEqual(result["execution_provider"], "codex")
        self.assertEqual(result["ownership"]["fence"], 1)
        self.assertNotIn("pid", result)
        self.assertNotIn("log", result)
        active = self.root / ".auto-orchestrator" / "teamwork" / "active.json"
        self.assertFalse(active.exists())
        plan = json.loads(Path(result["plan"]).read_text(encoding="utf-8"))
        self.assertTrue(plan["native"])
        self.assertEqual(plan["execution_mode"], "native")
        self.assertEqual(plan["native_platform"], "codex")
        self.assertEqual(plan["approval"]["status"], "pending")
        self.assertEqual(plan["approval"]["plan_sha256"],
                         teamwork_chat.plan_digest(plan))
        for task in plan["tasks"]:
            self.assertEqual(task["provider"], "codex")
            self.assertNotIn("sandbox", task)
            self.assertNotIn("approval_policy", task)
        state = teamwork_chat._engine_status(self.root, "native-1")
        self.assertEqual(state["run_id"], "native-1")
        self.assertEqual([call[0] for call in fake.calls],
                         ["status", "activate"])
        self.assertNotIn("release", [call[0] for call in fake.calls])

    def test_plan_approval_is_required_and_digest_mismatch_is_rejected(self):
        fake = self._fake()
        self._start_native(fake, run_id="native-approval")
        with mock.patch.object(universal, "_ide_handoff_module",
                               return_value=fake):
            with self.assertRaises(universal.UniversalError) as missing:
                universal.start(_args(
                    self.root, platform="codex", request=None, plan="native-approval"))
            self.assertIn("approval is required", str(missing.exception))
            approved = universal.approve_plan(_args(
                self.root, platform="codex", run_id="native-approval",
                approver="human"))
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(approved["approver"], "human")
            self.assertEqual(approved["plan_sha256"],
                             teamwork_chat.plan_digest(json.loads(
                                 Path(approved["plan"]).read_text(encoding="utf-8"))))
            plan_path = Path(approved["plan"])
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["notes"].append("tampered after approval")
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            with self.assertRaises(universal.UniversalError) as mismatch:
                universal.start(_args(
                    self.root, platform="codex", request=None,
                    plan="native-approval"))
        self.assertIn("digest", str(mismatch.exception).lower())

    def test_small_solo_plan_approve_and_start_are_explicit(self):
        fake = self._fake()
        with mock.patch.object(universal, "_ide_handoff_module",
                               return_value=fake):
            planned = universal.plan(_args(
                self.root, platform="codex",
                request="Küçük bir yazım hatasını düzelt", mode="solo",
                run_id="solo-plan"))
            self.assertEqual(planned["status"], "plan_ready")
            self.assertEqual(planned["approval"]["status"], "pending")
            self.assertFalse((self.root / ".auto-orchestrator" / "teamwork"
                              / "active.json").exists())
            with self.assertRaises(universal.UniversalError):
                universal.start(_args(
                    self.root, platform="codex", request=None, plan="solo-plan"))
            universal.approve_plan(_args(
                self.root, platform="codex", run_id="solo-plan",
                approver="human"))
            started = universal.start(_args(
                self.root, platform="codex", request=None, plan="solo-plan"))
        self.assertEqual(started["status"], "native_plan_ready")
        self.assertTrue(started["approved"])
        self.assertFalse((self.root / ".auto-orchestrator" / "teamwork"
                          / "active.json").exists())

    def test_foreign_owner_is_rejected_but_context_is_read_only(self):
        fake = self._fake(owner={"owner_id": "cursor-session", "ide": "cursor",
                                 "session_id": "cursor-session",
                                 "token_present": True})
        fake.fence = 4
        fake.state = "active"
        with mock.patch.object(universal, "_ide_handoff_module",
                               return_value=fake):
            context = universal.context(_args(
                self.root, platform="codex", limit=100,
                selection_budget=100, module=None))
            self.assertTrue(context["ownership"]["foreign"])
            self.assertEqual(context["owner_platform"], "cursor")
            self.assertEqual(context["fence"], 4)
            with self.assertRaises(universal.UniversalError) as rejected:
                universal.plan(_args(
                    self.root, platform="codex", request="fix typo",
                    request_file=None, request_stdin=False, mode="solo",
                    run_id="foreign-plan"))
            self.assertIn("cursor", str(rejected.exception))
            with self.assertRaises(universal.UniversalError):
                self._start_native(fake, platform="codex", run_id="foreign")
            status = universal.ide(_args(
                self.root, platform="codex", ide_command="status"))
        self.assertTrue(status["foreign"])
        self.assertFalse(any(call[0] == "activate" for call in fake.calls))

    def test_handoff_prepare_and_accept_cli(self):
        fake = self._fake()
        def run(*argv):
            output = io.StringIO()
            errors = io.StringIO()
            with mock.patch.object(universal, "_ide_handoff_module",
                                   return_value=fake), \
                    redirect_stdout(output), redirect_stderr(errors):
                code = universal.main([
                    "--root", str(self.root), "--platform", "codex", *argv])
            self.assertEqual(code, 0, errors.getvalue())
            return json.loads(output.getvalue())

        activated = run("ide", "activate", "--owner-id", "source")
        self.assertEqual(activated["status"], "active")
        prepared = run("ide", "prepare", "--target-platform", "opencode",
                       "--summary", "continue in opencode", "--owner-id",
                       "source")
        self.assertEqual(prepared["status"], "handoff_prepared")
        handoff = prepared["result"]["handoff"]
        released = run("ide", "release", "--handoff",
                       handoff["handoff_id"], "--owner-id", "source")
        self.assertEqual(released["status"], "released")
        with mock.patch.object(universal, "_ide_handoff_module",
                               return_value=fake), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = universal.main([
                "--root", str(self.root), "--platform", "opencode",
                "ide", "accept", "--handoff", handoff["handoff_id"],
                "--digest", handoff["digest"], "--owner-id", "target"])
        self.assertEqual(code, 0)
        names = [call[0] for call in fake.calls]
        for expected in ("activate", "prepare", "release", "accept"):
            self.assertIn(expected, names)
        self.assertNotIn("recover", names)

    def test_native_report_records_evidence_without_self_verification(self):
        fake = self._fake()
        self._start_native(fake, run_id="native-report")
        with mock.patch.object(universal, "_ide_handoff_module",
                               return_value=fake):
            universal.approve_plan(_args(
                self.root, platform="codex", run_id="native-report",
                approver="human"))
            universal.start(_args(
                self.root, platform="codex", request=None,
                plan="native-report"))
            prose = self.root / "prose.json"
            prose.write_text(json.dumps(["all tests passed"]),
                             encoding="utf-8")
            with self.assertRaises(universal.UniversalError) as rejected:
                universal.native_report(_args(
                    self.root, platform="codex", run_id="native-report",
                    task_id="build", status="passed", verdict="passed",
                    tests=str(prose), summary="done", lease=None))
            self.assertIn("prose", str(rejected.exception))
            failed = self.root / "failed.json"
            failed.write_text(json.dumps([
                {"name": "unit", "status": "failed", "exit_code": 1}]),
                encoding="utf-8")
            with self.assertRaises(universal.UniversalError):
                universal.native_report(_args(
                    self.root, platform="codex", run_id="native-report",
                    task_id="build", status="passed", verdict="passed",
                    tests=str(failed), summary="done", lease=None))
            tests = self.root / "tests.json"
            tests.write_text(json.dumps([
                {"name": "unit", "command": "python -m unittest",
                 "status": "passed", "exit_code": 0}]), encoding="utf-8")
            recorded = universal.native_report(_args(
                self.root, platform="codex", run_id="native-report",
                task_id="build", status="passed", verdict="passed",
                tests=str(tests), summary="done", lease=None))
        self.assertEqual(recorded["record_status"], "recorded")
        self.assertFalse(recorded["verified"])
        self.assertFalse(recorded["completion_recorded"])
        self.assertIsNone(recorded["native_session_id"])
        report = json.loads(Path(recorded["report"]).read_text(encoding="utf-8"))
        self.assertFalse(report["verified"])
        self.assertIsNone(report["native_session_id"])
        state = teamwork_chat._engine_status(self.root, "native-report")
        task = state["milestones"][0]["tasks"][0]
        self.assertEqual(task["state"], "pending")

    def test_import_native_rejects_unconfirmed_and_recommended_fields(self):
        def write(payload, name):
            path = self.root / name
            path.write_text(json.dumps(payload), encoding="utf-8")
            return path

        unconfirmed = write({
            "platform": "antigravity",
            "answers": [{"question_key": "auth_session", "decision": "JWT",
                         "confirmed": False, "confirmed_by": "user"}],
        }, "unconfirmed.json")
        model = write({
            "platform": "antigravity",
            "answers": [{"question_key": "auth_session", "decision": "JWT",
                         "confirmed": True, "confirmed_by": "model"}],
        }, "model.json")
        recommended = write({
            "platform": "antigravity",
            "answers": [{"question_key": "auth_session",
                         "decision": "JWT", "confirmed": True,
                         "confirmed_by": "user",
                         "recommended": True}],
        }, "recommended.json")
        valid = write({
            "platform": "antigravity",
            "interview_id": "native-1",
            "answers": [{"question_key": "auth_session", "decision": "JWT",
                         "rationale": "user selected JWT",
                         "confirmed": True, "confirmed_by": "user",
                         "requirements": ["JWT access and refresh tokens"]}],
        }, "valid.json")
        fake = self._fake()
        with mock.patch.object(universal, "_ide_handoff_module",
                               return_value=fake):
            for path in (unconfirmed, model, recommended):
                with self.assertRaises(universal.UniversalError):
                    universal.grill(_args(
                        self.root, platform="antigravity",
                        grill_command="import-native", file=str(path)))
            result = universal.grill(_args(
                self.root, platform="antigravity",
                grill_command="import-native", file=str(valid)))
        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["source"], "antigravity-native")
        with ProjectMemory(self.root) as memory:
            decisions = memory.list_decisions(status="confirmed")
        self.assertTrue(any(item["decision"] == "JWT" for item in decisions))

    def test_codex_skill_recommends_native_execution_and_no_second_provider(self):
        text = (Path(__file__).resolve().parents[1] / ".codex" / "skills" /
                "teamwork" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("--native", text)
        self.assertIn("approve-plan", text)
        self.assertIn("native-report", text)
        self.assertIn("ide prepare", text)
        self.assertIn("ide release", text)
        self.assertIn("native subagent", text.lower())
        self.assertNotIn("--provider opencode", text)
        self.assertIn("Never launch OpenCode", text)
        self.assertIn("never launches OpenCode", text)

    def test_provider_mismatch_explains_native_vs_managed(self):
        args = _args(self.root, platform="codex", origin_platform="codex",
                     request="Küçük bir metin düzeltmesi yap",
                     mode="solo", run_id="managed-mismatch")
        with self.assertRaises(teamwork_chat.ChatError) as raised:
            teamwork_chat.start(args)
        message = str(raised.exception)
        self.assertIn("no managed execution adapter", message)
        self.assertIn("--native", message)
        self.assertIn("managed", message)


@unittest.skipUnless(real_handoff is not None, "ide_handoff is not installed")
class RealHandoffCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / "file.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run([
            "git", "-c", "user.email=a@b", "-c", "user.name=a",
            "commit", "-qm", "init"], cwd=self.root, check=True)

    def _run(self, platform, *argv):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = universal.main([
                "--root", str(self.root), "--platform", platform, *argv])
        self.assertEqual(code, 0, errors.getvalue())
        return json.loads(output.getvalue())

    def test_real_prepare_release_accept_round_trip(self):
        self.assertEqual(self._run(
            "codex", "ide", "activate", "--owner-id", "source")["status"],
            "active")
        prepared = self._run(
            "codex", "ide", "prepare", "--target-platform", "opencode",
            "--summary", "continue", "--owner-id", "source")
        handoff = prepared["result"]["handoff"]
        self.assertEqual(prepared["status"], "handoff_prepared")
        released = self._run(
            "codex", "ide", "release", "--handoff", handoff["handoff_id"],
            "--owner-id", "source")
        self.assertEqual(released["status"], "released")
        accepted = self._run(
            "opencode", "ide", "accept", "--handoff", handoff["handoff_id"],
            "--digest", handoff["digest"], "--owner-id", "target")
        self.assertEqual(accepted["status"], "transferred")
        status = self._run("opencode", "ide", "status")
        self.assertEqual(status["owner"]["ide"], "opencode")
        self.assertFalse(status["result"]["native_cancel_ack"])


if __name__ == "__main__":
    unittest.main()
