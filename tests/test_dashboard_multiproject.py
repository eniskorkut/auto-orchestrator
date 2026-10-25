from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tools import dashboard as dashboard_module
from tools.dashboard import Catalog, DashboardServer, _demo_catalog
from tools.project_registry import ProjectRegistry
from tools.supervisor import Supervisor


def write_run(root: Path, run_id: str, events: list[dict]) -> Path:
    path = root / ".auto-orchestrator" / "runs" / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


class MultiProjectTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.alpha = self.base / "alpha"
        self.beta = self.base / "beta"
        self.alpha.mkdir()
        self.beta.mkdir()
        self.registry_path = self.base / "registry" / "projects.json"
        self.registry = ProjectRegistry(self.registry_path)
        self.alpha_project, _ = self.registry.add(self.alpha, name="Alpha Lab", theme="ai")
        self.beta_project, _ = self.registry.add(self.beta, name="Beta Lab", theme="security")

    def start_server(self, catalog: Catalog):
        server = DashboardServer(("127.0.0.1", 0), catalog.default_store, catalog=catalog)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._stop, server, thread)
        return server, thread

    @staticmethod
    def _stop(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def request(self, server, path: str, headers: dict | None = None):
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response, response.read()
        finally:
            connection.close()

    def catalog(self, **kwargs):
        return Catalog.load(self.registry_path, **kwargs)


class ProjectIsolationTests(MultiProjectTestCase):
    def setUp(self):
        super().setUp()
        now = time.time()
        write_run(self.alpha, "shared-run", [
            {"event": "start", "platform": "opencode", "time": now - 100},
            {"event": "agent-start", "agent_id": "a1b2c3d4", "time": now - 99},
            {"event": "agent-stop", "agent_id": "a1b2c3d4", "time": now - 95},
            {"event": "finish", "status": "success", "verified": False, "time": now - 90},
        ])
        write_run(self.beta, "shared-run", [
            {"event": "start", "platform": "codex", "time": now - 80},
            {"event": "agent-start", "agent_id": "b2c3d4e5", "time": now - 79},
            {"event": "agent-stop", "agent_id": "b2c3d4e5", "time": now - 75},
            {"event": "finish", "status": "failed", "verified": False, "time": now - 70},
        ])
        self.registry.add_agent(self.alpha_project["project_id"], name="Ada",
                                role="RAG Engineer", platform="opencode")
        self.registry.add_agent(self.beta_project["project_id"], name="Mira",
                                role="Security Engineer", platform="cursor")
        self.catalog_instance = self.catalog()
        self.server, _ = self.start_server(self.catalog_instance)

    def test_identical_run_ids_never_mix(self):
        response, body = self.request(
            self.server, f"/api/projects/{self.alpha_project['project_id']}/state")
        self.assertEqual(response.status, 200)
        alpha_state = json.loads(body)
        self.assertEqual(alpha_state["project_id"], self.alpha_project["project_id"])
        self.assertEqual(alpha_state["project"]["name"], "Alpha Lab")
        self.assertEqual([run["id"] for run in alpha_state["runs"]], ["shared-run"])
        self.assertEqual(alpha_state["runs"][0]["platform"], "opencode")
        self.assertEqual(alpha_state["runs"][0]["status"], "success")

        response, body = self.request(
            self.server, f"/api/projects/{self.beta_project['project_id']}/state")
        beta_state = json.loads(body)
        self.assertEqual(beta_state["project"]["name"], "Beta Lab")
        self.assertEqual(beta_state["runs"][0]["platform"], "codex")
        self.assertEqual(beta_state["runs"][0]["status"], "failed")

        response, body = self.request(
            self.server,
            f"/api/projects/{self.alpha_project['project_id']}/runs/shared-run")
        alpha_detail = json.loads(body)
        self.assertEqual(alpha_detail["project"]["project_id"],
                         self.alpha_project["project_id"])
        self.assertEqual(alpha_detail["run"]["platform"], "opencode")

        response, body = self.request(
            self.server,
            f"/api/projects/{self.beta_project['project_id']}/runs/shared-run")
        beta_detail = json.loads(body)
        self.assertEqual(beta_detail["run"]["platform"], "codex")
        self.assertEqual(beta_detail["run"]["status"], "failed")
        self.assertNotEqual(alpha_detail["run"]["fingerprint"],
                            beta_detail["run"]["fingerprint"])

        response, body = self.request(self.server, "/api/run/shared-run")
        legacy = json.loads(body)
        self.assertEqual(legacy["run"]["platform"], "opencode")
        self.assertNotIn("project", legacy)

    def test_office_and_inspector_are_project_scoped(self):
        response, body = self.request(
            self.server, f"/api/projects/{self.alpha_project['project_id']}/office")
        self.assertEqual(response.status, 200)
        alpha_office = json.loads(body)
        self.assertEqual(alpha_office["project_id"], self.alpha_project["project_id"])
        self.assertEqual([agent["name"] for agent in alpha_office["agents"]], ["Ada"])

        response, body = self.request(
            self.server, f"/api/projects/{self.beta_project['project_id']}/office")
        beta_office = json.loads(body)
        self.assertEqual([agent["name"] for agent in beta_office["agents"]], ["Mira"])
        self.assertNotIn("Ada", body.decode())

        alpha_agent = alpha_office["agents"][0]["agent_definition_id"]
        beta_agent = beta_office["agents"][0]["agent_definition_id"]
        response, body = self.request(
            self.server,
            f"/api/projects/{self.alpha_project['project_id']}/agents/{alpha_agent}")
        self.assertEqual(response.status, 200)
        inspector = json.loads(body)
        self.assertEqual(inspector["agent"]["name"], "Ada")
        self.assertFalse(inspector["terminal"]["supported"])

        response, _ = self.request(
            self.server,
            f"/api/projects/{self.alpha_project['project_id']}/agents/{beta_agent}")
        self.assertEqual(response.status, 404)

    def test_global_etag_changes_when_binding_changes(self):
        first = self.registry.add_agent(self.alpha_project["project_id"], name="Ada",
                                        role="Engineer", platform="opencode")
        second = self.registry.add_agent(self.alpha_project["project_id"], name="Grace",
                                         role="Tester", platform="codex")
        server, _ = self.start_server(self.catalog())
        response, body = self.request(server, "/api/global")
        self.assertEqual(response.status, 200)
        etag = response.getheader("ETag")
        self.assertEqual(json.loads(body)["projects"][0]["linked_agent_count"], 0)
        self.registry.bind_agent(self.alpha_project["project_id"],
                                 first["agent_definition_id"], "a" * 64, run_id="run-1")
        response, body = self.request(server, "/api/global", {"If-None-Match": etag})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)["projects"][0]["linked_agent_count"], 1)
        etag = response.getheader("ETag")
        self.registry.bind_agent(self.alpha_project["project_id"],
                                 second["agent_definition_id"], "b" * 64, run_id="run-1")
        response, body = self.request(server, "/api/global", {"If-None-Match": etag})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)["projects"][0]["linked_agent_count"], 2)
        self.assertNotEqual(response.getheader("ETag"), etag)

    def test_malformed_bindings_do_not_become_no_binding(self):
        bindings = self.alpha / ".auto-orchestrator" / "agent-bindings.json"
        bindings.parent.mkdir(parents=True, exist_ok=True)
        bindings.write_text('{"version":1,"bindings":[{"agent_definition_id":"bad"}]}', encoding="utf-8")
        catalog = self.catalog()
        server, _ = self.start_server(catalog)
        response, body = self.request(
            server, f"/api/projects/{self.alpha_project['project_id']}/office")
        self.assertEqual(response.status, 404)
        self.assertIn("Binding evidence is unavailable", body.decode())

    def test_deeply_nested_bindings_fail_closed_without_a_crash(self):
        bindings = self.alpha / ".auto-orchestrator" / "agent-bindings.json"
        bindings.parent.mkdir(parents=True, exist_ok=True)
        bindings.write_text('{"version":1,"bindings":' + '[' * 100000 + ']' * 100000 + '}',
                            encoding="utf-8")
        server, _ = self.start_server(self.catalog())
        response, body = self.request(
            server, f"/api/projects/{self.alpha_project['project_id']}/office")
        self.assertIn(response.status, (404, 503))
        self.assertIn("Binding evidence is unavailable", body.decode())

    def test_project_state_etag_includes_registry_metadata(self):
        server, _ = self.start_server(self.catalog())
        path = f"/api/projects/{self.alpha_project['project_id']}/state"
        response, _ = self.request(server, path)
        self.assertEqual(response.status, 200)
        etag = response.getheader("ETag")
        self.registry.update(self.alpha_project["project_id"], name="Renamed Lab")
        server2, _ = self.start_server(self.catalog())
        response, body = self.request(server2, path, {"If-None-Match": etag})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)["project"]["name"], "Renamed Lab")
        self.assertNotEqual(response.getheader("ETag"), etag)

    def test_unknown_project_and_run_return_404(self):
        response, _ = self.request(self.server, "/api/projects/p_" + "0" * 16 + "/state")
        self.assertEqual(response.status, 404)
        response, _ = self.request(self.server, "/api/projects/not-a-project/state")
        self.assertEqual(response.status, 404)
        response, _ = self.request(
            self.server,
            f"/api/projects/{self.alpha_project['project_id']}/runs/missing")
        self.assertEqual(response.status, 404)
        response, _ = self.request(self.server, "/api/projects/%2e%2e/state")
        self.assertEqual(response.status, 404)


class GlobalOverviewTests(MultiProjectTestCase):
    def test_totals_are_aggregated_without_run_payloads(self):
        now = time.time()
        write_run(self.alpha, "alpha-run", [
            {"event": "start", "platform": "opencode", "time": now - 100},
            {"event": "agent-start", "agent_id": "a1b2c3d4", "time": now - 99},
            {"event": "agent-stop", "agent_id": "a1b2c3d4", "time": now - 95},
            {"event": "finish", "status": "success", "time": now - 90},
        ])
        write_run(self.beta, "beta-run", [
            {"event": "start", "platform": "codex", "time": now - 100},
        ])
        self.registry.add_agent(self.alpha_project["project_id"], name="Ada", role="R")
        supervisor = Supervisor(self.alpha)
        try:
            supervisor.init("sup-run", {
                "max_parallel": 1,
                "tasks": [
                    {"id": "one", "role": "implementer", "objective": "One",
                     "read_scope": [], "write_scope": []},
                    {"id": "two", "role": "tester", "objective": "Two",
                     "read_scope": [], "write_scope": []},
                ],
            })
            supervisor.claim("sup-run")
        finally:
            supervisor.close()
        server, _ = self.start_server(self.catalog())
        response, body = self.request(server, "/api/global")
        self.assertEqual(response.status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["totals"]["projects"], 2)
        self.assertEqual(payload["totals"]["runs"], 2)
        self.assertEqual(payload["totals"]["open"], 1)
        self.assertEqual(payload["totals"]["agents"], 1)
        self.assertEqual(payload["totals"]["active_tasks"], 1)
        self.assertEqual(payload["totals"]["errors"], 0)
        rows = {row["project_id"]: row for row in payload["projects"]}
        alpha_row = rows[self.alpha_project["project_id"]]
        self.assertEqual(alpha_row["overview"]["tasks"], 2)
        self.assertEqual(alpha_row["overview"]["active_tasks"], 1)
        self.assertEqual(alpha_row["agent_count"], 1)
        for row in payload["projects"]:
            self.assertNotIn("runs", row)
            self.assertNotIn("supervisor", row)
            self.assertNotIn("worktrees", row)
            self.assertTrue({
                "runs", "verified", "open", "live", "agents", "tasks",
                "active_tasks", "blockers", "last_activity", "verified_rate",
            } <= set(row["overview"]))
        etag = response.getheader("ETag")
        response, body = self.request(server, "/api/global", {"If-None-Match": etag})
        self.assertEqual(response.status, 304)
        self.assertEqual(body, b"")

    def test_project_error_does_not_break_other_projects(self):
        write_run(self.alpha, "alpha-run", [
            {"event": "start", "platform": "opencode", "time": time.time() - 20},
        ])
        self.beta.rmdir()
        server, _ = self.start_server(self.catalog())
        response, body = self.request(server, "/api/global")
        self.assertEqual(response.status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["totals"]["errors"], 1)
        rows = {row["project_id"]: row for row in payload["projects"]}
        alpha_row = rows[self.alpha_project["project_id"]]
        beta_row = rows[self.beta_project["project_id"]]
        self.assertEqual(alpha_row["status"], "ok")
        self.assertEqual(alpha_row["overview"]["runs"], 1)
        self.assertEqual(beta_row["status"], "error")
        self.assertIn("unavailable", beta_row["error"])
        self.assertEqual(beta_row["overview"]["runs"], 0)

        response, _ = self.request(
            server, f"/api/projects/{self.beta_project['project_id']}/state")
        self.assertEqual(response.status, 503)
        response, body = self.request(
            server, f"/api/projects/{self.alpha_project['project_id']}/state")
        self.assertEqual(response.status, 200)
        self.assertEqual(len(json.loads(body)["runs"]), 1)


class RegistryEndpointTests(MultiProjectTestCase):
    def test_projects_endpoint_and_get_requests_do_not_write(self):
        server, _ = self.start_server(self.catalog())
        before = self.registry_path.read_bytes()
        response, body = self.request(server, "/api/projects")
        self.assertEqual(response.status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["registry_path"], str(self.registry_path))
        self.assertEqual({project["name"] for project in payload["projects"]},
                         {"Alpha Lab", "Beta Lab"})
        names = [project["name"] for project in payload["projects"]]
        self.assertEqual(names, ["Alpha Lab", "Beta Lab"])

        for path in ("/api/state", "/api/global", "/api/projects",
                     f"/api/projects/{self.alpha_project['project_id']}/state",
                     f"/api/projects/{self.alpha_project['project_id']}/office"):
            response, _ = self.request(server, path)
            self.assertEqual(response.status, 200, path)
        self.assertEqual(self.registry_path.read_bytes(), before)

    def test_root_auto_registration_and_no_register(self):
        fresh = self.base / "fresh" / "projects.json"
        extra = self.base / "gamma"
        extra.mkdir()
        catalog = Catalog.load(fresh, root=extra)
        self.assertTrue(fresh.is_file())
        payload = json.loads(fresh.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["projects"]), 1)
        self.assertEqual(catalog.default_id, payload["projects"][0]["project_id"])

        no_write = self.base / "no-register" / "projects.json"
        catalog = Catalog.load(no_write, root=extra, no_register=True)
        self.assertFalse(no_write.exists())
        self.assertEqual(len(catalog.projects), 1)
        self.assertIsNotNone(catalog.project_state(catalog.default_id))

    def test_bindings_are_not_shared_between_projects(self):
        alpha_agent = self.registry.add_agent(
            self.alpha_project["project_id"], name="Ada", role="RAG Engineer")
        beta_agent = self.registry.add_agent(
            self.beta_project["project_id"], name="Mira", role="Security Engineer")
        self.registry.bind_agent(self.alpha_project["project_id"],
                                 alpha_agent["agent_definition_id"],
                                 "a" * 64, run_id="alpha-run")
        catalog = self.catalog()
        alpha_office = catalog.office(self.alpha_project["project_id"])
        beta_office = catalog.office(self.beta_project["project_id"])
        self.assertIsNotNone(alpha_office["agents"][0]["binding"])
        self.assertIsNone(beta_office["agents"][0]["binding"])
        self.assertEqual(catalog.bindings(self.beta_project["project_id"]), [])
        self.assertEqual(alpha_office["agents"][0]["status"], "unknown")

    def test_legacy_state_keeps_shape_and_adds_metadata(self):
        server, _ = self.start_server(self.catalog())
        response, body = self.request(server, "/api/state")
        self.assertEqual(response.status, 200)
        state = json.loads(body)
        self.assertEqual(state["project"], str(self.alpha.resolve()))
        self.assertEqual(state["project_id"], self.alpha_project["project_id"])
        self.assertEqual(state["project_meta"]["name"], "Alpha Lab")
        etag = response.getheader("ETag")
        response, body = self.request(server, "/api/state", {"If-None-Match": etag})
        self.assertEqual(response.status, 304)
        self.assertEqual(body, b"")


class DemoModeTests(unittest.TestCase):
    def test_demo_catalog_has_three_projects_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(os.environ, {"AUTO_ORCHESTRATOR_HOME": temp}):
                catalog = _demo_catalog()
            self.assertEqual(len(catalog.projects), 3)
            self.assertIsNone(catalog.registry_path)
            names = {entry.project["name"] for entry in catalog.projects.values()}
            self.assertEqual(names, {"AI Research Lab", "Security Lab", "Web Studio"})
            statuses = set()
            for project_id, entry in catalog.projects.items():
                self.assertFalse(Path(entry.project["root"]).exists())
                office = catalog.office(project_id)
                self.assertTrue(office["agents"])
                self.assertEqual(office["project_id"], project_id)
                statuses.update(agent["status"] for agent in office["agents"])
                inspector = catalog.inspector(
                    project_id, office["agents"][0]["agent_definition_id"])
                self.assertFalse(inspector["terminal"]["supported"])
            self.assertTrue({"working", "dispatching", "completed", "failed",
                             "unknown"} <= statuses)
            self.assertTrue(catalog.global_overview()["demo"])
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
