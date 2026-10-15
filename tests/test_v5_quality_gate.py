import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from tools.perf import append_event, analyze, load_events, path_for, EventError
from tools.quality_gate import load_policy, assess, GateError

class V5EvidenceGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy_path = self.root / ".auto-orchestrator" / "verification.json"
        self.policy_path.parent.mkdir(parents=True)
        self.raw = b'{"version":1,"auto_verify":true,"required_checks":[{"id":"tests","tool":"bash","command":"python -m pytest -q"}]}'
        self.policy_path.write_bytes(self.raw)
        self.policy = load_policy(self.root)
        self.pid = hashlib.sha256(self.raw).hexdigest()

    def event(self, name, time, **kwargs):
        append_event(self.root,"v5",name,at=time,**kwargs)

    def history(self):
        return load_events(path_for(self.root,"v5"))

    def start(self):
        self.event("start",1,platform="opencode")
        self.event("agent-start",2,agent_id="a")
        self.event("agent-start",2,agent_id="b")
        self.event("agent-stop",6,agent_id="a")
        self.event("agent-stop",6,agent_id="b")

    def check(self, time, code=0, policy_hash=None):
        self.event("check",time,check_id="tests",exit_code=code,source="native-tool",
                   policy_hash=self.pid if policy_hash is None else policy_hash)

    def test_verified_after_finish_with_native_exit_evidence(self):
        self.start()
        self.event("mutation",6.2)
        self.check(7)
        self.event("finish",8,status="success")
        self.assertTrue(assess(self.history(),self.policy)["verified"])
        self.event("verified",9,source="quality-gate")
        events=self.history()
        self.assertEqual(events[-1]["check_ids"],["tests"])
        result=analyze("v5",events,load_policy(self.root))
        self.assertTrue(result.verified)
        self.assertTrue(result.has_valid_estimate)
        self.assertEqual(result.elapsed_seconds,7)
        self.assertEqual(result.estimated_parallelism_seconds,1)

    def test_late_untrusted_verification_rejected(self):
        self.start()
        self.event("finish",9,status="success")
        with self.assertRaises(EventError):
            self.event("verified",10)
        with self.assertRaises(EventError):
            self.event("verified",11,source="quality-gate")

    def test_failed_and_stale_test_evidence_rejected(self):
        for fresh,code in ((True,1),(False,0)):
            with self.subTest(fresh=fresh,code=code):
                self.root.joinpath(".auto-orchestrator/runs/v5.jsonl").unlink(missing_ok=True)
                self.start()
                self.check(6,code=code)
                if not fresh:
                    self.event("mutation",6.5)
                self.event("finish",8,status="success")
                with self.assertRaises(EventError):
                    self.event("verified",9,source="quality-gate")

    def test_policy_change_invalidates_native_evidence(self):
        self.start()
        self.check(6)
        self.event("finish",9,status="success")
        self.policy_path.write_text(json.dumps({
            "version":1,"auto_verify":True,
            "required_checks":[{"id":"tests","tool":"bash","command":"python -m unittest"}]}))
        with self.assertRaises(EventError):
            self.event("verified",10,source="quality-gate")

    def test_cancelled_and_failed_execution_do_not_verify(self):
        for status in ("cancelled","failed"):
            with self.subTest(status=status):
                self.root.joinpath(".auto-orchestrator/runs/v5.jsonl").unlink(missing_ok=True)
                self.start();self.check(6);self.event("finish",9,status=status)
                with self.assertRaises(EventError):self.event("verified",10,source="quality-gate")

    def test_no_policy_or_invalid_policy_is_fail_closed(self):
        self.assertIsNotNone(load_policy(self.root))
        self.policy_path.unlink()
        self.assertIsNone(load_policy(self.root))
        self.policy_path.write_text('{"version":1,"auto_verify":false,"required_checks":[]}')
        with self.assertRaises(GateError):load_policy(self.root)

    def test_duplicate_and_unmatched_spans_reject_evidence(self):
        self.event("start",1,platform="opencode")
        self.event("agent-start",2,agent_id="a")
        self.check(6)
        self.event("finish",8,status="success")
        with self.assertRaises(EventError):self.event("verified",9,source="quality-gate")

    def test_legacy_v4_finish_and_verified_remain_readable(self):
        legacy=[{"event":"start","platform":"codex","time":1},
                {"event":"verified","time":2},
                {"event":"finish","time":3,"status":"success","verified":False}]
        self.assertTrue(analyze("legacy",legacy).verified)
        self.assertFalse(analyze("legacy",legacy).has_valid_estimate)

if __name__=="__main__":
    unittest.main()
