"""Codex and Antigravity global-hook integration regression tests."""
import json
import tempfile
import unittest
from pathlib import Path

from tools.install_global import execute, InstallError
from tools.hook_integration import merge, remove, HookIntegrationError
from tools.hook_bridge import root_for_payload

class NativeHookIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_codex_preserves_existing_hook_and_idempotence(self):
        hooks = self.home / ".codex/hooks.json"
        hooks.parent.mkdir(parents=True)
        custom = {"description":"My rules","hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo existing"}]}]}}
        hooks.write_text(json.dumps(custom),encoding="utf-8")
        execute(self.home,{"codex"})
        installed = json.loads(hooks.read_text(encoding="utf-8"))
        self.assertEqual(installed["description"],"My rules")
        self.assertEqual(installed["hooks"]["Stop"][0],custom["hooks"]["Stop"][0])
        self.assertEqual(len(installed["hooks"]["SubagentStart"]),1)
        self.assertEqual(len(installed["hooks"]["Stop"]),2)
        self.assertIn("auto-orchestrator",installed["hooks"]["SubagentStart"][0]["hooks"][0]["command"])
        execute(self.home,{"codex"})
        self.assertEqual(json.loads(hooks.read_text()),installed)
        manifest = json.loads((self.home/".auto-orchestrator/auto-orchestrator-global-manifest.json").read_text())
        self.assertEqual(manifest["hook_injections"][str(hooks.resolve())]["platform"],"codex")
        execute(self.home,{"codex"},uninstall=True)
        self.assertEqual(json.loads(hooks.read_text()),custom)

    def test_antigravity_preserves_other_namespaces(self):
        hooks=self.home/".gemini/config/hooks.json"
        hooks.parent.mkdir(parents=True)
        original={"team-formatter":{"PostInvocation":[{"type":"command","command":"echo ok"}]}}
        hooks.write_text(json.dumps(original),encoding="utf-8")
        execute(self.home,{"antigravity"})
        updated=json.loads(hooks.read_text())
        self.assertEqual(updated["team-formatter"],original["team-formatter"])
        self.assertIn("auto-orchestrator",updated)
        execute(self.home,{"antigravity"})
        self.assertEqual(json.loads(hooks.read_text()),updated)
        execute(self.home,{"antigravity"},uninstall=True)
        self.assertEqual(json.loads(hooks.read_text()),original)

    def test_antigravity_conflict_fails_before_mutating_files(self):
        hooks=self.home/".gemini/config/hooks.json"
        hooks.parent.mkdir(parents=True)
        original={"auto-orchestrator":{"Stop":[{"command":"user-managed"}]}}
        hooks.write_text(json.dumps(original),encoding="utf-8")
        with self.assertRaises(InstallError):
            execute(self.home,{"antigravity"})
        self.assertEqual(json.loads(hooks.read_text()),original)
        self.assertFalse((self.home/".gemini/config/skills/auto-orchestrator/SKILL.md").exists())

    def test_bad_codex_json_fails_safely(self):
        hooks=self.home/".codex/hooks.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text("{bad",encoding="utf-8")
        with self.assertRaises(ValueError):
            execute(self.home,{"codex"})
        self.assertEqual(hooks.read_text(),"{bad")
        self.assertFalse((self.home/".codex/skills/auto-orchestrator/SKILL.md").exists())

    def test_dry_run_does_not_touch_existing_hooks(self):
        hooks=self.home/".codex/hooks.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text('{"hooks":{}}')
        execute(self.home,{"codex"},dry_run=True)
        self.assertEqual(hooks.read_text(),'{"hooks":{}}')
        self.assertFalse((self.home/".auto-orchestrator").exists())

    def test_uninstall_preserves_user_modified_injected_hook(self):
        hooks=self.home/".gemini/config/hooks.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text('{"user":{"Stop":[]}}')
        execute(self.home,{"antigravity"})
        modified=json.loads(hooks.read_text())
        modified["auto-orchestrator"]["Stop"][0]["command"]="echo manually edited"
        hooks.write_text(json.dumps(modified),encoding="utf-8")
        notes=execute(self.home,{"antigravity"},uninstall=True)
        self.assertIn("preserved user-modified hook entry"," ".join(notes))
        self.assertEqual(json.loads(hooks.read_text()),modified)

    def test_hooks_payload_is_not_a_subagent_runtime(self):
        with tempfile.TemporaryDirectory() as project:
            p=Path(project)
            self.assertEqual(root_for_payload({"cwd":str(p)},"codex"),p.resolve())
            self.assertEqual(root_for_payload({"workspacePaths":[str(p)]},"antigravity"),p.resolve())
            self.assertNotEqual(root_for_payload({"workspacePaths":[str(p),"/another"]},"antigravity"),p.resolve())

    def test_missing_hook_entries_not_duplicated(self):
        desired={"hooks":{"Stop":[{"hooks":[{"command":"python hook.py"}]}]}}
        current={"hooks":{"Stop":[{"hooks":[{"command":"python hook.py"}]}]}}
        same,changed=merge(current,desired,"codex")
        self.assertFalse(changed)
        self.assertEqual(same,current)
        stripped,removed,modified=remove(same,desired,"codex")
        self.assertTrue(removed)
        self.assertFalse(modified)
        self.assertEqual(stripped,{})

if __name__=="__main__":
    unittest.main()
