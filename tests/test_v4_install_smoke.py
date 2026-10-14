import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tools.install import install
from tools.install_global import execute

SOURCE=Path(__file__).resolve().parents[1]

class V4InstallerSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.project=self.home/"sample"
        self.project.mkdir()

    def test_project_install_contains_portable_tools(self):
        install(self.project,"all","inherit",False,False)
        for filename in ("supervisor.py","router.py","worktree.py"):
            self.assertTrue((self.project/"tools"/filename).is_file())
        self.assertTrue((self.project/".agents/skills/auto-orchestrator/references/supervision.md").exists())

    def test_global_install_preserves_tool_files_and_uninstalls(self):
        execute(self.home,{"codex","opencode","antigravity"},"v2","inherit")
        for relative in (
            ".codex/auto-orchestrator/tools/supervisor.py",
            ".config/opencode/auto-orchestrator/tools/router.py",
            ".gemini/config/auto-orchestrator/tools/worktree.py"):
            self.assertTrue((self.home/relative).exists(),relative)
        execute(self.home,{"codex","opencode","antigravity"},"v2","inherit",uninstall=True)
        self.assertFalse((self.home/".codex/auto-orchestrator/tools/supervisor.py").exists())

    def test_cli_dependency_and_structured_output_smoke(self):
        install(self.project,"codex","inherit",False,False)
        sample_plan={"tasks":[{"id":"docs","role":"explorer","objective":"Inspect documentation",
                              "read_scope":["README.md"]}]}
        plan=self.project/"plan.json"
        plan.write_text(json.dumps(sample_plan),encoding="utf-8")
        def call(*args):
            process=subprocess.run([sys.executable,*args],cwd=self.project,capture_output=True,text=True)
            self.assertEqual(process.returncode,0,process.stderr)
            return json.loads(process.stdout)
        initialized=call("tools/supervisor.py","init","--run-id","live","--plan","plan.json")
        self.assertEqual(initialized["tasks"],1)
        claimed=call("tools/supervisor.py","claim","--run-id","live")["claimed"][0]
        result=self.project/"result.json"
        result.write_text(json.dumps({"summary":"inspected","changed_files":[],
                                     "tests":[{"name":"manual inspection","status":"passed"}],"findings":[]}),
                          encoding="utf-8")
        call("tools/supervisor.py","completed","--run-id","live","--task-id","docs",
             "--lease",claimed["lease"],"--result","result.json")
        self.assertTrue(call("tools/supervisor.py","status","--run-id","live")["complete"])
        response=call("tools/router.py","--root",".","brief","--plan","plan.json","--task-id","docs")
        self.assertEqual(response["task_id"],"docs")

if __name__=="__main__":
    unittest.main()
