"""Cross-layer checks against the real repository tree.

The unit suite serves temp frontend fixtures. These tests instead start the
backend with its default ``frontend/`` directory, so a break in the wiring the
browser E2E depends on (static routes, config.js, API base) fails here too,
without needing Playwright.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import app  # noqa: E402

# Selectors and globals the browser E2E (e2e/check.cjs) relies on.
REQUIRED_TEST_IDS = (
    "task-form",
    "task-input",
    "task-submit",
    "error",
    "status",
    "task-list",
    "empty",
)


class RealFrontendIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # No frontend_dir override: serve the repository's real frontend/.
        cls.server = app.create_server(port=0)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.base = "http://%s:%d" % (host, port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, payload=None):
        data = None
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

    def test_index_html_exposes_e2e_selectors(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        html = body.decode("utf-8")
        for test_id in REQUIRED_TEST_IDS:
            with self.subTest(test_id=test_id):
                self.assertIn('data-testid="%s"' % test_id, html)

    def test_config_js_sets_same_origin_api_base(self):
        status, headers, body = self.request("GET", "/config.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        self.assertIn('window.AO_API_BASE = ""', body.decode("utf-8"))

    def test_frontend_assets_are_served(self):
        for path in ("/app.js", "/styles.css", "/config.js", "/index.html"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(body, "asset %s is empty" % path)

    def test_create_list_toggle_round_trip(self):
        status, _, body = self.request(
            "POST", "/api/tasks", payload={"title": "  ship it  "}
        )
        self.assertEqual(status, 201)
        task = json.loads(body)["task"]
        self.assertEqual(task, {"id": 1, "title": "ship it", "done": False})

        status, _, body = self.request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"tasks": [task]})

        status, _, body = self.request(
            "PATCH", "/api/tasks/1", payload={"done": True}
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["task"]["done"])

        status, _, body = self.request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["tasks"][0]["done"])

    def test_invalid_title_is_rejected(self):
        status, _, body = self.request("POST", "/api/tasks", payload={"title": "   "})
        self.assertEqual(status, 400)
        self.assertTrue(json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
