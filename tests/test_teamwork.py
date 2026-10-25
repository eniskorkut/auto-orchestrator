from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tools.supervisor import Supervisor
from tools.teamwork import (
    HANDOFF_FIELDS,
    TeamworkEngine,
    TeamworkError,
    validate_teamwork_plan,
)


def task(task_id, milestone, role, depends_on=None, *, phase="build",
         write_scope=None, read_scope=None, objective=None):
    return {
        "id": task_id,
        "milestone": milestone,
        "role": role,
        "phase": phase,
        "objective": objective or f"Complete {task_id}",
        "depends_on": list(depends_on or []),
        "read_scope": list(read_scope or ["src/"]),
        "write_scope": list(write_scope or []),
        "verify_commands": ["python3.11 -m unittest tests.test_teamwork -q"],
        "provider": "opencode",
        "model": "opencode-go/deepseek-v4.1-flash",
    }


def sample_plan(run_id="team-run", *, max_repair=2):
    return {
        "run_id": run_id,
        "max_parallel": 3,
        "pattern": "distributed_coding",
        "milestones": [
            {
                "id": "M1", "objective": "Discover the repository",
                "depends_on": [], "acceptance": ["Discovery is complete"],
                "verification_policy": {"require_roles": [], "max_repair": max_repair},
            },
            {
                "id": "M2", "objective": "Build the change",
                "depends_on": ["M1"], "acceptance": ["The change is built"],
                "verification_policy": {"require_roles": [], "max_repair": max_repair},
            },
            {
                "id": "M3", "objective": "Verify the change",
                "depends_on": ["M2"], "acceptance": ["Tests pass"],
                "verification_policy": {"require_roles": ["tester"], "max_repair": max_repair},
            },
            {
                "id": "M4", "objective": "Review the change",
                "depends_on": ["M3"], "acceptance": ["Review verdicts are explicit"],
                "verification_policy": {
                    "require_roles": ["reviewer", "challenger", "auditor"],
                    "max_repair": max_repair,
                },
            },
        ],
        "tasks": [
            task("discover", "M1", "explorer", read_scope=["src/", "tests/"]),
            task("build", "M2", "implementer", ["discover"],
                 write_scope=["src/"]),
            task("verify", "M3", "tester", ["build"], phase="verify",
                 write_scope=["tests/"], read_scope=["src/", "tests/"]),
            task("review", "M4", "reviewer", ["verify"], phase="verify",
                 read_scope=["src/", "tests/"]),
            task("challenge", "M4", "challenger", ["verify"], phase="verify",
                 read_scope=["src/", "tests/"]),
            task("audit", "M4", "auditor", ["verify"], phase="verify",
                 read_scope=["src/", "tests/"]),
        ],
    }


def legacy_plan(run_id="legacy-run"):
    return {
        "run_id": run_id,
        "max_parallel": 2,
        "tasks": [
            {
                "id": "explore", "role": "explorer",
                "objective": "Inspect the existing behavior",
                "depends_on": [], "read_scope": ["src/"], "write_scope": [],
            },
            {
                "id": "change", "role": "implementer",
                "objective": "Make the requested change",
                "depends_on": ["explore"], "read_scope": ["src/"],
                "write_scope": ["src/"],
            },
        ],
    }


def result(task_id, verdict="pass", changed_files=None, decisions=None,
           findings=None):
    return {
        "summary": f"Completed {task_id}",
        "changed_files": list(changed_files or []),
        "tests": [{"name": f"{task_id}-check", "status": "passed"}],
        "findings": list(findings or []),
        "verdict": verdict,
        "decisions": list(decisions or [f"decision-{task_id}"]),
        "open_issues": [],
    }


def changed_for(task_id):
    if task_id.startswith("build"):
        return ["src/app.py"]
    if task_id == "verify":
        return ["tests/test_app.py"]
    return []


class TeamworkTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = TeamworkEngine(self.root)
        self.supervisor = Supervisor(self.root)
        self.addCleanup(self.supervisor.close)
        self.addCleanup(self.engine.close)

    def init_sample(self, *, run_id="team-run", max_repair=2):
        plan = sample_plan(run_id, max_repair=max_repair)
        self.engine.init(plan)
        return plan

    def milestone(self, run_id, milestone_id):
        state = self.engine.status(run_id)
        return next(item for item in state["milestones"]
                    if item["id"] == milestone_id)

    def complete_milestone(self, run_id, milestone_id, *, verdicts=None,
                           results=None):
        milestone = self.milestone(run_id, milestone_id)
        remaining = {item["id"] for item in milestone["tasks"]
                     if item["state"] == "pending"}
        verdicts = dict(verdicts or {})
        results = dict(results or {})
        while remaining:
            claimed = self.supervisor.claim(run_id)["claimed"]
            self.assertTrue(claimed, f"No task could be claimed for {milestone_id}")
            for item in claimed:
                task_id = item["id"]
                self.assertIn(task_id, remaining,
                              f"Claimed {task_id} outside {milestone_id}")
                payload = results.get(
                    task_id,
                    result(task_id, verdicts.get(task_id, "pass"),
                           changed_for(task_id)))
                self.supervisor.change(run_id, task_id, item["lease"],
                                       "completed", result=payload)
                remaining.remove(task_id)
        return self.engine.status(run_id)


class TeamworkValidationTests(unittest.TestCase):
    def assert_invalid(self, plan):
        with self.assertRaises(TeamworkError):
            validate_teamwork_plan(plan)

    def test_legacy_plan_derives_one_milestone(self):
        normalized = validate_teamwork_plan(legacy_plan())
        self.assertEqual([item["id"] for item in normalized["milestones"]], ["M1"])
        self.assertEqual(normalized["milestones"][0]["task_ids"],
                         ["explore", "change"])
        self.assertEqual(normalized["tasks"]["explore"]["milestone"], "M1")
        self.assertEqual(normalized["milestones"][0]["verification_policy"],
                         {"require_roles": [], "max_repair": 2})

    def test_task_without_milestone_is_rejected(self):
        plan = sample_plan()
        plan["tasks"][0].pop("milestone")
        self.assert_invalid(plan)

    def test_unknown_milestone_and_duplicate_milestone_are_rejected(self):
        plan = sample_plan()
        plan["tasks"][0]["milestone"] = "M9"
        self.assert_invalid(plan)

        plan = sample_plan()
        plan["milestones"].append(copy.deepcopy(plan["milestones"][0]))
        self.assert_invalid(plan)

    def test_milestone_dependency_cycle_is_rejected(self):
        plan = sample_plan()
        plan["milestones"][0]["depends_on"] = ["M4"]
        self.assert_invalid(plan)

    def test_empty_milestone_is_rejected(self):
        plan = sample_plan()
        plan["milestones"].append({
            "id": "M5", "objective": "Empty", "depends_on": ["M4"],
            "acceptance": [], "verification_policy": {
                "require_roles": [], "max_repair": 2},
        })
        self.assert_invalid(plan)

    def test_task_inversion_and_unordered_dependency_are_rejected(self):
        def two_milestone_plan(milestone_dependencies):
            return {
                "run_id": "inversion",
                "milestones": [
                    {"id": "M1", "objective": "One",
                     "depends_on": list(milestone_dependencies),
                     "verification_policy": {"require_roles": [], "max_repair": 2}},
                    {"id": "M2", "objective": "Two", "depends_on": [],
                     "verification_policy": {"require_roles": [], "max_repair": 2}},
                ],
                "tasks": [
                    task("a", "M1", "explorer", ["b"]),
                    task("b", "M2", "implementer"),
                ],
            }

        # M1 depends on a task in M2 without the corresponding milestone edge.
        self.assert_invalid(two_milestone_plan([]))
        # The same dependency is legal when the milestone dependency exists.
        validate_teamwork_plan(two_milestone_plan(["M2"]))

    def test_task_dependency_in_unordered_sibling_milestone_is_rejected(self):
        plan = {
            "run_id": "unordered",
            "milestones": [
                {"id": "M1", "objective": "One", "depends_on": [],
                 "verification_policy": {"require_roles": [], "max_repair": 2}},
                {"id": "M2", "objective": "Two", "depends_on": [],
                 "verification_policy": {"require_roles": [], "max_repair": 2}},
            ],
            "tasks": [
                task("a", "M1", "explorer", ["b"]),
                task("b", "M2", "implementer"),
            ],
        }
        self.assert_invalid(plan)

    def test_invalid_pattern_policy_phase_and_role_are_rejected(self):
        plan = sample_plan()
        plan["pattern"] = "made_up"
        self.assert_invalid(plan)

        plan = sample_plan()
        plan["milestones"][0]["verification_policy"]["require_roles"] = ["wizard"]
        self.assert_invalid(plan)

        plan = sample_plan()
        plan["milestones"][0]["verification_policy"]["max_repair"] = 100
        self.assert_invalid(plan)

        plan = sample_plan()
        plan["tasks"][0]["phase"] = "deploy"
        self.assert_invalid(plan)

        plan = sample_plan()
        plan["milestones"][0]["depends_on"] = ["M9"]
        self.assert_invalid(plan)

    def test_unknown_role_still_rejected_by_supervisor_validation(self):
        plan = sample_plan()
        plan["tasks"][0]["role"] = "wizard"
        self.assert_invalid(plan)


class TeamworkEngineTests(TeamworkTestCase):
    def test_ready_dependency_order_and_progress(self):
        self.init_sample()
        state = self.engine.status("team-run")
        self.assertEqual(state["pattern"], "distributed_coding")
        self.assertEqual([item["status"] for item in state["milestones"]],
                         ["ready", "planned", "planned", "planned"])
        self.assertEqual(state["verified_progress"], 0.0)
        self.assertIsNone(state["estimated_progress"])

        state = self.complete_milestone("team-run", "M1")
        self.assertEqual(self.milestone("team-run", "M1")["status"], "passed")
        self.assertEqual(self.milestone("team-run", "M2")["status"], "ready")
        self.assertEqual(state["verified_progress"], 0.25)

        state = self.complete_milestone("team-run", "M2")
        self.assertEqual(self.milestone("team-run", "M3")["status"], "ready")
        self.assertEqual(state["verified_progress"], 0.5)

        state = self.complete_milestone("team-run", "M3")
        self.assertEqual(self.milestone("team-run", "M4")["status"], "ready")
        self.assertEqual(state["verified_progress"], 0.75)

        state = self.complete_milestone("team-run", "M4")
        self.assertEqual(state["verified_progress"], 1.0)
        self.assertIsNone(state["estimated_progress"])
        self.assertTrue(state["complete"])
        self.assertEqual(state["status"], "passed")

    def test_independent_milestones_can_be_ready_together(self):
        plan = {
            "run_id": "independent",
            "milestones": [
                {"id": "M1", "objective": "One", "depends_on": [],
                 "verification_policy": {"require_roles": [], "max_repair": 2}},
                {"id": "M2", "objective": "Two", "depends_on": [],
                 "verification_policy": {"require_roles": [], "max_repair": 2}},
            ],
            "tasks": [
                task("a", "M1", "explorer", write_scope=["a/"],
                     read_scope=["a/"]),
                task("b", "M2", "explorer", write_scope=["b/"],
                     read_scope=["b/"]),
            ],
        }
        self.engine.init(plan)
        state = self.engine.status("independent")
        self.assertEqual(state["milestones"][0]["status"], "ready")
        self.assertEqual(state["milestones"][1]["status"], "ready")
        self.assertEqual(state["status"], "ready")

    def test_running_verifying_and_explicit_verification(self):
        self.init_sample()
        claimed = self.supervisor.claim("team-run")["claimed"]
        self.assertEqual(claimed[0]["id"], "discover")
        self.assertEqual(self.milestone("team-run", "M1")["status"], "running")
        with self.assertRaises(TeamworkError):
            self.engine.begin_verification("team-run", "M1")
        self.supervisor.change("team-run", "discover", claimed[0]["lease"],
                               "completed", result=result("discover"))
        self.complete_milestone("team-run", "M2")
        verify = self.supervisor.claim("team-run")["claimed"][0]
        self.assertEqual(verify["id"], "verify")
        # A running verification task is never a pass.
        self.assertEqual(self.milestone("team-run", "M3")["status"], "running")
        self.supervisor.change("team-run", "verify", verify["lease"], "completed",
                               result=result("verify", "fail",
                                             changed_for("verify")))
        self.assertEqual(self.milestone("team-run", "M3")["status"], "failed")
        self.assertNotEqual(self.milestone("team-run", "M3")["status"], "passed")
        self.assertEqual(self.milestone("team-run", "M3")["missing_roles"], ["tester"])
        self.assertEqual(self.milestone("team-run", "M3")["satisfied_roles"], [])
        with self.assertRaises(TeamworkError):
            self.engine.begin_verification("team-run", "M3")

        # A completed verification task without a verdict leaves the milestone
        # verifying; it must never be inferred as a pass.
        self.engine.reopen("team-run", "verify", "Re-run with a recorded verdict")
        verify = self.supervisor.claim("team-run")["claimed"][0]
        incomplete = result("verify", changed_files=changed_for("verify"))
        incomplete.pop("verdict")
        self.supervisor.change("team-run", "verify", verify["lease"], "completed",
                               result=incomplete)
        self.assertEqual(self.milestone("team-run", "M3")["status"], "verifying")
        output = self.engine.begin_verification("team-run", "M3")
        self.assertEqual(output["status"], "verifying")
        self.assertEqual(output["milestone_id"], "M3")
        self.assertEqual(output["estimated_progress"], None)

        self.engine.reopen("team-run", "verify", "Record a passing verdict")
        verify = self.supervisor.claim("team-run")["claimed"][0]
        self.supervisor.change("team-run", "verify", verify["lease"], "completed",
                               result=result("verify", "pass",
                                             changed_for("verify")))
        self.assertEqual(self.milestone("team-run", "M3")["status"], "passed")

    def test_pass_requires_every_task_and_role_verdict(self):
        self.init_sample()
        self.complete_milestone("team-run", "M1")
        self.complete_milestone("team-run", "M2")
        self.complete_milestone("team-run", "M3")
        self.assertEqual(self.milestone("team-run", "M4")["status"], "ready")
        self.complete_milestone("team-run", "M4",
                                verdicts={"challenge": "fail"})
        milestone = self.milestone("team-run", "M4")
        self.assertEqual(milestone["status"], "failed")
        self.assertNotEqual(milestone["status"], "passed")
        self.assertEqual(milestone["missing_roles"], ["challenger"])
        self.assertEqual(milestone["satisfied_roles"], ["reviewer", "auditor"])

        reopened = self.engine.reopen("team-run", "challenge",
                                      "Counterexample requires a repair")
        self.assertEqual(reopened["state"], "pending")
        self.assertEqual(reopened["milestone_status"], "repairing")
        self.assertFalse(reopened["native_cancellation_required"])
        self.complete_milestone("team-run", "M4",
                                verdicts={"review": "pass", "challenge": "pass",
                                          "audit": "pass"})
        self.assertEqual(self.milestone("team-run", "M4")["status"], "passed")
        self.assertEqual(self.engine.status("team-run")["verified_progress"], 1.0)

    def test_failed_milestone_blocks_dependents_and_can_be_repaired(self):
        self.init_sample()
        self.complete_milestone("team-run", "M1")
        build = self.supervisor.claim("team-run")["claimed"][0]
        self.assertEqual(build["id"], "build")
        self.supervisor.change("team-run", "build", build["lease"], "failed",
                               reason="Observed compile failure")
        state = self.engine.status("team-run")
        self.assertEqual(self.milestone("team-run", "M2")["status"], "failed")
        self.assertEqual(self.milestone("team-run", "M3")["status"], "blocked")
        self.assertEqual(self.milestone("team-run", "M4")["status"], "blocked")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["verified_progress"], 0.25)
        next_state = self.engine.next("team-run")
        self.assertIsNone(next_state["next_milestone"])
        self.assertEqual(next_state["repair_required"], ["M2"])

        reopened = self.engine.reopen("team-run", "build",
                                      "Repair the compile failure")
        self.assertEqual(reopened["milestone_status"], "repairing")
        self.assertEqual(self.milestone("team-run", "M2")["status"], "repairing")
        self.complete_milestone("team-run", "M2")
        self.assertEqual(self.milestone("team-run", "M2")["status"], "passed")
        self.assertEqual(self.milestone("team-run", "M3")["status"], "ready")

    def test_repair_limit_is_enforced(self):
        self.init_sample(max_repair=1)
        self.complete_milestone("team-run", "M1")
        self.complete_milestone("team-run", "M2")
        verify = self.supervisor.claim("team-run")["claimed"][0]
        self.supervisor.change("team-run", "verify", verify["lease"], "completed",
                               result=result("verify", "fail",
                                             changed_for("verify")))
        self.engine.reopen("team-run", "verify", "First repair")
        verify = self.supervisor.claim("team-run")["claimed"][0]
        self.supervisor.change("team-run", "verify", verify["lease"], "completed",
                               result=result("verify", "fail",
                                             changed_for("verify")))
        self.assertEqual(self.milestone("team-run", "M3")["status"], "failed")
        with self.assertRaises(TeamworkError):
            self.engine.reopen("team-run", "verify", "Second repair")
        verify = self.milestone("team-run", "M3")["tasks"][0]
        self.assertEqual(verify["state"], "completed")

    def test_reopen_clears_lease_result_and_keeps_attempt_history(self):
        self.init_sample()
        self.complete_milestone("team-run", "M1")
        self.complete_milestone("team-run", "M2")
        verify = self.supervisor.claim("team-run")["claimed"][0]
        self.supervisor.change("team-run", "verify", verify["lease"], "completed",
                               result=result("verify", "fail",
                                             changed_for("verify")))
        self.assertEqual(self.milestone("team-run", "M3")["status"], "failed")
        reopen = self.engine.reopen("team-run", "verify", "Need more evidence")
        self.assertEqual(reopen["state"], "pending")
        row = self.supervisor.conn.execute(
            "SELECT state,lease,result,attempts FROM tasks "
            "WHERE run_id=? AND id=?", ("team-run", "verify")).fetchone()
        self.assertEqual(row["state"], "pending")
        self.assertIsNone(row["lease"])
        self.assertIsNone(row["result"])
        self.assertEqual(row["attempts"], 1)
        milestone = self.milestone("team-run", "M3")
        self.assertEqual(milestone["status"], "repairing")
        self.assertEqual(milestone["repair_iterations"], 1)
        self.assertEqual(milestone["last_error"], "Need more evidence")

        with self.assertRaises(TeamworkError):
            self.engine.reopen("team-run", "verify", "Still pending")
        claimed = self.supervisor.claim("team-run")["claimed"][0]
        self.assertEqual(claimed["id"], "verify")
        self.assertEqual(claimed["attempt"], 2)
        self.supervisor.change("team-run", "verify", claimed["lease"],
                               "completed", result=result("verify", "pass",
                                                          changed_for("verify")))
        self.assertEqual(self.milestone("team-run", "M3")["status"], "passed")

    def test_reopen_terminal_milestone_is_rejected(self):
        self.init_sample()
        self.complete_milestone("team-run", "M1")
        with self.assertRaises(TeamworkError):
            self.engine.reopen("team-run", "discover", "Cannot happen")

    def test_cancelled_pending_task_milestone_is_explicit(self):
        self.init_sample()
        cancelled = self.supervisor.cancel("team-run", "discover")
        self.assertFalse(cancelled["native_cancellation_required"])
        self.assertEqual(self.milestone("team-run", "M1")["status"], "cancelled")
        self.assertIn(self.milestone("team-run", "M2")["status"],
                      ("blocked", "planned"))

    def test_begin_verification_requires_completed_dependencies(self):
        self.init_sample()
        self.complete_milestone("team-run", "M1")
        with self.assertRaises(TeamworkError):
            self.engine.begin_verification("team-run", "M2")

    def test_next_reports_the_first_actionable_milestone(self):
        self.init_sample()
        state = self.engine.next("team-run")
        self.assertEqual(state["next_milestone"], "M1")
        self.assertEqual(state["milestone_id"], "M1")
        self.assertEqual(state["pending_tasks"], ["discover"])
        self.assertEqual(state["ready"], ["M1"])
        self.assertFalse(state["complete"])

        self.complete_milestone("team-run", "M1")
        self.assertEqual(self.engine.next("team-run")["next_milestone"], "M2")

    def test_restart_reloads_persisted_state(self):
        self.init_sample()
        self.complete_milestone("team-run", "M1")
        handoff = self.engine.record_handoff("team-run", "M1")
        restarted = TeamworkEngine(self.root)
        try:
            state = restarted.status("team-run")
        finally:
            restarted.close()
        self.assertEqual(state["milestones"][0]["status"], "passed")
        self.assertEqual(state["milestones"][1]["status"], "ready")
        self.assertEqual(state["pattern"], "distributed_coding")
        self.assertEqual(state["tasks"][0]["milestone"], "M1")
        self.assertTrue(state["tasks"][0]["verify_commands"])
        self.assertEqual(handoff["version"], 1)
        reopened = TeamworkEngine(self.root)
        try:
            restored = reopened.handoff("team-run", "M1")
        finally:
            reopened.close()
        self.assertEqual(restored["milestone_id"], "M1")
        self.assertEqual(restored["version"], 1)
        self.assertTrue(Path(restored["path"]).is_file())

    def test_legacy_plan_initializes_and_passes_as_one_milestone(self):
        plan = legacy_plan()
        output = self.engine.init(plan)
        self.assertEqual(output["milestone_count"], 1)
        self.assertEqual(output["task_count"], 2)
        self.assertEqual(output["milestones"][0]["id"], "M1")
        supervisor_state = self.supervisor.status("legacy-run")
        self.assertEqual(len(supervisor_state["tasks"]), 2)
        self.assertEqual(supervisor_state["tasks"][0]["state"], "pending")
        self.assertEqual(output["max_parallel"], 2)

        state = self.complete_milestone("legacy-run", "M1")
        self.assertEqual(state["milestones"][0]["status"], "passed")
        self.assertEqual(state["verified_progress"], 1.0)

    def test_duplicate_init_is_idempotent_only_for_the_same_plan(self):
        plan = self.init_sample()
        second = self.engine.init(plan)
        self.assertEqual(second["run_id"], "team-run")
        changed = copy.deepcopy(plan)
        changed["milestones"][0]["objective"] = "Different objective"
        with self.assertRaises(TeamworkError):
            self.engine.init(changed)

    def test_handoff_is_versioned_and_idempotent(self):
        self.init_sample()
        self.complete_milestone("team-run", "M1")
        first = self.engine.record_handoff("team-run", "M1")
        self.assertEqual(first["version"], 1)
        self.assertFalse(first["unchanged"])
        path = Path(first["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "team-run-M1.v1.json")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(first["sha256"],
                         hashlib.sha256(path.read_bytes()).hexdigest())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), set(HANDOFF_FIELDS))
        self.assertEqual(payload["milestone_id"], "M1")
        self.assertEqual(payload["completed_tasks"], ["discover"])
        self.assertEqual(payload["next_milestone"], "M2")
        self.assertIn("M1 passed", payload["context_summary"])
        self.assertTrue(payload["evidence_refs"])
        self.assertIn("supervisor://team-run/tasks/discover",
                      payload["evidence_refs"])

        second = self.engine.record_handoff("team-run", "M1")
        self.assertTrue(second["unchanged"])
        self.assertEqual(second["version"], 1)
        self.assertEqual(Path(second["path"]), path)
        files = list((self.root / ".auto-orchestrator" / "handoffs")
                     .glob("team-run-M1.v*.json"))
        self.assertEqual(len(files), 1)

        # Change the persisted evidence: the next handoff must be a new version.
        changed = result("discover", decisions=["second decision"])
        self.supervisor.conn.execute(
            "UPDATE tasks SET result=? WHERE run_id=? AND id=?",
            (json.dumps(changed), "team-run", "discover"))
        third = self.engine.record_handoff("team-run", "M1")
        self.assertFalse(third["unchanged"])
        self.assertEqual(third["version"], 2)
        self.assertEqual(Path(third["path"]).name, "team-run-M1.v2.json")
        self.assertEqual(third["decisions"], ["second decision"])
        latest = self.engine.handoff("team-run", "M1")
        self.assertEqual(latest["version"], 2)
        self.assertEqual(latest["path"], third["path"])

    def test_handoff_requires_a_passed_milestone(self):
        self.init_sample()
        with self.assertRaises(TeamworkError):
            self.engine.record_handoff("team-run", "M1")
        with self.assertRaises(TeamworkError):
            self.engine.handoff("team-run", "M1")

    def test_managed_sessions_are_persisted_and_updated(self):
        self.init_sample()
        first = self.engine.record_managed_session(
            "team-run", "discover", 1, provider="opencode",
            session_id="ses_abc123", status="running",
            events_path=".auto-orchestrator/managed/team-run/1.jsonl",
            model="opencode-go/deepseek-v4.1-flash", cost=0.01,
            tokens={"input": 10, "output": 2})
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["tokens"], {"input": 10, "output": 2})
        second = self.engine.record_managed_session(
            "team-run", "discover", 1, provider="opencode",
            session_id="ses_abc123", status="completed", finished_at=123.0,
            cost=0.02, tokens={"input": 12, "output": 3},
            cancel_state="not_requested")
        self.assertEqual(second["started_at"], first["started_at"])
        state = self.engine.status("team-run")
        self.assertEqual(len(state["managed_sessions"]), 1)
        self.assertEqual(state["managed_sessions"][0]["status"], "completed")

        restarted = TeamworkEngine(self.root)
        try:
            sessions = restarted.managed_sessions("team-run")
            reloaded = restarted.status("team-run")["managed_sessions"]
        finally:
            restarted.close()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(sessions[0]["tokens"], {"input": 12, "output": 3})
        self.assertEqual(sessions[0]["finished_at"], 123.0)
        with self.assertRaises(TeamworkError):
            self.engine.record_managed_session(
                "team-run", "discover", 2, provider="opencode",
                session_id="ses_bad", status="invented")


if __name__ == "__main__":
    unittest.main()
