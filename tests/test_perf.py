import json
import tempfile
import unittest
from pathlib import Path
from tools.perf import EventError, append_event, analyze, load_events, path_for, observed_overlap, display


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def emit(self,event,t,**kw):
        append_event(self.root,'run_1',event,at=t,**kw)

    def get(self):
        return analyze('run_1',load_events(path_for(self.root,'run_1')))

    def test_parallel_estimate(self):
        self.emit('start',100,platform='codex')
        self.emit('agent-start',102,agent_id='a')
        self.emit('agent-start',103,agent_id='b')
        self.emit('agent-stop',117,agent_id='b')
        self.emit('agent-stop',119,agent_id='a')
        self.emit('finish',122,status='success',verified=True)
        s=self.get()
        self.assertTrue(s.has_valid_estimate)
        self.assertEqual(s.subagents,2)
        self.assertEqual(s.elapsed_seconds,22)
        self.assertEqual(s.estimated_parallelism_seconds,9)

    def test_failure_never_claims_savings(self):
        self.emit('start',100,platform='opencode')
        for agent in ('a','b'): self.emit('agent-start',101,agent_id=agent)
        for agent in ('a','b'): self.emit('agent-stop',110,agent_id=agent)
        self.emit('finish',112,status='failed')
        self.assertFalse(self.get().has_valid_estimate)

    def test_unverified_never_claims_savings(self):
        self.emit('start',100,platform='opencode')
        for agent in ('a','b'): self.emit('agent-start',101,agent_id=agent)
        for agent in ('a','b'): self.emit('agent-stop',110,agent_id=agent)
        self.emit('finish',112,status='success')
        self.assertFalse(self.get().has_valid_estimate)

    def test_missing_stop_never_claims(self):
        self.emit('start',100,platform='codex')
        self.emit('agent-start',101,agent_id='a')
        self.emit('agent-start',101,agent_id='b')
        self.emit('agent-stop',110,agent_id='a')
        self.emit('finish',111,status='success',verified=True)
        self.assertFalse(self.get().has_valid_estimate)

    def test_negative_benefit(self):
        self.emit('start',100,platform='codex')
        self.emit('agent-start',102,agent_id='a')
        self.emit('agent-stop',104,agent_id='a')
        self.emit('agent-start',108,agent_id='b')
        self.emit('agent-stop',109,agent_id='b')
        self.emit('finish',130,status='success',verified=True)
        self.assertFalse(self.get().has_valid_estimate)

    def test_observed_overlap_is_independent_of_long_parent_wall_time(self):
        self.emit('start',100,platform='codex')
        self.emit('agent-start',110,agent_id='a')
        self.emit('agent-start',112,agent_id='b')
        self.emit('agent-stop',125,agent_id='b')
        self.emit('agent-stop',130,agent_id='a')
        self.emit('finish',200,status='success',verified=True)
        summary=self.get()
        self.assertEqual(summary.observed_overlap_seconds,13)
        self.assertFalse(summary.has_valid_estimate)
        self.assertIn('Observed native tool span overlap: 13.0 sec',display(summary))
        self.assertIn('NOT actual time saved',display(summary))

    def test_observed_overlap_requires_verified_execution(self):
        self.emit('start',100,platform='opencode')
        self.emit('agent-start',101,agent_id='a')
        self.emit('agent-start',101,agent_id='b')
        self.emit('agent-stop',110,agent_id='a')
        self.emit('agent-stop',110,agent_id='b')
        self.emit('finish',112,status='success')
        self.assertIsNone(self.get().observed_overlap_seconds)
        self.assertNotIn('Observed native tool span overlap',display(self.get()))

    def test_overlap_intervals_do_not_confuse_disjoint_spans_with_parallel_work(self):
        self.assertEqual(observed_overlap([('a',100,110),('b',110,120)]),0)
        self.assertEqual(observed_overlap([('a',100,120),('b',110,130)]),10)
        self.assertEqual(observed_overlap([('a',100,120),('b',100,120),('c',100,120)]),40)

    def test_path_traversal_rejected(self):
        with self.assertRaises(EventError): path_for(self.root,'../evil')
        with self.assertRaises(EventError): append_event(self.root,'run_1','start',at=100,platform='codex',agent_id='../bad')

    def test_finish_prevents_mutation(self):
        self.emit('start',100,platform='codex')
        self.emit('finish',110,status='success',verified=True)
        with self.assertRaises(EventError):self.emit('agent-start',111,agent_id='a')

    def test_duplicate_run_rejected(self):
        self.emit('start',100,platform='codex')
        with self.assertRaises((EventError,FileExistsError)):self.emit('start',120,platform='codex')

    def test_read_invalid_log(self):
        f=path_for(self.root,'bad');f.parent.mkdir(parents=True);f.write_text('{bad\n')
        with self.assertRaises(EventError):load_events(f)

    def test_only_agent_one_no_estimate(self):
        self.emit('start',100,platform='codex')
        self.emit('agent-start',101,agent_id='a')
        self.emit('agent-stop',103,agent_id='a')
        self.emit('finish',105,status='success',verified=True)
        self.assertFalse(self.get().has_valid_estimate)

    def test_end_before_agent_stop(self):
        self.emit('start',100,platform='codex')
        self.emit('agent-start',101,agent_id='a')
        self.emit('agent-stop',109,agent_id='a')
        self.emit('agent-start',103,agent_id='b')
        self.emit('agent-stop',115,agent_id='b')
        self.emit('finish',112,status='success',verified=True)
        self.assertFalse(self.get().has_valid_estimate)


if __name__=='__main__':unittest.main()

class VerifiedEventTests(unittest.TestCase):
    def test_verified_before_finish_is_accepted(self):
        from tools.perf import analyze
        events=[{'event':'start','platform':'codex','time':1.0},
                {'event':'agent-start','agent_id':'a','time':2.0},
                {'event':'agent-start','agent_id':'b','time':2.0},
                {'event':'agent-stop','agent_id':'a','time':8.0},
                {'event':'agent-stop','agent_id':'b','time':8.0},
                {'event':'verified','time':9.0},
                {'event':'finish','status':'success','verified':False,'time':10.0}]
        s=analyze('run-1',events)
        self.assertTrue(s.has_valid_estimate)
        self.assertTrue(s.verified)
        self.assertEqual(s.estimated_parallelism_seconds,3)

    def test_verified_event_cannot_override_failure(self):
        from tools.perf import analyze
        events=[{'event':'start','platform':'codex','time':1},
                {'event':'verified','time':2},
                {'event':'finish','status':'failed','time':3}]
        self.assertFalse(analyze('run-1',events).has_valid_estimate)
