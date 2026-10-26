"""Focused backend tests for the in-memory task manager.

Run from the repository root with either:

    python3.11 -m unittest tests.test_backend -v
    python3.11 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import select
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import app  # noqa: E402


class NormalizeTitleTests(unittest.TestCase):
    def test_accepts_and_trims_valid_titles(self):
        cases = {
            "Buy milk": "Buy milk",
            "  Buy milk  ": "Buy milk",
            "a": "a",
            "a b  c": "a b  c",
            "x" * 80: "x" * 80,
            "  " + "x" * 80 + "  ": "x" * 80,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(app.normalize_title(raw), expected)

    def test_rejects_blank_titles(self):
        for raw in ("", "   ", "\n\t ", "\u00a0"):
            with self.subTest(raw=raw):
                with self.assertRaises(app.ValidationError):
                    app.normalize_title(raw)

    def test_rejects_titles_longer_than_80_characters(self):
        with self.assertRaises(app.ValidationError):
            app.normalize_title("x" * 81)
        with self.assertRaises(app.ValidationError):
            app.normalize_title("  " + "x" * 81 + "  ")

    def test_rejects_non_string_titles(self):
        for raw in (None, 42, 3.14, True, ["title"], {"title": "x"}):
            with self.subTest(raw=raw):
                with self.assertRaises(app.ValidationError):
                    app.normalize_title(raw)


class TaskStoreTests(unittest.TestCase):
    def test_create_and_list_preserve_order(self):
        store = app.TaskStore()
        first = store.create("one")
        second = store.create("two")
        self.assertEqual(first, {"id": 1, "title": "one", "done": False})
        self.assertEqual(second, {"id": 2, "title": "two", "done": False})
        self.assertEqual(store.list_tasks(), [first, second])

    def test_returned_tasks_do_not_alias_store_state(self):
        store = app.TaskStore()
        task = store.create("one")
        task["title"] = "mutated"
        task["done"] = True
        self.assertEqual(
            store.list_tasks(), [{"id": 1, "title": "one", "done": False}]
        )

    def test_set_done_updates_known_task_and_ignores_unknown(self):
        store = app.TaskStore()
        store.create("one")
        updated = store.set_done(1, True)
        self.assertEqual(updated, {"id": 1, "title": "one", "done": True})
        self.assertIsNone(store.set_done(99, True))
        self.assertIsNone(store.set_done(0, False))


class HttpApiTests(unittest.TestCase):
    """End-to-end tests against a real server on an ephemeral port."""

    frontend_files = {
        "index.html": b"<!doctype html><title>Tasks</title>",
        "app.js": b"console.log('app');\n",
        "styles.css": b"body { margin: 0; }\n",
        "config.js": b'window.AO_API_BASE = "";\n',
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.frontend = Path(self.tmp.name) / "frontend"
        self.frontend.mkdir()
        for name, content in self.frontend_files.items():
            (self.frontend / name).write_bytes(content)

        self.server = app.create_server(port=0, frontend_dir=str(self.frontend))
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = "http://%s:%d" % (host, port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    # -- helpers --------------------------------------------------------------

    def request(self, method, path, *, payload=None, raw=None):
        data = raw
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def assert_json_response(self, response, status, payload):
        code, headers, body = response
        self.assertEqual(code, status, body)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(json.loads(body), payload)

    def create_task(self, title):
        code, _, body = self.request("POST", "/api/tasks", payload={"title": title})
        self.assertEqual(code, 201, body)
        return json.loads(body)["task"]

    # -- GET /api/tasks -------------------------------------------------------

    def test_list_tasks_starts_empty(self):
        self.assert_json_response(
            self.request("GET", "/api/tasks"), 200, {"tasks": []}
        )

    def test_list_tasks_after_creates(self):
        first = self.create_task("one")
        second = self.create_task("two")
        self.assert_json_response(
            self.request("GET", "/api/tasks"),
            200,
            {"tasks": [first, second]},
        )

    # -- POST /api/tasks ------------------------------------------------------

    def test_create_task_returns_201_with_task(self):
        code, headers, body = self.request(
            "POST", "/api/tasks", payload={"title": "Buy milk"}
        )
        self.assertEqual(code, 201)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(
            json.loads(body),
            {"task": {"id": 1, "title": "Buy milk", "done": False}},
        )

    def test_create_task_trims_surrounding_whitespace(self):
        task = self.create_task("  Buy milk  ")
        self.assertEqual(task["title"], "Buy milk")

    def test_create_task_accepts_boundary_lengths(self):
        self.assertEqual(self.create_task("a")["title"], "a")
        longest = "x" * 80
        self.assertEqual(self.create_task(longest)["title"], longest)

    def test_create_task_rejects_invalid_titles(self):
        invalid = ("", "   ", "\n\t", "x" * 81, None, 42, True, ["x"])
        for title in invalid:
            with self.subTest(title=title):
                code, headers, body = self.request(
                    "POST", "/api/tasks", payload={"title": title}
                )
                self.assertEqual(code, 400, body)
                self.assertIn("application/json", headers.get("Content-Type", ""))
                error = json.loads(body)
                self.assertIsInstance(error.get("error"), str)
                self.assertTrue(error["error"])
        self.assert_json_response(
            self.request("GET", "/api/tasks"), 200, {"tasks": []}
        )

    def test_create_task_rejects_missing_title(self):
        code, _, body = self.request("POST", "/api/tasks", payload={})
        self.assertEqual(code, 400, body)
        self.assertIsInstance(json.loads(body).get("error"), str)

    def test_create_task_rejects_malformed_json(self):
        code, _, body = self.request(
            "POST", "/api/tasks", raw=b"{not json"
        )
        self.assertEqual(code, 400, body)
        self.assertIn("error", json.loads(body))

    def test_create_task_rejects_non_object_body(self):
        for payload in (["a"], "text", 7):
            with self.subTest(payload=payload):
                code, _, body = self.request(
                    "POST", "/api/tasks", payload=payload
                )
                self.assertEqual(code, 400, body)
                self.assertIn("error", json.loads(body))

    def test_create_task_rejects_empty_body(self):
        code, _, body = self.request("POST", "/api/tasks", raw=b"")
        self.assertEqual(code, 400, body)
        self.assertIn("error", json.loads(body))

    def test_ids_increment(self):
        self.assertEqual(self.create_task("one")["id"], 1)
        self.assertEqual(self.create_task("two")["id"], 2)
        self.assertEqual(self.create_task("three")["id"], 3)

    # -- PATCH /api/tasks/<id> ------------------------------------------------

    def test_patch_toggles_done(self):
        created = self.create_task("one")

        code, headers, body = self.request(
            "PATCH", "/api/tasks/%d" % created["id"], payload={"done": True}
        )
        self.assertEqual(code, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(
            json.loads(body),
            {"task": {"id": created["id"], "title": "one", "done": True}},
        )

        code, _, body = self.request(
            "PATCH", "/api/tasks/%d" % created["id"], payload={"done": False}
        )
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)["task"]["done"])

        self.assert_json_response(
            self.request("GET", "/api/tasks"),
            200,
            {"tasks": [{"id": created["id"], "title": "one", "done": False}]},
        )

    def test_patch_unknown_id_returns_404(self):
        self.assert_json_response(
            self.request("PATCH", "/api/tasks/999", payload={"done": True}),
            404,
            {"error": "task not found"},
        )

    def test_patch_invalid_done_value_returns_400(self):
        task = self.create_task("one")
        for payload in ({}, {"done": "true"}, {"done": 1}, {"done": None}):
            with self.subTest(payload=payload):
                code, _, body = self.request(
                    "PATCH", "/api/tasks/%d" % task["id"], payload=payload
                )
                self.assertEqual(code, 400, body)
                self.assertIn("error", json.loads(body))
        # A failed PATCH must not mutate the task.
        self.assert_json_response(
            self.request("GET", "/api/tasks"),
            200,
            {"tasks": [task]},
        )

    def test_patch_invalid_json_returns_400(self):
        task = self.create_task("one")
        code, _, body = self.request(
            "PATCH", "/api/tasks/%d" % task["id"], raw=b"nope"
        )
        self.assertEqual(code, 400, body)
        self.assertIn("error", json.loads(body))

    def test_patch_non_numeric_id_returns_404(self):
        for path in ("/api/tasks/abc", "/api/tasks/", "/api/tasks/1.5", "/api/tasks/-1"):
            with self.subTest(path=path):
                code, _, body = self.request("PATCH", path, payload={"done": True})
                self.assertEqual(code, 404, body)
                self.assertIn("error", json.loads(body))

    # -- routing and static files --------------------------------------------

    def test_unknown_routes_return_json_404(self):
        for method, path in (
            ("GET", "/api/nope"),
            ("GET", "/tasks"),
            ("POST", "/api/task"),
        ):
            with self.subTest(method=method, path=path):
                code, headers, body = self.request(method, path)
                self.assertEqual(code, 404, body)
                self.assertIn("application/json", headers.get("Content-Type", ""))
                self.assertEqual(json.loads(body), {"error": "not found"})

    def test_static_files_are_served(self):
        expected_types = {
            "/": ("index.html", "text/html"),
            "/index.html": ("index.html", "text/html"),
            "/app.js": ("app.js", "javascript"),
            "/styles.css": ("styles.css", "text/css"),
            "/config.js": ("config.js", "javascript"),
        }
        for path, (filename, content_type) in expected_types.items():
            with self.subTest(path=path):
                code, headers, body = self.request("GET", path)
                self.assertEqual(code, 200)
                self.assertIn(content_type, headers.get("Content-Type", ""))
                self.assertEqual(body, self.frontend_files[filename])

    def test_missing_static_file_returns_404(self):
        (self.frontend / "styles.css").unlink()
        self.assert_json_response(
            self.request("GET", "/styles.css"), 404, {"error": "not found"}
        )

    def test_head_request_has_no_body(self):
        code, headers, body = self.request("HEAD", "/api/tasks")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers.get("Content-Length", "0")), 0)


class CommandLineTests(unittest.TestCase):
    def test_print_port_serves_until_terminated(self):
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "backend" / "app.py"),
                "--port",
                "0",
                "--print-port",
            ],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stdout is not None
            ready, _, _ = select.select([process.stdout], [], [], 15)
            self.assertTrue(ready, "server did not print a port within 15s")
            line = process.stdout.readline().decode("utf-8").strip()
            self.assertTrue(line.isdigit(), "expected a bare port number, got %r" % line)
            port = int(line)

            with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/tasks" % port, timeout=5
            ) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(json.loads(resp.read()), {"tasks": []})
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


if __name__ == "__main__":
    unittest.main()
