import json
import tempfile
import unittest
from pathlib import Path
from tools.install import install, Conflict
from tools.install_global import execute
from tools.doctor import check

class CursorInstallerTests(unittest.TestCase):
    def test_project_install_hooks_are_scoped_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            install(root,"cursor","inherit",False,False)
            cfg=json.loads((root/".cursor/hooks.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["version"],1)
            self.assertEqual(set(cfg["hooks"]),{"sessionStart","sessionEnd","subagentStop"})
            for entries in cfg["hooks"].values():
                self.assertEqual(len(entries),1)
                self.assertIn("--root .",entries[0]["command"])
                self.assertIn("hook_bridge.py",entries[0]["command"])
            self.assertIn("model: inherit",(root/".cursor/agents/auto_implementer.md").read_text())
            self.assertTrue((root/"tools/supervisor.py").is_file())
            self.assertEqual((root/"AGENTS.md").read_text().count("<!-- auto-orchestrator:begin -->"),1)
            install(root,"cursor","inherit",False,False)
            self.assertEqual((root/"AGENTS.md").read_text().count("<!-- auto-orchestrator:begin -->"),1)

    def test_project_user_hooks_collision_blocks_without_mutating(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            hooks=root/".cursor/hooks.json"
            hooks.parent.mkdir()
            hooks.write_text('{"version":1,"hooks":{"sessionStart":[{"command":"user-owned"}]}}')
            with self.assertRaises(Conflict):
                install(root,"cursor","inherit",False,False)
            self.assertIn("user-owned",hooks.read_text())
            self.assertFalse((root/"AGENTS.md").exists())
            self.assertFalse((root/"tools/supervisor.py").exists())

    def test_global_installer_does_not_guess_workspace_or_modify_user_cursor_hooks(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)
            hooks=home/".cursor/hooks.json"
            hooks.parent.mkdir(parents=True)
            hooks.write_text('{"version":1,"hooks":{"subagentStart":[{"command":"user"}]}}')
            original=hooks.read_bytes()
            execute(home,{"cursor"},dry_run=True)
            self.assertFalse((home/".cursor/skills/auto-orchestrator/SKILL.md").exists())
            execute(home,{"cursor"})
            self.assertEqual(hooks.read_bytes(),original)
            self.assertTrue(all(check(home,"cursor")["cursor"].values()))
            execute(home,{"cursor"})
            self.assertEqual(hooks.read_bytes(),original)
            execute(home,{"cursor"},uninstall=True)
            self.assertEqual(hooks.read_bytes(),original)
            self.assertFalse((home/".cursor/skills/auto-orchestrator/SKILL.md").exists())

if __name__=="__main__":
    unittest.main()
