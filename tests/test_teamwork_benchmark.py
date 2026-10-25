from __future__ import annotations

import unittest

from tools.teamwork_benchmark import BenchmarkError, compare, normalize_run, overlap_seconds


def run(mode="serial", **overrides):
    value = {
        "label": mode,
        "mode": mode,
        "wall_time_seconds": 100.0,
        "verified": True,
        "verified_tasks": 4,
        "total_tasks": 4,
        "tests_passed": 8,
        "tests_total": 8,
        "cost_usd": 0.10,
        "agent_intervals": [],
        "model": "opencode-go/deepseek-v4.1-flash",
        "hardware": "local",
        "cache": "warm",
        "acceptance": ["all checks pass"],
    }
    value.update(overrides)
    return value


class NormalizeTests(unittest.TestCase):
    def test_bounds_and_types(self):
        with self.assertRaises(BenchmarkError):
            normalize_run({})
        with self.assertRaises(BenchmarkError):
            normalize_run(run(wall_time_seconds=-1))
        with self.assertRaises(BenchmarkError):
            normalize_run(run(verified="yes"))
        with self.assertRaises(BenchmarkError):
            normalize_run(run(agent_intervals=[{"start": 5, "end": 3}]))
        with self.assertRaises(BenchmarkError):
            normalize_run(run(agent_intervals=[1] * 201))


class OverlapTests(unittest.TestCase):
    def test_union_not_double_counted(self):
        intervals = [
            {"agent": "a", "start": 0, "end": 10},
            {"agent": "b", "start": 5, "end": 15},
            {"agent": "c", "start": 20, "end": 25},
        ]
        self.assertEqual(overlap_seconds(intervals), 20)

    def test_empty_is_zero(self):
        self.assertEqual(overlap_seconds([]), 0)


class CompareTests(unittest.TestCase):
    def test_verified_comparison_separates_observed_and_verified(self):
        serial = run("serial", wall_time_seconds=100, agent_intervals=[
            {"agent": "one", "start": 0, "end": 100},
        ])
        parallel = run("parallel", wall_time_seconds=60, cost_usd=0.12, agent_intervals=[
            {"agent": "backend", "start": 0, "end": 55},
            {"agent": "frontend", "start": 0, "end": 50},
        ])
        result = compare(serial, parallel)
        self.assertEqual(result["observed"]["parallel_agent_span_seconds"], 55)
        self.assertEqual(result["verified_improvement"]["wall_time_difference_seconds"], 40)
        self.assertTrue(result["verified_improvement"]["comparable"])
        self.assertFalse(result["verified_improvement"]["human_time_saved_claim"])
        self.assertEqual(result["cost"]["difference_usd"], -0.02)
        self.assertEqual(result["quality"]["parallel_test_pass_ratio"], 1.0)

    def test_unverified_or_mismatched_runs_never_claim_improvement(self):
        serial = run("serial")
        for parallel in (
            run("parallel", verified=False),
            run("parallel", total_tasks=3),
            run("parallel", tests_total=7),
            run("parallel", wall_time_seconds=0),
        ):
            result = compare(serial, parallel)
            self.assertFalse(result["verified_improvement"]["comparable"])
            self.assertIsNone(result["verified_improvement"]["wall_time_difference_seconds"])
            self.assertTrue(result["verified_improvement"]["reason"])

    def test_repairs_retries_and_quality_are_preserved(self):
        result = compare(run("serial", repairs=0, retries=0),
                         run("parallel", repairs=2, retries=3, cost_usd=0.2))
        self.assertEqual(result["quality"]["serial_repairs"], 0)
        self.assertEqual(result["quality"]["parallel_repairs"], 2)
        self.assertEqual(result["parallel"]["retries"], 3)


if __name__ == "__main__":
    unittest.main()
