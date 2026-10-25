"""Installer coverage for the read-only evidence dashboard and its assets."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.install import Conflict, install
from tools.install_global import execute

SOURCE = Path(__file__).resolve().parents[1]
ASSETS = ("index.html", "app.css", "app.js")
PLATFORM_TOOLS = {
    "codex": ".codex/auto-orchestrator/tools",
    "opencode": ".config/opencode/auto-orchestrator/tools",
    "antigravity": ".gemini/config/auto-orchestrator/tools",
    "cursor": ".cursor/auto-orchestrator/tools",
}


def source_bytes(relative: str) -> bytes:
    return (SOURCE / relative).read_bytes()


class ProjectDashboardInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name)

    def assert_dashboard(self, tools: Path) -> None:
        self.assertEqual((tools / "dashboard.py").read_bytes(), source_bytes("tools/dashboard.py"))
        for name in ASSETS:
            path = tools / "dashboard_assets" / name
            self.assertTrue(path.is_file(), str(path))
            self.assertEqual(path.read_bytes(), source_bytes(f"tools/dashboard_assets/{name}"))

    def test_project_install_places_dashboard_and_assets(self):
        install(self.target, "opencode", "inherit", False, False)
        self.assert_dashboard(self.target / "tools")
        self.assertTrue((self.target / "AGENTS.md").is_file())

    def test_project_dry_run_writes_nothing(self):
        results = dict(install(self.target, "opencode", "inherit", True, False))
        self.assertEqual(results["tools/dashboard.py"], "created")
        self.assertEqual(results["tools/dashboard_assets/app.css"], "created")
        self.assertEqual(list(self.target.iterdir()), [])

    def test_project_conflicting_asset_refused_without_partial_install(self):
        asset = self.target / "tools/dashboard_assets/app.css"
        asset.parent.mkdir(parents=True)
        asset.write_text("user styles", encoding="utf-8")
        with self.assertRaises(Conflict):
            install(self.target, "opencode", "inherit", False, False)
        self.assertEqual(asset.read_text(encoding="utf-8"), "user styles")
        self.assertFalse((self.target / "tools/dashboard.py").exists())
        self.assertFalse((self.target / "tools/dashboard_assets/index.html").exists())
        self.assertFalse((self.target / "AGENTS.md").exists())


class GlobalDashboardInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_global_install_places_dashboard_on_every_platform(self):
        execute(self.home, set(PLATFORM_TOOLS))
        for platform, relative in PLATFORM_TOOLS.items():
            with self.subTest(platform=platform):
                tools = self.home / relative
                self.assertEqual((tools / "dashboard.py").read_bytes(),
                                 source_bytes("tools/dashboard.py"))
                for name in ASSETS:
                    path = tools / "dashboard_assets" / name
                    self.assertTrue(path.is_file(), str(path))
                    self.assertEqual(path.read_bytes(),
                                     source_bytes(f"tools/dashboard_assets/{name}"))

    def test_global_uninstall_removes_managed_dashboard_files(self):
        platforms = set(PLATFORM_TOOLS)
        execute(self.home, platforms)
        execute(self.home, platforms, uninstall=True)
        for relative in PLATFORM_TOOLS.values():
            tools = self.home / relative
            self.assertFalse((tools / "dashboard.py").exists())
            for name in ASSETS:
                self.assertFalse((tools / "dashboard_assets" / name).exists())

    def test_global_uninstall_preserves_modified_dashboard_asset(self):
        platforms = set(PLATFORM_TOOLS)
        execute(self.home, platforms)
        modified = {}
        for relative in PLATFORM_TOOLS.values():
            path = self.home / relative / "dashboard_assets" / "app.js"
            path.write_text("user customization: " + relative, encoding="utf-8")
            modified[path] = path.read_text(encoding="utf-8")
        notes = execute(self.home, platforms, uninstall=True)
        self.assertIn("preserved modified file", "\n".join(notes))
        for path, content in modified.items():
            self.assertTrue(path.is_file(), str(path))
            self.assertEqual(path.read_text(encoding="utf-8"), content)
            self.assertFalse((path.parent.parent / "dashboard.py").exists())
            self.assertFalse((path.parent / "index.html").exists())


if __name__ == "__main__":
    unittest.main()
