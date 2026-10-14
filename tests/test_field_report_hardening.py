"""Regressions from the OpenCode V2 Windows field report."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tools.install import Conflict, install
from tools.install_global import InstallError, execute


class FieldReportHardening(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.project = self.base / "project"
        self.project.mkdir()

    def test_opencode_global_includes_telemetry_and_benchmark(self):
        execute(self.home, {"opencode"}, opencode_version="v2")
        root = self.home / ".config/opencode/auto-orchestrator/tools"
        self.assertTrue((root / "perf.py").is_file())
        self.assertTrue((root / "benchmark.py").is_file())
        self.assertTrue((root / "supervisor.py").is_file())
        self.assertTrue((self.home / ".config/opencode/plugins/auto_orchestrator.js").is_file())
        execute(self.home, {"opencode"}, opencode_version="v2", uninstall=True)
        self.assertFalse((root / "perf.py").exists())

    def test_project_installs_benchmark_and_absolute_hook_command(self):
        install(self.project, "codex", "inherit", False, False)
        self.assertTrue((self.project / "tools/benchmark.py").is_file())
        hooks = json.loads((self.project / ".codex/hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn(str(Path(sys.executable)), command)
        self.assertIn(str((self.project / "tools/hook_bridge.py").resolve()), command)
        self.assertNotIn("python tools/hook_bridge.py", command)

    def test_global_hook_uses_absolute_interpreter(self):
        execute(self.home, {"codex"})
        hooks = json.loads((self.home / ".codex/hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["SubagentStart"][0]["hooks"][0]["command"]
        self.assertIn(str(Path(sys.executable)), command)

    def make_link(self, location, target):
        try:
            location.parent.mkdir(parents=True, exist_ok=True)
            location.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("Symlink creation unavailable: " + str(exc))

    def test_global_install_rejects_link_outside_home(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.make_link(self.home / ".config", outside)
        with self.assertRaises(InstallError):
            execute(self.home, {"opencode"}, opencode_version="v2")
        self.assertFalse((outside / "opencode").exists())

    def test_project_install_rejects_link_outside_project(self):
        outside = self.base / "external"
        outside.mkdir()
        self.make_link(self.project / ".opencode", outside)
        with self.assertRaises(Conflict):
            install(self.project, "opencode", "inherit", False, False)
        self.assertFalse((outside / "agents").exists())

    def test_uninstall_rejects_manifest_path_outside_home(self):
        managed = self.home / ".auto-orchestrator"
        managed.mkdir()
        outside = self.base / "outside-file"
        outside.write_text("not owned")
        from tools.install_global import MANIFEST
        (managed / MANIFEST).write_text(
            json.dumps({"files": {str(outside): "fake-hash"}, "guidance": []}), encoding="utf-8"
        )
        with self.assertRaises(InstallError):
            execute(self.home, {"opencode"}, uninstall=True)
        self.assertEqual(outside.read_text(), "not owned")


if __name__ == "__main__":
    unittest.main()
