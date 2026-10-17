import tempfile
import unittest

from tools.supervisor import Supervisor, SupervisorError

RESULT = {"summary":"tested","changed_files":[],"tests":[{"name":"unit","status":"passed"}],"findings":[]}

def sample(id, dependency=None):
    return {"id":id,"role":"implementer","objective":"Implement scoped feature",
            "depends_on": [dependency] if dependency else [],
            "write_scope":[id],"read_scope":[]}

class NativeSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s=Supervisor(self.temp.name)
        self.addCleanup(self.s.close)

    def test_real_stop_and_structured_result_both_required(self):
        self.s.init("run",{"native_supervision":True,"tasks":[sample("alpha"),sample("beta","alpha")]})
        first=self.s.claim("run")["claimed"][0]
        with self.assertRaises(SupervisorError):
            self.s.change("run","alpha",first["lease"],"completed",result=RESULT)
        self.s.bind_native("run","alpha",first["lease"],"codex","native-agent-1")
        with self.assertRaises(SupervisorError):
            self.s.change("run","alpha",first["lease"],"completed",result=RESULT)
        self.assertFalse(self.s.claim("run")["claimed"])
        self.s.observe_native("codex","native-agent-1","start")
        self.assertTrue(self.s.observe_native("codex","native-agent-1","stop")["matched"])
        self.assertTrue(self.s.status("run")["tasks"][0]["native_stop_observed"])
        self.assertFalse(self.s.status("run")["complete"])
        self.s.change("run","alpha",first["lease"],"completed",result=RESULT)
        self.assertEqual(self.s.claim("run")["claimed"][0]["id"],"beta")

    def test_ambiguous_binding_rejected_and_unrelated_stop_ignored(self):
        self.s.init("run",{"native_supervision":True,"tasks":[sample("alpha"),sample("beta")]})
        items=self.s.claim("run")["claimed"]
        a,b=items
        self.s.bind_native("run",a["id"],a["lease"],"cursor","subagent-id-1")
        self.assertTrue(self.s.bind_native("run",a["id"],a["lease"],"cursor","subagent-id-1")["idempotent"])
        with self.assertRaises(SupervisorError):
            self.s.bind_native("run",b["id"],b["lease"],"cursor","subagent-id-1")
        with self.assertRaises(SupervisorError):
            self.s.bind_native("run",a["id"],a["lease"],"cursor","different-id")
        self.assertFalse(self.s.observe_native("cursor","unknown-id","stop")["matched"])
        with self.assertRaises(SupervisorError):
            self.s.change("run",a["id"],a["lease"],"completed",result=RESULT)

    def test_timeout_keeps_scope_until_native_stop_and_confirmation(self):
        self.s.init("run",{"native_supervision":True,"tasks":[
            {**sample("alpha"),"timeout_seconds":10},
            {"id":"beta","role":"tester","objective":"Review alpha","depends_on":[],
             "read_scope":["alpha"],"write_scope":[]}]})
        a=self.s.claim("run",now=100)["claimed"][0]
        self.s.bind_native("run","alpha",a["lease"],"codex","native-timeout")
        result=self.s.claim("run",now=111)
        self.assertEqual(result["timed_out"],["alpha"])
        self.assertFalse(result["claimed"])
        with self.assertRaises(SupervisorError):
            self.s.change("run","alpha",a["lease"],"cancelled")
        self.s.observe_native("codex","native-timeout","stop")
        self.s.change("run","alpha",a["lease"],"cancelled")
        self.s.retry("run","alpha","real retry")
        self.assertFalse(self.s.status("run")["tasks"][0]["native_stop_observed"])

    def test_stale_after_retry_or_terminal_is_not_counted(self):
        self.s.init("run",{"native_supervision":True,"tasks":[sample("alpha")]})
        a=self.s.claim("run")["claimed"][0]
        self.s.bind_native("run","alpha",a["lease"],"codex","native-attempt-1")
        self.s.observe_native("codex","native-attempt-1","stop")
        self.s.change("run","alpha",a["lease"],"failed",reason="observed failure")
        self.s.retry("run","alpha","retry with evidence")
        b=self.s.claim("run")["claimed"][0]
        self.assertFalse(self.s.observe_native("codex","native-attempt-1","stop")["matched"])
        with self.assertRaises(SupervisorError):
            self.s.bind_native("run","alpha",a["lease"],"codex","native-attempt-2")
        self.s.bind_native("run","alpha",b["lease"],"codex","native-attempt-2")
        self.s.observe_native("codex","native-attempt-2","stop")
        self.s.change("run","alpha",b["lease"],"completed",result=RESULT)
        self.assertFalse(self.s.observe_native("codex","native-attempt-2","stop")["matched"])

    def test_old_run_without_opt_in_remains_usable(self):
        self.s.init("run",{"tasks":[sample("alpha")]})
        a=self.s.claim("run")["claimed"][0]
        with self.assertRaises(SupervisorError):
            self.s.bind_native("run","alpha",a["lease"],"codex","agent-legacy")
        self.s.change("run","alpha",a["lease"],"completed",result=RESULT)
        self.assertTrue(self.s.status("run")["complete"])

if __name__=="__main__":
    unittest.main()
