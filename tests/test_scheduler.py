from __future__ import annotations

import threading
import time
import unittest

from tools.scheduler import PollScheduler, SchedulerError


class FakePoll:
    def __init__(self, duration: float, *, fail: bool = False):
        self.duration = duration
        self.fail = fail
        self.start = time.monotonic()
        self.calls = 0

    def __call__(self, handle, *, timeout=0):
        self.calls += 1
        if time.monotonic() - self.start >= self.duration:
            if self.fail:
                return {"status": "failed", "error": "simulated"}
            return {"status": "completed", "text": handle}
        return {"status": "running"}


class SchedulerTests(unittest.TestCase):
    def test_independent_tasks_overlap_in_wall_time(self):
        scheduler = PollScheduler(max_workers=2, poll_slice=0.05)
        start = time.monotonic()
        scheduler.submit("backend", "backend", FakePoll(0.5), timeout=5)
        scheduler.submit("frontend", "frontend", FakePoll(0.5), timeout=5)
        completed = {}
        while len(completed) < 2:
            for key, state in scheduler.wait(timeout=2):
                completed[key] = state
        elapsed = time.monotonic() - start
        scheduler.close()
        self.assertEqual(set(completed), {"backend", "frontend"})
        self.assertLess(elapsed, 1.8, "two 0.5s tasks should overlap with CI overhead")
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertGreaterEqual(scheduler.completed, 2)

    def test_bounded_concurrency_and_completion_queue(self):
        scheduler = PollScheduler(max_workers=1, poll_slice=0.05)
        scheduler.submit("one", "one", FakePoll(0.1), timeout=5)
        with self.assertRaises(SchedulerError):
            scheduler.submit("two", "two", FakePoll(0.1), timeout=5)
        results = scheduler.wait(timeout=2)
        self.assertEqual(results[0][0], "one")
        scheduler.submit("two", "two", FakePoll(0.05), timeout=5)
        self.assertEqual(scheduler.wait(timeout=2)[0][0], "two")
        scheduler.close()

    def test_failure_timeout_and_duplicate_keys_are_bounded(self):
        scheduler = PollScheduler(max_workers=2, poll_slice=0.05)
        scheduler.submit("bad", "bad", FakePoll(1.0, fail=True), timeout=5)
        with self.assertRaises(SchedulerError):
            scheduler.submit("bad", "bad", FakePoll(0.01), timeout=5)
        key, state = scheduler.wait(timeout=2)[0]
        self.assertEqual((key, state["status"]), ("bad", "failed"))
        with self.assertRaises(SchedulerError):
            scheduler.submit("", "x", FakePoll(0.01), timeout=5)
        with self.assertRaises(SchedulerError):
            scheduler.submit("x", "x", FakePoll(0.01), timeout=0)
        scheduler.close()

    def test_wait_without_completion_returns_empty(self):
        scheduler = PollScheduler(max_workers=1, poll_slice=0.05)
        scheduler.submit("slow", "slow", FakePoll(0.3), timeout=5)
        self.assertEqual(scheduler.drain(), [])
        time.sleep(0.35)
        self.assertEqual(scheduler.drain()[0][0], "slow")
        scheduler.close()

    def test_close_is_safe_with_active_watchers(self):
        scheduler = PollScheduler(max_workers=2, poll_slice=0.05)
        scheduler.submit("a", "a", FakePoll(0.2), timeout=5)
        scheduler.submit("b", "b", FakePoll(0.2), timeout=5)
        scheduler.close()
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
