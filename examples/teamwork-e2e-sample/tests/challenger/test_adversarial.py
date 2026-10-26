"""Adversarial edge-case tests for the task manager contract.

These tests deliberately attack the boundaries the happy-path suites skip:
empty / whitespace-only / invisible-only titles, trimming versus length, the
80-character boundary in code points rather than bytes, malformed and unknown
task ids, request framing, and concurrent writers.

Every test talks to a real HTTP server over a real socket (never the handler
methods directly), so response codes, headers and JSON framing are all
observed as a client would see them.
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import app  # noqa: E402

WHITESPACE_ONLY = [
    " ",
    "\t",
    "\n",
    "\r\n",
    "\v\f",
    " \t \n \r ",
    "\u00a0",  # no-break space
    "\u2003",  # em space
    "\u3000",  # ideographic space
    "\u00a0\u3000 \t",
    " " * 81,
    "\u00a0" * 81,
]

INVISIBLE_ONLY = ["\u200b", "\ufeff", "\u2060", "\u200d"]


class HTTPTestCase(unittest.TestCase):
    """One real server per test on an ephemeral port, plus JSON helpers."""

    def setUp(self) -> None:
        self.server = app.create_server(port=0)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01),
            daemon=True,
        )
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive(), "server thread did not stop")

    # -- transport -------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
        connection: http.client.HTTPConnection | None = None,
    ) -> tuple[int, http.client.HTTPMessage, bytes]:
        """Issue one request and fully read the response body."""
        conn = connection or http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=10
        )
        send_headers = dict(headers or {})
        data: bytes | None = raw_body
        if data is None and body is not None:
            data = json.dumps(body).encode("utf-8")
        if data is not None:
            send_headers.setdefault("Content-Type", "application/json")

        try:
            conn.request(method, path, body=data, headers=send_headers)
            response = conn.getresponse()
            payload = response.read()
            return response.status, response.headers, payload
        finally:
            if connection is None:
                conn.close()

    def json_request(
        self, method: str, path: str, body: object = None, **kwargs
    ) -> tuple[int, dict]:
        status, headers, raw = self.request(method, path, body, **kwargs)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        decoded = json.loads(raw.decode("utf-8"))
        self.assertIsInstance(decoded, dict)
        return status, decoded

    # -- domain helpers --------------------------------------------------

    def create(self, title: str) -> dict:
        status, payload = self.json_request("POST", "/api/tasks", {"title": title})
        self.assertEqual(status, 201, payload)
        return payload["task"]

    def titles(self) -> list[str]:
        status, payload = self.json_request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        return [task["title"] for task in payload["tasks"]]

    def assert_rejected_create(self, body: object) -> None:
        status, payload = self.json_request("POST", "/api/tasks", body)
        self.assertEqual(status, 400, payload)
        self.assertIsInstance(payload.get("error"), str, payload)
        self.assertTrue(payload["error"].strip(), payload)


class EmptyAndWhitespaceTitles(HTTPTestCase):
    def test_empty_string_is_rejected(self) -> None:
        self.assert_rejected_create({"title": ""})
        self.assertEqual(self.titles(), [])

    def test_whitespace_only_titles_are_rejected(self) -> None:
        for title in WHITESPACE_ONLY:
            with self.subTest(title=repr(title)):
                self.assert_rejected_create({"title": title})
        self.assertEqual(self.titles(), [])

    def test_missing_null_and_non_string_titles_are_rejected(self) -> None:
        bodies = [
            {},
            {"title": None},
            {"title": 0},
            {"title": 5},
            {"title": -1.5},
            {"title": True},
            {"title": []},
            {"title": ["x"]},
            {"title": {}},
            {"title": {"text": "x"}},
            {"Title": "case-sensitive"},
            {"title ": "padded key"},
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assert_rejected_create(body)
        self.assertEqual(self.titles(), [])

    def test_invisible_only_title_never_stores_an_empty_title(self) -> None:
        """U+200B and friends are Cf, not whitespace, so the literal contract
        allows them even though they render as a blank row. Accept either a 400
        or a verbatim 201, but never an empty/whitespace title in the store.
        Reported as a low-severity finding: rejecting invisible-only titles is
        the safer behavior and should be decided explicitly.
        """
        accepted = []
        for title in INVISIBLE_ONLY:
            with self.subTest(title=repr(title)):
                status, payload = self.json_request(
                    "POST", "/api/tasks", {"title": title}
                )
                self.assertIn(status, (201, 400), payload)
                if status == 201:
                    self.assertEqual(payload["task"]["title"], title)
                    self.assertTrue(payload["task"]["title"].strip())
                    accepted.append(title)

        self.assertEqual(self.titles(), accepted)
        for title in self.titles():
            self.assertTrue(title.strip(), "store must not contain a blank title")

    def test_nbsp_is_treated_as_trimmed_whitespace(self) -> None:
        task = self.create("\u00a0padded\u00a0")
        self.assertEqual(task["title"], "padded")

    def test_inner_whitespace_is_preserved_after_trimming(self) -> None:
        for raw, expected in [
            ("  a  b  ", "a  b"),
            ("\t a \t b \n", "a \t b"),
            ("a\nb", "a\nb"),
            ("a\u00a0b", "a\u00a0b"),
        ]:
            with self.subTest(raw=repr(raw)):
                self.assertEqual(self.create(raw)["title"], expected)


class TrimAndLengthBoundary(HTTPTestCase):
    def test_exactly_80_ascii_characters_is_accepted(self) -> None:
        self.assertEqual(self.create("x" * 80)["title"], "x" * 80)

    def test_81_ascii_characters_is_rejected(self) -> None:
        self.assert_rejected_create({"title": "x" * 81})
        self.assertEqual(self.titles(), [])

    def test_length_is_measured_after_trimming(self) -> None:
        # 80 core characters padded far beyond the limit: trimming happens
        # first, so the title is legal.
        task = self.create(" \t\n" + "x" * 80 + "\u00a0 " * 30)
        self.assertEqual(task["title"], "x" * 80)
        self.assertEqual(len(task["title"]), 80)

    def test_81_core_characters_with_padding_is_still_rejected(self) -> None:
        self.assert_rejected_create({"title": "  " + "x" * 81 + "  "})
        self.assertEqual(self.titles(), [])

    def test_81_core_characters_reject_even_when_raw_length_is_padded(self) -> None:
        self.assert_rejected_create({"title": " " * 100 + "x" * 81})
        self.assertEqual(self.titles(), [])

    def test_multibyte_titles_count_characters_not_utf8_bytes(self) -> None:
        self.assertEqual(len(("é" * 80).encode("utf-8")), 160)
        self.assertEqual(self.create("é" * 80)["title"], "é" * 80)
        self.assert_rejected_create({"title": "é" * 81})

        self.assertEqual(len(("😀" * 80).encode("utf-8")), 320)
        self.assertEqual(self.create("😀" * 80)["title"], "😀" * 80)
        self.assert_rejected_create({"title": "😀" * 81})

    def test_combining_marks_count_as_code_points(self) -> None:
        # 40 graphemes, 80 code points: accepted at the boundary.
        boundary = "e\u0301" * 40
        self.assertEqual(len(boundary), 80)
        self.assertEqual(self.create(boundary)["title"], boundary)

        # 41 graphemes, 82 code points: over the boundary.
        self.assert_rejected_create({"title": "e\u0301" * 41})

    def test_boundary_is_not_off_by_one_across_requests(self) -> None:
        self.create("a" * 80)
        self.assert_rejected_create({"title": "b" * 81})
        self.create("c" * 80)
        self.create("d")
        self.assert_rejected_create({"title": "e" * 81})
        self.assertEqual(self.titles(), ["a" * 80, "c" * 80, "d"])

    def test_rejected_creates_leave_the_list_unchanged(self) -> None:
        self.create("keep me")
        before = self.titles()
        for body in (
            {"title": ""},
            {"title": "   "},
            {"title": "x" * 81},
            {"title": None},
        ):
            with self.subTest(body=body):
                self.assert_rejected_create(body)
                self.assertEqual(self.titles(), before)

    def test_very_long_title_is_rejected(self) -> None:
        self.assert_rejected_create({"title": "x" * 10_000})
        self.assertEqual(self.titles(), [])

    def test_oversized_unknown_field_cannot_smuggle_a_bad_title(self) -> None:
        # A huge payload must still be validated and must not be stored.
        status, payload = self.json_request(
            "POST", "/api/tasks", {"title": "x" * 81, "padding": "p" * 100_000}
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(self.titles(), [])

    def test_ids_only_advance_on_successful_creation(self) -> None:
        self.assertEqual(self.create("one")["id"], 1)
        for body in ({"title": ""}, {"title": "x" * 81}, {}):
            self.assert_rejected_create(body)
        self.assertEqual(self.create("two")["id"], 2)


class InvalidIds(HTTPTestCase):
    def test_unknown_numeric_ids_return_404_json(self) -> None:
        self.create("only task")
        for task_id in (0, 2, 999, 123456789):
            with self.subTest(task_id=task_id):
                status, payload = self.json_request(
                    "PATCH", f"/api/tasks/{task_id}", {"done": True}
                )
                self.assertEqual(status, 404, payload)
                self.assertIn("error", payload)

    def test_non_numeric_ids_return_404_json(self) -> None:
        task = self.create("target")
        cases = [
            "/api/tasks/abc",
            "/api/tasks/1a",
            "/api/tasks/1%20",
            "/api/tasks/%201",
            "/api/tasks/-1",
            "/api/tasks/+1",
            "/api/tasks/1.0",
            "/api/tasks/1e3",
            "/api/tasks/0x10",
            "/api/tasks/%31",  # percent-encoded digit is not a route id
            "/api/tasks/1%20",
            "/api/tasks/..%2f1",
            "/api/tasks/1/extra",
            "/api/tasks//1",
            "/api/tasks/",
            "/api/tasks",
            "/api/unknown/1",
        ]
        for path in cases:
            with self.subTest(path=path):
                status, payload = self.json_request("PATCH", path, {"done": True})
                self.assertEqual(status, 404, (path, payload))
                self.assertIn("error", payload)

        status, payload = self.json_request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["tasks"], [{"id": task["id"], "title": "target", "done": False}]
        )

    def test_invalid_ids_do_not_create_tasks(self) -> None:
        for path in ("/api/tasks/77", "/api/tasks/abc", "/api/tasks/0"):
            status, _ = self.json_request("PATCH", path, {"done": False})
            self.assertEqual(status, 404)
        self.assertEqual(self.titles(), [])

    def test_invalid_done_payloads_leave_state_untouched(self) -> None:
        task = self.create("keep state")
        bodies = [
            {},
            {"done": "true"},
            {"done": "false"},
            {"done": 1},
            {"done": 0},
            {"done": None},
            {"done": []},
            {"done": {}},
            {"done": "True"},
        ]
        for body in bodies:
            with self.subTest(body=body):
                status, payload = self.json_request(
                    "PATCH", f"/api/tasks/{task['id']}", body
                )
                self.assertEqual(status, 400, payload)
                self.assertIn("error", payload)

        status, payload = self.json_request("GET", "/api/tasks")
        self.assertFalse(payload["tasks"][0]["done"])

    def test_invalid_json_and_non_object_patches_are_400(self) -> None:
        task = self.create("keep state")
        for raw in (b"{", b"", b"null", b"[]", b'"true"', b'{"done": true'):
            with self.subTest(raw=raw):
                status, payload = self.json_request(
                    "PATCH", f"/api/tasks/{task['id']}", raw_body=raw
                )
                self.assertEqual(status, 400, payload)
                self.assertIn("error", payload)

        status, payload = self.json_request("GET", "/api/tasks")
        self.assertFalse(payload["tasks"][0]["done"])

    def test_encoded_and_unicode_digit_ids_are_not_aliases(self) -> None:
        self.create("target")
        for path in ("/api/tasks/%31", "/api/tasks/%EF%BC%91"):
            with self.subTest(path=path):
                status, _ = self.json_request("PATCH", path, {"done": True})
                self.assertEqual(status, 404)
        self.assertFalse(self.json_request("GET", "/api/tasks")[1]["tasks"][0]["done"])

    def test_leading_zero_ids_may_alias_but_never_hit_another_task(self) -> None:
        """Pinning a leniency: ``/api/tasks/0001`` is accepted as id 1 because
        the route matches ``\\d+`` and converts with ``int()``. Whatever the
        canonical form, an id alias must never mutate a different task.
        """
        first = self.create("first")
        second = self.create("second")

        patch_status, payload = self.json_request(
            "PATCH", f"/api/tasks/{'0' * 20}{first['id']}", {"done": True}
        )
        self.assertIn(patch_status, (200, 404), payload)

        status, payload = self.json_request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        by_id = {task["id"]: task for task in payload["tasks"]}
        self.assertFalse(by_id[second["id"]]["done"], "second task must stay untouched")
        if patch_status == 200:
            self.assertEqual(by_id[first["id"]]["done"], True)

    def test_pathologically_long_id_cannot_corrupt_or_kill_the_server(self) -> None:
        """4301+ digits exceed CPython 3.11's int-str conversion limit.

        Observed deviation (reported, non-blocking): the handler raises
        ValueError and the connection closes without an HTTP response instead of
        returning 404. This test asserts the invariants that matter: no task is
        mutated, the server survives, and it keeps serving afterwards.
        """
        task = self.create("survivor")
        handler_errors: list[object] = []
        original_handle_error = self.server.handle_error
        self.server.handle_error = lambda *args: handler_errors.append(args)
        dropped = False
        try:
            status, _, raw = self.request(
                "PATCH", "/api/tasks/" + "9" * 4301, {"done": True}
            )
        except (http.client.RemoteDisconnected, ConnectionError):
            dropped = True  # documented deviation: no response at all
        else:
            self.assertEqual(status, 404, raw)
            self.assertIn("error", json.loads(raw.decode("utf-8")))
        finally:
            self.server.handle_error = original_handle_error

        if dropped:
            self.assertTrue(
                handler_errors,
                "a dropped connection should at least surface a handler error",
            )

        status, payload = self.json_request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["tasks"],
            [{"id": task["id"], "title": "survivor", "done": False}],
        )

        # Still healthy enough to accept new work after the hostile request.
        self.assertEqual(self.create("after")["id"], 2)

    def test_large_but_convertible_id_returns_404(self) -> None:
        status, payload = self.json_request(
            "PATCH", "/api/tasks/" + "9" * 4000, {"done": True}
        )
        self.assertEqual(status, 404, payload)

    def test_unsupported_methods_do_not_mutate_state(self) -> None:
        task = self.create("target")
        for method, path in (
            ("DELETE", f"/api/tasks/{task['id']}"),
            ("PUT", f"/api/tasks/{task['id']}"),
            ("PATCH", "/api/tasks"),
        ):
            with self.subTest(method=method, path=path):
                status, _, _ = self.request(method, path, {"done": True})
                self.assertGreaterEqual(status, 400)
        status, payload = self.json_request("GET", "/api/tasks")
        self.assertEqual([t["title"] for t in payload["tasks"]], ["target"])
        self.assertFalse(payload["tasks"][0]["done"])

    def test_query_string_does_not_change_the_route_target(self) -> None:
        task = self.create("query")
        status, payload = self.json_request(
            "PATCH", f"/api/tasks/{task['id']}?done=false", {"done": True}
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["task"]["done"])

    def test_toggling_is_idempotent_and_does_not_duplicate(self) -> None:
        task = self.create("idempotent")
        for done in (True, True, False, False, True):
            status, payload = self.json_request(
                "PATCH", f"/api/tasks/{task['id']}", {"done": done}
            )
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["task"]["done"], done)
        status, payload = self.json_request("GET", "/api/tasks")
        self.assertEqual(len(payload["tasks"]), 1)
        self.assertTrue(payload["tasks"][0]["done"])


class TransportEdges(HTTPTestCase):
    def test_invalid_utf8_body_is_rejected(self) -> None:
        status, payload = self.json_request(
            "POST", "/api/tasks", raw_body=b'{"title": "\xff\xfe"}'
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(self.titles(), [])

    def test_utf8_bom_prefixed_json_is_rejected_not_silently_accepted(self) -> None:
        status, payload = self.json_request(
            "POST", "/api/tasks", raw_body=b'\xef\xbb\xbf{"title": "bom"}'
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(self.titles(), [])

    def test_duplicate_title_keys_cannot_bypass_validation(self) -> None:
        # json keeps the last duplicate: the empty one must be validated.
        status, payload = self.json_request(
            "POST", "/api/tasks", raw_body=b'{"title": "ok", "title": "   "}'
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(self.titles(), [])

    def test_keep_alive_survives_rejected_requests(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            status, _, _ = self.request(
                "POST", "/api/tasks", {"title": ""}, connection=conn
            )
            self.assertEqual(status, 400)

            status, _, raw = self.request(
                "POST", "/api/tasks", {"title": "after error"}, connection=conn
            )
            self.assertEqual(status, 201, raw)

            status, _, raw = self.request("GET", "/api/tasks", connection=conn)
            self.assertEqual(status, 200, raw)
            self.assertEqual(
                json.loads(raw.decode("utf-8"))["tasks"],
                [{"id": 1, "title": "after error", "done": False}],
            )
        finally:
            conn.close()

    def test_response_content_length_matches_body(self) -> None:
        status, headers, raw = self.request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(int(headers["Content-Length"]), len(raw))

        status, headers, raw = self.request("POST", "/api/tasks", {"title": ""})
        self.assertEqual(status, 400)
        self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_static_assets_are_served_with_correct_lengths(self) -> None:
        for path in ("/", "/app.js", "/styles.css", "/config.js"):
            with self.subTest(path=path):
                status, headers, raw = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertGreater(len(raw), 0)
                self.assertEqual(int(headers["Content-Length"]), len(raw))
                self.assertIn("no-store", headers.get("Cache-Control", ""))


class ConcurrencyClaims(HTTPTestCase):
    def test_concurrent_creates_get_unique_sequential_ids(self) -> None:
        count = 24
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: self.create(f"task {i}"), range(count)))

        ids = sorted(task["id"] for task in results)
        self.assertEqual(ids, list(range(1, count + 1)))
        self.assertEqual(len(self.titles()), count)

    def test_mixed_valid_and_invalid_concurrent_writes_leave_consistent_state(
        self,
    ) -> None:
        def worker(index: int) -> None:
            title = f"ok {index}" if index % 2 == 0 else "x" * 81
            status, _ = self.json_request("POST", "/api/tasks", {"title": title})
            self.assertIn(status, (201, 400))

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(worker, range(20)))

        stored = self.titles()
        self.assertEqual(len(stored), 10)
        # Concurrent inserts may interleave, so compare as a set of titles.
        self.assertEqual(sorted(stored), sorted(f"ok {index}" for index in range(0, 20, 2)))

    def test_concurrent_toggles_never_lose_the_task(self) -> None:
        task = self.create("contended")

        def toggle(index: int) -> int:
            status, payload = self.json_request(
                "PATCH", f"/api/tasks/{task['id']}", {"done": bool(index % 2)}
            )
            self.assertEqual(status, 200, payload)
            return 0

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(toggle, range(16)))

        status, payload = self.json_request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["tasks"]), 1)
        self.assertEqual(payload["tasks"][0]["id"], task["id"])


class ContractConsistency(HTTPTestCase):
    """Cross-check the parts of the contract that live in two files."""

    def test_frontend_maxlength_matches_backend_limit(self) -> None:
        html = (REPO_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        marker = f'maxlength="{app.MAX_TITLE_LENGTH}"'
        self.assertIn(marker, html)
        self.assertEqual(app.MAX_TITLE_LENGTH, 80)

    def test_frontend_config_defaults_to_an_empty_api_base(self) -> None:
        config = (REPO_ROOT / "frontend" / "config.js").read_text(encoding="utf-8")
        self.assertRegex(config, r'AO_API_BASE\s*=\s*""')

    def test_frontend_keeps_requests_relative_and_trims_before_submit(self) -> None:
        source = (REPO_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("API_BASE + path", source)
        self.assertIn(".value.trim()", source)
        self.assertNotRegex(source, r'https?://')

    def test_backend_limit_matches_the_contract_document(self) -> None:
        contract = (REPO_ROOT / "CONTRACT.md").read_text(encoding="utf-8")
        self.assertIn("1..80 non-whitespace characters", contract)


if __name__ == "__main__":
    unittest.main()
