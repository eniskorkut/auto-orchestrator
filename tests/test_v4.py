import json
import tempfile
import unittest
from pathlib import Path

from tools.supervisor import Supervisor, SupervisorError, validate_plan
from tools.router import brief, discover
from tools.worktree import git, create, preview, cleanup, WorktreeError

RESULT={"summary":"verified","changed_files":[],"tests":[{"name":"unit","status":"passed"}],"findings":[]}
def task(tid,deps=None,read=None,write=None,timeout=60,retries=2):
    return {"id":tid,"role":"explorer","objective":"Inspect "+tid,"depends_on":deps or [],
            "read_scope":read or [],"write_scope":write or [],"timeout_seconds":timeout,"max_retries":retries}

class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s=Supervisor(self.tmp.name)

    def test_dependencies_and_parallel_claim(self):
        self.s.init("run",{"tasks":[task("a"),task("b"),task("c",deps=["a","b"])]})
        first=self.s.claim("run",now=100)["claimed"]
        self.assertEqual([x["id"] for x in first],["a","b"])
        self.assertFalse(self.s.claim("run",now=101)["claimed"])
        for x in first:self.s.change("run",x["id"],x["lease"],"completed",now=102,result=RESULT)
        second=self.s.claim("run",now=103)["claimed"]
        self.assertEqual(len(second),1)
        self.s.change("run","c",second[0]["lease"],"completed",now=104,result=RESULT)
        self.assertTrue(self.s.status("run")["complete"])

    def test_read_write_collision(self):
        self.s.init("run",{"tasks":[task("a",write=["src/api"]),task("b",read=["src/api/user.py"]),
                                           task("c",read=["docs"])]})
        first=self.s.claim("run")["claimed"]
        self.assertEqual([x["id"] for x in first],["a","c"])

    def test_timeout_and_stale_lease(self):
        self.s.init("run",{"tasks":[task("a",write=["src"],timeout=10),
                                           task("b",write=["src/main.py"])]})
        initial=self.s.claim("run",now=100)["claimed"][0]
        expired=self.s.claim("run",now=111)
        self.assertEqual(expired["timed_out"],["a"])
        self.assertFalse(expired["claimed"])
        with self.assertRaises(SupervisorError):self.s.retry("run","a","new evidence")
        self.s.change("run","a",initial["lease"],"cancelled",now=112)
        self.s.retry("run","a","new test evidence")
        next_run=self.s.claim("run",now=113)["claimed"][0]
        with self.assertRaises(SupervisorError):
            self.s.change("run","a",initial["lease"],"completed",result=RESULT)
        self.assertNotEqual(next_run["lease"],initial["lease"])

    def test_cancel_and_retry_budget(self):
        self.s.init("run",{"tasks":[task("a",retries=1)]})
        one=self.s.claim("run")["claimed"][0]
        self.assertTrue(self.s.cancel("run","a")["native_cancellation_required"])
        self.s.change("run","a",one["lease"],"cancelled")
        self.s.retry("run","a","corrected information")
        two=self.s.claim("run")["claimed"][0]
        self.s.change("run","a",two["lease"],"failed",reason="test failed")
        with self.assertRaises(SupervisorError):self.s.retry("run","a","again")

    def test_structured_result_and_invalid_plans(self):
        self.s.init("run",{"tasks":[task("a")]})
        one=self.s.claim("run")["claimed"][0]
        with self.assertRaises(SupervisorError):
            self.s.change("run","a",one["lease"],"completed",result={"summary":"incomplete"})
        self.s.change("run","a",one["lease"],"completed",result=RESULT)
        self.assertEqual(self.s.status("run")["tasks"][0]["result"]["tests"][0]["status"],"passed")
        with self.assertRaises(SupervisorError):
            validate_plan({"tasks":[task("a",deps=["b"]),task("b",deps=["a"])]})

class RouterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
    def test_skill_discovery_and_bounded_brief(self):
        p=self.root/".agents/skills/fastapi/SKILL.md"
        p.parent.mkdir(parents=True)
        p.write_text("---\nname: fastapi\ndescription: FastAPI backend API validation routes\n---\nHelp.\n")
        plan={"tasks":[{"id":"backend","role":"implementer","objective":"Fix FastAPI API routes",
                        "write_scope":["backend"]}]}
        result=brief(self.root,plan,"backend",home=self.root)
        self.assertEqual(result["selected_skills"][0]["name"],"fastapi")
        self.assertEqual(result["write_scope"],["backend"])
        self.assertNotIn("Help.",json.dumps(result))

class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/"repo"
        self.root.mkdir()
        git(self.root,"init")
        git(self.root,"config","user.email","test@example.test")
        git(self.root,"config","user.name","Tester")
        (self.root/"README.md").write_text("initial\n")
        git(self.root,"add",".")
        git(self.root,"commit","-m","initial")
    def test_non_destructive_preview_and_clean_cleanup(self):
        data=create(self.root,"alpha")
        wt=Path(data["path"])
        (wt/"README.md").write_text("changed\n")
        git(wt,"add",".")
        git(wt,"commit","-m","change")
        result=preview(self.root,"alpha")
        self.assertTrue(result["ready"])
        self.assertIn("README.md",result["changed_files"])
        self.assertEqual((self.root/"README.md").read_text(),"initial\n")
        cleaned=cleanup(self.root,"alpha")
        self.assertTrue(cleaned["worktree_removed"])
        self.assertFalse(wt.exists())
    def test_dirty_worktree_refuses_cleanup(self):
        data=create(self.root,"beta")
        wt=Path(data["path"])
        (wt/"README.md").write_text("uncommitted")
        with self.assertRaises(WorktreeError):cleanup(self.root,"beta")

if __name__=="__main__":unittest.main()
