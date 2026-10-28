"""V7.9 continuity brief stays read-only and carries the resume context."""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools import universal_teamwork as universal

REQUEST = ("Add a short contributor note to the README and one small unit "
           "test. Keep the change documentation-only.")


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "proje"
        self.root.mkdir()
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "tests@example.invalid"),
                     ("config", "user.name", "V79 Brief")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True,
                           capture_output=True)
        (self.root / ".gitignore").write_text(".auto-orchestrator/\n",
                                              encoding="utf-8")
        (self.root / "README.md").write_text("# demo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "init"],
                       check=True, capture_output=True)

    def cli(self, platform, *args, expect=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = universal.main(["--root", str(self.root), "--platform",
                                   platform, *map(str, args)])
        self.assertEqual(code, expect,
                         f"{args} -> {code}: {stderr.getvalue()[:400]}")
        text = stdout.getvalue().strip()
        return json.loads(text) if text else {}

    def request_path(self):
        from tools import teamwork_chat
        value = teamwork_chat.request_path(
            type("A", (), {"root": str(self.root)})())
        target = Path(value["absolute_path"])
        target.write_text(REQUEST, encoding="utf-8")
        return str(target)

    def _snapshot(self):
        state = self.root / ".auto-orchestrator"
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in state.rglob("*") if path.is_file()
        } if state.exists() else {}

    def test_brief_on_an_empty_project_is_read_only(self):
        before = self._snapshot()
        brief = self.cli("cursor", "brief")
        self.assertTrue(brief["read_only"])
        self.assertFalse(brief["ownership"]["active"])
        self.assertTrue(brief["resume_steps"])
        self.assertFalse((self.root / ".auto-orchestrator" / "memory.sqlite3")
                         .exists())
        self.assertEqual(self._snapshot(), before)

    def test_brief_after_handoff_shows_the_real_resume_state(self):
        relative = self.request_path()
        self.cli("codex", "ide", "activate")
        self.cli("codex", "plan", "--request-file", relative, "--run-id",
                 "run-brief-1", "--grill-mode", "off")
        self.cli("codex", "approve-plan", "--run-id", "run-brief-1",
                 "--approver", "tests")
        self.cli("codex", "start", "--plan", "run-brief-1")
        prepared = self.cli(
            "codex", "ide", "prepare", "--target-platform", "opencode",
            "--confirm-idle", "user", "--next-action",
            "Finish the README note")
        item = prepared["result"]["handoff"]
        self.cli("codex", "ide", "release", "--handoff", item["handoff_id"],
                 "--confirm-idle", "user")
        self.cli("opencode", "ide", "accept", "--handoff", item["handoff_id"],
                 "--digest", item["digest"])
        brief = self.cli("opencode", "brief")
        self.assertEqual(brief["active_ide"], "opencode")
        self.assertEqual(brief["previous_ide"], "codex")
        self.assertEqual(brief["run"]["run_id"], "run-brief-1")
        self.assertEqual(brief["next_action"], "Finish the README note")
        self.assertTrue(brief["run"]["pending_tasks"])
        self.assertIn("Next action", " ".join(brief["resume_steps"]))
        self.assertFalse(brief["ownership"]["foreign"])

    def test_brief_is_read_only_for_a_foreign_owner(self):
        self.cli("codex", "ide", "activate")
        before = self._snapshot()
        brief = self.cli("cursor", "brief")
        self.assertTrue(brief["ownership"]["foreign"])
        self.assertEqual(brief["active_ide"], "codex")
        self.assertIn("Ownership is held by codex",
                      " ".join(brief["resume_steps"]))
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
