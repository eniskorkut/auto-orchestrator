from __future__ import annotations

import http.client
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tools.dashboard import Catalog, DashboardServer, Store
from tools.event_stream import StreamError, StreamHub, StreamManager, parse_last_event_id


def write_run(root: Path, run_id: str, events: list[dict]) -> Path:
    path = root / ".auto-orchestrator" / "runs" / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


class EventClient:
    """Small SSE reader that never advances past a bound message."""

    def __init__(self, host: str, port: int, path: str, last_event_id=None):
        self.connection = http.client.HTTPConnection(host, port, timeout=8)
        headers = {}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        self.connection.request("GET", path, headers=headers)
        self.response = self.connection.getresponse()

    def read_message(self):
        fields = {}
        while True:
            line = self.response.readline()
            if not line:
                return None
            text = line.decode("utf-8").rstrip("\r\n")
            if not text:
                if fields:
                    return fields
                continue
            if text.startswith(":"):
                return {"comment": text[1:].strip()}
            key, _, value = text.partition(":")
            if value.startswith(" "):
                value = value[1:]
            fields[key] = value

    def next_event(self):
        while True:
            message = self.read_message()
            if message is None:
                return None
            if "comment" in message or "event" not in message:
                continue
            return {
                "id": int(message["id"]),
                "event": message["event"],
                "data": json.loads(message["data"]),
            }

    def close(self):
        self.connection.close()


class SSETestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        started = time.time() - 5
        write_run(self.root, "first", [
            {"event": "start", "platform": "opencode", "time": started},
        ])
        self.catalog = Catalog.from_store(Store(self.root))
        self.project_id = self.catalog.default_id
        self.server = DashboardServer(("127.0.0.1", 0), self.catalog.default_store,
                                      catalog=self.catalog)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]
        self.path = f"/api/projects/{self.project_id}/events"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def client(self, last_event_id=None):
        client = EventClient(self.host, self.port, self.path, last_event_id)
        self.addCleanup(client.close)
        return client

    def request(self, path: str, headers: dict | None = None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response, response.read()
        finally:
            connection.close()

    def test_snapshot_headers_and_server_stays_responsive(self):
        client = self.client()
        self.assertEqual(client.response.status, 200)
        self.assertEqual(client.response.getheader("Content-Type"), "text/event-stream")
        self.assertEqual(client.response.getheader("Cache-Control"), "no-cache")
        self.assertIn("default-src 'self'",
                      client.response.getheader("Content-Security-Policy"))
        first = client.read_message()
        self.assertEqual(first, {"retry": "2500"})
        snapshot = client.next_event()
        self.assertEqual(snapshot["event"], "snapshot")
        self.assertEqual(snapshot["id"], 1)
        self.assertEqual(snapshot["data"]["type"], "snapshot")
        self.assertEqual(snapshot["data"]["project_id"], self.project_id)
        self.assertEqual(snapshot["data"]["sequence_id"], 1)
        self.assertEqual(len(snapshot["data"]["state"]["runs"]), 1)
        self.assertNotIn("reconnect", snapshot["data"])

        # The SSE connection holds one handler thread, not the accept loop.
        response, body = self.request("/api/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        response, body = self.request(f"/api/projects/{self.project_id}/state")
        self.assertEqual(response.status, 200)
        state = json.loads(body)
        self.assertEqual(state["fingerprint"],
                         snapshot["data"]["state"]["fingerprint"])
        etag = response.getheader("ETag")
        response, body = self.request(f"/api/projects/{self.project_id}/state",
                                      {"If-None-Match": etag, "Accept-Encoding": "gzip"})
        self.assertEqual(response.status, 304)
        self.assertEqual(body, b"")

    def test_update_after_file_change_and_last_event_id_replay(self):
        client = self.client()
        snapshot = client.next_event()
        self.assertEqual(snapshot["event"], "snapshot")
        write_run(self.root, "second", [
            {"event": "start", "platform": "codex", "time": time.time() - 1},
        ])
        update = client.next_event()
        self.assertEqual(update["event"], "update")
        self.assertGreater(update["id"], snapshot["id"])
        self.assertEqual(update["data"]["type"], "update")
        self.assertEqual(update["data"]["sequence_id"], update["id"])
        self.assertEqual(len(update["data"]["state"]["runs"]), 2)
        newest = update["data"]["state"]["runs"][0]
        self.assertEqual(newest["id"], "second")
        self.assertEqual(newest["status"], "open")
        self.assertNotEqual(newest["status"], "success")
        self.assertNotIn("finish", newest)

        replay = self.client(last_event_id=snapshot["id"])
        replayed = replay.next_event()
        self.assertEqual(replayed["event"], "update")
        self.assertEqual(replayed["id"], update["id"])
        self.assertEqual(replayed["data"]["state"]["fingerprint"],
                         update["data"]["state"]["fingerprint"])

        stale = self.client(last_event_id=999999)
        fresh = stale.next_event()
        self.assertEqual(fresh["event"], "snapshot")
        self.assertTrue(fresh["data"]["reconnect"])
        self.assertEqual(fresh["data"]["state"]["runs"], update["data"]["state"]["runs"])

        malformed = self.client(last_event_id="not-a-sequence")
        recovered = malformed.next_event()
        self.assertEqual(recovered["event"], "snapshot")
        self.assertTrue(recovered["data"]["reconnect"])

    def test_closing_a_stream_does_not_break_the_server(self):
        client = self.client()
        self.assertEqual(client.next_event()["event"], "snapshot")
        client.close()
        response, body = self.request("/api/health")
        self.assertEqual(response.status, 200)
        second = self.client()
        self.assertEqual(second.next_event()["event"], "snapshot")

    def test_unknown_project_is_404_and_writes_are_rejected(self):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request("GET", "/api/projects/p_" + "0" * 16 + "/events")
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
        finally:
            connection.close()
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request("POST", self.path)
            response = connection.getresponse()
            self.assertEqual(response.status, 405)
            response.read()
        finally:
            connection.close()


class StreamHubTests(unittest.TestCase):
    def test_bounded_buffer_replays_or_sends_reconnect_snapshot(self):
        state = {"n": 0}

        def provider():
            return {"fingerprint": f"fp-{state['n']}", "runs": []}

        hub = StreamHub("p_" + "2" * 16, provider, buffer_size=2,
                        poll_interval=0.02, heartbeat=0.1)
        hub.start()
        subscriber = hub.subscribe()
        self.assertEqual(subscriber.initial[0].event, "snapshot")
        for target in (2, 3, 4):
            state["n"] = target - 1
            self.assertTrue(hub.wait_for_sequence(target))
        self.assertEqual(hub.buffered, 2)

        replay = hub.subscribe(3)
        self.assertEqual([event.sequence for event in replay.initial], [4])
        self.assertEqual(replay.initial[0].event, "update")

        fallback = hub.subscribe(1)
        self.assertEqual(fallback.initial[0].event, "snapshot")
        self.assertTrue(fallback.initial[0].data["reconnect"])
        self.assertEqual(fallback.initial[0].sequence, 4)

        current = hub.subscribe(4)
        self.assertEqual(current.initial, [])

        malformed = hub.subscribe("nope")
        self.assertEqual(malformed.initial[0].event, "snapshot")
        self.assertTrue(malformed.initial[0].data["reconnect"])

        absent = hub.subscribe(None)
        self.assertEqual(absent.initial[0].event, "snapshot")
        self.assertNotIn("reconnect", absent.initial[0].data)
        hub.close()

    def test_no_update_without_fingerprint_change_and_tabs_agree(self):
        state = {"n": 0}

        def provider():
            return {"fingerprint": f"fp-{state['n']}", "runs": []}

        hub = StreamHub("p_" + "3" * 16, provider, poll_interval=0.02,
                        heartbeat=0.1)
        hub.start()
        first = hub.subscribe()
        second = hub.subscribe()
        try:
            with self.assertRaises(queue.Empty):
                first.next(0.15)
            with self.assertRaises(queue.Empty):
                second.next(0.15)
            state["n"] = 1
            self.assertTrue(hub.wait_for_sequence(2))
            one = first.next(1.0)
            two = second.next(1.0)
            self.assertIsNotNone(one)
            self.assertIsNotNone(two)
            self.assertEqual(one.sequence, two.sequence)
            self.assertEqual(one.data, two.data)
            self.assertEqual(one.event, "update")
        finally:
            first.close()
            second.close()
            hub.close()


class StreamManagerLimitTests(unittest.TestCase):
    def test_hub_cap_is_enforced_when_all_hubs_have_subscribers(self):
        manager = StreamManager(
            lambda project_id: (lambda: {"fingerprint": "fp", "project_id": project_id}),
            max_hubs=1, heartbeat=10, poll_interval=10)
        first_id = "p_" + "1" * 16
        second_id = "p_" + "2" * 16
        first = manager.hub(first_id)
        subscriber = first.subscribe()
        try:
            with self.assertRaises(StreamError):
                manager.hub(second_id)
        finally:
            subscriber.close()
            manager.close()
        with self.assertRaises(StreamError):
            manager.hub(first_id)


class LastEventIdTests(unittest.TestCase):
    def test_huge_or_malformed_ids_fall_back_to_a_fresh_snapshot(self):
        self.assertIsNone(parse_last_event_id("9" * 5000))
        self.assertIsNone(parse_last_event_id(""))
        self.assertIsNone(parse_last_event_id("abc"))
        self.assertIsNone(parse_last_event_id(True))
        self.assertEqual(parse_last_event_id("12"), 12)


if __name__ == "__main__":
    unittest.main()
