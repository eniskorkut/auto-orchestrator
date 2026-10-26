#!/usr/bin/env python3
"""In-memory task manager backend.

Implements the HTTP contract from CONTRACT.md using only the Python standard
library. Tasks live in memory for the lifetime of the process.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

MAX_TITLE_LENGTH = 80
MAX_BODY_BYTES = 64 * 1024

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FRONTEND_DIR = os.path.join(REPO_ROOT, "frontend")

# path -> (file name inside frontend/, response content type)
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/config.js": ("config.js", "text/javascript; charset=utf-8"),
}

TASK_PATH = "/api/tasks"
TASK_ID_RE = re.compile(r"^/api/tasks/(\d+)$")


class ValidationError(ValueError):
    """Raised when a request payload does not satisfy the contract."""


def normalize_title(raw: Any) -> str:
    """Validate and normalize a task title.

    A title must be a string whose trimmed form is between 1 and 80
    characters long.
    """
    if not isinstance(raw, str):
        raise ValidationError("title must be a string")
    title = raw.strip()
    if not title:
        raise ValidationError("title must not be empty")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValidationError(
            "title must be at most %d characters" % MAX_TITLE_LENGTH
        )
    return title


class TaskStore:
    """Thread-safe in-memory task collection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[int, dict[str, Any]] = {}
        self._next_id = 1

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(task) for task in self._tasks.values()]

    def create(self, title: str) -> dict[str, Any]:
        with self._lock:
            task = {"id": self._next_id, "title": title, "done": False}
            self._tasks[task["id"]] = task
            self._next_id += 1
            return dict(task)

    def set_done(self, task_id: int, done: bool) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task["done"] = done
            return dict(task)


class ApiHandler(BaseHTTPRequestHandler):
    """HTTP handler for the task API and the static frontend files."""

    server_version = "TaskManager/1.0"
    protocol_version = "HTTP/1.1"

    # Filled in by create_server().
    store: TaskStore
    frontend_dir: str

    # -- request entry points -------------------------------------------------

    def do_GET(self) -> None:
        path = self._path()
        if path == TASK_PATH:
            self._send_json(200, {"tasks": self.store.list_tasks()})
        elif path in STATIC_FILES:
            self._serve_static(path)
        else:
            self._not_found()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        if self._path() != TASK_PATH:
            self._discard_body()
            self._not_found()
            return
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise ValidationError("request body must be a JSON object")
            task = self.store.create(normalize_title(payload.get("title")))
        except ValidationError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(201, {"task": task})

    def do_PATCH(self) -> None:
        match = TASK_ID_RE.match(self._path())
        if match is None:
            self._discard_body()
            self._not_found()
            return
        task_id = int(match.group(1))
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise ValidationError("request body must be a JSON object")
            if "done" not in payload:
                raise ValidationError("done is required")
            done = payload["done"]
            if not isinstance(done, bool):
                raise ValidationError("done must be a boolean")
        except ValidationError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        task = self.store.set_done(task_id, done)
        if task is None:
            self._send_json(404, {"error": "task not found"})
            return
        self._send_json(200, {"task": task})

    # -- helpers --------------------------------------------------------------

    def _path(self) -> str:
        return urlsplit(self.path).path

    def _content_length(self) -> int:
        header = self.headers.get("Content-Length")
        if header is None:
            return 0
        try:
            length = int(header)
        except ValueError:
            raise ValidationError("invalid Content-Length header") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValidationError("request body too large")
        return length

    def _read_raw_body(self) -> bytes:
        try:
            length = self._content_length()
        except ValidationError:
            # The body framing is unusable, so do not try to reuse the socket.
            self.close_connection = True
            raise
        return self.rfile.read(length) if length else b""

    def _discard_body(self) -> None:
        try:
            self._read_raw_body()
        except (ValidationError, OSError):
            self.close_connection = True

    def _read_json(self) -> Any:
        raw = self._read_raw_body()
        if not raw:
            raise ValidationError("request body must be JSON")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("request body must be valid JSON") from None

    def _serve_static(self, path: str) -> None:
        filename, content_type = STATIC_FILES[path]
        full_path = os.path.join(self.frontend_dir, filename)
        try:
            with open(full_path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._not_found()
            return
        self._send(200, body, content_type)

    def _not_found(self) -> None:
        self._send_json(404, {"error": "not found"})

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the console quiet for normal requests.
        pass

    def log_error(self, format: str, *args: Any) -> None:
        sys.stderr.write(
            "%s - - %s\n" % (self.address_string(), format % args)
        )


def create_server(
    host: str = "127.0.0.1",
    port: int = 0,
    frontend_dir: str | None = None,
) -> ThreadingHTTPServer:
    """Create an HTTP server with its own in-memory task store."""
    store = TaskStore()
    handler = type(
        "BoundApiHandler",
        (ApiHandler,),
        {
            "store": store,
            "frontend_dir": os.path.abspath(frontend_dir or DEFAULT_FRONTEND_DIR),
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.store = store  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="In-memory task manager backend")
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8000, help="bind port (0 picks a free port)")
    parser.add_argument(
        "--print-port",
        action="store_true",
        help="print the bound port to stdout before serving",
    )
    parser.add_argument(
        "--frontend-dir",
        default=None,
        help="directory containing index.html and friends (defaults to ./frontend)",
    )
    args = parser.parse_args(argv)

    server = create_server(
        host=args.host, port=args.port, frontend_dir=args.frontend_dir
    )
    host, port = server.server_address[:2]
    if args.print_port:
        print(port, flush=True)
    else:
        print("Serving on http://%s:%d" % (host, port), flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
