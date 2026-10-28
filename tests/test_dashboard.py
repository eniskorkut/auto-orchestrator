from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.dashboard import Catalog, DashboardServer, Store
from tools.supervisor import Supervisor


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


class DashboardDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_gate_verified_run_and_self_report_are_distinct(self):
        policy_hash = write_policy(self.root)
        started = time.time() - 500
        write_run(self.root, "verified", [
            {"event": "start", "platform": "opencode", "time": started},
            {"event": "route", "delegation": "consider-parallel", "max_agents": 3,
             "selected_count": 1, "latency_ms": 12, "time": started + 1},
            {"event": "agent-start", "agent_id": "a1b2c3d4", "time": started + 2},
            {"event": "agent-start", "agent_id": "b2c3d4e5", "time": started + 7},
            {"event": "agent-stop", "agent_id": "a1b2c3d4", "time": started + 30},
            {"event": "agent-stop", "agent_id": "b2c3d4e5", "time": started + 31},
            {"event": "mutation", "time": started + 25},
            {"event": "check", "check_id": "unit", "exit_code": 0,
             "source": "native-tool", "policy_hash": policy_hash, "time": started + 32},
            {"event": "finish", "status": "success", "verified": False, "time": started + 35},
            {"event": "verified", "source": "quality-gate", "check_ids": ["unit"], "time": started + 36},
        ])
        write_run(self.root, "claimed", [
            {"event": "start", "platform": "codex", "time": started},
            {"event": "agent-start", "agent_id": "c3d4e5f6", "time": started + 1},
            {"event": "agent-stop", "agent_id": "c3d4e5f6", "time": started + 5},
            {"event": "finish", "status": "success", "verified": True, "time": started + 6},
        ])
        state = Store(self.root).state()
        runs = {run["id"]: run for run in state["runs"]}
        self.assertTrue(runs["verified"]["verified"])
        self.assertFalse(runs["verified"]["self_reported"])
        self.assertEqual(runs["verified"]["verification"]["status"], "quality-gate")
        self.assertTrue(runs["verified"]["has_valid_estimate"])
        self.assertTrue(runs["claimed"]["self_reported"])
        self.assertFalse(runs["claimed"]["verified"])
        self.assertEqual(runs["claimed"]["verification"]["status"], "self-reported")
        self.assertEqual(state["overview"]["verified"], 1)
        self.assertEqual(state["overview"]["self_reported"], 1)
        self.assertEqual(runs["verified"]["routing_advice_followed"], True)

    def test_rejected_verification_does_not_count(self):
        write_policy(self.root)
        started = time.time() - 300
        write_run(self.root, "stale", [
            {"event": "start", "platform": "opencode", "time": started},
            {"event": "agent-start", "agent_id": "a1b2c3d4", "time": started + 1},
            {"event": "agent-stop", "agent_id": "a1b2c3d4", "time": started + 2},
            {"event": "finish", "status": "success", "verified": False, "time": started + 3},
            {"event": "verified", "source": "quality-gate", "check_ids": ["unit"],
             "time": started + 4},
        ])
        run = Store(self.root).state()["runs"][0]
        self.assertFalse(run["verified"])
        self.assertFalse(run["self_reported"])
        self.assertEqual(run["verification"]["status"], "rejected")

    def test_open_run_live_flag_decays_without_a_log_change(self):
        started = time.time() - 10
        write_run(self.root, "fresh-open", [
            {"event": "start", "platform": "opencode", "time": started},
            {"event": "mutation", "time": started + 1},
        ])
        store = Store(self.root)
        self.assertTrue(store.state()["runs"][0]["live"])
        with mock.patch("tools.dashboard.ACTIVE_WINDOW", 1):
            run = store.state()["runs"][0]
            self.assertFalse(run["live"])
            self.assertFalse(store.detail("fresh-open")["run"]["live"])

    def test_missing_or_nonfinite_event_times_do_not_crash(self):
        started = time.time() - 100
        write_run(self.root, "no-times", [
            {"event": "start", "platform": "opencode", "time": started},
            {"event": "mutation"},
            {"event": "finish", "status": "success", "verified": False},
            {"event": "verified", "source": "manual"},
        ])
        detail = Store(self.root).detail("no-times")
        self.assertIsNone(detail["finish"]["offset"])
        self.assertIsNone(detail["verified_event"]["offset"])
        self.assertEqual(detail["mutations"], [])
        path = self.root / ".auto-orchestrator" / "runs" / "nan.jsonl"
        path.write_text(
            '{"event":"start","platform":"opencode","time":NaN}\n',
            encoding="utf-8",
        )
        runs = {run["id"]: run for run in Store(self.root).state()["runs"]}
        self.assertEqual(runs["nan"]["status"], "invalid")

    def test_live_decay_changes_state_fingerprint(self):
        started = time.time() - 10
        write_run(self.root, "live-run", [
            {"event": "start", "platform": "opencode", "time": started},
            {"event": "agent-start", "agent_id": "a1b2c3d4", "time": started + 1},
        ])
        store = Store(self.root)
        first = store.state()
        self.assertTrue(first["runs"][0]["live"])
        with mock.patch("tools.dashboard.ACTIVE_WINDOW", 0):
            second = store.state()
        self.assertFalse(second["runs"][0]["live"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_policy_change_invalidates_cached_verification(self):
        policy_hash = write_policy(self.root)
        started = time.time() - 200
        write_run(self.root, "policy-run", [
            {"event": "start", "platform": "opencode", "time": started},
            {"event": "agent-start", "agent_id": "a1b2c3d4", "time": started + 1},
            {"event": "agent-stop", "agent_id": "a1b2c3d4", "time": started + 2},
            {"event": "check", "check_id": "unit", "exit_code": 0,
             "source": "native-tool", "policy_hash": policy_hash, "time": started + 3},
            {"event": "finish", "status": "success", "verified": False, "time": started + 4},
            {"event": "verified", "source": "quality-gate", "check_ids": ["unit"],
             "time": started + 5},
        ])
        store = Store(self.root)
        self.assertTrue(store.state()["runs"][0]["verified"])
        self.assertTrue(store.detail("policy-run")["run"]["verified"])

        policy_path = self.root / ".auto-orchestrator" / "verification.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["required_checks"][0]["command"] = "python -m unittest discover"
        policy_path.write_text(json.dumps(policy, separators=(",", ":")) + "\n", encoding="utf-8")

        run = store.state()["runs"][0]
        self.assertFalse(run["verified"])
        self.assertEqual(run["verification"]["status"], "rejected")
        self.assertFalse(store.detail("policy-run")["run"]["verified"])

    def test_torn_last_line_is_displayed_as_partial_open_run(self):
        started = time.time() - 10
        path = self.root / ".auto-orchestrator" / "runs" / "open.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"event": "start", "platform": "opencode", "time": started}) + "\n"
            + json.dumps({"event": "agent-start", "agent_id": "a1b2c3d4",
                          "time": started + 1}) + "\n"
            + '{"event":"mutation","time":',
            encoding="utf-8",
        )
        run = Store(self.root).state()["runs"][0]
        self.assertEqual(run["status"], "open")
        self.assertTrue(run["partial"])
        self.assertEqual(run["open_agent_count"], 1)
        detail = Store(self.root).detail("open")
        self.assertTrue(detail["partial"])
        self.assertIsNone(detail["finish"])

    def test_supervisor_snapshot_never_exposes_lease(self):
        supervisor = Supervisor(self.root)
        try:
            plan = {
                "max_parallel": 2,
                "native_supervision": True,
                "tasks": [
                    {"id": "schema", "role": "explorer", "objective": "Map schema",
                     "read_scope": ["db/"], "write_scope": []},
                    {"id": "api", "role": "implementer", "objective": "Implement API",
                     "depends_on": ["schema"], "read_scope": ["db/"],
                     "write_scope": ["src/"]},
                ],
            }
            supervisor.init("plan-1", plan)
            claim = supervisor.claim("plan-1")
            self.assertTrue(claim["claimed"])
            lease = claim["claimed"][0]["lease"]
            status = supervisor.status("plan-1")
        finally:
            supervisor.close()
        self.assertTrue(lease)
        state = Store(self.root).state()
        self.assertTrue(state["supervisor"]["available"])
        run = state["supervisor"]["runs"][0]
        self.assertEqual(run["id"], "plan-1")
        self.assertEqual(len(run["tasks"]), 2)
        tasks = {task["id"]: task for task in run["tasks"]}
        self.assertTrue(tasks["schema"]["dependencies_met"])
        self.assertFalse(tasks["api"]["dependencies_met"])
        self.assertNotIn(lease, json.dumps(state))
        self.assertNotIn("lease", json.dumps(state).lower())
        self.assertEqual(status["run_id"], "plan-1")

    def test_demo_mode_is_labelled_and_writes_nothing(self):
        before = sorted(p.name for p in self.root.iterdir())
        state = Store(self.root, demo=True).state()
        self.assertTrue(state["demo"])
        self.assertEqual(state["project"], "demo-project")
        self.assertGreaterEqual(state["overview"]["runs"], 4)
        self.assertEqual(before, sorted(p.name for p in self.root.iterdir()))

    def test_demo_ide_ownership_is_labelled_and_native_stays_unknown(self):
        catalog = Catalog.from_store(Store(self.root, demo=True))
        try:
            payload = catalog.ide(catalog.default_id)
            memory = catalog.memory(catalog.default_id)
            milestones = catalog.milestones(catalog.default_id)
        finally:
            catalog.close()
        for item in (payload, memory["ide"], milestones["ide"]):
            self.assertTrue(item["demo"])
            self.assertEqual(item["source"], "demo")
            self.assertEqual(item["ownership"]["state"], "demo")
            self.assertEqual(item["last_ide"], "demo")
            self.assertIsNone(item["active_ide"])
            self.assertIsNone(item["previous_ide"])
            self.assertIsNone(item["native"]["connected"])
            self.assertFalse(item["native"]["claimed_live"])
            self.assertEqual(item["next_action"]["code"], "demo")
            self.assertFalse(item["stale_context"]["is_stale"])
        self.assertEqual(payload["current"]["milestone_id"], "M2")
        self.assertEqual(milestones["current_run_id"], "demo-project")
        self.assertTrue(memory["demo"])
        self.assertIn("DEMO", payload["next_action"]["label"].upper())


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = DashboardServer(("127.0.0.1", 0), Store(self.root, demo=True))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, method: str, path: str, headers: dict | None = None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            body = response.read()
            return response, body
        finally:
            connection.close()

    def test_state_detail_and_security_headers(self):
        response, body = self.request("GET", "/api/state")
        self.assertEqual(response.status, 200)
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        state = json.loads(body)
        self.assertTrue(state["demo"])
        self.assertNotIn("lease", body.decode().lower())

        response, body = self.request("GET", "/api/run/demo-verified")
        self.assertEqual(response.status, 200)
        detail = json.loads(body)
        self.assertEqual(detail["run"]["id"], "demo-verified")
        self.assertTrue(detail["run"]["verified"])
        self.assertGreaterEqual(len(detail["agents"]), 2)

    def test_v71_office_assets_are_served(self):
        for path in ("/assets/office.js", "/assets/i18n.js"):
            response, body = self.request("GET", path)
            self.assertEqual(response.status, 200, path)
            self.assertGreater(len(body), 1000)
            self.assertIn("text/javascript", response.getheader("Content-Type"))
        response, body = self.request("GET", "/")
        self.assertIn(b"/assets/office.js", body)
        self.assertIn(b"/assets/i18n.js", body)

    def test_rejects_dns_rebinding_hosts_and_cross_site_origins(self):
        response, _ = self.request("GET", "/api/state", {"Host": "attacker.example"})
        self.assertEqual(response.status, 403)
        host, port = self.host, self.port
        response, _ = self.request("GET", "/api/state", {
            "Origin": f"http://attacker.example:{port}",
        })
        self.assertEqual(response.status, 403)
        response, _ = self.request("GET", "/api/state", {
            "Sec-Fetch-Site": "cross-site",
        })
        self.assertEqual(response.status, 403)
        response, _ = self.request("GET", "/api/state", {
            "Origin": f"http://127.0.0.1:{port}",
        })
        self.assertEqual(response.status, 200)

    def test_rejects_writes_traversal_and_invalid_ids(self):
        response, _ = self.request("POST", "/api/state")
        self.assertEqual(response.status, 405)
        response, _ = self.request("GET", "/api/run/..")
        self.assertEqual(response.status, 404)
        response, _ = self.request("GET", "/api/run/%2e%2e%2fsecret")
        self.assertEqual(response.status, 404)
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"Evidence Console", body)
        self.assertIn(b"/assets/app.js", body)


if __name__ == "__main__":
    unittest.main()
