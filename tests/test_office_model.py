from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from tools.dashboard import Store
from tools.event_model import (
    EventModelError,
    RunSequencer,
    SequenceConflict,
    normalize_events,
    normalize_run_event,
)
from tools.office_model import (
    DISPATCHING_DETAIL,
    NO_BINDING_DETAIL,
    ROLE_ROOMS,
    STALE_DETAIL,
    WORKING_DETAIL,
    build_inspector,
    build_office,
)
from tools.supervisor import Supervisor

PROJECT_ID = "p_" + "1" * 16
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def agent(agent_id="a_000000000001", name="Ada", role="Engineer", platform="opencode",
          **extra):
    value = {
        "agent_definition_id": agent_id,
        "name": name,
        "role": role,
        "platform": platform,
        "model": None,
        "color": "#7dd3fc",
        "created_at": 0,
    }
    value.update(extra)
    return value


def project(*agents, project_id=PROJECT_ID):
    return {
        "project_id": project_id,
        "name": "Test Lab",
        "description": "",
        "root": "/tmp/test-project",
        "theme": "ai",
        "created_at": 0,
        "updated_at": 0,
        "agents": list(agents),
    }


def binding(agent_id="a_000000000001", run_id="run-1", native_hash=HASH_A,
            task_id=None, session_id=None):
    return {
        "agent_definition_id": agent_id,
        "native_hash": native_hash,
        "run_id": run_id,
        "session_id": session_id,
        "task_id": task_id,
        "bound_at": 0,
    }


def write_run(root: Path, run_id: str, events: list[dict]) -> Path:
    path = root / ".auto-orchestrator" / "runs" / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


def write_policy(root: Path) -> str:
    policy = {
        "version": 1,
        "auto_verify": True,
        "required_checks": [{"id": "unit", "tool": "bash", "command": "python -m unittest"}],
    }
    raw = (json.dumps(policy, separators=(",", ":")) + "\n").encode()
    path = root / ".auto-orchestrator" / "verification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def terminal_run(started: float, status: str, native_hash: str = HASH_A) -> list[dict]:
    return [
        {"event": "start", "platform": "opencode", "time": started},
        {"event": "agent-start", "agent_id": "a1b2c3d4", "native_hash": native_hash,
         "time": started + 1},
        {"event": "agent-stop", "agent_id": "a1b2c3d4", "time": started + 5},
        {"event": "finish", "status": status, "verified": False, "time": started + 6},
    ]


class OfficeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)


class OfficeStatusTests(OfficeTestCase):
    def test_no_binding_is_unknown_not_idle(self):
        office = build_office(project(agent()), Store(self.root).state())
        entry = office["agents"][0]
        self.assertEqual(entry["status"], "unknown")
        self.assertEqual(entry["status_label"], "Unknown")
        self.assertEqual(entry["evidence"]["source"], "none")
        self.assertEqual(entry["evidence"]["confidence"], "unknown")
        self.assertEqual(entry["evidence"]["detail"], NO_BINDING_DETAIL)
        self.assertIsNone(entry["binding"])
        self.assertIsNone(entry["current"]["run_id"])
        self.assertEqual(office["summary"], {"agents": 1, "working": 0, "unknown": 1,
                                             "completed": 0, "unlinked": 0,
                                             "managed": 0})

    def test_failed_run_maps_to_failed(self):
        write_run(self.root, "failed-run",
                  terminal_run(time.time() - 300, "failed")[:-1]
                  + [{"event": "finish", "status": "failed", "verified": False,
                      "time": time.time() - 290}])
        store = Store(self.root)
        office = build_office(project(agent()), store.state(),
                              bindings=[binding(run_id="failed-run")],
                              detail_provider=store.detail)
        entry = office["agents"][0]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["status_label"], "Failed")
        self.assertEqual(entry["evidence"]["source"], "native-hook")
        self.assertTrue(entry["evidence"]["native_stop_observed"])

    def test_verified_completion_requires_gate_evidence(self):
        policy_hash = write_policy(self.root)
        started = time.time() - 400
        write_run(self.root, "verified-run", [
            {"event": "start", "platform": "opencode", "time": started},
            {"event": "agent-start", "agent_id": "a1b2c3d4", "native_hash": HASH_A,
             "time": started + 1},
            {"event": "agent-stop", "agent_id": "a1b2c3d4", "time": started + 10},
            {"event": "check", "check_id": "unit", "exit_code": 0,
             "source": "native-tool", "policy_hash": policy_hash, "time": started + 12},
            {"event": "finish", "status": "success", "verified": False,
             "time": started + 14},
            {"event": "verified", "source": "quality-gate", "check_ids": ["unit"],
             "time": started + 15},
        ])
        store = Store(self.root)
        office = build_office(project(agent()), store.state(),
                              bindings=[binding(run_id="verified-run")],
                              detail_provider=store.detail)
        entry = office["agents"][0]
        self.assertEqual(entry["status"], "completed")
        self.assertIn("quality-gate", entry["evidence"]["detail"])
        self.assertTrue(entry["evidence"]["native_stop_observed"])
        inspector = build_inspector(project(agent()), "a_000000000001", store.state(),
                                    bindings=[binding(run_id="verified-run")],
                                    detail_provider=store.detail)
        self.assertTrue(inspector["activity"]["run"]["verified"])
        self.assertEqual(inspector["evidence"]["verification"]["status"], "quality-gate")

    def test_unverified_completion_is_labelled_unverified(self):
        write_run(self.root, "plain-run", terminal_run(time.time() - 300, "success"))
        store = Store(self.root)
        office = build_office(project(agent()), store.state(),
                              bindings=[binding(run_id="plain-run")],
                              detail_provider=store.detail)
        entry = office["agents"][0]
        self.assertEqual(entry["status"], "completed")
        self.assertIn("verification not attested", entry["evidence"]["detail"])
        self.assertFalse(entry["current"]["native_hash"] is None)

    def test_live_open_span_is_working_with_observed_confidence(self):
        started = time.time() - 10
        write_run(self.root, "live-run", [
            {"event": "start", "platform": "opencode", "time": started},
            {"event": "agent-start", "agent_id": "a1b2c3d4", "native_hash": HASH_A,
             "time": started + 1},
        ])
        store = Store(self.root)
        office = build_office(project(agent()), store.state(),
                              bindings=[binding(run_id="live-run")],
                              detail_provider=store.detail)
        entry = office["agents"][0]
        self.assertEqual(entry["status"], "working")
        self.assertEqual(entry["status_label"], "Working")
        self.assertEqual(entry["evidence"]["confidence"], "observed")
        self.assertEqual(entry["evidence"]["detail"], WORKING_DETAIL)
        self.assertTrue(entry["evidence"]["open_span"])
        self.assertFalse(entry["evidence"]["native_stop_observed"])

    def test_stale_open_span_is_unknown(self):
        started = time.time() - 1000
        write_run(self.root, "stale-run", [
            {"event": "start", "platform": "opencode", "time": started},
            {"event": "agent-start", "agent_id": "a1b2c3d4", "native_hash": HASH_A,
             "time": started + 1},
        ])
        store = Store(self.root)
        office = build_office(project(agent()), store.state(),
                              bindings=[binding(run_id="stale-run")],
                              detail_provider=store.detail)
        entry = office["agents"][0]
        self.assertEqual(entry["status"], "unknown")
        self.assertEqual(entry["evidence"]["detail"], STALE_DETAIL)
        self.assertTrue(entry["evidence"]["open_span"])

    def test_running_task_without_native_start_is_dispatching_inferred(self):
        supervisor = Supervisor(self.root)
        try:
            supervisor.init("supervisor-run", {
                "max_parallel": 1,
                "tasks": [{"id": "work", "role": "implementer",
                           "objective": "Implement the change", "read_scope": [],
                           "write_scope": []}],
            })
            supervisor.claim("supervisor-run")
        finally:
            supervisor.close()
        store = Store(self.root)
        office = build_office(project(agent()), store.state(),
                              bindings=[binding(run_id="supervisor-run",
                                                native_hash=HASH_B, task_id="work")],
                              detail_provider=store.detail)
        entry = office["agents"][0]
        self.assertEqual(entry["status"], "dispatching")
        self.assertEqual(entry["status_label"], "Dispatching")
        self.assertEqual(entry["evidence"]["source"], "supervisor-sqlite")
        self.assertEqual(entry["evidence"]["confidence"], "inferred")
        self.assertEqual(entry["evidence"]["detail"], DISPATCHING_DETAIL)

    def test_completed_supervisor_task_uses_native_stop_evidence(self):
        supervisor = Supervisor(self.root)
        try:
            supervisor.init("done-run", {
                "max_parallel": 1,
                "native_supervision": True,
                "tasks": [{"id": "work", "role": "implementer",
                           "objective": "Implement the change", "read_scope": [],
                           "write_scope": []}],
            })
            lease = supervisor.claim("done-run")["claimed"][0]["lease"]
            supervisor.bind_native("done-run", "work", lease, "opencode",
                                   native_hash=HASH_C)
            supervisor.observe_hash("opencode", HASH_C, "start")
            supervisor.observe_hash("opencode", HASH_C, "stop")
            supervisor.change("done-run", "work", lease, "completed", result={
                "summary": "Done", "changed_files": [],
                "tests": [{"name": "unit", "status": "passed"}], "findings": [],
            })
        finally:
            supervisor.close()
        store = Store(self.root)
        office = build_office(project(agent()), store.state(),
                              bindings=[binding(run_id="done-run", native_hash=HASH_C,
                                                task_id="work")],
                              detail_provider=store.detail)
        entry = office["agents"][0]
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["evidence"]["source"], "supervisor-sqlite")
        self.assertTrue(entry["evidence"]["native_stop_observed"])
        self.assertIn("Task completed", entry["evidence"]["detail"])

    def test_roles_and_timing_never_create_reviewing_or_running_tests(self):
        started = time.time() - 20
        write_run(self.root, "role-run", [
            {"event": "start", "platform": "opencode", "time": started},
            {"event": "agent-start", "agent_id": "a1b2c3d4", "native_hash": HASH_A,
             "time": started + 1},
            {"event": "check", "check_id": "tests", "exit_code": 0,
             "source": "native-tool", "time": started + 2},
        ])
        store = Store(self.root)
        office = build_office(
            project(agent(role="Reviewer"), agent("a_000000000002", "Test Bot", "tester")),
            store.state(),
            bindings=[binding(run_id="role-run")], detail_provider=store.detail)
        statuses = {entry["status"] for entry in office["agents"]}
        self.assertNotIn("reviewing", statuses)
        self.assertNotIn("running-tests", statuses)
        self.assertEqual([entry["status"] for entry in office["agents"]],
                         ["working", "unknown"])


class InspectorTests(OfficeTestCase):
    def test_inspector_has_no_fake_terminal_model_cost_or_diff(self):
        write_run(self.root, "run-1", terminal_run(time.time() - 300, "success"))
        store = Store(self.root)
        state = store.state()
        inspector = build_inspector(project(agent()), "a_000000000001", state,
                                    bindings=[binding(run_id="run-1")],
                                    detail_provider=store.detail)
        self.assertFalse(inspector["terminal"]["supported"])
        self.assertEqual(
            inspector["terminal"]["reason"],
            "No authenticated live terminal stream is attached. "
            "Observed lifecycle events only.")
        self.assertIsNone(inspector["identity"]["verified_model"])
        self.assertEqual(inspector["identity"]["model_note"],
                         "Model is not attested by run telemetry")
        self.assertFalse(inspector["files"]["diff_available"])
        self.assertIsNone(inspector["files"]["commit"])
        self.assertIsNone(inspector["files"]["merge_ready"])
        self.assertEqual(inspector["files"]["changed_files"], [])
        encoded = json.dumps(inspector).lower()
        self.assertNotIn("cost", encoded)
        self.assertNotIn("terminal_stream\"", encoded)
        self.assertFalse(inspector["evidence"]["native_stop_observed"] is None)

    def test_inspector_without_binding_is_explicitly_empty(self):
        inspector = build_inspector(project(agent()), "a_000000000001",
                                    Store(self.root).state())
        self.assertIsNone(inspector["binding"])
        self.assertIsNone(inspector["task"])
        self.assertEqual(inspector["activity"]["events"], [])
        self.assertEqual(inspector["activity"]["checks"], [])
        self.assertEqual(inspector["files"]["changed_files"], [])
        self.assertEqual(inspector["evidence"]["diagnostics"], [])
        with self.assertRaises(ValueError):
            build_inspector(project(agent()), "a_" + "f" * 12,
                            Store(self.root).state())

    def test_inspector_normalises_events_and_drops_unknown_fields(self):
        started = time.time() - 300
        write_run(self.root, "secret-run", [
            {"event": "start", "platform": "opencode", "time": started},
            {"event": "agent-start", "agent_id": "a1b2c3d4", "native_hash": HASH_A,
             "time": started + 1, "prompt": "SECRET PROMPT", "code": "SECRET CODE",
             "cwd": "/private/secret"},
            {"event": "agent-stop", "agent_id": "a1b2c3d4", "time": started + 4,
             "args": {"command": "SECRET COMMAND"}},
            {"event": "finish", "status": "success", "verified": False,
             "time": started + 5},
        ])
        store = Store(self.root)
        inspector = build_inspector(project(agent()), "a_000000000001",
                                    store.state(),
                                    bindings=[binding(run_id="secret-run")],
                                    detail_provider=store.detail)
        encoded = json.dumps(inspector)
        self.assertNotIn("SECRET", encoded)
        self.assertTrue(inspector["activity"]["events"])
        expected = {"schema_version", "project_id", "run_id", "task_id",
                    "attempt_id", "agent_id", "session_id", "platform",
                    "event_type", "timestamp", "sequence_id", "evidence_source",
                    "confidence", "payload"}
        for event in inspector["activity"]["events"]:
            self.assertEqual(set(event), expected)
            if event["event_type"] in ("agent-start", "agent-stop"):
                self.assertEqual(event["payload"].get("agent_id"), "a1b2c3d4")
            self.assertNotIn("prompt", event["payload"])
            self.assertNotIn("code", event["payload"])
        self.assertEqual(inspector["activity"]["run"]["id"], "secret-run")

    def test_unlinked_sessions_exclude_bound_spans_only(self):
        started = time.time() - 60
        write_run(self.root, "multi-run", [
            {"event": "start", "platform": "opencode", "time": started},
            {"event": "agent-start", "agent_id": "a1b2c3d4", "native_hash": HASH_A,
             "time": started + 1},
            {"event": "agent-start", "agent_id": "b2c3d4e5", "native_hash": HASH_B,
             "time": started + 2},
            {"event": "agent-stop", "agent_id": "a1b2c3d4", "time": started + 5},
            {"event": "agent-stop", "agent_id": "b2c3d4e5", "time": started + 6},
        ])
        store = Store(self.root)
        office = build_office(project(agent()), store.state(),
                              bindings=[binding(run_id="multi-run", native_hash=HASH_A)],
                              detail_provider=store.detail)
        self.assertEqual(len(office["unlinked_sessions"]), 1)
        unlinked = office["unlinked_sessions"][0]
        self.assertEqual(unlinked["agent_id"], "b2c3d4e5")
        self.assertEqual(unlinked["native_hash"], HASH_B)
        self.assertEqual(unlinked["status"], "unknown")
        self.assertFalse(unlinked["open"])
        self.assertEqual(office["summary"]["unlinked"], 1)


    def test_orphan_binding_does_not_hide_unlinked_evidence(self):
        started = time.time() - 60
        write_run(self.root, "orphan-run", [
            {"event": "start", "platform": "opencode", "time": started},
            {"event": "agent-start", "agent_id": "a1b2c3d4", "native_hash": HASH_A,
             "time": started + 1},
            {"event": "agent-stop", "agent_id": "a1b2c3d4", "time": started + 2},
        ])
        store = Store(self.root)
        orphan = {**binding(run_id="orphan-run", native_hash=HASH_A),
                  "agent_definition_id": "a_ffffffffffff"}
        office = build_office(project(agent()), store.state(), bindings=[orphan],
                              detail_provider=store.detail)
        self.assertEqual(len(office["unlinked_sessions"]), 1)
        self.assertEqual(office["agents"][0]["status"], "unknown")


class OfficeRoomTests(OfficeTestCase):
    def _state(self, tasks):
        supervisor = Supervisor(self.root)
        try:
            supervisor.init("rooms-run", {"max_parallel": 3, "tasks": tasks})
        finally:
            supervisor.close()
        return Store(self.root).state()

    @staticmethod
    def _task(task_id, role, **extra):
        return {"id": task_id, "role": role, "objective": f"Work {task_id}",
                "read_scope": [], "write_scope": [], **extra}

    @staticmethod
    def _binding(agent_id, task_id):
        digest = hashlib.sha256(agent_id.encode()).hexdigest()
        return binding(agent_id=agent_id, run_id="rooms-run",
                       native_hash=digest, task_id=task_id)

    def test_room_assignment_matrix(self):
        tasks = [
            self._task("explicit", "implementer", department="backend"),
            self._task("assigned", "implementer", assigned_room="meeting"),
            self._task("agent-default", "implementer"),
            self._task("agent-assigned", "implementer"),
            self._task("agent-over-role", "tester"),
            self._task("tester", "tester"),
            self._task("reviewer", "reviewer"),
            self._task("explorer", "explorer"),
            self._task("implementer", "implementer"),
            self._task("unknown-dept", "implementer", department="billing"),
        ]
        agents = [
            agent("a_000000000001", role="Frontend", default_room="frontend"),
            agent("a_000000000002", role="Frontend", default_room="frontend"),
            agent("a_000000000003", default_room="frontend"),
            agent("a_000000000004", assigned_room="meeting"),
            agent("a_000000000005", default_room="frontend"),
            agent("a_000000000006"),
            agent("a_000000000007"),
            agent("a_000000000008"),
            agent("a_000000000009"),
            agent("a_000000000010"),
        ]
        bindings = [
            self._binding(f"a_{index:012d}", task["id"])
            for index, task in enumerate(tasks, start=1)
        ]
        office = build_office(project(*agents), self._state(tasks),
                              bindings=bindings)
        by_id = {entry["agent_definition_id"]: entry for entry in office["agents"]}
        expected = {
            "a_000000000001": ("backend", "task.department", "backend"),
            "a_000000000002": ("meeting", "task.department", "meeting"),
            "a_000000000003": ("frontend", "agent.default_room", None),
            "a_000000000004": ("meeting", "agent.default_room", None),
            "a_000000000005": ("frontend", "agent.default_room", None),
            "a_000000000006": ("qa", "task.role", None),
            "a_000000000007": ("review", "task.role", None),
            "a_000000000008": ("research", "task.role", None),
            "a_000000000009": ("lobby", "unassigned", None),
            "a_000000000010": ("lobby", "unassigned", "billing"),
        }
        task_roles = {
            "a_000000000001": "implementer",
            "a_000000000006": "tester",
            "a_000000000007": "reviewer",
            "a_000000000008": "explorer",
        }
        for agent_id, (room, source, department) in expected.items():
            entry = by_id[agent_id]
            self.assertEqual(entry["room"], room, agent_id)
            self.assertEqual(entry["room_source"], source, agent_id)
            self.assertEqual(entry["current"]["department"], department, agent_id)
            if agent_id in task_roles:
                self.assertEqual(entry["current"]["task_role"], task_roles[agent_id])
                self.assertEqual(entry["current"]["task_state"], "pending")
            self.assertTrue(entry["spawnable"], agent_id)
            self.assertTrue(entry["session_key"].startswith(agent_id + ":rooms-run:"))

    def test_implementer_without_explicit_department_stays_in_lobby(self):
        tasks = [self._task("work", "implementer")]
        office = build_office(project(agent()), self._state(tasks),
                              bindings=[self._binding("a_000000000001", "work")])
        entry = office["agents"][0]
        self.assertEqual((entry["room"], entry["room_source"]),
                         ("lobby", "unassigned"))
        self.assertEqual(entry["current"]["task_role"], "implementer")
        self.assertEqual(entry["current"]["task_state"], "pending")

    def test_unbound_registry_agent_has_no_spawnable_session(self):
        office = build_office(project(agent(), agent(
            "a_000000000002", name="Linus", default_room="backend")),
            Store(self.root).state())
        first, second = office["agents"]
        self.assertEqual((first["room"], first["room_source"]), ("lobby", "unassigned"))
        self.assertIsNone(first["session_key"])
        self.assertFalse(first["spawnable"])
        self.assertEqual((second["room"], second["room_source"]),
                         ("backend", "agent.default_room"))
        self.assertIsNone(second["session_key"])
        self.assertFalse(second["spawnable"])

    def test_bound_session_key_is_stable_and_prefixed(self):
        office = build_office(project(agent()), Store(self.root).state(),
                              bindings=[binding(run_id="run-1", native_hash=HASH_A)])
        entry = office["agents"][0]
        self.assertEqual(entry["session_key"],
                         f"a_000000000001:run-1:{HASH_A[:12]}")
        self.assertTrue(entry["spawnable"])

    def test_unlinked_duplicate_events_share_one_session_key(self):
        spans = [
            {"id": "a1b2c3d4", "native_hash": HASH_A, "open": False,
             "duration": 4.0},
            {"id": "a1b2c3d4", "native_hash": HASH_A, "open": False,
             "duration": 4.0},
        ]
        state = {"runs": [{"id": "dup-run", "agent_count": 2, "live": False,
                           "status": "success", "last_event_at": 10}]}

        def detail_provider(run_id):
            return {"run": state["runs"][0], "agents": spans}

        office = build_office(project(agent()), state, bindings=[],
                              detail_provider=detail_provider)
        self.assertEqual(len(office["unlinked_sessions"]), 1)
        unlinked = office["unlinked_sessions"][0]
        self.assertEqual(unlinked["session_key"], "s:dup-run:a1b2c3d4")
        self.assertEqual((unlinked["room"], unlinked["room_source"]),
                         ("lobby", "unlinked"))
        self.assertTrue(unlinked["spawnable"])

    def test_inspector_exposes_additive_room_fields(self):
        tasks = [self._task("work", "tester", department="qa")]
        state = self._state(tasks)
        inspector = build_inspector(project(agent()), "a_000000000001", state,
                                    bindings=[self._binding("a_000000000001", "work")])
        self.assertEqual(inspector["agent"]["room"], "qa")
        self.assertEqual(inspector["agent"]["room_source"], "task.department")
        self.assertTrue(inspector["agent"]["spawnable"])
        self.assertEqual(inspector["agent"]["current"]["task_role"], "tester")
        self.assertEqual(inspector["agent"]["current"]["department"], "qa")


class OfficeTeamworkRoleTests(OfficeTestCase):
    def test_v73_verification_roles_map_to_review_or_qa(self):
        roles = ("critic", "challenger", "auditor", "success-auditor")
        for role in roles:
            with self.subTest(role=role):
                self.assertIn(ROLE_ROOMS[role], ("review", "qa"))

        tasks = [{
            "id": f"task-{index}", "role": role,
            "objective": f"Verify {role}", "read_scope": [], "write_scope": [],
        } for index, role in enumerate(roles)]
        supervisor = Supervisor(self.root)
        try:
            supervisor.init("teamwork-roles", {"max_parallel": 3, "tasks": tasks})
        finally:
            supervisor.close()
        agents = [agent(f"a_{index:012d}", name=role.title())
                  for index, role in enumerate(roles, start=1)]
        bindings = [binding(agent_id=f"a_{index:012d}", run_id="teamwork-roles",
                            native_hash=hashlib.sha256(role.encode()).hexdigest(),
                            task_id=f"task-{index - 1}")
                    for index, role in enumerate(roles, start=1)]
        office = build_office(project(*agents), Store(self.root).state(),
                              bindings=bindings)
        by_id = {entry["agent_definition_id"]: entry for entry in office["agents"]}
        for index, role in enumerate(roles, start=1):
            entry = by_id[f"a_{index:012d}"]
            self.assertEqual(entry["current"]["task_role"], role)
            self.assertEqual(entry["room_source"], "task.role")
            self.assertEqual(entry["room"], ROLE_ROOMS[role])


class EventModelTests(unittest.TestCase):
    def test_unknown_fields_are_stripped_from_safe_schema(self):
        event = {
            "event": "agent-start", "time": 12.5, "agent_id": "a1b2c3d4",
            "native_hash": HASH_A, "prompt": "SECRET", "code": "SECRET",
            "cwd": "/private", "args": {"command": "SECRET"},
        }
        normalized = normalize_run_event(PROJECT_ID, "run-1", event, 1)
        self.assertEqual(set(normalized), {
            "schema_version", "project_id", "run_id", "task_id", "attempt_id",
            "agent_id", "session_id", "platform", "event_type", "timestamp",
            "sequence_id", "evidence_source", "confidence", "payload",
        })
        self.assertEqual(normalized["payload"],
                         {"agent_id": "a1b2c3d4", "native_hash_short": HASH_A[:12]})
        self.assertNotIn("SECRET", json.dumps(normalized))
        self.assertEqual(normalized["platform"], "unknown")
        self.assertEqual(normalized["evidence_source"], "native-hook")
        self.assertEqual(normalized["confidence"], "observed")

    def test_unknown_event_kind_is_explicit_and_empty(self):
        normalized = normalize_run_event(
            PROJECT_ID, "run-1",
            {"event": "tool-call", "time": 1, "prompt": "SECRET"}, 3)
        self.assertEqual(normalized["event_type"], "unknown")
        self.assertEqual(normalized["payload"], {})
        self.assertEqual(normalized["evidence_source"], "unknown")
        self.assertEqual(normalized["confidence"], "unknown")
        self.assertEqual(normalized["sequence_id"], 3)

    def test_event_ordering_is_preserved(self):
        events = [
            {"event": "start", "platform": "opencode", "time": 1.0},
            {"event": "mutation", "time": 2.0},
            {"event": "finish", "status": "success", "time": 3.0},
        ]
        normalized = normalize_events(PROJECT_ID, "run-1", events)
        self.assertEqual([event["event_type"] for event in normalized],
                         ["start", "mutation", "finish"])
        self.assertEqual([event["sequence_id"] for event in normalized], [1, 2, 3])
        self.assertEqual([event["timestamp"] for event in normalized], [1.0, 2.0, 3.0])

    def test_duplicate_or_out_of_order_sequence_is_rejected(self):
        sequencer = RunSequencer()
        self.assertEqual(sequencer.next(PROJECT_ID, "run-1"), 1)
        self.assertEqual(sequencer.next(PROJECT_ID, "run-1"), 2)
        with self.assertRaises(SequenceConflict):
            sequencer.observe(PROJECT_ID, "run-1", 2)
        with self.assertRaises(SequenceConflict):
            sequencer.observe(PROJECT_ID, "run-1", 1)
        self.assertEqual(sequencer.observe(PROJECT_ID, "run-1", 3), 3)
        self.assertEqual(sequencer.next(PROJECT_ID, "run-2"), 1)

    def test_missing_identity_and_invalid_sequence_are_rejected(self):
        for project_id, run_id, sequence in (
                ("not-a-project", "run-1", 1),
                (PROJECT_ID, "../run", 1),
                (PROJECT_ID, "run-1", 0),
                (PROJECT_ID, "run-1", True)):
            with self.assertRaises(EventModelError):
                normalize_run_event(project_id, run_id,
                                    {"event": "start", "time": 1}, sequence)
        with self.assertRaises(EventModelError):
            normalize_run_event(PROJECT_ID, "run-1", {"event": "start"}, 1)
        with self.assertRaises(EventModelError):
            normalize_run_event(PROJECT_ID, "run-1", "not-an-object", 1)

    def test_evidence_source_is_not_overstated(self):
        manual = normalize_run_event(PROJECT_ID, "run-1", {
            "event": "verified", "source": "manual", "time": 1,
        }, 1)
        self.assertEqual(manual["evidence_source"], "run-log")
        check = normalize_run_event(PROJECT_ID, "run-1", {
            "event": "check", "check_id": "unit", "exit_code": 0, "time": 1,
        }, 2)
        self.assertEqual(check["evidence_source"], "run-log")
        native = normalize_run_event(PROJECT_ID, "run-1", {
            "event": "check", "check_id": "unit", "exit_code": 0,
            "source": "native-tool", "time": 1,
        }, 3)
        self.assertEqual(native["evidence_source"], "native-tool")

    def test_check_payload_whitelists_safe_values(self):
        normalized = normalize_run_event(PROJECT_ID, "run-1", {
            "event": "check", "time": 1, "check_id": "unit", "exit_code": 0,
            "source": "native-tool", "command": "SECRET", "policy_hash": HASH_A,
        }, 1)
        self.assertEqual(normalized["payload"],
                         {"check_id": "unit", "exit_code": 0, "passed": True,
                          "source": "native-tool"})
        self.assertNotIn("SECRET", json.dumps(normalized))


if __name__ == "__main__":
    unittest.main()
