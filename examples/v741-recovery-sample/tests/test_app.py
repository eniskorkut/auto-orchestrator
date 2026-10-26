"""Unit tests for the stdlib task manager backend (CONTRACT.md)."""

from __future__ import annotations

import json
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend import app  # noqa: E402


class BackendTestCase(unittest.TestCase):
    """Starts an isolated server with a temporary frontend directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.frontend_dir = Path(self._tmp.name)
        (self.frontend_dir / "index.html").write_text(
            "<!doctype html><title>tasks</title>", encoding="utf-8"
        )
        (self.frontend_dir / "app.js").write_text("// app", encoding="utf-8")
        (self.frontend_dir / "styles.css").write_text("/* css */", encoding="utf-8")
        (self.frontend_dir / "config.js").write_text(
            'window.AO_API_BASE = "";', encoding="utf-8"
        )

        self.server = app.make_server(port=0, frontend_dir=self.frontend_dir)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method: str, path: str, body: object = None, raw: bytes | None = None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            payload = raw
            headers = {}
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
            if payload is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            data = response.read()
            content_type = response.getheader("Content-Type", "")
            parsed = json.loads(data) if "application/json" in content_type else data
            return response.status, parsed
        finally:
            connection.close()


class TaskApiTests(BackendTestCase):
    def test_list_initially_empty(self) -> None:
        status, payload = self.request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"tasks": []})

    def test_create_task_and_list(self) -> None:
        status, payload = self.request("POST", "/api/tasks", {"title": "  Write tests  "})
        self.assertEqual(status, 201)
        self.assertEqual(payload, {"task": {"id": 1, "title": "Write tests", "done": False}})

        status, payload = self.request("POST", "/api/tasks", {"title": "Second"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["task"]["id"], 2)
        self.assertFalse(payload["task"]["done"])

        status, payload = self.request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["tasks"],
            [
                {"id": 1, "title": "Write tests", "done": False},
                {"id": 2, "title": "Second", "done": False},
            ],
        )

    def test_title_validation(self) -> None:
        cases = [
            ("missing title", {}),
            ("null title", {"title": None}),
            ("non-string title", {"title": 7}),
            ("empty title", {"title": ""}),
            ("whitespace title", {"title": "   \t\n "}),
            ("too long", {"title": "x" * (app.MAX_TITLE_LENGTH + 1)}),
        ]
        for label, body in cases:
            with self.subTest(label=label):
                status, payload = self.request("POST", "/api/tasks", body)
                self.assertEqual(status, 400)
                self.assertIn("error", payload)

        for label, raw in [
            ("malformed json", b"{not json"),
            ("json array", b"[]"),
            ("empty body", b""),
        ]:
            with self.subTest(label=label):
                status, payload = self.request("POST", "/api/tasks", raw=raw)
                self.assertEqual(status, 400)
                self.assertIn("error", payload)

        _, payload = self.request("GET", "/api/tasks")
        self.assertEqual(payload["tasks"], [])

    def test_title_length_boundaries(self) -> None:
        status, _ = self.request("POST", "/api/tasks", {"title": "x" * app.MAX_TITLE_LENGTH})
        self.assertEqual(status, 201)
        status, _ = self.request("POST", "/api/tasks", {"title": "x"})
        self.assertEqual(status, 201)

    def test_patch_toggles_done(self) -> None:
        self.request("POST", "/api/tasks", {"title": "Toggle me"})

        status, payload = self.request("PATCH", "/api/tasks/1", {"done": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"task": {"id": 1, "title": "Toggle me", "done": True}})

        status, payload = self.request("PATCH", "/api/tasks/1", {"done": False})
        self.assertEqual(status, 200)
        self.assertFalse(payload["task"]["done"])

        _, listing = self.request("GET", "/api/tasks")
        self.assertFalse(listing["tasks"][0]["done"])

    def test_patch_unknown_task_is_404(self) -> None:
        status, payload = self.request("PATCH", "/api/tasks/99", {"done": True})
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

        status, _ = self.request("PATCH", "/api/tasks/not-a-number", {"done": True})
        self.assertEqual(status, 404)

    def test_patch_validation(self) -> None:
        self.request("POST", "/api/tasks", {"title": "Keep state"})
        cases = [
            ("missing done", {}),
            ("string done", {"done": "true"}),
            ("numeric done", {"done": 1}),
            ("null done", {"done": None}),
        ]
        for label, body in cases:
            with self.subTest(label=label):
                status, payload = self.request("PATCH", "/api/tasks/1", body)
                self.assertEqual(status, 400)
                self.assertIn("error", payload)

        status, _ = self.request("PATCH", "/api/tasks/1", raw=b"not json")
        self.assertEqual(status, 400)

        _, listing = self.request("GET", "/api/tasks")
        self.assertFalse(listing["tasks"][0]["done"])

    def test_unknown_routes_are_404(self) -> None:
        for method, path in [
            ("GET", "/api/unknown"),
            ("GET", "/app.js/extra"),
            ("POST", "/api/tasks/1"),
        ]:
            with self.subTest(method=method, path=path):
                status, _ = self.request(method, path)
                self.assertEqual(status, 404)


class StaticFileTests(BackendTestCase):
    def test_serves_frontend_files(self) -> None:
        cases = [
            ("/", b"<!doctype html><title>tasks</title>"),
            ("/app.js", b"// app"),
            ("/styles.css", b"/* css */"),
            ("/config.js", b'window.AO_API_BASE = "";'),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                status, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(body, expected)

    def test_missing_frontend_file_is_404(self) -> None:
        (self.frontend_dir / "app.js").unlink()
        status, _ = self.request("GET", "/app.js")
        self.assertEqual(status, 404)

    def test_traversal_is_rejected(self) -> None:
        for path in ["/../CONTRACT.md", "/%2e%2e/CONTRACT.md", "/frontend/app.js"]:
            with self.subTest(path=path):
                status, _ = self.request("GET", path)
                self.assertEqual(status, 404)


class ParseHelperTests(unittest.TestCase):
    def test_parse_title(self) -> None:
        self.assertEqual(app.parse_title({"title": "  hi  "}), "hi")
        with self.assertRaises(app.ValidationError):
            app.parse_title({"title": "   "})
        with self.assertRaises(app.ValidationError):
            app.parse_title({"title": "x" * 81})

    def test_parse_done(self) -> None:
        self.assertIs(app.parse_done({"done": True}), True)
        self.assertIs(app.parse_done({"done": False}), False)
        for body in ({}, {"done": 1}, {"done": "false"}, None):
            with self.assertRaises(app.ValidationError):
                app.parse_done(body)


class CommandLineTests(unittest.TestCase):
    def test_print_port_and_serve(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "backend/app.py", "--port", "0", "--print-port"],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._terminate, process)
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(ready, "server did not print a port in time")
            port = int(process.stdout.readline().strip())
            self.assertGreater(port, 0)

            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/api/tasks")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"tasks": []})
            finally:
                connection.close()
        finally:
            self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


if __name__ == "__main__":
    unittest.main()
