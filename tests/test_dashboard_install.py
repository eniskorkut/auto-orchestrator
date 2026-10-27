"""Installer coverage for the read-only evidence dashboard and V7.1 runtime."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.install import Conflict, install
from tools.install_global import execute

SOURCE = Path(__file__).resolve().parents[1]
ASSETS = ("index.html", "app.css", "app.js", "i18n.js", "office.js")
RUNTIME_MODULES = (
    "project_registry.py",
    "event_stream.py",
    "event_model.py",
    "office_model.py",
    "capabilities.py",
    "telemetry.py",
    "project_planner.py",
    "executor.py",
    "teamwork.py",
    "teamwork_planner.py",
    "managed_exec.py",
    "teamwork_runner.py",
    "teamwork_benchmark.py",
    "teamwork_cli.py",
    "teamwork_chat.py",
    "scheduler.py",
    "run_owner.py",
    "repo_profile.py",
    "project_identity.py",
    "project_memory.py",
    "repo_facts.py",
    "grill_me.py",
    "universal_teamwork.py",
    "requirements_contract.py",
    "git_memory_hook.py",
    "completion_engine.py",
    "context_selector.py",
    "grill_llm.py",
    "grill_benchmark.py",
)
OPTIONAL_RUNTIME_MODULES = ("assumption_guard.py", "ide_handoff.py")
PLATFORM_TOOLS = {
    "codex": ".codex/auto-orchestrator/tools",
    "opencode": ".config/opencode/auto-orchestrator/tools",
    "antigravity": ".gemini/config/auto-orchestrator/tools",
    "cursor": ".cursor/auto-orchestrator/tools",
}


def source_bytes(relative: str) -> bytes:
    return (SOURCE / relative).read_bytes()


def assert_runtime(test: unittest.TestCase, tools: Path) -> None:
    test.assertEqual((tools / "dashboard.py").read_bytes(),
                     source_bytes("tools/dashboard.py"))
    for name in ASSETS:
        path = tools / "dashboard_assets" / name
        test.assertTrue(path.is_file(), str(path))
        test.assertEqual(path.read_bytes(),
                         source_bytes(f"tools/dashboard_assets/{name}"))
    for name in RUNTIME_MODULES:
        path = tools / name
        test.assertTrue(path.is_file(), str(path))
        test.assertEqual(path.read_bytes(), source_bytes(f"tools/{name}"))
    for name in OPTIONAL_RUNTIME_MODULES:
        if (SOURCE / "tools" / name).is_file():
            path = tools / name
            test.assertTrue(path.is_file(), str(path))
            test.assertEqual(path.read_bytes(), source_bytes(f"tools/{name}"))


class ProjectDashboardInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name)

    def test_project_install_places_dashboard_and_v71_modules(self):
        install(self.target, "opencode", "inherit", False, False)
        assert_runtime(self, self.target / "tools")
        self.assertTrue((self.target / "AGENTS.md").is_file())
        for name in ("teamwork.md", "grill-me.md"):
            command = self.target / ".opencode" / "commands" / name
            self.assertTrue(command.is_file(), str(command))
            self.assertIn("$ARGUMENTS", command.read_text(encoding="utf-8"))
        skill = self.target / ".agents" / "skills" / "grill-me" / "SKILL.md"
        self.assertTrue(skill.is_file())
        self.assertIn("one question", skill.read_text(encoding="utf-8").lower())
        self.assertTrue(
            (self.target / ".opencode" / "skills" / "teamwork" / "SKILL.md").is_file())

    def test_project_install_ships_cursor_and_antigravity_role_agents(self):
        install(self.target, "all", "inherit", False, False)
        for role in ("planner", "implementer", "tester", "reviewer"):
            cursor = self.target / ".cursor" / "agents" / f"auto_{role}.md"
            antigravity = (self.target / ".agents" / "agents" /
                           f"auto_{role}.md")
            self.assertTrue(cursor.is_file(), str(cursor))
            self.assertTrue(antigravity.is_file(), str(antigravity))
            self.assertIn("name:", cursor.read_text(encoding="utf-8"))
            self.assertIn("subagent: true",
                          antigravity.read_text(encoding="utf-8"))
            for path in (cursor, antigravity):
                self.assertNotIn("/teamwork", path.read_text(encoding="utf-8"))

    def test_project_install_ships_v78_handoff_module_when_present(self):
        install(self.target, "opencode", "inherit", False, False)
        for name in ("ide_handoff.py",):
            source = SOURCE / "tools" / name
            if source.is_file():
                installed = self.target / "tools" / name
                self.assertEqual(installed.read_bytes(), source.read_bytes())
            else:
                self.assertFalse((self.target / "tools" / name).exists())

    def test_project_antigravity_install_copies_ao_grill_skill_to_cli(self):
        install(self.target, "antigravity", "inherit", False, False)
        skill = (self.target / ".gemini" / "antigravity-cli" / "skills" /
                 "ao-grill-me" / "SKILL.md")
        self.assertTrue(skill.is_file(), str(skill))
        self.assertEqual(skill.read_bytes(),
                         source_bytes(".agents/skills/ao-grill-me/SKILL.md"))
        self.assertFalse(
            (self.target / ".gemini" / "antigravity-cli" / "skills" /
             "grill-me").exists())

    def test_project_dry_run_writes_nothing(self):
        results = dict(install(self.target, "opencode", "inherit", True, False))
        self.assertEqual(results["tools/dashboard.py"], "created")
        self.assertEqual(results["tools/dashboard_assets/app.css"], "created")
        for name in RUNTIME_MODULES:
            self.assertEqual(results[f"tools/{name}"], "created")
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

    def test_project_reinstall_upgrades_missing_v71_modules(self):
        install(self.target, "opencode", "inherit", False, False)
        missing = self.target / "tools" / "project_planner.py"
        missing.unlink()
        results = dict(install(self.target, "opencode", "inherit", False, False))
        self.assertIn(results["tools/project_planner.py"], {"created", "updated"})
        self.assertEqual(missing.read_bytes(), source_bytes("tools/project_planner.py"))
        self.assertEqual(results["tools/dashboard.py"], "unchanged")

    def test_each_conflicting_v71_module_aborts_preflight(self):
        for name in RUNTIME_MODULES:
            with self.subTest(module=name), tempfile.TemporaryDirectory() as temp:
                target = Path(temp)
                module = target / "tools" / name
                module.parent.mkdir(parents=True)
                module.write_text("user-owned " + name, encoding="utf-8")
                with self.assertRaises(Conflict):
                    install(target, "opencode", "inherit", False, False)
                self.assertEqual(module.read_text(encoding="utf-8"),
                                 "user-owned " + name)
                self.assertFalse((target / "tools/dashboard.py").exists())
                self.assertFalse((target / "AGENTS.md").exists())


class GlobalDashboardInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_global_install_places_dashboard_and_v71_modules_on_every_platform(self):
        execute(self.home, set(PLATFORM_TOOLS))
        for platform, relative in PLATFORM_TOOLS.items():
            with self.subTest(platform=platform):
                assert_runtime(self, self.home / relative)

    def test_global_antigravity_cli_gets_ao_grill_skill(self):
        execute(self.home, {"antigravity"})
        skill = (self.home / ".gemini" / "antigravity-cli" / "skills" /
                 "ao-grill-me" / "SKILL.md")
        self.assertTrue(skill.is_file(), str(skill))
        self.assertEqual(skill.read_bytes(),
                         source_bytes(".agents/skills/ao-grill-me/SKILL.md"))
        self.assertFalse(
            (self.home / ".gemini" / "antigravity-cli" / "skills" / "grill-me").exists())
        self.assertFalse(
            (self.home / ".gemini" / "config" / "skills" / "grill-me").exists())

    def test_global_install_ships_cursor_and_antigravity_role_agents(self):
        execute(self.home, {"cursor", "antigravity"})
        for role in ("planner", "implementer", "tester", "reviewer"):
            cursor = self.home / ".cursor" / "agents" / f"auto_{role}.md"
            antigravity = (self.home / ".gemini" / "config" / "agents" /
                           f"auto_{role}" / "agent.md")
            self.assertTrue(cursor.is_file(), str(cursor))
            self.assertTrue(antigravity.is_file(), str(antigravity))
        # Antigravity keeps its native commands: no invented shadow file.
        self.assertFalse((self.home / ".gemini/antigravity-cli/skills/teamwork").exists())
        self.assertFalse((self.home / ".gemini/config/skills/grill-me").exists())

    def test_global_uninstall_preserves_user_manifest_directory_files(self):
        platforms = set(PLATFORM_TOOLS)
        execute(self.home, platforms)
        user_file = self.home / ".auto-orchestrator" / "user-note.txt"
        user_file.write_text("keep me", encoding="utf-8")
        execute(self.home, platforms, uninstall=True)
        self.assertEqual(user_file.read_text(encoding="utf-8"), "keep me")
        self.assertFalse(
            (self.home / ".auto-orchestrator" /
             "auto-orchestrator-global-manifest.json").exists())

    def test_global_manifest_records_v71_modules(self):
        execute(self.home, set(PLATFORM_TOOLS))
        manifest = json.loads(
            (self.home / ".auto-orchestrator" /
             "auto-orchestrator-global-manifest.json").read_text(encoding="utf-8")
        )
        recorded = manifest["files"]
        for relative in PLATFORM_TOOLS.values():
            for name in RUNTIME_MODULES + OPTIONAL_RUNTIME_MODULES:
                source = SOURCE / "tools" / name
                if source.is_file():
                    self.assertIn(str((self.home / relative / name).resolve()),
                                  recorded)
        self.assertIn("dashboard.py", "\n".join(recorded))

    def test_global_dry_run_writes_no_runtime_files(self):
        notes = execute(self.home, {"opencode"}, dry_run=True)
        self.assertTrue(any("would install" in note for note in notes))
        tools = self.home / ".config/opencode/auto-orchestrator/tools"
        self.assertFalse(tools.exists())
        self.assertFalse((self.home / ".auto-orchestrator").exists())

    def test_global_reinstall_restores_missing_v71_module(self):
        platforms = set(PLATFORM_TOOLS)
        execute(self.home, platforms)
        tools = self.home / PLATFORM_TOOLS["opencode"]
        (tools / "project_planner.py").unlink()
        notes = execute(self.home, platforms)
        self.assertTrue((tools / "project_planner.py").is_file())
        self.assertTrue(any("installed" in note for note in notes))

    def test_global_uninstall_removes_managed_dashboard_and_v71_modules(self):
        platforms = set(PLATFORM_TOOLS)
        execute(self.home, platforms)
        execute(self.home, platforms, uninstall=True)
        for relative in PLATFORM_TOOLS.values():
            tools = self.home / relative
            self.assertFalse((tools / "dashboard.py").exists())
            for name in ASSETS:
                self.assertFalse((tools / "dashboard_assets" / name).exists())
            for name in RUNTIME_MODULES + OPTIONAL_RUNTIME_MODULES:
                self.assertFalse((tools / name).exists(), str(tools / name))
        cli_skill = (self.home / ".gemini" / "antigravity-cli" / "skills" /
                     "ao-grill-me" / "SKILL.md")
        self.assertFalse(cli_skill.exists())

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

    def test_global_uninstall_preserves_modified_v71_module(self):
        platforms = set(PLATFORM_TOOLS)
        execute(self.home, platforms)
        modified = {}
        for relative in PLATFORM_TOOLS.values():
            path = self.home / relative / "telemetry.py"
            path.write_text("user customization: " + relative, encoding="utf-8")
            modified[path] = path.read_text(encoding="utf-8")
        notes = execute(self.home, platforms, uninstall=True)
        self.assertIn("preserved modified file", "\n".join(notes))
        for path, content in modified.items():
            self.assertTrue(path.is_file(), str(path))
            self.assertEqual(path.read_text(encoding="utf-8"), content)
            self.assertFalse((path.parent / "dashboard.py").exists())
            self.assertFalse((path.parent / "capabilities.py").exists())


if __name__ == "__main__":
    unittest.main()
