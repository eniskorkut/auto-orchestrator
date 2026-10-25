#!/usr/bin/env python3
"""Per-project Server-Sent Events hub for the V7.1 Evidence Console.

Design constraints:
- one monotonic sequence per project, shared by every browser tab;
- a bounded replay buffer (32 events by default);
- an initial snapshot, then updates only when the state fingerprint changes;
- a disconnect/reconnect never invents a finish or completion event;
- no handler thread is owned here: the HTTP handler drains a subscriber queue
  and writes heartbeats, so a slow browser cannot block the server.

Only the Python standard library is used.
"""
from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

BUFFER_SIZE = 32
HEARTBEAT_SECONDS = 15.0
RETRY_MILLISECONDS = 2500
POLL_SECONDS = 1.0
SUBSCRIBER_QUEUE_SIZE = 32
MAX_SUBSCRIBERS = 32
MAX_HUBS = 8
MAX_SEQUENCE = 2 ** 63 - 1

PROJECT_ID = re.compile(r"^p_[0-9a-f]{16}$")

RETRY_FRAME = f"retry: {RETRY_MILLISECONDS}\n\n".encode("ascii")
HEARTBEAT_FRAME = b": heartbeat\n\n"


class StreamError(ValueError):
    pass


def parse_last_event_id(value) -> int | None:
    """Parse a Last-Event-ID header; malformed values force a fresh snapshot."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        value = value.strip()
        if not value or len(value) > 20 or not value.isdigit():
            return None
        try:
            number = int(value)
        except (ValueError, TypeError):
            return None
    else:
        return None
    if not 1 <= number <= MAX_SEQUENCE:
        return None
    return number


def _fingerprint(state: dict) -> str:
    value = state.get("fingerprint")
    if isinstance(value, str) and value:
        return value
    encoded = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class StreamEvent:
    sequence: int
    event: str
    data: dict

    def encode(self) -> bytes:
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"),
                             default=str)
        return (f"id: {self.sequence}\nevent: {self.event}\ndata: {payload}\n\n"
                ).encode("utf-8")


class Subscriber:
    """Consumer end of one SSE connection."""

    def __init__(self, hub: "StreamHub", mailbox: queue.Queue,
                 initial: list[StreamEvent]):
        self.hub = hub
        self.mailbox = mailbox
        self.initial = initial
        self._closed = False

    @property
    def heartbeat(self) -> float:
        return self.hub.heartbeat

    def next(self, timeout: float):
        """Return the next event, or ``None`` when the hub asked us to stop."""
        item = self.mailbox.get(timeout=timeout)
        return item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.hub._unsubscribe(self.mailbox)


class StreamHub:
    """A single project's snapshot/update stream."""

    def __init__(self, project_id: str, provider, *, buffer_size: int = BUFFER_SIZE,
                 heartbeat: float = HEARTBEAT_SECONDS,
                 poll_interval: float = POLL_SECONDS,
                 queue_size: int = SUBSCRIBER_QUEUE_SIZE):
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise StreamError("Invalid project id")
        if not callable(provider):
            raise StreamError("A state provider is required")
        self.project_id = project_id
        self.provider = provider
        self.buffer_size = max(1, int(buffer_size))
        self.heartbeat = max(0.05, float(heartbeat))
        self.poll_interval = max(0.01, float(poll_interval))
        self.queue_size = max(2, int(queue_size))
        self._condition = threading.Condition()
        self._refresh_lock = threading.Lock()
        self._events: deque[StreamEvent] = deque(maxlen=self.buffer_size)
        self._subscribers: set[queue.Queue] = set()
        self._state: dict | None = None
        self._fingerprint: str | None = None
        self._sequence = 0
        self._closed = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._start_lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        with self._start_lock:
            if self._closed:
                raise StreamError("Stream hub is closed")
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run,
                                            name=f"ao-sse-{self.project_id}",
                                            daemon=True)
            self._thread.start()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            subscribers = list(self._subscribers)
            self._subscribers.clear()
            self._condition.notify_all()
        for mailbox in subscribers:
            try:
                mailbox.put_nowait(None)
            except queue.Full:
                pass
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    @property
    def sequence(self) -> int:
        with self._condition:
            return self._sequence

    @property
    def subscriber_count(self) -> int:
        with self._condition:
            return len(self._subscribers)

    @property
    def buffered(self) -> int:
        with self._condition:
            return len(self._events)

    def state(self) -> dict | None:
        with self._condition:
            return None if self._state is None else json.loads(json.dumps(self._state))

    # -- provider polling ----------------------------------------------
    def _run(self) -> None:
        self._refresh()
        while not self._stop.wait(self.poll_interval):
            # Do not read project evidence for a hub nobody is watching.
            if self.subscriber_count == 0:
                continue
            self._refresh()

    def _refresh(self) -> None:
        """Read the provider and publish at most one update per fingerprint."""
        with self._refresh_lock:
            try:
                state = self.provider()
            except Exception:  # Provider errors never fabricate an event.
                with self._condition:
                    self._condition.notify_all()
                return
            if not isinstance(state, dict):
                return
            try:
                state = json.loads(json.dumps(state, ensure_ascii=False, default=str))
            except (TypeError, ValueError, RecursionError):
                return
            fingerprint = _fingerprint(state)
            with self._condition:
                if self._closed:
                    return
                if self._state is None:
                    self._state = state
                    self._fingerprint = fingerprint
                    self._sequence = max(1, self._sequence)
                    event = StreamEvent(
                        self._sequence, "snapshot",
                        self._data("snapshot", state, reconnect=None),
                    )
                    self._events.append(event)
                    self._condition.notify_all()
                    return
                if fingerprint == self._fingerprint:
                    return
                self._state = state
                self._fingerprint = fingerprint
                self._sequence += 1
                event = StreamEvent(
                    self._sequence, "update",
                    self._data("update", state, reconnect=None),
                )
                self._events.append(event)
                self._broadcast_locked(event)
                self._condition.notify_all()

    def _data(self, kind: str, state: dict, *, reconnect: bool | None) -> dict:
        data = {
            "type": kind,
            "project_id": self.project_id,
            "sequence_id": self._sequence,
            "state": state,
        }
        if reconnect:
            data["reconnect"] = True
        return data

    def _broadcast_locked(self, event: StreamEvent) -> None:
        for mailbox in list(self._subscribers):
            try:
                mailbox.put_nowait(event)
            except queue.Full:
                # Too slow: drop it and let the browser reconnect with its
                # Last-Event-ID, which will replay or send a fresh snapshot.
                self._subscribers.discard(mailbox)
                try:
                    while True:
                        mailbox.get_nowait()
                except queue.Empty:
                    pass
                try:
                    mailbox.put_nowait(None)
                except queue.Full:
                    pass

    # -- subscriptions --------------------------------------------------
    def subscribe(self, last_event_id=None) -> Subscriber:
        self.start()
        raw_last = last_event_id
        last = parse_last_event_id(raw_last)
        malformed = raw_last is not None and last is None
        if self.subscriber_count == 0:
            # Refresh before the first viewer so a fresh snapshot is not stale
            # simply because the hub was idle.
            self._refresh()
        with self._condition:
            if self._state is None:
                self._condition.wait_for(
                    lambda: self._state is not None or self._closed, timeout=10.0)
            if self._state is None:
                raise StreamError("Project state is unavailable")
            if len(self._subscribers) >= MAX_SUBSCRIBERS:
                raise StreamError("Too many stream subscribers")
            initial: list[StreamEvent] = []
            if last is None:
                initial = [self._snapshot_locked(reconnect=malformed)]
            elif last > self._sequence:
                initial = [self._snapshot_locked(reconnect=True)]
            elif last == self._sequence:
                initial = []
            else:
                earliest = self._events[0].sequence if self._events else None
                if self._events and earliest is not None and earliest <= last + 1:
                    initial = [event for event in self._events
                               if event.sequence > last]
                else:
                    initial = [self._snapshot_locked(reconnect=True)]
            if self._closed:
                raise StreamError("Stream hub is closed")
            mailbox: queue.Queue = queue.Queue(maxsize=self.queue_size)
            self._subscribers.add(mailbox)
            return Subscriber(self, mailbox, initial)

    def _snapshot_locked(self, *, reconnect: bool) -> StreamEvent:
        return StreamEvent(
            self._sequence, "snapshot",
            self._data("snapshot", self._state or {}, reconnect=reconnect),
        )

    def _unsubscribe(self, mailbox: queue.Queue) -> None:
        with self._condition:
            self._subscribers.discard(mailbox)

    # -- testing helpers ------------------------------------------------
    def wait_for_sequence(self, target: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._sequence < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


class StreamManager:
    """Lazily created, bounded collection of per-project hubs."""

    def __init__(self, provider_factory, *, max_hubs: int = MAX_HUBS,
                 buffer_size: int = BUFFER_SIZE, heartbeat: float = HEARTBEAT_SECONDS,
                 poll_interval: float = POLL_SECONDS):
        if not callable(provider_factory):
            raise StreamError("A provider factory is required")
        self._factory = provider_factory
        self._max_hubs = max(1, int(max_hubs))
        self._buffer_size = buffer_size
        self._heartbeat = heartbeat
        self._poll_interval = poll_interval
        self._hubs: OrderedDict[str, StreamHub] = OrderedDict()
        self._lock = threading.Lock()
        self._closed = False

    def hub(self, project_id: str) -> StreamHub:
        with self._lock:
            if self._closed:
                raise StreamError("Stream manager is closed")
            hub = self._hubs.get(project_id)
            if hub is not None:
                self._hubs.move_to_end(project_id)
                return hub
            provider = self._factory(project_id)
            hub = StreamHub(project_id, provider, buffer_size=self._buffer_size,
                            heartbeat=self._heartbeat,
                            poll_interval=self._poll_interval)
            self._hubs[project_id] = hub
            hub.start()
            if not self._evict_locked(keep=project_id):
                self._hubs.pop(project_id, None)
                hub.close()
                raise StreamError("Too many active stream hubs")
            return hub

    def _evict_locked(self, *, keep: str) -> bool:
        while len(self._hubs) > self._max_hubs:
            removed = False
            for key in list(self._hubs):
                if key == keep:
                    continue
                candidate = self._hubs[key]
                if candidate.subscriber_count == 0:
                    self._hubs.pop(key)
                    candidate.close()
                    removed = True
                    break
            if not removed:
                return False
        return True

    def project_ids(self) -> list[str]:
        with self._lock:
            return list(self._hubs)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            hubs = list(self._hubs.values())
            self._hubs.clear()
        for hub in hubs:
            hub.close()
