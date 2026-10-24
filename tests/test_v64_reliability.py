"""V6.4: conservative lifecycle diagnostics, alternatives and incomplete baselines."""
import json
import tempfile
import unittest
from pathlib import Path
from tools.perf import analyze, diagnose, append_event, load_events, path_for, EventError, main as perf_main
from tools.benchmark import compare, BenchmarkError
from tools.quality_gate import validate_policy, GateError


class EvidenceDiagnostics(unittest.TestCase):
    def test_missing_finish_with_open_child(self):
        events = [
            {"event":"start","time":1.0,"platform":"opencode"},
            {"event":"agent-start","time":2.0,"agent_id":"opaque"},
            {"event":"diagnostic","time":3.0,"reason":"event_stream_closed"},
        ]
        d=diagnose("safe-id",events)
        self.assertEqual(d["classification"],"missing_terminal_open_native_spans")
        self.assertEqual(d["open_native_spans"],1)
        self.assertFalse(d["finished"])
        a=analyze("safe-id",events)
        self.assertFalse(a.verified)
        self.assertIsNone(a.elapsed_seconds)

    def test_stream_close_is_not_a_finish(self):
        events=[{"event":"start","time":1.0,"platform":"opencode"},
                {"event":"route","time":2.0},
                {"event":"diagnostic","time":3.0,"reason":"event_stream_error"}]
        d=diagnose("r",events)
        self.assertEqual(d["classification"],"missing_terminal_subscription_interrupted")
        self.assertFalse(d["finished"])
        self.assertIsNone(analyze("r",events).elapsed_seconds)

    def test_advice_is_separate_from_actual_native_dispatch(self):
        events=[
            {"event":"start","time":1.0,"platform":"opencode"},
            {"event":"route","time":1.1,"delegation":"single",
             "max_agents":1,"latency_ms":153},
            {"event":"agent-start","time":2.0,"agent_id":"child1"},
            {"event":"agent-stop","time":3.0,"agent_id":"child1"},
            {"event":"finish","time":4.0,"status":"success","verified":False},
        ]
        summary=analyze("run",events)
        self.assertEqual(summary.route_latency_ms,153)
        self.assertEqual(summary.observed_native_starts,1)
        self.assertEqual(summary.observed_max_native_concurrency,1)
        self.assertFalse(summary.routing_advice_followed)
        self.assertFalse(summary.verified)

    def test_parallel_recommendation_caps_concurrent_not_total(self):
        events=[
            {"event":"start","time":1.0,"platform":"opencode"},
            {"event":"route","time":1.1,"delegation":"consider-parallel",
             "max_agents":2,"latency_ms":99},
            {"event":"agent-start","time":2.0,"agent_id":"a"},
            {"event":"agent-start","time":2.2,"agent_id":"b"},
            {"event":"agent-stop","time":3.0,"agent_id":"a"},
            {"event":"agent-start","time":3.1,"agent_id":"c"},
            {"event":"agent-stop","time":4.0,"agent_id":"b"},
            {"event":"agent-stop","time":5.0,"agent_id":"c"},
            {"event":"finish","time":6.0,"status":"success","verified":False},
        ]
        summary=analyze("run",events)
        self.assertEqual(summary.observed_native_starts,3)
        self.assertEqual(summary.observed_max_native_concurrency,2)
        self.assertTrue(summary.routing_advice_followed)

    def test_audit_lists_incomplete_without_synthetic_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            append_event(root,"unfinished","start",platform="opencode",at=1)
            append_event(root,"finished","start",platform="opencode",at=2)
            append_event(root,"finished","finish",status="failed",at=3)
            from unittest.mock import patch
            from io import StringIO
            with patch("sys.stdout",new_callable=StringIO) as output:
                code=perf_main(["--root",str(root),"audit","--json"])
            self.assertEqual(code,0)
            data=json.loads(output.getvalue())
            self.assertEqual(data["run_count"],2)
            self.assertEqual(data["categories"]["missing_terminal_reason_unknown"],1)
            self.assertEqual(data["categories"]["terminal_recorded"],1)

    def test_session_deleted_has_specific_classification(self):
        events=[{"event":"start","time":1.0,"platform":"opencode"},
                {"event":"diagnostic","time":2.0,"reason":"session_deleted_without_terminal"}]
        self.assertEqual(diagnose("r",events)["classification"],
                         "missing_terminal_session_deleted")

    def test_terminal_record_is_distinct(self):
        events=[{"event":"start","time":1.0,"platform":"opencode"},
                {"event":"finish","time":2.0,"status":"failed","verified":False}]
        self.assertEqual(diagnose("r",events)["classification"],"terminal_recorded")

    def test_malformed_reason_not_accepted(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            append_event(root,"r","start",platform="opencode",at=1)
            with self.assertRaisesRegex(EventError,"Diagnostic requires"):
                append_event(root,"r","diagnostic",source="secret-user-text",at=2)


class StrictAlternates(unittest.TestCase):
    def policy(self,alternates):
        return {"version":1,"auto_verify":True,
                "required_checks":[{"id":"unit","tool":"shell",
                                    "command":"python -m unittest -q",
                                    "alternates":alternates}]}

    def test_exact_declared_alternatives(self):
        self.assertTrue(validate_policy(self.policy(["python -m unittest -q ."])))
        for value in ("x", ["python -m unittest -q"], ["a","a"],
                      ["a"]*5, ["a\nother"]):
            with self.subTest(value=value):
                with self.assertRaises(GateError):
                    validate_policy(self.policy(value))


class HonestIncompleteBenchmarks(unittest.TestCase):
    def rows(self):
        return [dict(task="small",commit="sha",model="same",mode=mode,
                     status="success",verified=True,elapsed_seconds=elapsed)
                for mode,times in (("serial",[10,12,14]),("orchestrated",[8,9,10]))
                for elapsed in times]

    def test_no_speedup_when_any_run_unfinished(self):
        rows=self.rows()
        rows[-1].update(status="incomplete",verified=False,elapsed_seconds=None)
        report=compare(rows)[0]
        self.assertEqual(report["orchestrated_incomplete"],1)
        self.assertIsNone(report["speedup_percent"])
        self.assertEqual(report["orchestrated_success_rate"],2/3)

    def test_incomplete_cannot_claim_elapsed_or_verified(self):
        rows=self.rows()
        rows[-1].update(status="incomplete",verified=True,elapsed_seconds=None)
        with self.assertRaises(BenchmarkError):compare(rows)
        rows[-1].update(status="incomplete",verified=False,elapsed_seconds=23)
        with self.assertRaises(BenchmarkError):compare(rows)


if __name__=="__main__":
    unittest.main()
