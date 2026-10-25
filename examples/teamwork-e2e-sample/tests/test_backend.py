"""Focused backend tests for backend/app.py (stdlib unittest only)."""

from __future__ import annotations

import json
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib import error, request

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
APP_PATH = BACKEND_DIR / "app.py"
sys.path.insert(0, str(BACKEND_DIR))

import app  # noqa: E402


class ServerTestCase(unittest.TestCase):
    """Starts one real HTTP server per test on an ephemeral port."""

    def setUp(self) -> None:
        self.server = app.create_server(port=0)
        # A short poll interval keeps per-test shutdown near-instant instead of
        # waiting up to 0.5s, so the whole suite stays well under CI timeouts.
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01),
            daemon=True,
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive(), "server thread did not stop")

    def call(self, method: str, path: str, body: object = None):
        data = None
        headers = {}
        if body is not None:
            if isinstance(body, bytes):
                data = body
                headers["Content-Type"] = "application/json"
            else:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
        req = request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with request.urlopen(req, timeout=5) as response:
                return response.status, response.headers, response.read()
        except error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def call_json(self, method: str, path: str, body: object = None):
        status, headers, raw = self.call(method, path, body)
        payload = json.loads(raw.decode("utf-8"))
        return status, headers, payload

    def create_task(self, title: str):
        status, _, payload = self.call_json("POST", "/api/tasks", {"title": title})
        self.assertEqual(status, 201, payload)
        return payload["task"]


class ListTasksTests(ServerTestCase):
    def test_starts_empty(self) -> None:
        status, headers, payload = self.call_json("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"tasks": []})
        self.assertIn("application/json", headers["Content-Type"])

    def test_lists_created_tasks_in_order(self) -> None:
        first = self.create_task("first")
        second = self.create_task("second")
        self.assertEqual(first["id"], 1)
        self.assertEqual(second["id"], 2)

        status, _, payload = self.call_json("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(payload["tasks"], [first, second])


class CreateTaskTests(ServerTestCase):
    def test_creates_task_with_defaults(self) -> None:
        status, headers, payload = self.call_json(
            "POST", "/api/tasks", {"title": "Write tests"}
        )
        self.assertEqual(status, 201)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(
            payload, {"task": {"id": 1, "title": "Write tests", "done": False}}
        )

    def test_trims_surrounding_whitespace(self) -> None:
        task = self.create_task("  padded title  ")
        self.assertEqual(task["title"], "padded title")

    def test_accepts_title_of_exactly_80_characters(self) -> None:
        title = "x" * 80
        task = self.create_task(title)
        self.assertEqual(task["title"], title)

    def test_rejects_invalid_titles(self) -> None:
        cases = [
            "",
            "   ",
            "\n\t ",
            "x" * 81,
            None,
            5,
            True,
            ["a"],
        ]
        for body in ({"title": value} for value in cases):
            with self.subTest(body=body):
                status, _, payload = self.call_json("POST", "/api/tasks", body)
                self.assertEqual(status, 400)
                self.assertIsInstance(payload.get("error"), str)
                self.assertTrue(payload["error"])

        status, _, payload = self.call_json("POST", "/api/tasks", {})
        self.assertEqual(status, 400)
        self.assertEqual(
            self.call_json("GET", "/api/tasks")[2], {"tasks": []}
        )

    def test_rejects_invalid_json_and_non_object_bodies(self) -> None:
        for raw in (b"{not json", b"", b"[]", b'"title"', b"null"):
            with self.subTest(raw=raw):
                status, _, payload = self.call_json("POST", "/api/tasks", raw)
                self.assertEqual(status, 400)
                self.assertIn("error", payload)

    def test_ids_keep_increasing_after_rejected_requests(self) -> None:
        self.assertEqual(self.create_task("one")["id"], 1)
        self.call_json("POST", "/api/tasks", {"title": ""})
        self.assertEqual(self.create_task("two")["id"], 2)


class ToggleTaskTests(ServerTestCase):
    def test_toggles_done_true_and_false(self) -> None:
        task = self.create_task("toggle me")

        status, _, payload = self.call_json(
            "PATCH", f"/api/tasks/{task['id']}", {"done": True}
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            payload, {"task": {"id": task["id"], "title": "toggle me", "done": True}}
        )
        self.assertEqual(self.call_json("GET", "/api/tasks")[2]["tasks"][0]["done"], True)

        status, _, payload = self.call_json(
            "PATCH", f"/api/tasks/{task['id']}", {"done": False}
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["task"]["done"])

    def test_unknown_task_returns_404(self) -> None:
        for path in ("/api/tasks/999", "/api/tasks/0"):
            with self.subTest(path=path):
                status, _, payload = self.call_json(
                    "PATCH", path, {"done": True}
                )
                self.assertEqual(status, 404)
                self.assertIn("error", payload)
        self.assertEqual(self.call_json("GET", "/api/tasks")[2], {"tasks": []})

    def test_invalid_done_values_return_400(self) -> None:
        task = self.create_task("keep me")
        for body in ({}, {"done": "true"}, {"done": 1}, {"done": None}):
            with self.subTest(body=body):
                status, _, payload = self.call_json(
                    "PATCH", f"/api/tasks/{task['id']}", body
                )
                self.assertEqual(status, 400)
                self.assertIn("error", payload)

        tasks = self.call_json("GET", "/api/tasks")[2]["tasks"]
        self.assertFalse(tasks[0]["done"])

    def test_bad_paths_return_404(self) -> None:
        for path in ("/api/tasks", "/api/tasks/abc", "/api/tasks/1/extra"):
            with self.subTest(path=path):
                status, _, payload = self.call_json("PATCH", path, {"done": True})
                self.assertEqual(status, 404)
                self.assertIn("error", payload)


class UnknownRouteTests(ServerTestCase):
    def test_unknown_api_paths_return_json_404(self) -> None:
        for path in ("/api/unknown", "/api/tasks/1"):
            with self.subTest(path=path):
                status, headers, payload = self.call_json("GET", path)
                self.assertEqual(status, 404)
                self.assertEqual(payload, {"error": "not found"})
                self.assertIn("application/json", headers["Content-Type"])

    def test_unknown_methods_return_404(self) -> None:
        status, _, payload = self.call_json("POST", "/api/other", {})
        self.assertEqual(status, 404)
        self.assertIn("error", payload)


class StaticFileTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        (root / "index.html").write_text("<h1>Tasks</h1>", encoding="utf-8")
        (root / "app.js").write_text("console.log('app');", encoding="utf-8")
        (root / "styles.css").write_text("body { margin: 0; }", encoding="utf-8")
        (root / "config.js").write_text(
            'window.AO_API_BASE = "";', encoding="utf-8"
        )
        self.patch = mock.patch.object(app, "FRONTEND_DIR", root)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temp_dir.cleanup()
        super().tearDown()

    def test_serves_contract_routes(self) -> None:
        cases = [
            ("/", b"<h1>Tasks</h1>", "text/html"),
            ("/index.html", b"<h1>Tasks</h1>", "text/html"),
            ("/app.js", b"console.log('app');", "text/javascript"),
            ("/styles.css", b"body { margin: 0; }", "text/css"),
            (
                "/config.js",
                b'window.AO_API_BASE = "";',
                "text/javascript",
            ),
        ]
        for path, expected_body, expected_type in cases:
            with self.subTest(path=path):
                status, headers, body = self.call("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(body, expected_body)
                self.assertIn(expected_type, headers["Content-Type"])

    def test_missing_or_unknown_files_return_404(self) -> None:
        for path in ("/missing.txt", "/..%2fCONTRACT.md", "/backend/app.py"):
            with self.subTest(path=path):
                status, headers, body = self.call("GET", path)
                self.assertEqual(status, 404)
                self.assertIn("text/plain", headers["Content-Type"])
                self.assertNotIn(b"Teamwork E2E sample contract", body)


class CliTests(unittest.TestCase):
    def test_print_port_prints_bound_port_and_serves(self) -> None:
        process = subprocess.Popen(
            [sys.executable, str(APP_PATH), "--port", "0", "--print-port"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(BACKEND_DIR.parent),
        )
        try:
            readable, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(readable, "server did not print a port")
            line = process.stdout.readline().strip()
            self.assertRegex(line, r"^\d+$")
            port = int(line)
            self.assertGreater(port, 0)

            with request.urlopen(
                f"http://127.0.0.1:{port}/api/tasks", timeout=5
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    json.loads(response.read().decode("utf-8")), {"tasks": []}
                )

            create = request.Request(
                f"http://127.0.0.1:{port}/api/tasks",
                data=json.dumps({"title": "from cli"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(create, timeout=5) as response:
                self.assertEqual(response.status, 201)
                self.assertEqual(
                    json.loads(response.read().decode("utf-8"))["task"]["title"],
                    "from cli",
                )
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
