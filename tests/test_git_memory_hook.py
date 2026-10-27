from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import git_memory_hook


class GitMemoryHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "-C", str(self.root), "init"],
                       check=True, capture_output=True)

    @unittest.skipIf(os.name == "nt", "Git hook shell markers are POSIX-covered")
    def test_install_preserves_existing_hook_and_uninstall_restores_it(self):
        path = git_memory_hook.hook_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\necho user-hook\n", encoding="utf-8")
        installed = git_memory_hook.install(self.root)
        self.assertEqual(installed["state"], "installed")
        text = path.read_text(encoding="utf-8")
        self.assertIn("echo user-hook", text)
        self.assertIn(git_memory_hook.BEGIN, text)
        if os.name != "nt":
            self.assertTrue(path.stat().st_mode & stat.S_IXUSR)
        self.assertTrue(git_memory_hook.status(self.root)["managed"])
        removed = git_memory_hook.uninstall(self.root)
        self.assertEqual(removed["state"], "updated")
        restored = path.read_text(encoding="utf-8")
        self.assertIn("echo user-hook", restored)
        self.assertNotIn(git_memory_hook.BEGIN, restored)

    def test_hook_is_idempotent_and_does_not_touch_unmanaged_hook(self):
        path = git_memory_hook.hook_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\necho untouched\n", encoding="utf-8")
        self.assertEqual(git_memory_hook.uninstall(self.root)["state"],
                         "preserved")
        self.assertIn("echo untouched", path.read_text(encoding="utf-8"))
        first = git_memory_hook.install(self.root)
        second = git_memory_hook.install(self.root)
        self.assertEqual(first["state"], "installed")
        self.assertIn(second["state"], ("unchanged", "updated"))
        self.assertEqual(path.read_text(encoding="utf-8").count(
            git_memory_hook.BEGIN), 1)

    @unittest.skipIf(os.name == "nt", "Git hook shell execution is POSIX-covered")
    def test_commit_runs_the_managed_capture_hook(self):
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "universal_teamwork.py").write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "root = Path(sys.argv[sys.argv.index('--root') + 1])\n"
            "(root / 'hook-ran.json').write_text(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8")
        git_memory_hook.install(self.root)
        subprocess.run(["git", "-C", str(self.root), "config",
                        "user.email", "t@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config",
                        "user.name", "T"], check=True)
        (self.root / "README.md").write_text("hook\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "hook"],
                       check=True, capture_output=True)
        self.assertTrue((self.root / "hook-ran.json").is_file())

    def test_non_git_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(git_memory_hook.HookError):
                git_memory_hook.install(Path(temp))


if __name__ == "__main__":
    unittest.main()
