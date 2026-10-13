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

if __name__=='__main__':unittest.main()
