import json
import tempfile
import unittest
from pathlib import Path

from tools.hook_bridge import handle_codex, handle_antigravity, handle_cursor, root_for_payload
from tools.perf import load_events, path_for
from tools.supervisor import Supervisor, SupervisorError

TASK = {"id":"backend","role":"implementer","objective":"Implement isolated backend",
        "write_scope":["backend"],"read_scope":[]}
RESULT = {"summary":"verified","changed_files":["backend/api.py"],
          "tests":[{"name":"unit","status":"passed"}],"findings":[]}

class NativePlatformSimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.supervisor=Supervisor(self.root)
        self.addCleanup(self.supervisor.close)

    def claim(self):
        self.supervisor.init("run",{"native_supervision":True,"tasks":[TASK]})
        return self.supervisor.claim("run")["claimed"][0]

    def test_codex_hook_stop_observes_bound_agent_but_not_task_completion(self):
        lease=self.claim()
        self.supervisor.bind_native("run","backend",lease["lease"],"codex","agent-1234")
        base={"session_id":"session-id","turn_id":"turn-id"}
        handle_codex({**base,"hook_event_name":"UserPromptSubmit"},self.root)
        handle_codex({**base,"hook_event_name":"SubagentStart","agent_id":"agent-1234"},self.root)
        self.assertFalse(self.supervisor.status("run")["tasks"][0]["native_stop_observed"])
        handle_codex({**base,"hook_event_name":"SubagentStop","agent_id":"agent-1234"},self.root)
        self.assertTrue(self.supervisor.status("run")["tasks"][0]["native_stop_observed"])
        self.assertFalse(self.supervisor.status("run")["complete"])
        self.supervisor.change("run","backend",lease["lease"],"completed",result=RESULT)
        self.assertTrue(self.supervisor.status("run")["complete"])
        content=''.join(p.read_text() for p in (self.root/'.auto-orchestrator/runs').glob('*.jsonl'))
        self.assertNotIn("agent-1234",content)
        self.assertNotIn("session-id",content)

    def test_codex_native_stop_without_turn_id_still_acknowledges_bound_supervisor(self):
        lease=self.claim()
        self.supervisor.bind_native("run","backend",lease["lease"],"codex","agent-without-turn")
        handle_codex({"hook_event_name":"SubagentStop","session_id":"parent-session",
                      "agent_id":"agent-without-turn"},self.root)
        self.assertTrue(self.supervisor.status("run")["tasks"][0]["native_stop_observed"])
        self.assertFalse((self.root/'.auto-orchestrator/runs').exists())

    def test_cursor_documented_stop_has_no_id_and_cannot_unlock_supervisor(self):
        lease=self.claim()
        self.supervisor.bind_native("run","backend",lease["lease"],"cursor","cursor-agent-1")
        first=handle_cursor({"session_id":"cursor-parent"},self.root,"sessionStart")
        self.assertIn("additional_context",first)
        self.assertEqual(handle_cursor({"parent_conversation_id":"cursor-parent",
            "subagent_id":"cursor-agent-1","task":"SENSITIVE"},self.root,"subagentStart"),
            {})
        handle_cursor({"parent_conversation_id":"cursor-parent","status":"completed",
            "subagent_type":"generalPurpose","task":"SENSITIVE","summary":"secret data"},self.root,"subagentStop")
        self.assertFalse(self.supervisor.status("run")["tasks"][0]["native_stop_observed"])
        with self.assertRaises(SupervisorError):
            self.supervisor.change("run","backend",lease["lease"],"completed",result=RESULT)
        handle_cursor({"session_id":"cursor-parent","reason":"completed"},self.root,"sessionEnd")
        events=load_events(path_for(self.root,next(p.stem for p in
           (self.root/'.auto-orchestrator/runs').glob('*.jsonl'))))
        self.assertEqual([e["event"] for e in events],["start","finish"])
        self.assertEqual(events[-1]["status"],"success")
        self.assertNotIn("SENSITIVE",json.dumps(events))

    def test_cursor_optional_id_on_stop_only_if_exact(self):
        lease=self.claim()
        self.supervisor.bind_native("run","backend",lease["lease"],"cursor","cursor-agent-2")
        handle_cursor({"parent_conversation_id":"cursor-parent","subagent_id":"other-agent",
                       "status":"completed"},self.root,"subagentStop")
        self.assertFalse(self.supervisor.status("run")["tasks"][0]["native_stop_observed"])
        handle_cursor({"parent_conversation_id":"cursor-parent","subagent_id":"cursor-agent-2",
                       "status":"completed"},self.root,"subagentStop")
        self.assertTrue(self.supervisor.status("run")["tasks"][0]["native_stop_observed"])

    def test_antigravity_stop_tracks_session_not_subagent(self):
        payload={"conversationId":"private","workspacePaths":[str(self.root)]}
        handle_antigravity(payload,self.root,"SessionStart")
        self.assertEqual(handle_antigravity({**payload,"fullyIdle":False,
            "terminationReason":"model_stop"},self.root,"Stop")["decision"],"allow")
        handle_antigravity({**payload,"fullyIdle":True,
            "terminationReason":"error"},self.root,"Stop")
        events=load_events(next((self.root/'.auto-orchestrator/runs').glob('*.jsonl')))
        self.assertEqual([x["event"] for x in events],["start","finish"])
        self.assertEqual(events[-1]["status"],"failed")

    def test_root_fail_closed_for_ambiguous_workspaces(self):
        self.assertIsNone(root_for_payload({"workspacePaths":[str(self.root),"/other"]},"antigravity"))
        self.assertIsNone(root_for_payload({},"codex"))
        self.assertIsNone(root_for_payload({},"cursor"))

if __name__=="__main__":
    unittest.main()
