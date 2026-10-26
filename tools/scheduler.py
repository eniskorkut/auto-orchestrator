#!/usr/bin/env python3
"""Small event-driven poller for already-running managed tasks.

The scheduler never starts processes itself. It owns at most ``max_workers``
watcher threads, each blocking on the adapter's bounded ``poll()`` call until
that task finishes, then pushes a completion record onto an in-process queue.
The coordinator thread can wait on the queue without polling every task in a
sleep loop.

This keeps CPU use bounded and makes real concurrency observable: tasks that
finish at the same time arrive in arbitrary queue order without one task
blocking another.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_WORKERS_LIMIT = 32


class SchedulerError(ValueError):
    pass


@dataclass
class Submitted:
    key: str
    handle: Any
    poll: Callable[..., dict]
    timeout: float
    started_at: float = field(default_factory=time.monotonic)
    deadline: float = 0.0
    thread: threading.Thread | None = None


class PollScheduler:
    def __init__(self, *, max_workers: int = 3, poll_slice: float = 1.0):
        if type(max_workers) is not int or not 1 <= max_workers <= MAX_WORKERS_LIMIT:
            raise SchedulerError("max_workers must be 1..32")
        if not isinstance(poll_slice, (int, float)) or not 0.05 <= poll_slice <= 30:
            raise SchedulerError("poll_slice must be 0.05..30 seconds")
        self.max_workers = max_workers
        self.poll_slice = float(poll_slice)
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._active: dict[str, Submitted] = {}
        self._closed = False
        self._completed = 0
        self._timeouts = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._active)

    @property
    def completed(self) -> int:
        return self._completed

    @property
    def timeouts(self) -> int:
        return self._timeouts

    def submit(self, key: str, handle: Any, poll: Callable[..., dict],
               *, timeout: float = 900.0) -> None:
        if not isinstance(key, str) or not key:
            raise SchedulerError("key must be a non-empty string")
        if not callable(poll):
            raise SchedulerError("poll must be callable")
        if not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 86400:
            raise SchedulerError("timeout must be 0.1..86400 seconds")
        with self._lock:
            if self._closed:
                raise SchedulerError("scheduler is closed")
            if key in self._active:
                raise SchedulerError(f"task already scheduled: {key}")
            if len(self._active) >= self.max_workers:
                raise SchedulerError("scheduler is at its concurrency bound")
            record = Submitted(key=key, handle=handle, poll=poll, timeout=float(timeout))
            record.deadline = record.started_at + record.timeout
            thread = threading.Thread(
                target=self._watch, args=(record,),
                name=f"ao-poll-{key}", daemon=True,
            )
            record.thread = thread
            self._active[key] = record
            thread.start()

    def _watch(self, record: Submitted) -> None:
        state: dict
        try:
            while True:
                remaining = record.deadline - time.monotonic()
                if remaining <= 0:
                    state = {"status": "timed_out", "key": record.key,
                             "error": "scheduler deadline exceeded"}
                    with self._lock:
                        self._timeouts += 1
                    self._queue.put((record.key, state))
                    return
                try:
                    state = record.poll(
                        record.handle, timeout=min(self.poll_slice, remaining)
                    )
                except Exception as exc:  # adapter exceptions are completion evidence
                    state = {"status": "failed", "key": record.key,
                             "error": f"{type(exc).__name__}: {exc}"[:1000]}
                if not isinstance(state, dict):
                    state = {"status": "failed", "key": record.key,
                             "error": "adapter poll returned a non-object"}
                if state.get("status") != "running":
                    self._queue.put((record.key, state))
                    return
        finally:
            with self._lock:
                self._active.pop(record.key, None)

    def wait(self, *, timeout: float | None = None) -> list[tuple[str, dict]]:
        """Return completed records, blocking up to ``timeout`` for the first."""
        results = []
        if timeout is None:
            key, state = self._queue.get()
            results.append((key, state))
            self._completed += 1
        else:
            try:
                key, state = self._queue.get(timeout=max(0.0, timeout))
            except queue.Empty:
                return []
            results.append((key, state))
            self._completed += 1
        while True:
            try:
                results.append(self._queue.get_nowait())
                self._completed += 1
            except queue.Empty:
                break
        return results

    def drain(self) -> list[tuple[str, dict]]:
        return self.wait(timeout=0.0)

    def active_keys(self) -> list[str]:
        with self._lock:
            return list(self._active)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            active = list(self._active.values())
        for record in active:
            # Watcher threads are daemon and only call a bounded poll; closing
            # the scheduler never kills an external process.
            if record.thread is not None:
                record.thread.join(timeout=0.2)
