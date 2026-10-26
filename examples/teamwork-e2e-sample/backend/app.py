#!/usr/bin/env python3.11
"""In-memory task manager backend built only on the Python 3.11 stdlib."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"

MAX_TITLE_LENGTH = 80

# Only these paths are served from frontend/. The whitelist also prevents
# path traversal because clients cannot ask for arbitrary files.
STATIC_ROUTES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
    "/config.js": "config.js",
}

TASK_DETAIL_RE = re.compile(r"^/api/tasks/(\d+)$")


class TaskStore:
    """Thread-safe in-memory task storage."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: list[dict[str, object]] = []
        self._next_id = 1

    def list_tasks(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(task) for task in self._tasks]

    def create_task(self, title: str) -> dict[str, object]:
        with self._lock:
            task = {"id": self._next_id, "title": title, "done": False}
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
    protocol_version = "HTTP/1.1"
    server_version = "TeamworkTaskServer/1.0"

    # -- HTTP verbs -----------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = urlparse(self.path).path
        if path == "/api/tasks":
            self._send_json(HTTPStatus.OK, {"tasks": self.server.store.list_tasks()})
            return
        if path.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        raw_body = self._read_body()
        if raw_body is None:
            return  # response already sent

        path = urlparse(self.path).path
        if path != "/api/tasks":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        payload, error = self._parse_json_object(raw_body)
        if error is not None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": error})
            return

        title, error = self._validate_title(payload.get("title"))
        if error is not None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": error})
            return

        task = self.server.store.create_task(title)
        self._send_json(HTTPStatus.CREATED, {"task": task})

    def do_PATCH(self) -> None:  # noqa: N802 (http.server API)
        raw_body = self._read_body()
        if raw_body is None:
            return  # response already sent

        path = urlparse(self.path).path
        match = TASK_DETAIL_RE.match(path)
        if match is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        payload, error = self._parse_json_object(raw_body)
        if error is not None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": error})
            return

        done = payload.get("done")
        if not isinstance(done, bool):
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "done must be a boolean"}
            )
            return

        task = self.server.store.set_done(int(match.group(1)), done)
        if task is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "task not found"})
            return

        self._send_json(HTTPStatus.OK, {"task": task})

    # -- helpers --------------------------------------------------------

    def _read_body(self) -> bytes | None:
        """Read the request body, or send 400 and return None on bad length."""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
            if length < 0:
                raise ValueError
        except ValueError:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"}
            )
            return None
        return self.rfile.read(length)

    @staticmethod
    def _parse_json_object(raw_body: bytes) -> tuple[dict, str | None]:
        try:
            payload = json.loads(raw_body.decode("utf-8") if raw_body else "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}, "invalid JSON body"
        if not isinstance(payload, dict):
            return {}, "JSON body must be an object"
        return payload, None

    @staticmethod
    def _validate_title(value: object) -> tuple[str | None, str | None]:
        if not isinstance(value, str):
            return None, "title is required"
        title = value.strip()
        if not title:
            return None, "title must not be empty"
        if len(title) > MAX_TITLE_LENGTH:
            return None, f"title must be at most {MAX_TITLE_LENGTH} characters"
        return title, None

    def _serve_static(self, path: str) -> None:
        decoded = unquote(path)
        filename = STATIC_ROUTES.get(decoded)
        if filename is None:
            self._send_text(HTTPStatus.NOT_FOUND, "Not found")
            return

        file_path = FRONTEND_DIR / filename
        try:
            body = file_path.read_bytes()
        except OSError:
            self._send_text(HTTPStatus.NOT_FOUND, "Not found")
            return

        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if filename.endswith(".js"):
            content_type = "text/javascript"
        elif filename.endswith(".css"):
            content_type = "text/css"
        elif filename.endswith(".html"):
            content_type = "text/html"
        self._send_bytes(HTTPStatus.OK, body, f"{content_type}; charset=utf-8")

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_text(self, status: HTTPStatus, text: str) -> None:
        self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Keep server output clean; the CLI owns stdout for --print-port."""


class TaskHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: TaskStore | None = None) -> None:
        super().__init__(address, TaskRequestHandler)
        self.store = store if store is not None else TaskStore()


def create_server(host: str = "127.0.0.1", port: int = 0) -> TaskHTTPServer:
    """Create (but do not run) a task server bound to host:port."""
    return TaskHTTPServer((host, port), TaskStore())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="In-memory task manager backend")
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8000, help="bind port (0 = free port)")
    parser.add_argument(
        "--print-port",
        action="store_true",
        help="print the bound port to stdout before serving",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
    raise SystemExit(main())
