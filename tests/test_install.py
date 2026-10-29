import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tools.install import (Conflict, END, MANIFEST_RELATIVE, START,
                           V78_MODULES, V80_MODULES, install, uninstall)

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.dest=Path(self.temp.name)

    def tearDown(self):self.temp.cleanup()

    def call(self, platform='all', mode='inherit', dry=False, force=False):
        return install(self.dest,platform,mode,dry,force)

    def test_all_installs(self):
        self.call()
        self.assertTrue((self.dest/'.agents/skills/auto-orchestrator/SKILL.md').exists())
        self.assertTrue((self.dest/'.agents/skills/ao-grill-me/SKILL.md').exists())
        self.assertTrue((self.dest/'.opencode/agents/auto_tester.md').exists())
        self.assertTrue((self.dest/'.agents/agents/auto_tester.md').exists())
        self.assertTrue((self.dest/'.codex/agents/auto_tester.toml').exists())
        self.assertTrue((self.dest/'.codex/agents/auto_planner.toml').exists())
        self.assertTrue((self.dest/'.cursor/agents/auto_planner.md').exists())
        for role in ('planner', 'implementer', 'tester', 'reviewer'):
            self.assertTrue(
                (self.dest/f'.cursor/agents/auto_{role}.md').is_file(), role)
            self.assertTrue(
                (self.dest/f'.agents/agents/auto_{role}.md').is_file(), role)
        self.assertTrue((self.dest/'.codex/skills/teamwork/SKILL.md').exists())
        self.assertTrue((self.dest/'.opencode/skills/teamwork/SKILL.md').exists())
        self.assertTrue((self.dest/'tools/perf.py').exists())

    def test_opencode_project_install_has_plugin_and_trusted_helpers(self):
        self.call('opencode')
        plugin = self.dest/'.opencode/plugins/auto_orchestrator.js'
        self.assertTrue(plugin.is_file())
        self.assertIn("resolveHelperScript", plugin.read_text(encoding='utf-8'))
        self.assertTrue((self.dest/'tools/supervisor.py').is_file())
        self.assertTrue((self.dest/'tools/router.py').is_file())
        self.assertFalse((self.dest/'.opencode/auto-orchestrator/tools').exists())

    def test_idempotent(self):
        self.call();snapshot=(self.dest/'AGENTS.md').read_text()
        self.call();self.assertEqual(snapshot,(self.dest/'AGENTS.md').read_text())
        self.assertEqual(snapshot.count(START),1)

    def test_preserves_user_guidance(self):
        p=self.dest/'AGENTS.md';p.write_text('# My project\nKeep security checks.\n')
        self.call('antigravity')
        self.assertIn('Keep security checks.',p.read_text())

    def test_preserves_user_gitignore(self):
        p=self.dest/'.gitignore';p.write_text('.env\n')
        self.call('opencode')
        self.assertIn('.env',p.read_text())
        text = p.read_text()
        self.assertIn('.auto-orchestrator/*', text)
        self.assertIn('!.auto-orchestrator/project.json', text)
        self.assertNotIn('\n.auto-orchestrator/\n', '\n' + text + '\n')

    def test_blocks_conflict_without_partial_install(self):
        p=self.dest/'.codex/config.toml';p.parent.mkdir();p.write_text('[myconfig]\nimportant=true\n')
        with self.assertRaises(Conflict):self.call('all')
        self.assertFalse((self.dest/'AGENTS.md').exists())

    def test_dry_run_writes_nothing(self):
        self.call(dry=True)
        self.assertFalse((self.dest/'AGENTS.md').exists())

    def test_luna_max_config(self):
        self.call('codex','luna-max')
        txt=(self.dest/'.codex/config.toml').read_text()
        self.assertIn('default_subagent_model = "gpt-6-luna"',txt)
        self.assertIn('default_subagent_reasoning_effort = "max"',txt)
        self.assertNotIn('model = ',(self.dest/'.codex/agents/auto_tester.toml').read_text())

    def test_switch_codex_model_mode(self):
        self.call('codex','inherit')
        self.call('codex','luna-max')
        txt=(self.dest/'.codex/config.toml').read_text()
        self.assertIn('default_subagent_model = "gpt-6-luna"',txt)

    def test_platform_scoped(self):
        self.call('opencode')
        self.assertFalse((self.dest/'.codex').exists())
        self.assertFalse((self.dest/'.agents/agents').exists())

    def test_scoped_reinstall_preserves_other_platform_integration(self):
        self.call('all')
        codex_agent = self.dest/'.codex/agents/auto_tester.toml'
        self.assertTrue(codex_agent.is_file())
        self.call('opencode')
        self.assertTrue(codex_agent.is_file())

    def test_conflicting_profile_fails(self):
        p=self.dest/'.opencode/agents/auto_tester.md';p.parent.mkdir(parents=True)
        p.write_text('important user file')
        with self.assertRaises(Conflict):self.call('opencode')
        self.assertEqual(p.read_text(),'important user file')

    def test_project_uninstall_preserves_user_files_and_modified_generated_files(self):
        guidance = self.dest/'AGENTS.md'
        guidance.write_text('# My project\nKeep security checks.\n')
        user_file = self.dest/'notes.txt'
        user_file.write_text('user notes')
        gitignore = self.dest/'.gitignore'
        gitignore.write_text('.env\n')
        self.call('opencode')
        generated = self.dest/'tools/dashboard.py'
        modified = self.dest/'tools/dashboard_assets/app.css'
        modified.write_text('user customization', encoding='utf-8')
        self.assertTrue(generated.is_file())
        dry = dict(uninstall(self.dest, dry_run=True))
        self.assertTrue(generated.is_file())
        self.assertTrue((self.dest/MANIFEST_RELATIVE).is_file())
        self.assertEqual(dry['tools/dashboard.py'], 'would-remove')
        result = dict(uninstall(self.dest))
        self.assertFalse(generated.exists())
        self.assertEqual(modified.read_text(encoding='utf-8'),
                         'user customization')
        self.assertEqual(result['tools/dashboard_assets/app.css'], 'preserved')
        self.assertEqual(user_file.read_text(), 'user notes')
        self.assertIn('Keep security checks.', guidance.read_text())
        self.assertNotIn(START, guidance.read_text())
        self.assertIn('.env', gitignore.read_text())
        self.assertNotIn('.auto-orchestrator/*', gitignore.read_text())
        # A preserved user-modified generated file keeps the manifest so the
        # user can repair or explicitly remove it later.
        self.assertTrue((self.dest/MANIFEST_RELATIVE).exists())

    def test_project_uninstall_without_manifest_removes_nothing(self):
        user_file = self.dest/'notes.txt'
        user_file.write_text('user notes')
        self.assertEqual(uninstall(self.dest), [])
        self.assertEqual(user_file.read_text(), 'user notes')

    def test_v80_runtime_modules_are_shipped_and_importable(self):
        self.call('opencode')
        tools_dir = self.dest / 'tools'
        for name in V80_MODULES:
            self.assertTrue((tools_dir / name).is_file(), name)
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        commands = [
            ['--help'],
            ['brief'],
            ['native-dispatch', 'status', '--run-id', 'run-clean'],
            ['native-verify', '--help'],
        ]
        for extra in commands:
            with self.subTest(command=extra):
                process = subprocess.run(
                    [sys.executable, str(tools_dir / 'universal_teamwork.py'),
                     '--root', str(self.dest), '--platform', 'opencode',
                     *extra], cwd=str(self.dest), env=env,
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(process.returncode, 0,
                                 process.stderr[-800:])
        dashboard = subprocess.run(
            [sys.executable, str(tools_dir / 'dashboard.py'), '--help'],
            cwd=str(self.dest), env=env, capture_output=True, text=True,
            timeout=60)
        self.assertEqual(dashboard.returncode, 0, dashboard.stderr[-500:])

    def test_v80_runtime_modules_survive_uninstall_reinstall(self):
        self.call('opencode')
        module = self.dest / 'tools' / 'native_dispatch.py'
        self.assertTrue(module.is_file())
        uninstall(self.dest)
        self.assertFalse(module.exists())
        self.call('opencode')
        self.assertTrue(module.is_file())
        self.assertEqual(module.read_bytes(),
                         (ROOT / 'tools' / 'native_dispatch.py').read_bytes())

    def test_v78_handoff_module_is_shipped_when_present(self):
        self.call('opencode')
        tools_dir = self.dest / 'tools'
        for name in V78_MODULES:
            source = ROOT / 'tools' / name
            if source.is_file():
                self.assertEqual((tools_dir / name).read_bytes(),
                                 source.read_bytes(), name)
            else:
                self.assertFalse((tools_dir / name).exists(), name)
        manifest = json.loads(
            (self.dest / MANIFEST_RELATIVE).read_text(encoding='utf-8'))
        for name in V78_MODULES:
            relative = f'tools/{name}'
            if (ROOT / 'tools' / name).is_file():
                self.assertIn(relative, manifest['files'])

    def test_v78_handoff_module_upgrades_with_manifest_ownership(self):
        source = ROOT / 'tools' / 'ide_handoff.py'
        self.call('opencode')
        module = self.dest / 'tools' / 'ide_handoff.py'
        if not source.is_file():
            self.assertFalse(module.exists())
            return
        module.write_text('old generated handoff module', encoding='utf-8')
        manifest_path = self.dest / MANIFEST_RELATIVE
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['files']['tools/ide_handoff.py'] = hashlib.sha256(
            module.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        results = dict(self.call('opencode'))
        self.assertEqual(results['tools/ide_handoff.py'], 'updated')
        self.assertEqual(module.read_bytes(), source.read_bytes())
        self.assertTrue(module.with_name('ide_handoff.py.bak').is_file())

    def test_reinstall_preserves_user_memory_hooks_and_untracked_tools(self):
        memory = self.dest / '.auto-orchestrator' / 'memory.sqlite3'
        memory.parent.mkdir(parents=True)
        memory.write_bytes(b'user memory placeholder')
        user_tool = self.dest / 'tools' / 'my_helper.py'
        user_tool.parent.mkdir(parents=True)
        user_tool.write_text('print("mine")\n', encoding='utf-8')
        cursor_hooks = self.dest / '.cursor' / 'hooks.json'
        cursor_hooks.parent.mkdir(parents=True)
        cursor_hooks.write_text('{"mine": true}\n', encoding='utf-8')
        with self.assertRaises(Conflict):
            self.call('cursor')
        self.assertEqual(cursor_hooks.read_text(encoding='utf-8'),
                         '{"mine": true}\n')
        self.assertEqual(memory.read_bytes(), b'user memory placeholder')
        self.assertEqual(user_tool.read_text(encoding='utf-8'),
                         'print("mine")\n')
        # A different platform can install without touching another
        # platform's user-owned hooks or the shared memory file.
        self.call('opencode')
        self.assertEqual(cursor_hooks.read_text(encoding='utf-8'),
                         '{"mine": true}\n')
        self.assertEqual(memory.read_bytes(), b'user memory placeholder')
        uninstall(self.dest)
        self.assertEqual(memory.read_bytes(), b'user memory placeholder')
        self.assertEqual(user_tool.read_text(encoding='utf-8'),
                         'print("mine")\n')
        self.assertEqual(cursor_hooks.read_text(encoding='utf-8'),
                         '{"mine": true}\n')

    def test_v78_obsolete_agent_file_is_pruned_only_when_managed(self):
        self.call('cursor')
        obsolete = self.dest / '.cursor' / 'agents' / 'old_v78_role.md'
        obsolete.write_text('old generated role\n', encoding='utf-8')
        manifest_path = self.dest / MANIFEST_RELATIVE
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['files']['.cursor/agents/old_v78_role.md'] = hashlib.sha256(
            obsolete.read_bytes()).hexdigest()
        manifest['created'].append('.cursor/agents/old_v78_role.md')
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        results = dict(self.call('cursor'))
        self.assertEqual(results['.cursor/agents/old_v78_role.md'],
                         'removed-obsolete')
        self.assertFalse(obsolete.exists())

    def test_manifest_upgrade_replaces_untouched_generated_module(self):
        self.call('opencode')
        module = self.dest/'tools/requirements_contract.py'
        module.write_text('old generated module', encoding='utf-8')
        manifest_path = self.dest/MANIFEST_RELATIVE
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['files']['tools/requirements_contract.py'] = hashlib.sha256(
            module.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        results = dict(self.call('opencode'))
        self.assertEqual(results['tools/requirements_contract.py'], 'updated')
        self.assertEqual(module.read_bytes(),
                         (ROOT/'tools/requirements_contract.py').read_bytes())

    def test_manifest_upgrade_refuses_user_modified_generated_module(self):
        self.call('opencode')
        module = self.dest/'tools/dashboard.py'
        module.write_text('my own dashboard', encoding='utf-8')
        with self.assertRaises(Conflict):
            self.call('opencode')
        self.assertEqual(module.read_text(), 'my own dashboard')

    def test_uninstall_removes_antigravity_cli_skill_it_created(self):
        self.call('antigravity')
        skill = (self.dest/'.gemini/antigravity-cli/skills/ao-grill-me/SKILL.md')
        self.assertTrue(skill.is_file())
        uninstall(self.dest)
        self.assertFalse(skill.exists())

    def test_antigravity_does_not_shadow_native_grill_me(self):
        self.call('antigravity')
        self.assertTrue(
            (self.dest/'.gemini/antigravity-cli/skills/ao-grill-me/SKILL.md').is_file())
        self.assertFalse(
            (self.dest/'.gemini/antigravity-cli/skills/grill-me').exists())
        self.assertFalse((self.dest/'.gemini/config/skills/grill-me').exists())

    def test_antigravity_upgrade_prunes_old_grill_me_generated_path(self):
        self.call('antigravity')
        old = self.dest/'.gemini/antigravity-cli/skills/grill-me/SKILL.md'
        old.parent.mkdir(parents=True)
        old.write_text('old generated grill-me\n', encoding='utf-8')
        manifest_path = self.dest/MANIFEST_RELATIVE
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['files']['.gemini/antigravity-cli/skills/grill-me/SKILL.md'] = \
            hashlib.sha256(old.read_bytes()).hexdigest()
        manifest['created'].append('.gemini/antigravity-cli/skills/grill-me/SKILL.md')
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        results = dict(self.call('antigravity'))
        self.assertEqual(
            results['.gemini/antigravity-cli/skills/grill-me/SKILL.md'],
            'removed-obsolete')
        self.assertFalse(old.exists())
        self.assertTrue(
            (self.dest/'.gemini/antigravity-cli/skills/ao-grill-me/SKILL.md').is_file())

    def test_managed_skill_upgrade_creates_backup_and_prunes_obsolete_paths(self):
        self.call('opencode')
        skill = self.dest/'.agents/skills/auto-orchestrator/SKILL.md'
        stale = skill.read_text(encoding='utf-8') + '\nstale generated line\n'
        skill.write_text(stale, encoding='utf-8')
        manifest_path = self.dest/MANIFEST_RELATIVE
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['files']['.agents/skills/auto-orchestrator/SKILL.md'] = hashlib.sha256(
            skill.read_bytes()).hexdigest()
        obsolete = self.dest/'.opencode/commands/old-teamwork.md'
        obsolete.write_text('old generated command\n', encoding='utf-8')
        manifest['files']['.opencode/commands/old-teamwork.md'] = hashlib.sha256(
            obsolete.read_bytes()).hexdigest()
        manifest['created'].append('.opencode/commands/old-teamwork.md')
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        results = dict(self.call('opencode'))
        self.assertEqual(results['.agents/skills/auto-orchestrator/SKILL.md'],
                         'updated')
        self.assertTrue(skill.with_name('SKILL.md.bak').is_file())
        self.assertEqual(skill.with_name('SKILL.md.bak').read_text(encoding='utf-8'),
                         stale)
        self.assertEqual(results['.opencode/commands/old-teamwork.md'],
                         'removed-obsolete')
        self.assertFalse(obsolete.exists())
        upgraded = json.loads(manifest_path.read_text(encoding='utf-8'))
        self.assertNotIn('.opencode/commands/old-teamwork.md',
                         upgraded['files'])
        self.assertNotIn('.opencode/commands/old-teamwork.md',
                         upgraded['created'])

    def test_user_modified_skill_is_preserved_not_upgraded(self):
        self.call('opencode')
        skill = self.dest/'.agents/skills/auto-orchestrator/SKILL.md'
        skill.write_text('my own skill', encoding='utf-8')
        with self.assertRaises(Conflict):
            self.call('opencode')
        self.assertEqual(skill.read_text(encoding='utf-8'), 'my own skill')

    def test_uninstall_keeps_file_that_preexisted_identical(self):
        skill = self.dest/'.agents/skills/grill-me/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_bytes((ROOT/'.agents/skills/grill-me/SKILL.md').read_bytes())
        self.call('opencode')
        uninstall(self.dest)
        self.assertTrue(skill.is_file())
        self.assertEqual(skill.read_bytes(),
                         (ROOT/'.agents/skills/grill-me/SKILL.md').read_bytes())


class OpenCodeVersionTests(unittest.TestCase):
    def test_v2_default_and_v1_switch(self):
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)
            install(target,'opencode','inherit',False,False)
            plugin=target/'.opencode/plugins/auto_orchestrator.js'
            self.assertIn("id: 'auto-orchestrator'",plugin.read_text())
            with self.assertRaises(Conflict):
                install(target,'opencode','inherit',False,False,'v1')
            install(target,'opencode','inherit',False,True,'v1')
            self.assertIn('AutoOrchestratorPlugin',plugin.read_text())
            self.assertTrue(plugin.with_name('auto_orchestrator.js.bak').exists())

if __name__=='__main__':unittest.main()
