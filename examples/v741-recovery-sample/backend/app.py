#!/usr/bin/env python3.11
"""In-memory task manager backend.

Implements the API described in CONTRACT.md using only the Python 3.11
standard library. Tasks live in memory for the lifetime of the process.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

TITLE_MAX_LENGTH = 80
MAX_BODY_BYTES = 64 * 1024

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/config.js": ("config.js", "text/javascript; charset=utf-8"),
}

TASK_PATH = re.compile(r"^/api/tasks/([1-9][0-9]*)$")


class ValidationError(ValueError):
    """Raised when a request body does not match the contract."""


class RequestError(Exception):
    """A request-level error that maps to a JSON error response."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def validate_title(raw: object) -> str:
    """Trim and validate a task title.

    Valid titles have 1..TITLE_MAX_LENGTH non-whitespace characters after
    trimming surrounding whitespace.
    """
    if not isinstance(raw, str):
        raise ValidationError("title must be a string")
    title = raw.strip()
    if not title:
        raise ValidationError("title must not be empty")
    if len(title) > TITLE_MAX_LENGTH:
        raise ValidationError(
            f"title must be at most {TITLE_MAX_LENGTH} characters"
        )
    return title


class TaskStore:
    """Thread-safe in-memory task collection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: list[dict[str, object]] = []
        self._next_id = 1

    def list_tasks(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(task) for task in self._tasks]

    def create_task(self, title: str) -> dict[str, object]:
        with self._lock:
            task: dict[str, object] = {
                "id": self._next_id,
                "title": title,
                "done": False,
            }
            self._next_id += 1
            self._tasks.append(task)
            return dict(task)

    def set_done(self, task_id: int, done: bool) -> dict[str, object] | None:
        with self._lock:
            for task in self._tasks:
                if task["id"] == task_id:
                    task["done"] = done
                    return dict(task)
        return None


class TaskRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler for the task API and the static frontend."""

    server_version = "TaskAPI/1.0"
    sys_version = ""

    @property
    def store(self) -> TaskStore:
        return self.server.store  # type: ignore[attr-defined]

    # -- response helpers -------------------------------------------------

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _read_json_object(self) -> dict[str, object]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except ValueError as exc:
            raise RequestError("invalid Content-Length header") from exc

        if length <= 0:
            raise RequestError("request body must be a JSON object")
        if length > MAX_BODY_BYTES:
            raise RequestError("request body is too large", status=413)

        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RequestError("request body must be valid JSON") from exc
        if not isinstance(data, dict):
            raise RequestError("request body must be a JSON object")
        return data

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/tasks":
            self._send_json(200, {"tasks": self.store.list_tasks()})
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path != "/api/tasks":
            self._send_error_json(404, "not found")
            return
        try:
            body = self._read_json_object()
            title = validate_title(body.get("title"))
        except (RequestError, ValidationError) as exc:
            status = exc.status if isinstance(exc, RequestError) else 400
            self._send_error_json(status, str(exc))
            return
        self._send_json(201, {"task": self.store.create_task(title)})

    def do_PATCH(self) -> None:
        path = urlsplit(self.path).path
        match = TASK_PATH.match(path)
        if match is None:
            self._send_error_json(404, "not found")
            return
        try:
            body = self._read_json_object()
            done = body.get("done")
            if not isinstance(done, bool):
                raise ValidationError("done must be a boolean")
        except (RequestError, ValidationError) as exc:
            status = exc.status if isinstance(exc, RequestError) else 400
            self._send_error_json(status, str(exc))
            return

        task = self.store.set_done(int(match.group(1)), done)
        if task is None:
            self._send_error_json(404, "task not found")
            return
        self._send_json(200, {"task": task})

    # -- static frontend --------------------------------------------------

    def _serve_static(self, path: str) -> None:
        entry = STATIC_FILES.get(path)
        if entry is None:
            self._send_error_json(404, "not found")
            return
        filename, content_type = entry
        try:
            body = (FRONTEND_DIR / filename).read_bytes()
        except OSError:
            self._send_error_json(404, "not found")
            return
        self._send_bytes(200, body, content_type)

    # Keep test and CLI output clean; request logs are not part of the API.
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class TaskHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying its own in-memory task store."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler] = TaskRequestHandler,
    ) -> None:
        self.store = TaskStore()
        super().__init__(server_address, handler_class)


def create_server(host: str = "127.0.0.1", port: int = 0) -> TaskHTTPServer:
    """Create a server bound to ``host:port`` without serving yet."""
    return TaskHTTPServer((host, port))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="In-memory task manager backend")
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8000, help="bind port (0 = ephemeral)")
    parser.add_argument(
        "--print-port",
        action="store_true",
        help="print the bound port before serving",
    )
    args = parser.parse_args(argv)

    server = create_server(args.host, args.port)
    if args.print_port:
        print(server.server_address[1], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
