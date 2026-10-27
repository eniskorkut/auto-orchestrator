import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from tools.install import install, uninstall, Conflict, START, END, MANIFEST_RELATIVE

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
        self.assertTrue((self.dest/'.opencode/agents/auto_tester.md').exists())
        self.assertTrue((self.dest/'.agents/agents/auto_tester.md').exists())
        self.assertTrue((self.dest/'.codex/agents/auto_tester.toml').exists())
        self.assertTrue((self.dest/'tools/perf.py').exists())

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
        self.assertFalse((self.dest/MANIFEST_RELATIVE).exists())

    def test_project_uninstall_without_manifest_removes_nothing(self):
        user_file = self.dest/'notes.txt'
        user_file.write_text('user notes')
        self.assertEqual(uninstall(self.dest), [])
        self.assertEqual(user_file.read_text(), 'user notes')

    def test_manifest_upgrade_replaces_untouched_generated_module(self):
        self.call('opencode')
        module = self.dest/'tools/requirements_contract.py'
        module.write_text('old generated module', encoding='utf-8')
        manifest_path = self.dest/MANIFEST_RELATIVE
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['files']['tools/requirements_contract.py'] = hashlib.sha256(
            b'old generated module').hexdigest()
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
        skill = (self.dest/'.gemini/antigravity-cli/skills/grill-me/SKILL.md')
        self.assertTrue(skill.is_file())
        uninstall(self.dest)
        self.assertFalse(skill.exists())

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
