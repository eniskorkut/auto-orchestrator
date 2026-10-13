import unittest
from tools.benchmark import compare, BenchmarkError
class BenchmarkTests(unittest.TestCase):
    def rows(self):
        return [dict(task='auth',commit='abc',model='same',mode=mode,elapsed_seconds=sec,status='success',verified=True)
                for mode,durations in [('serial',[100,110,90]),('orchestrated',[60,70,65])]
                for sec in durations]
    def test_matched_benchmark(self):
        r=compare(self.rows())[0]
        self.assertEqual(r['seconds_saved'],35)
        self.assertEqual(r['speedup_percent'],35)
    def test_insufficient_samples(self):
        self.assertIsNone(compare(self.rows()[:-1])[0]['seconds_saved'])
    def test_unmatched_models(self):
        rows=self.rows()
        for row in rows[:3]:row['model']='different'
        self.assertTrue(all(x['seconds_saved'] is None for x in compare(rows)))
    def test_failed_validation_blocks_claim(self):
        rows=self.rows();rows[0]['verified']=False
        self.assertIsNone(compare(rows)[0]['seconds_saved'])
    def test_invalid_duration(self):
        rows=self.rows();rows[0]['elapsed_seconds']=-1
        with self.assertRaises(BenchmarkError):compare(rows)
