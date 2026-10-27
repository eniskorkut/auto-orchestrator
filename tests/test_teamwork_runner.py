from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time

from tools.run_owner import read_process_identity
import unittest
from pathlib import Path

from tools.teamwork import TeamworkEngine, TeamworkError
from tools.teamwork_runner import (
    RunnerError,
    TeamworkRunner,
    adapter_options,
    parse_verdict,
    path_allowed,
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout


class FakeAdapter:
    provider = "fake"

    def __init__(self, verdicts=None, outside_scope=False):
        self.verdicts = verdicts if isinstance(verdicts, dict) else {}
        self.outside_scope = outside_scope
        self.started = []
        self.polls = 0
        self.counts = {}

    def available(self):
        return True

    def close(self):
        return None

    def capabilities(self):
        return {"provider": "fake", "managed_session": True}

    def start(self, *, task_id, prompt, cwd, title=None, model=None, agent=None, resume=None):
        self.started.append({"task_id": task_id, "cwd": Path(cwd), "prompt": prompt,
                             "model": model, "resume": resume})
        cwd = Path(cwd)
        self.counts[task_id] = self.counts.get(task_id, 0) + 1
        if task_id == "backend":
            (cwd / "backend").mkdir(exist_ok=True)
            (cwd / "backend" / "app.py").write_text(
                f"VALUE = {self.counts[task_id]}\n", encoding="utf-8")
        elif task_id == "frontend":
            (cwd / "frontend").mkdir(exist_ok=True)
            (cwd / "frontend" / "index.html").write_text("<h1>ok</h1>\n", encoding="utf-8")
        elif task_id == "challenger":
            (cwd / "tests" / "challenger").mkdir(parents=True, exist_ok=True)
            (cwd / "tests" / "challenger" / "edge.py").write_text("OK = True\n", encoding="utf-8")
        if self.outside_scope:
            (cwd / "README.md").write_text("tampered\n", encoding="utf-8")
        return {"session_id": f"ses_{task_id}_{len(self.started)}",
                "events_path": str(cwd / ".events.jsonl"),
                "status": "running"}

    def poll(self, handle, *, timeout=0):
        self.polls += 1
        task_id = handle["session_id"].split("_")[1]
        queue = self.verdicts.get(task_id) or []
        verdict = queue.pop(0) if queue else "pass" 
        text = json.dumps({"verdict": verdict, "findings": [], "summary": "ok"})
        return {
            "provider": "fake", "session_id": handle["session_id"],
            "status": "completed", "text": text, "tool_count": 1,
            "tokens": {"input": 10, "output": 5}, "cost": 0.001,
            "events_path": handle["events_path"], "exit_code": 0,
            "errors": [],
        }

    def cancel(self, handle, *, grace=1.0):
        return {"requested": True, "terminated": True,
                "native_cancel_ack": False, "local_signal_only": True}


class DelayedFakeAdapter(FakeAdapter):
    def __init__(self, duration=0.6, **kwargs):
        super().__init__(**kwargs)
        self.duration = duration
        self.start_times = {}
        self.end_times = {}
        self.active = 0
        self.max_active = 0
        self.lock = __import__("threading").Lock()

    def start(self, **kwargs):
        handle = super().start(**kwargs)
        task_id = kwargs["task_id"]
        with self.lock:
            self.start_times[task_id] = time.monotonic()
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        handle["due"] = time.monotonic() + self.duration
        return handle

    def poll(self, handle, *, timeout=0):
        if time.monotonic() < handle["due"]:
            return {"status": "running", "session_id": handle["session_id"],
                    "events_path": handle["events_path"]}
        task_id = handle["session_id"].split("_")[1]
        with self.lock:
            if task_id not in self.end_times:
                self.end_times[task_id] = time.monotonic()
                self.active -= 1
        state = super().poll(handle, timeout=timeout)
        return state


def make_repo(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "README.md").write_text("# sample\n", encoding="utf-8")
    (root / ".gitignore").write_text(".auto-orchestrator/\n", encoding="utf-8")
    git(root, "init")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")


def plan(run_id="runner-test", *, report_verdict="pass", max_repair=1):
    return {
        "run_id": run_id,
        "max_parallel": 2,
        "pattern": "distributed_coding",
        "milestones": [{
            "id": "M1",
            "objective": "Build and verify",
            "depends_on": [],
            "acceptance": ["backend and review pass"],
            "verification_policy": {
                "require_roles": ["reviewer"],
                "max_repair": max_repair,
            },
        }],
        "tasks": [
            {
                "id": "backend", "role": "implementer", "milestone": "M1",
                "phase": "build", "objective": "Create backend",
                "acceptance": ["backend file exists"],
                "read_scope": [], "write_scope": ["backend/"],
                "depends_on": [], "timeout_seconds": 30, "max_retries": 1,
                "verify_commands": [f'"{sys.executable}" -c "print(\'ok\')"'],
                "department": "backend", "provider": "fake",
            },
            {
                "id": "tester", "role": "tester", "milestone": "M1",
                "phase": "verify", "objective": "Run tests",
                "acceptance": ["tests pass"],
                "read_scope": ["backend/"], "write_scope": ["tests/"],
                "depends_on": ["backend"], "timeout_seconds": 30, "max_retries": 1,
                "verify_commands": [f'"{sys.executable}" -c "print(\'test ok\')"'],
                "department": "qa", "provider": "fake",
            },
            {
                "id": "critic", "role": "reviewer", "milestone": "M1",
                "phase": "verify", "objective": "Review",
                "acceptance": ["review passes"],
                "read_scope": ["backend/"], "write_scope": [],
                "depends_on": ["tester"], "timeout_seconds": 30, "max_retries": 1,
                "verify_commands": [],
                "department": "review", "provider": "fake",
            },
        ],
    }


def parallel_plan(run_id="parallel-test"):
    return {
        "run_id": run_id,
        "max_parallel": 2,
        "pattern": "distributed_coding",
        "milestones": [{
            "id": "M1", "objective": "Two independent features",
            "depends_on": [], "acceptance": ["both pass"],
            "verification_policy": {"require_roles": [], "max_repair": 0},
        }],
        "tasks": [
            {
                "id": "backend", "role": "implementer", "milestone": "M1",
                "phase": "build", "objective": "Backend",
                "acceptance": ["backend exists"],
                "read_scope": [], "write_scope": ["backend/"], "depends_on": [],
                "timeout_seconds": 30, "max_retries": 0,
                "verify_commands": [f'"{sys.executable}" -c "print(\'backend\')"'],
                "department": "backend", "provider": "fake",
            },
            {
                "id": "frontend", "role": "implementer", "milestone": "M1",
                "phase": "build", "objective": "Frontend",
                "acceptance": ["frontend exists"],
                "read_scope": [], "write_scope": ["frontend/"], "depends_on": [],
                "timeout_seconds": 30, "max_retries": 0,
                "verify_commands": [f'"{sys.executable}" -c "print(\'frontend\')"'],
                "department": "frontend", "provider": "fake",
            },
        ],
    }


class RunnerGitRecoveryTests(unittest.TestCase):
    def test_interrupted_clean_merge_is_completed_on_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            make_repo(root)
            base = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
            git(root, "checkout", "-b", "task-branch")
            (root / "backend").mkdir()
            (root / "backend" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "-m", "task work")
            git(root, "checkout", base)
            merge = subprocess.run(
                ["git", "-C", str(root), "merge", "--no-commit", "--no-ff",
                 "task-branch"], capture_output=True, text=True)
            self.assertEqual(merge.returncode, 0, merge.stderr)
            self.assertTrue((root / ".git" / "MERGE_HEAD").exists())
            runner = TeamworkRunner(root, plan("merge-recovery"),
                                    adapter=FakeAdapter(), max_seconds=10)
            try:
                commit = runner._reconcile_interrupted_merge()
            finally:
                runner.close()
            self.assertIsNotNone(commit)
            self.assertFalse((root / ".git" / "MERGE_HEAD").exists())
            self.assertTrue((root / "backend" / "app.py").is_file())

    def test_interrupted_conflicting_merge_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            make_repo(root)
            base = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
            (root / "README.md").write_text("base\n", encoding="utf-8")
            git(root, "commit", "-am", "base change")
            git(root, "checkout", "-b", "task-branch")
            (root / "README.md").write_text("task\n", encoding="utf-8")
            git(root, "commit", "-am", "task change")
            git(root, "checkout", base)
            (root / "README.md").write_text("main\n", encoding="utf-8")
            git(root, "commit", "-am", "main change")
            merge = subprocess.run(
                ["git", "-C", str(root), "merge", "--no-commit", "--no-ff",
                 "task-branch"], capture_output=True, text=True)
            self.assertNotEqual(merge.returncode, 0)
            runner = TeamworkRunner(root, plan("merge-conflict"),
                                    adapter=FakeAdapter(), max_seconds=10)
            try:
                with self.assertRaises(RunnerError):
                    runner._reconcile_interrupted_merge()
            finally:
                runner.close()


class RunnerHelpersTests(unittest.TestCase):
    def test_codex_provider_options_are_normalized_and_validated(self):
        provider, options, key = adapter_options({
            "id": "codex", "provider": " Codex ",
            "model": "gpt-6.1-sol", "sandbox": "read-only",
            "approval_policy": "never",
        }, "default", {})
        self.assertEqual(provider, "codex")
        self.assertEqual(options["sandbox"], "read-only")
        self.assertEqual(options["approval_policy"], "never")
        self.assertEqual(key, ("codex", "read-only", "never"))
        with self.assertRaises(RunnerError):
            adapter_options({"provider": "codex", "sandbox": []}, "default", {})
        with self.assertRaises(RunnerError):
            adapter_options({"provider": "codex", "approval_policy": {}},
                            "default", {})

    def test_path_scope_and_verdict_parser(self):
        self.assertTrue(path_allowed("backend/app.py", ["backend/"]))
        self.assertFalse(path_allowed("frontend/app.js", ["backend/"]))
        self.assertEqual(parse_verdict('x {"verdict":"pass"} y')["verdict"], "pass")
        self.assertEqual(parse_verdict('{"verdict":"fail"}')["verdict"], "fail")
        self.assertIsNone(parse_verdict("no json"))


class RunnerExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        make_repo(self.root)

    def test_happy_path_commits_worktree_integrates_and_handoffs(self):
        adapter = FakeAdapter()
        runner = TeamworkRunner(self.root, plan(), adapter=adapter, max_seconds=60)
        self.addCleanup(runner.close)
        summary = runner.run()
        self.assertEqual(summary["status"], "passed", summary)
        self.assertTrue((self.root / "backend" / "app.py").is_file(),
                        "integrated worktree change is present")
        self.assertTrue((self.root / ".auto-orchestrator" / "handoffs").is_dir())
        evidence = list((self.root / ".auto-orchestrator" / "evidence" / "runner-test").glob("*.json"))
        self.assertGreater(len(evidence), 0)
        inspect = TeamworkEngine(self.root)
        self.addCleanup(inspect.close)
        state = inspect.status("runner-test")
        self.assertTrue(state["complete"])
        self.assertEqual(len(state["managed_sessions"]), 3)
        self.assertTrue(all(item["status"] == "completed"
                            for item in state["managed_sessions"]))
        self.assertTrue(any(item.get("tool_count") == 1
                            for item in state["managed_sessions"]))

    def test_generated_caches_are_not_scoped_as_agent_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git(root, "init")
            git(root, "config", "user.email", "t@example.invalid")
            git(root, "config", "user.name", "T")
            (root / "README.md").write_text("base\n", encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "-m", "base")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"x")
            runner = TeamworkRunner(root, plan(run_id="cache-test"),
                                    adapter=FakeAdapter(), max_seconds=10)
            try:
                self.assertEqual(runner._changed_files(root), [])
            finally:
                runner.close()

    def test_out_of_scope_edit_fails_without_completing(self):
        adapter = FakeAdapter(outside_scope=True)
        runner = TeamworkRunner(self.root, plan(run_id="scope-test"), adapter=adapter,
                                max_seconds=30)
        self.addCleanup(runner.close)
        summary = runner.run()
        self.assertEqual(summary["status"], "failed", summary)
        inspect = TeamworkEngine(self.root)
        self.addCleanup(inspect.close)
        state = inspect.status("scope-test")
        backend = next(task for task in state["tasks"] if task["id"] == "backend")
        self.assertEqual(backend["state"], "failed")
        self.assertIn("outside its write scope", backend["reason"])

    def test_failed_verification_reopens_implementer_and_retries(self):
        adapter = FakeAdapter(verdicts={"tester": ["fail", "pass"]})
        runner = TeamworkRunner(self.root, plan(run_id="repair-test", max_repair=2),
                                adapter=adapter, max_seconds=60)
        self.addCleanup(runner.close)
        summary = runner.run()
        self.assertEqual(summary["status"], "passed", summary)
        inspect = TeamworkEngine(self.root)
        self.addCleanup(inspect.close)
        state = inspect.status("repair-test")
        milestone = state["milestones"][0]
        self.assertGreaterEqual(milestone["repair_iterations"], 1)
        backend_starts = [item for item in adapter.started if item["task_id"] == "backend"]
        self.assertEqual(len(backend_starts), 2)
        self.assertEqual((self.root / "backend" / "app.py").read_text(encoding="utf-8"),
                         "VALUE = 2\n")

    def test_two_independent_worktrees_run_concurrently(self):
        adapter = DelayedFakeAdapter(duration=4.0)
        runner = TeamworkRunner(self.root, parallel_plan("parallel-test"),
                                adapter=adapter, max_seconds=20)
        self.addCleanup(runner.close)
        summary = runner.run()
        self.assertEqual(summary["status"], "passed", summary)
        # CI wall-clock and worktree startup times vary; prove concurrency from
        # the adapter's execution intervals instead of a fixed elapsed budget.
        self.assertGreaterEqual(adapter.max_active, 2, "both managed tasks were active together")
        overlap = min(adapter.end_times.values()) - max(adapter.start_times.values())
        self.assertGreater(overlap, 1.0, "task execution intervals must overlap")
        self.assertGreater(
            overlap, 0.25 * adapter.duration,
            "serialized tasks would have no positive interval overlap",
        )
        self.assertEqual(summary["timings"]["backend"]["cost"], 0.001)

    def test_cancel_flag_stops_locally_without_native_ack(self):
        runner = TeamworkRunner(self.root, parallel_plan("cancel-test"),
                                adapter=DelayedFakeAdapter(duration=2), max_seconds=10)
        self.addCleanup(runner.close)
        runner.cancel_path.parent.mkdir(parents=True, exist_ok=True)
        runner.cancel_path.write_text("{}", encoding="utf-8")
        summary = runner.run()
        self.assertEqual(summary["status"], "cancelled")
        self.assertFalse(summary["native_cancel_ack"])

    def _orphan_run(self, run_id, *, pid=None, token=None, uid=None):
        plan_value = parallel_plan(run_id)
        plan_value["tasks"] = plan_value["tasks"][:1]
        plan_value["milestones"][0]["verification_policy"]["max_repair"] = 1
        engine = TeamworkEngine(self.root)
        self.addCleanup(engine.close)
        engine.init(plan_value)
        claimed = engine.supervisor.claim(run_id)["claimed"][0]
        engine.record_managed_session(
            run_id, "backend", attempt=1, provider="fake",
            session_id="ses_orphan", status="running", model="fake",
            pid=pid, pid_start_token=token, pid_uid=uid,
        )
        engine.supervisor.conn.execute(
            "UPDATE tasks SET state='running', lease=?, started=?, attempts=1 "
            "WHERE run_id=? AND id='backend'",
            (claimed["lease"], time.time(), run_id),
        )
        return plan_value, engine

    def test_orphaned_running_task_without_identity_is_blocked(self):
        plan_value, engine = self._orphan_run("orphan-blocked")
        runner = TeamworkRunner(self.root, plan_value, adapter=FakeAdapter(),
                                max_seconds=30, recover_orphans=True)
        self.addCleanup(runner.close)
        summary = runner.run()
        self.assertEqual(summary["status"], "failed", summary)
        self.assertEqual(summary["recovery"], "recovery_failed")
        self.assertTrue(summary["recovery_blocked"])
        self.assertFalse(summary["recovered_orphans"][0]["process"]["recorded"])
        self.assertFalse(summary["recovered_orphans"][0]["native_cancel_ack"])
        row = engine.supervisor.conn.execute(
            "SELECT state FROM tasks WHERE run_id='orphan-blocked' AND id='backend'"
        ).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertNotEqual(row["state"], "completed")

    def test_recovery_never_signals_an_unowned_live_pid(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(30)"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        def cleanup_child():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)

        self.addCleanup(cleanup_child)
        identity = read_process_identity(child.pid)
        self.assertIsNotNone(identity)
        plan_value, engine = self._orphan_run(
            "orphan-unowned", pid=identity["pid"],
            token="reused-pid-token", uid=identity.get("uid"))
        runner = TeamworkRunner(self.root, plan_value, adapter=FakeAdapter(),
                                max_seconds=30, recover_orphans=True)
        self.addCleanup(runner.close)
        summary = runner.run()
        self.assertEqual(summary["status"], "failed", summary)
        self.assertTrue(summary["recovery_blocked"])
        self.assertIsNone(child.poll(), "unowned process must not be signaled")
        item = summary["recovered_orphans"][0]
        self.assertFalse(item["process"]["signaled"])
        self.assertEqual(item["process"]["status"], "pid_reused_or_unowned_process")

    def test_orphaned_running_task_with_stopped_identity_is_repaired(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        identity = read_process_identity(child.pid)
        child.wait(timeout=10)
        self.assertIsNotNone(identity)
        plan_value, engine = self._orphan_run(
            "orphan-test", pid=identity["pid"],
            token=identity["start_token"], uid=identity.get("uid"))
        runner = TeamworkRunner(self.root, plan_value, adapter=FakeAdapter(),
                                max_seconds=30, recover_orphans=True)
        self.addCleanup(runner.close)
        summary = runner.run()
        self.assertEqual(summary["status"], "passed", summary)
        self.assertEqual(summary["recovery"], "recovered")
        self.assertEqual(summary["recovered_orphans"][0]["task_id"], "backend")
        self.assertFalse(summary["recovered_orphans"][0]["native_cancel_ack"])
        sessions = engine.managed_sessions("orphan-test")
        self.assertTrue(any(item.get("cancel_state") == "orphaned"
                            for item in sessions))

    def test_restart_reloads_persisted_milestone_state(self):
        adapter = FakeAdapter()
        runner = TeamworkRunner(self.root, plan(run_id="restart-test"), adapter=adapter,
                                max_seconds=60)
        self.addCleanup(runner.close)
        summary = runner.run()
        self.assertEqual(summary["status"], "passed")
        engine = TeamworkEngine(self.root)
        self.addCleanup(engine.close)
        reloaded = engine.status("restart-test")
        self.assertTrue(reloaded["complete"])
        self.assertEqual(reloaded["verified_progress"], 1.0)


if __name__ == "__main__":
    unittest.main()
