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

    def test_antigravity_cli_grill_skill_and_v761_modules(self):
        source = Path(__file__).resolve().parents[1]
        self.run_install()
        cli_skill = (self.home/'.gemini/antigravity-cli/skills/grill-me/SKILL.md')
        self.assertTrue(cli_skill.is_file(), str(cli_skill))
        self.assertEqual(cli_skill.read_bytes(),
                         (source/'.agents/skills/grill-me/SKILL.md').read_bytes())
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
