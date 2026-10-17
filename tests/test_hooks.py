import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tools.hook_bridge import opaque
from tools.perf import analyze, load_events, path_for, append_event

ROOT = Path(__file__).resolve().parents[1]

class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def send(self,platform,event,payload):
        command=[sys.executable,str(ROOT/'tools/hook_bridge.py'),platform]
        if event: command.append(event)
        command+=['--root',str(self.project)]
        r=subprocess.run(command,input=json.dumps(payload),text=True,capture_output=True,timeout=5)
        self.assertEqual(r.returncode,0,r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else {}

    def test_codex_actual_events_and_no_prompt_leak(self):
        sid='private-session-id';tid='t1'
        base={'session_id':sid,'turn_id':tid}
        msg=self.send('codex','',{**base,'hook_event_name':'UserPromptSubmit','prompt':'secret API_TOKEN=123'})
        self.assertIn('additionalContext',msg['hookSpecificOutput'])
        self.send('codex','',{**base,'hook_event_name':'SubagentStart','agent_id':'child-A'})
        self.send('codex','',{**base,'hook_event_name':'SubagentStart','agent_id':'child-B'})
        self.send('codex','',{**base,'hook_event_name':'SubagentStop','agent_id':'child-A'})
        self.send('codex','',{**base,'hook_event_name':'SubagentStop','agent_id':'child-B'})
        run=opaque('codex',sid,tid)
        append_event(self.project,run,'verified')
        self.send('codex','',{**base,'hook_event_name':'Stop'})
        path=path_for(self.project,run)
        data=path.read_text()
        for secret in ('secret API_TOKEN=123',sid,'child-A','child-B'):
            self.assertNotIn(secret,data)
        summary=analyze(run,load_events(path))
        self.assertEqual(summary.subagents,2)
        self.assertTrue(summary.verified)
        self.assertEqual(summary.status,'success')

    def test_codex_native_stop_hooks_always_emit_safe_json(self):
        base={'session_id':'session-id','turn_id':'turn-id'}
        self.send('codex','',{**base,'hook_event_name':'UserPromptSubmit'})
        for event in ('SubagentStop','Stop'):
            result=self.send('codex','',{**base,'hook_event_name':event,
                                        'agent_id':'native-agent-id',
                                        'last_assistant_message':'SECRET'})
            self.assertEqual(result,{'continue':True})
            self.assertNotIn('SECRET',json.dumps(result))
        # SubagentStop may arrive without a turn_id; this is still a JSON
        # output contract, and can record native start/stop separately.
        result=self.send('codex','',{'session_id':'s','hook_event_name':'SubagentStop',
                                   'agent_id':'native-agent-id'})
        self.assertEqual(result,{'continue':True})
        self.assertEqual(self.send('codex','',{'hook_event_name':'Stop'}),{'continue':True})

    def test_codex_unverified_never_claims(self):
        base={'session_id':'s','turn_id':'t'}
        for e in ('UserPromptSubmit','Stop'):
            self.send('codex','',{**base,'hook_event_name':e})
        run=opaque('codex','s','t')
        summary=analyze(run,load_events(path_for(self.project,run)))
        self.assertFalse(summary.has_valid_estimate)

    def test_antigravity_stop_respects_background(self):
        base={'conversationId':'conversation-123'}
        r=self.send('antigravity','SessionStart',base)
        self.assertIn('injectSteps',r)
        r=self.send('antigravity','Stop',{**base,'fullyIdle':False,'terminationReason':'model_stop'})
        self.assertEqual(r['decision'],'allow')
        run=opaque('antigravity','conversation-123')
        self.assertEqual(analyze(run,load_events(path_for(self.project,run))).status,'running')
        self.send('antigravity','Stop',{**base,'fullyIdle':True,'terminationReason':'model_stop'})
        summary=analyze(run,load_events(path_for(self.project,run)))
        self.assertEqual(summary.status,'success')
        self.assertEqual(summary.subagents,0)
        self.assertFalse(summary.has_valid_estimate)

    def test_config_paths_have_valid_schema(self):
        codex=json.loads((ROOT/'.codex/hooks.json').read_text())
        antigravity=json.loads((ROOT/'.agents/hooks.json').read_text())
        self.assertIn('SubagentStart',codex['hooks'])
        self.assertIn('Stop',antigravity['auto-orchestrator'])
        self.assertNotIn('SubagentStart',antigravity['auto-orchestrator'])

if __name__=='__main__': unittest.main()
