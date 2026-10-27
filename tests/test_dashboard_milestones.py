from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tools.dashboard import Catalog, DashboardServer, Store
from tools.project_memory import ProjectMemory
from tools.run_owner import write_recovery_state
from tools.teamwork import TeamworkEngine


def plan(project_id: str) -> dict:
    return {
        "run_id": "team-milestone",
        "project_id": project_id,
        "max_parallel": 1,
        "pattern": "review_and_repair",
        "milestones": [{
            "id": "M1", "objective": "Verify a small change", "depends_on": [],
            "acceptance": ["evidence recorded"],
            "verification_policy": {"require_roles": [], "max_repair": 1},
        }],
        "tasks": [{
            "id": "discover", "role": "explorer", "milestone": "M1",
            "phase": "build", "objective": "Discover",
            "acceptance": ["report recorded"],
            "read_scope": [], "write_scope": [], "depends_on": [],
            "timeout_seconds": 60, "max_retries": 1,
            "verify_commands": [], "department": "research",
            "provider": "opencode", "model": "demo-model",
        }],
    }


class MilestoneDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.store = Store(self.root)
        self.catalog = Catalog.from_store(self.store)
        self.project_id = self.catalog.default_id

    def _seed(self) -> None:
        value = plan(self.project_id)
        path = self.root / ".auto-orchestrator" / "plans" / "team-milestone.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({**value, "version": 1}), encoding="utf-8")
        engine = TeamworkEngine(self.root)
        try:
            engine.init(value)
            claimed = engine.supervisor.claim("team-milestone")["claimed"][0]
            engine.supervisor.change(
                "team-milestone", "discover", claimed["lease"], "completed",
                result={"summary": "Synthetic discovery", "changed_files": [],
                        "tests": [], "findings": []},
            )
            engine.record_managed_session(
                "team-milestone", "discover", attempt=1, provider="opencode",
                session_id="ses_secret_should_not_leak", status="completed",
                model="demo-model", cost=0.01, tokens={"input": 4, "output": 2},
                events_path="/private/tmp/events.jsonl",
            )
            engine.status("team-milestone")
        finally:
            engine.close()

    def test_memory_endpoint_is_read_only_until_memory_exists(self):
        missing = self.catalog.memory(self.project_id)
        self.assertFalse(missing["available"])
        self.assertFalse((self.root / ".auto-orchestrator" / "project.json").exists())
        with ProjectMemory(self.root) as memory:
            memory.add_decision(topic="auth_session", decision="JWT",
                                status="confirmed", confirmed_by="user",
                                source="user")
            memory.write_context_files()
        payload = self.catalog.memory(self.project_id)
        self.assertTrue(payload["available"])
        self.assertTrue(any(item["decision"] == "JWT"
                            for item in payload["decisions"]))

    def test_memory_endpoint_exposes_continuity_and_contract_fields(self):
        with ProjectMemory(self.root) as memory:
            memory.add_decision(topic="auth", decision="JWT",
                                status="confirmed", confirmed_by="user",
                                source="user")
            memory.add_decision(topic="db", decision="SQLite",
                                status="code_derived", source="code")
            memory.add_requirement("Use secure cookies")
            resolved = memory.add_requirement("Rotate secrets")
            memory.resolve_requirement(resolved["requirement_id"],
                                       status="confirmed")
            interview_id = "iv_" + "a" * 16
            question_id = "q_" + "b" * 16
            now = time.time()
            memory.conn.execute(
                "INSERT INTO interviews(interview_id,project_id,"
                "origin_platform,request_hash,request_summary,mode,status,"
                "created_at,updated_at,schema_version) VALUES (?,?,?,?,?,?,?,?,?,1)",
                (interview_id, memory.project_id, "codex",
                 hashlib.sha256(b"login").hexdigest(), "Login feature",
                 "auto", "open", now, now))
            memory.conn.execute(
                "INSERT INTO interview_questions(interview_id,question_id,"
                "ordinal,question_key,question,options,status,depends_on,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (interview_id, question_id, 0, "auth_session",
                 "How should sessions work?",
                 json.dumps([{"id": "jwt", "label": "JWT"}]),
                 "open", "[]", now))
            memory.conn.commit()
            memory.record_handoff(
                run_id="run-1", milestone_id="M2", platform="codex",
                payload={"milestone_id": "M2", "context_summary": "M2 passed",
                         "verified_tests": ["test_login"]})
            memory.record_observation(kind="verification", files=["a.py"],
                                      test_evidence="11 tests passed")
            memory.update_meta(last_platform="codex", last_milestone="M2")
            contracts = memory.directory / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "current.json").write_text(json.dumps({
                "contract_id": "rc_" + "c" * 16,
                "contract_version": 3,
                "sha256": "d" * 64,
                "path": "rc_" + "c" * 16 + "-v3.json",
                "updated_at": now,
            }), encoding="utf-8")
            evidence = (self.root / ".auto-orchestrator" / "evidence" / "run-1")
            evidence.mkdir(parents=True)
            (evidence / "teamwork-summary-0.json").write_text(json.dumps({
                "requirements_contract": {
                    "contract_id": "rc_" + "c" * 16,
                    "contract_version": 3,
                    "status": "partially_verified",
                    "ok": True,
                    "fully_verified": False,
                    "passed": 1,
                    "not_verified": 2,
                    "checks": [
                        {"criterion_id": "ac_one", "kind": "code_evidence",
                         "status": "passed", "evidence": "code evidence: auth=true"},
                        {"criterion_id": "ac_two", "kind": "not_automated",
                         "status": "not_verified", "evidence": "manual"},
                    ],
                    "reasons": [],
                },
            }), encoding="utf-8")
            memory.write_context_files()
        payload = self.catalog.memory(self.project_id)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["last_ide"], "codex")
        self.assertEqual(payload["last_milestone"], "M2")
        self.assertEqual(payload["active_interview"]["interview_id"],
                         interview_id)
        self.assertEqual(payload["active_interview"]["question"]["question"],
                         "How should sessions work?")
        self.assertIsNotNone(payload["active_interview"]["age_seconds"])
        self.assertGreaterEqual(payload["active_interview"]["age_seconds"], 0)
        self.assertEqual(payload["question"]["question_id"], question_id)
        self.assertEqual(len(payload["confirmed_decisions"]), 1)
        self.assertEqual(len(payload["derived_decisions"]), 1)
        self.assertEqual([item["text"] for item in payload["blockers"]],
                         ["Use secure cookies"])
        self.assertEqual(payload["approved_requirements"]["contract_version"], 3)
        self.assertEqual(payload["approved_requirements"]["contract_id"],
                         "rc_" + "c" * 16)
        self.assertEqual(payload["contract_status"], "partially_verified")
        self.assertFalse(payload["contract_fully_verified"])
        self.assertEqual(payload["contract_not_verified"], 2)
        self.assertEqual(payload["contract_verification"]["status"],
                         "partially_verified")
        self.assertEqual(len(payload["handoffs"]), 1)
        self.assertTrue(payload["handoff"]["verified"])
        self.assertTrue(payload["stale"]["is_stale"])
        self.assertIn("missing_head", payload["stale"]["reasons"])
        self.assertEqual(payload["verification"]["test_evidence"],
                         "11 tests passed")

    def test_memory_endpoint_does_not_label_unverified_handoff_verified(self):
        with ProjectMemory(self.root) as memory:
            memory.record_handoff(
                run_id="run-1", milestone_id="M1", platform="codex",
                payload={"milestone_id": "M1", "context_summary": "M1 passed"})
            memory.update_meta(last_platform="opencode")
        payload = self.catalog.memory(self.project_id)
        self.assertFalse(payload["handoff"]["verified"])

    def test_recovery_labels_do_not_mislabel_normal_terminal_runs(self):
        self._seed()
        run = {"run_id": "team-milestone", "status": "passed",
               "managed_sessions": []}
        normal = self.catalog._attach_recovery(self.root, dict(run))
        self.assertIsNone(normal["recovery_state"])
        self.assertFalse(normal["owner_connected"])

        write_recovery_state(self.root, "team-milestone", "recovered")
        recovered = self.catalog._attach_recovery(self.root, dict(run))
        self.assertEqual(recovered["recovery_state"], "recovered")

        verifying = {"run_id": "team-milestone", "status": "failed",
                     "managed_sessions": []}
        write_recovery_state(self.root, "team-milestone", "verifying")
        failed = self.catalog._attach_recovery(self.root, verifying)
        self.assertEqual(failed["recovery_state"], "recovery_failed")

        cancelled = {"run_id": "team-milestone", "status": "cancelled",
                     "managed_sessions": []}
        stopped = self.catalog._attach_recovery(self.root, cancelled)
        self.assertEqual(stopped["recovery_state"], "cancelled")

    def test_catalog_exposes_verified_milestones_and_sanitizes_sessions(self):
        self._seed()
        payload = self.catalog.milestones(self.project_id)
        self.assertEqual(payload["current_run_id"], "team-milestone")
        self.assertEqual(payload["progress"]["passed"], 1)
        self.assertEqual(payload["progress"]["total"], 1)
        self.assertEqual(payload["verified_progress"], 1.0)
        self.assertIsNone(payload["estimated_progress"])
        run = payload["runs"][0]
        self.assertEqual(run["milestones"][0]["status"], "passed")
        session = run["managed_sessions"][0]
        self.assertNotIn("ses_secret_should_not_leak", json.dumps(session))
        self.assertEqual(session["session_hash"], __import__("hashlib").sha256(
            b"ses_secret_should_not_leak").hexdigest()[:12])
        self.assertNotIn("events_path", session)

    def test_milestone_execution_identity_mode_usage_and_contract_status(self):
        value = plan(self.project_id)
        value.update({
            "origin_platform": "codex",
            "execution_provider": "codex",
            "work_mode": "auto",
        })
        path = self.root / ".auto-orchestrator" / "plans" / "team-milestone.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({**value, "version": 1}), encoding="utf-8")
        engine = TeamworkEngine(self.root)
        try:
            engine.init(value)
            engine.record_managed_session(
                "team-milestone", "discover", attempt=1, provider="codex",
                session_id="ses_provider", status="completed", model="demo-model",
                cost=0.25, tokens={"input": 10, "output": 4},
            )
            engine.status("team-milestone")
        finally:
            engine.close()
        evidence = self.root / ".auto-orchestrator" / "evidence" / "team-milestone"
        evidence.mkdir(parents=True)
        (evidence / "teamwork-summary-0.json").write_text(json.dumps({
            "requirements_contract": {
                "contract_id": "rc_" + "e" * 16,
                "contract_version": 2,
                "status": "partially_verified",
                "ok": True,
                "fully_verified": False,
                "passed": 2,
                "not_verified": 1,
                "checks": [],
                "reasons": [],
            },
        }), encoding="utf-8")
        run = self.catalog.milestones(self.project_id)["runs"][0]
        self.assertEqual(run["mode_label"], "auto")
        self.assertEqual(run["execution"]["provider"], "codex")
        self.assertEqual(run["execution"]["origin_platform"], "codex")
        self.assertEqual(run["execution"]["native_or_managed"], "managed")
        self.assertEqual(run["execution_provider"], "codex")
        self.assertEqual(run["origin_platform"], "codex")
        self.assertEqual(run["native_or_managed"], "managed")
        self.assertFalse(run["execution"]["native_session_claimed"])
        self.assertFalse(run["execution"]["cross_ide_spawn"])
        self.assertEqual(run["contract_status"], "partially_verified")
        self.assertFalse(run["contract_fully_verified"])
        self.assertEqual(run["contract_not_verified"], 1)
        self.assertTrue(run["usage"]["cost_measured"])
        self.assertEqual(run["usage"]["cost_total_usd"], 0.25)
        self.assertTrue(run["usage"]["tokens_measured"])
        self.assertEqual(run["usage"]["tokens"]["input"], 10)
        self.assertEqual(run["usage"]["by_provider"][0]["provider"], "codex")
        self.assertEqual(run["usage"]["by_provider"][0]["cost_total_usd"], 0.25)

    def test_milestone_usage_keeps_unknown_distinct_from_zero(self):
        value = plan(self.project_id)
        path = self.root / ".auto-orchestrator" / "plans" / "team-milestone.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({**value, "version": 1}), encoding="utf-8")
        engine = TeamworkEngine(self.root)
        try:
            engine.init(value)
            engine.status("team-milestone")
        finally:
            engine.close()
        run = self.catalog.milestones(self.project_id)["runs"][0]
        self.assertIsNone(run["usage"]["cost_total_usd"])
        self.assertFalse(run["usage"]["cost_measured"])
        self.assertTrue(run["usage"]["cost_unknown"])
        self.assertIsNone(run["usage"]["tokens"]["input"])
        self.assertFalse(run["usage"]["tokens_measured"])
        self.assertTrue(run["usage"]["tokens_unknown"])
        self.assertEqual(run["usage"]["by_provider"], [])
        self.assertEqual(run["contract_status"], "not_run")
        self.assertIsNone(run["contract_fully_verified"])

    def test_demo_milestones_label_unknown_usage_and_demo_execution(self):
        demo = Catalog.from_store(Store(self.root, demo=True))
        try:
            payload = demo.milestones(demo.default_id)
        finally:
            demo.close()
        run = payload["runs"][0]
        self.assertTrue(run["demo"])
        self.assertEqual(run["execution"]["native_or_managed"], "demo")
        self.assertTrue(run["usage"]["cost_unknown"])
        self.assertTrue(run["usage"]["tokens_unknown"])
        self.assertIsNone(run["usage"]["cost_total_usd"])
        self.assertEqual(run["mode_label"], "balanced")

    def test_http_route_returns_project_scoped_milestones(self):
        self._seed()
        server = DashboardServer(("127.0.0.1", 0), self.store, catalog=self.catalog)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._stop, server, thread)
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.request("GET", f"/api/projects/{self.project_id}/milestones")
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        self.assertEqual(response.status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["project_id"], self.project_id)
        self.assertEqual(payload["runs"][0]["run_id"], "team-milestone")
        self.assertNotIn("ses_secret_should_not_leak", body.decode())

    def test_unknown_milestone_run_is_404_and_demo_is_labelled(self):
        self._seed()
        server = DashboardServer(("127.0.0.1", 0), self.store, catalog=self.catalog)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._stop, server, thread)
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.request("GET", f"/api/projects/{self.project_id}/milestones/missing")
            response = connection.getresponse()
            response.read()
        finally:
            connection.close()
        self.assertEqual(response.status, 404)

    @staticmethod
    def _stop(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
