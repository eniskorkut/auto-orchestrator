import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tools.install_global import execute, InstallError, BEGIN, END

class GlobalInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.home=Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def run_install(self, **kwargs):
        return execute(self.home, {'codex','opencode','antigravity'}, **kwargs)
    def test_all(self):
        self.run_install()
        self.assertTrue((self.home/'.codex/skills/auto-orchestrator/SKILL.md').exists())
        self.assertTrue((self.home/'.config/opencode/plugins/auto_orchestrator.js').exists())
        self.assertIn("id: 'auto-orchestrator'",(self.home/'.config/opencode/plugins/auto_orchestrator.js').read_text())
        self.assertTrue((self.home/'.gemini/config/skills/auto-orchestrator/SKILL.md').exists())
        self.assertTrue((self.home/'.gemini/config/agents/auto_explorer/agent.md').exists())
    def test_idempotent(self):
        self.run_install()
        self.run_install()
        txt=(self.home/'.codex/AGENTS.md').read_text()
        self.assertEqual(txt.count(BEGIN),1)
        self.assertEqual(txt.count(END),1)
    def test_dry_run(self):
        self.run_install(dry_run=True)
        self.assertFalse((self.home/'.auto-orchestrator').exists())
    def test_preserves_rules(self):
        path=self.home/'.codex/AGENTS.md'
        path.parent.mkdir()
        path.write_text('Keep security checks.\n')
        self.run_install()
        self.assertIn('Keep security checks.',path.read_text())
        self.run_install(uninstall=True)
        self.assertIn('Keep security checks.',path.read_text())
        self.assertNotIn(BEGIN,path.read_text())
    def test_preserves_modified_files_on_uninstall(self):
        self.run_install()
        path=self.home/'.config/opencode/agents/auto_tester.md'
        path.write_text('my customization')
        notes=self.run_install(uninstall=True)
        self.assertTrue(path.exists())
        self.assertIn('preserved modified file','\n'.join(notes))
        self.assertFalse((self.home/'.gemini/config/skills/auto-orchestrator/SKILL.md').exists())
    def test_collision_is_preflighted(self):
        path=self.home/'.config/opencode/plugins/auto_orchestrator.js'
        path.parent.mkdir(parents=True)
        path.write_text('user code')
        with self.assertRaises(InstallError):self.run_install()
        self.assertFalse((self.home/'.codex/skills/auto-orchestrator/SKILL.md').exists())
    def test_existing_codex_configuration(self):
        path=self.home/'.codex/config.toml'
        path.parent.mkdir()
        path.write_text('[other]\nsecret = "unchanged"\n')
        self.run_install()
        self.assertEqual(path.read_text(),'[other]\nsecret = "unchanged"\n')
        with self.assertRaises(InstallError):self.run_install(codex_model='luna-max')
    def test_codex_luna_max(self):
        self.run_install(codex_model='luna-max')
        self.assertIn('gpt-6-luna',(self.home/'.codex/config.toml').read_text())
    def test_classic_plugin_option(self):
        self.run_install(opencode_version='v1')
        txt=(self.home/'.config/opencode/plugins/auto_orchestrator.js').read_text()
        self.assertIn('AutoOrchestratorPlugin',txt)

    def test_antigravity_cli_ao_grill_skill_and_v761_modules(self):
        source = Path(__file__).resolve().parents[1]
        self.run_install()
        cli_skill = (self.home/'.gemini/antigravity-cli/skills/ao-grill-me/SKILL.md')
        self.assertTrue(cli_skill.is_file(), str(cli_skill))
        self.assertEqual(cli_skill.read_bytes(),
                         (source/'.agents/skills/ao-grill-me/SKILL.md').read_bytes())
        self.assertFalse((self.home/'.gemini/antigravity-cli/skills/grill-me').exists())
        self.assertFalse((self.home/'.gemini/config/skills/grill-me').exists())
        self.assertTrue((self.home/'.codex/skills/teamwork/SKILL.md').is_file())
        self.assertTrue((self.home/'.config/opencode/skills/teamwork/SKILL.md').is_file())
        tools = {
            'codex': self.home/'.codex/auto-orchestrator/tools',
            'opencode': self.home/'.config/opencode/auto-orchestrator/tools',
            'antigravity': self.home/'.gemini/config/auto-orchestrator/tools',
        }
        for base in tools.values():
            self.assertEqual((base/'requirements_contract.py').read_bytes(),
                             (source/'tools/requirements_contract.py').read_bytes())
            if (source/'tools/assumption_guard.py').is_file():
                self.assertTrue((base/'assumption_guard.py').is_file())


    def test_v80_runtime_modules_and_cli_smoke(self):
        self.run_install()
        base = self.home/'.config/opencode/auto-orchestrator/tools'
        for name in ('native_dispatch.py', 'verification_receipt.py',
                     'test_evidence.py', 'question_quality.py'):
            self.assertTrue((base/name).is_file(), name)
        root = Path(self.tmp.name)/'smoke-project'
        root.mkdir()
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        for extra in (['--help'], ['brief'],
                      ['native-dispatch', 'status', '--run-id', 'run-global'],
                      ['native-verify', '--help']):
            with self.subTest(command=extra):
                process = subprocess.run(
                    [sys.executable, str(base/'universal_teamwork.py'),
                     '--root', str(root), '--platform', 'opencode', *extra],
                    cwd=str(root), env=env, capture_output=True, text=True,
                    timeout=60)
                self.assertEqual(process.returncode, 0,
                                 process.stderr[-800:])

    def test_v78_handoff_module_is_installed_for_every_platform(self):
        source = Path(__file__).resolve().parents[1] / 'tools' / 'ide_handoff.py'
        self.run_install()
        for base in (
            self.home/'.codex/auto-orchestrator/tools',
            self.home/'.config/opencode/auto-orchestrator/tools',
            self.home/'.gemini/config/auto-orchestrator/tools',
        ):
            if source.is_file():
                self.assertEqual((base/'ide_handoff.py').read_bytes(),
                                 source.read_bytes())
            else:
                self.assertFalse((base/'ide_handoff.py').exists())
        # Cursor is not part of this helper's platforms; install it separately.
        execute(self.home, {'cursor'})
        cursor_tools = self.home/'.cursor/auto-orchestrator/tools'
        if source.is_file():
            self.assertEqual((cursor_tools/'ide_handoff.py').read_bytes(),
                             source.read_bytes())

    def test_reinstall_preserves_user_files_outside_managed_integration(self):
        self.run_install()
        memory = self.home/'.auto-orchestrator/memory.sqlite3'
        memory.parent.mkdir(parents=True, exist_ok=True)
        memory.write_bytes(b'user memory')
        hook_note = self.home/'.config/opencode/AGENTS.md'
        before = hook_note.read_text(encoding='utf-8')
        notes = self.run_install()
        self.assertTrue(any('installed' in note for note in notes))
        self.assertEqual(memory.read_bytes(), b'user memory')
        self.assertIn('auto-orchestrator', hook_note.read_text(encoding='utf-8'))
        self.assertNotEqual(before, '', 'the installer writes one managed block')
        self.run_install(uninstall=True)
        self.assertEqual(memory.read_bytes(), b'user memory')

    def test_global_antigravity_upgrade_prunes_old_grill_me_managed_copy(self):
        self.run_install()
        old = self.home/'.gemini/antigravity-cli/skills/grill-me/SKILL.md'
        old.parent.mkdir(parents=True)
        old.write_text('old generated grill-me\n', encoding='utf-8')
        manifest_path = self.home/'.auto-orchestrator/auto-orchestrator-global-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files'][str(old.resolve())] = hashlib.sha256(
            old.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        notes = self.run_install()
        self.assertFalse(old.exists())
        self.assertIn('pruned obsolete managed integration', '\n'.join(notes))
        self.assertTrue((self.home/'.gemini/antigravity-cli/skills/ao-grill-me/SKILL.md').is_file())

    def test_managed_skill_upgrade_backs_up_and_prunes_obsolete_integration(self):
        self.run_install()
        manifest_path = self.home/'.auto-orchestrator/auto-orchestrator-global-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        skill = self.home/'.codex/skills/auto-orchestrator/SKILL.md'
        old = skill.read_text(encoding='utf-8') + '\nold generated revision\n'
        skill.write_text(old, encoding='utf-8')
        manifest['files'][str(skill.resolve())] = hashlib.sha256(
            skill.read_bytes()).hexdigest()
        obsolete = self.home/'.codex/skills/old-generated/SKILL.md'
        obsolete.parent.mkdir(parents=True)
        obsolete.write_text('obsolete integration\n', encoding='utf-8')
        manifest['files'][str(obsolete.resolve())] = hashlib.sha256(
            obsolete.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        notes = self.run_install()
        self.assertEqual(skill.read_text(encoding='utf-8'),
                         (Path(__file__).resolve().parents[1] /
                          '.agents/skills/auto-orchestrator/SKILL.md').read_text(encoding='utf-8'))
        backup = skill.with_name(skill.name + '.bak')
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding='utf-8'), old)
        self.assertFalse(obsolete.exists())
        self.assertIn('pruned obsolete managed integration', '\n'.join(notes))
        updated = json.loads(manifest_path.read_text())
        self.assertNotIn(str(obsolete.resolve()), updated['files'])

    def test_global_codex_antigravity_hooks_point_at_installed_bridge(self):
        import json
        self.run_install()
        codex=json.loads((self.home/'.codex/hooks.json').read_text())
        cmd=codex['hooks']['SubagentStart'][0]['hooks'][0]['command']
        self.assertIn(str((self.home/'.codex/auto-orchestrator/tools/hook_bridge.py').resolve()),cmd)
        self.assertTrue((self.home/'.codex/auto-orchestrator/tools/perf.py').is_file())
        antigravity=json.loads((self.home/'.gemini/config/hooks.json').read_text())
        self.assertIn(str((self.home/'.gemini/config/auto-orchestrator/tools/hook_bridge.py').resolve()),
                      antigravity['auto-orchestrator']['SessionStart'][0]['command'])
    def test_existing_global_hooks_are_preserved(self):
        import json
        for rel in ['.codex/hooks.json','.gemini/config/hooks.json']:
            path=self.home/rel
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('{"owned-by-user":{}}')
        notes=self.run_install()
        self.assertIn('integrated native hooks',' '.join(notes))
        import json
        codex=json.loads((self.home/'.codex/hooks.json').read_text())
        self.assertEqual(codex['owned-by-user'],{})
        self.assertEqual(len(codex['hooks']['SubagentStart']),1)
        antigravity=json.loads((self.home/'.gemini/config/hooks.json').read_text())
        self.assertEqual(antigravity['owned-by-user'],{})
        self.assertIn('auto-orchestrator',antigravity)
        self.run_install()
        codex2=json.loads((self.home/'.codex/hooks.json').read_text())
        self.assertEqual(len(codex2['hooks']['SubagentStart']),1)
        self.run_install(uninstall=True)
        self.assertEqual(json.loads((self.home/'.codex/hooks.json').read_text()),{'owned-by-user':{}})
        self.assertEqual(json.loads((self.home/'.gemini/config/hooks.json').read_text()),{'owned-by-user':{}})

if __name__=='__main__':unittest.main()
