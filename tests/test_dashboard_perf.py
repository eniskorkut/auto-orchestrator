from __future__ import annotations

import gzip
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
from tools.dashboard import DashboardServer, Store


def write_run(root: Path, run_id: str, events: list[dict]) -> Path:
    path = root / ".auto-orchestrator" / "runs" / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


def simple_run(started: float, status: str = "success") -> list[dict]:
    events = [{"event": "start", "platform": "opencode", "time": started}]
    if status:
        events.append({"event": "finish", "status": status, "verified": False,
                       "time": started + 2})
    return events


class DashboardPerfDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_detail_cache_is_reused_until_the_log_changes(self):
        started = time.time() - 60
        path = write_run(self.root, "cached-run",
                         [{"event": "start", "platform": "opencode", "time": started}])
        store = Store(self.root)
        first = store.detail("cached-run")
        self.assertEqual(first["run"]["status"], "open")
        self.assertIsNone(first["finish"])

        with mock.patch.object(dashboard_module, "_read_jsonl",
                               side_effect=AssertionError("cached detail was re-read")):
            second = store.detail("cached-run")
        self.assertEqual(second["run"]["fingerprint"], first["run"]["fingerprint"])
        self.assertEqual(second["run"]["status"], "open")

        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "finish", "status": "success",
                                     "verified": False, "time": started + 5}) + "\n")
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

        third = store.detail("cached-run")
        self.assertEqual(third["run"]["status"], "success")
        self.assertEqual(third["finish"]["status"], "success")
        self.assertNotEqual(third["run"]["fingerprint"], first["run"]["fingerprint"])

    def test_state_parses_uncached_runs_in_parallel_newest_first(self):
        base = time.time() - 300
        for index in (3, 0, 5, 2, 4, 1):
            write_run(self.root, f"run-{index}", simple_run(base + index * 10))
        store = Store(self.root)
        real_summary = dashboard_module._summary
        threads: set[int] = set()
        calls: list[str] = []
        guard = threading.Lock()

        def wrapped(root, run_id, path):
            with guard:
                threads.add(threading.get_ident())
                calls.append(run_id)
            time.sleep(0.05)
            return real_summary(root, run_id, path)

        with mock.patch.object(dashboard_module, "STATE_WORKERS", 4), \
                mock.patch.object(dashboard_module, "_summary", wrapped):
            state = store.state()

        self.assertEqual(sorted(calls), sorted(f"run-{index}" for index in range(6)))
        self.assertEqual([run["id"] for run in state["runs"]],
                         [f"run-{index}" for index in range(5, -1, -1)])
        self.assertEqual(state["overview"]["runs"], 6)
        self.assertGreater(len(threads), 1)

    def test_supervisor_and_worktree_snapshots_are_cached(self):
        store = Store(self.root)
        supervisor = {"available": True, "runs": []}
        worktrees = [{"task": "demo", "dirty_files": 1}]
        with mock.patch.object(dashboard_module, "_supervisor", return_value=supervisor) as snapshots, \
                mock.patch.object(dashboard_module, "_worktrees", return_value=worktrees) as trees:
            first = store.state()
            second = store.state()
            self.assertEqual(snapshots.call_count, 1)
            self.assertEqual(trees.call_count, 1)
            self.assertEqual(first["supervisor"], second["supervisor"])
            self.assertEqual(second["worktrees"], worktrees)

            database = self.root / ".auto-orchestrator" / "supervisor.sqlite3"
            database.parent.mkdir(parents=True, exist_ok=True)
            database.write_bytes(b"not a sqlite database")
            store.state()
            self.assertEqual(snapshots.call_count, 2)

        with mock.patch.object(dashboard_module, "WORKTREE_TTL", 0.0), \
                mock.patch.object(dashboard_module, "_worktrees", return_value=worktrees) as trees:
            store.state()
            store.state()
            self.assertEqual(trees.call_count, 2)


class DashboardPerfHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = DashboardServer(("127.0.0.1", 0), Store(self.root))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, path: str, headers: dict | None = None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response, response.read()
        finally:
            connection.close()

    def tree(self):
        return sorted((str(path.relative_to(self.root)), path.stat().st_size,
                       path.stat().st_mtime_ns) for path in self.root.rglob("*")
                      if path.is_file() and not path.is_symlink())

    def test_state_etag_returns_304_and_changes_invalidate(self):
        started = time.time() - 100
        write_run(self.root, "first", simple_run(started))
        response, body = self.request("/api/state")
        self.assertEqual(response.status, 200)
        state = json.loads(body)
        etag = response.getheader("ETag")
        self.assertTrue(etag.startswith(f'"legacy-{state["fingerprint"]}-'))
        self.assertIn("Accept-Encoding", response.getheader("Vary"))

        response, body = self.request("/api/state", {
            "If-None-Match": etag, "Accept-Encoding": "gzip",
        })
        self.assertEqual(response.status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(response.getheader("ETag"), etag)
        self.assertIsNone(response.getheader("Content-Encoding"))
        self.assertIsNone(response.getheader("Content-Length"))
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")

        response, body = self.request("/api/state",
                                      {"If-None-Match": f'W/{etag}'})
        self.assertEqual(response.status, 304)
        self.assertEqual(body, b"")

        write_run(self.root, "second", simple_run(started + 10))
        response, body = self.request("/api/state", {"If-None-Match": etag})
        self.assertEqual(response.status, 200)
        changed = json.loads(body)
        self.assertEqual(len(changed["runs"]), 2)
        self.assertNotEqual(response.getheader("ETag"), etag)

    def test_gzip_json_and_static_assets_are_cached(self):
        started = time.time() - 100
        write_run(self.root, "gzip-run", simple_run(started))

        response, body = self.request("/api/state", {"Accept-Encoding": "gzip"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Encoding"), "gzip")
        self.assertIn("Accept-Encoding", response.getheader("Vary"))
        self.assertEqual(int(response.getheader("Content-Length")), len(body))
        state = json.loads(gzip.decompress(body))
        self.assertEqual([run["id"] for run in state["runs"]], ["gzip-run"])

        response, plain = self.request("/api/state")
        self.assertIsNone(response.getheader("Content-Encoding"))
        self.assertEqual(json.loads(plain)["fingerprint"], state["fingerprint"])

        with dashboard_module._ASSET_LOCK:
            dashboard_module._ASSET_CACHE.clear()
        original_compress = gzip.compress
        with mock.patch.object(dashboard_module.gzip, "compress",
                               wraps=original_compress) as compress:
            first_response, first_body = self.request(
                "/assets/app.js", {"Accept-Encoding": "gzip"})
            second_response, second_body = self.request(
                "/assets/app.js", {"Accept-Encoding": "gzip"})

        self.assertEqual(first_response.status, 200)
        self.assertEqual(second_response.status, 200)
        self.assertEqual(first_response.getheader("Content-Encoding"), "gzip")
        self.assertIn("Accept-Encoding", first_response.getheader("Vary"))
        self.assertEqual(int(first_response.getheader("Content-Length")), len(first_body))
        self.assertEqual(first_body, second_body)
        self.assertEqual(compress.call_count, 1)
        self.assertTrue(gzip.decompress(first_body).lstrip().startswith(b"(() =>"))

    def test_get_requests_do_not_write_project_files(self):
        started = time.time() - 100
        write_run(self.root, "read-only", simple_run(started))
        before = self.tree()
        self.request("/api/state")
        self.request("/api/state", {"Accept-Encoding": "gzip"})
        self.request("/api/run/read-only", {"Accept-Encoding": "gzip"})
        self.request("/assets/app.js", {"Accept-Encoding": "gzip"})
        self.request("/api/health")
        self.assertEqual(before, self.tree())


if __name__ == "__main__":
    unittest.main()
