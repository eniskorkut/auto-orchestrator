"""Focused backend tests for the in-memory task manager."""

from __future__ import annotations

import http.client
import json
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import app  # noqa: E402


class APITestCase(unittest.TestCase):
    """Runs a real HTTP server for each test class."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = app.create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.server.store = app.TaskStore()

    def request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, object | None, dict[str, str]]:
        payload = raw_body
        request_headers = dict(headers or {})
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
        if payload is not None:
            request_headers.setdefault("Content-Type", "application/json")

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=5
        )
        try:
            connection.request(method, path, body=payload, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            decoded = json.loads(data) if data else None
            return response.status, decoded, dict(response.getheaders())
        finally:
            connection.close()


class TaskAPITests(APITestCase):
    def test_list_tasks_starts_empty(self) -> None:
        status, body, headers = self.request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"tasks": []})
        self.assertIn("application/json", headers["Content-Type"])

    def test_create_task_and_list_it(self) -> None:
        status, body, _ = self.request("POST", "/api/tasks", {"title": "Buy milk"})
        self.assertEqual(status, 201)
        task = body["task"]
        self.assertEqual(task, {"id": 1, "title": "Buy milk", "done": False})

        status, body, _ = self.request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(body["tasks"], [task])

    def test_create_trims_surrounding_whitespace(self) -> None:
        status, body, _ = self.request("POST", "/api/tasks", {"title": "  Walk dog  "})
        self.assertEqual(status, 201)
        self.assertEqual(body["task"]["title"], "Walk dog")

    def test_title_length_boundaries(self) -> None:
        longest = "a" * 80
        status, body, _ = self.request("POST", "/api/tasks", {"title": longest})
        self.assertEqual(status, 201)
        self.assertEqual(body["task"]["title"], longest)

        status, body, _ = self.request("POST", "/api/tasks", {"title": "a" * 81})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_empty_and_whitespace_titles_are_rejected(self) -> None:
        for title in ("", "   ", "\t\n"):
            with self.subTest(title=title):
                status, body, _ = self.request(
                    "POST", "/api/tasks", {"title": title}
                )
                self.assertEqual(status, 400)
                self.assertIn("error", body)

    def test_invalid_title_payloads_are_rejected(self) -> None:
        payloads = [
            {},
            {"title": None},
            {"title": 42},
            {"title": ["a"]},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                status, body, _ = self.request("POST", "/api/tasks", payload)
                self.assertEqual(status, 400)
                self.assertIn("error", body)

        status, body, _ = self.request(
            "POST", "/api/tasks", raw_body=b"{not json"
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

        status, body, _ = self.request(
            "POST", "/api/tasks", raw_body=b"[]", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_missing_body_is_rejected(self) -> None:
        status, body, _ = self.request("POST", "/api/tasks")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_toggle_done_true_and_false(self) -> None:
        _, created, _ = self.request("POST", "/api/tasks", {"title": "Toggle me"})
        task_id = created["task"]["id"]

        status, body, _ = self.request(
            "PATCH", f"/api/tasks/{task_id}", {"done": True}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["task"], {"id": task_id, "title": "Toggle me", "done": True})

        status, body, _ = self.request(
            "PATCH", f"/api/tasks/{task_id}", {"done": False}
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["task"]["done"])

    def test_patch_validation_and_missing_tasks(self) -> None:
        status, body, _ = self.request(
            "PATCH", "/api/tasks/999", {"done": True}
        )
        self.assertEqual(status, 404)
        self.assertIn("error", body)

        _, created, _ = self.request("POST", "/api/tasks", {"title": "Task"})
        task_id = created["task"]["id"]

        for payload in ({}, {"done": "yes"}, {"done": 1}, {"done": None}):
            with self.subTest(payload=payload):
                status, body, _ = self.request(
                    "PATCH", f"/api/tasks/{task_id}", payload
                )
                self.assertEqual(status, 400)
                self.assertIn("error", body)

        for path in ("/api/tasks/0", "/api/tasks/abc", "/api/tasks/1/extra"):
            with self.subTest(path=path):
                status, _, _ = self.request("PATCH", path, {"done": True})
                self.assertEqual(status, 404)

    def test_unknown_api_routes_return_json_404(self) -> None:
        for method, path in (
            ("GET", "/api/missing"),
            ("POST", "/api/missing"),
            ("PATCH", "/api/missing"),
        ):
            with self.subTest(method=method, path=path):
                status, body, _ = self.request(method, path)
                self.assertEqual(status, 404)
                self.assertIn("error", body)


class StaticFileTests(APITestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.frontend = Path(self._tmp.name)
        (self.frontend / "index.html").write_text("<h1>Tasks</h1>", encoding="utf-8")
        (self.frontend / "app.js").write_text("console.log('app');", encoding="utf-8")
        (self.frontend / "styles.css").write_text("body { margin: 0; }", encoding="utf-8")
        (self.frontend / "config.js").write_text(
            'window.AO_API_BASE = "";', encoding="utf-8"
        )
        self._original_frontend = app.FRONTEND_DIR
        app.FRONTEND_DIR = self.frontend
        self.addCleanup(setattr, app, "FRONTEND_DIR", self._original_frontend)

    def fetch_text(self, path: str) -> tuple[int, str, str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            return response.status, body, response.getheader("Content-Type", "")
        finally:
            connection.close()

    def test_frontend_files_are_served(self) -> None:
        expectations = (
            ("/", "<h1>Tasks</h1>", "text/html"),
            ("/index.html", "<h1>Tasks</h1>", "text/html"),
            ("/app.js", "console.log('app');", "javascript"),
            ("/styles.css", "body { margin: 0; }", "text/css"),
            ("/config.js", 'window.AO_API_BASE = "";', "javascript"),
        )
        for path, expected_body, expected_type in expectations:
            with self.subTest(path=path):
                status, body, content_type = self.fetch_text(path)
                self.assertEqual(status, 200)
                self.assertEqual(body, expected_body)
                self.assertIn(expected_type, content_type)

    def test_missing_or_unknown_files_return_404(self) -> None:
        (self.frontend / "app.js").unlink()
        for path in ("/app.js", "/nope.txt", "/../CONTRACT.md", "/%2e%2e/CONTRACT.md"):
            with self.subTest(path=path):
                status, body, content_type = self.fetch_text(path)
                self.assertEqual(status, 404)
                self.assertIn("application/json", content_type)
                self.assertIn("error", json.loads(body))


class CLITests(unittest.TestCase):
    def test_print_port_binds_and_serves(self) -> None:
        process = subprocess.Popen(
            [sys.executable, str(BACKEND_DIR / "app.py"), "--port", "0", "--print-port"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(ready, "server did not print its port")
            line = process.stdout.readline().strip()
            self.assertTrue(line.isdigit(), f"unexpected port output: {line!r}")
            port = int(line)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/api/tasks")
                response = connection.getresponse()
                body = json.loads(response.read())
            finally:
                connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(body, {"tasks": []})
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
