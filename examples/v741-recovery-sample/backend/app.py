"""In-memory task manager backend (Python 3.11, standard library only).

Implements the API described in CONTRACT.md:

    GET   /api/tasks
    POST  /api/tasks
    PATCH /api/tasks/<id>

and serves the static files of the sibling ``frontend/`` directory.
"""

from __future__ import annotations

import argparse
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

MAX_TITLE_LENGTH = 80
MAX_BODY_BYTES = 1 << 20  # 1 MiB

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRONTEND_DIR = REPO_ROOT / "frontend"

# Only these paths are served, which also rules out path traversal.
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/config.js": ("config.js", "text/javascript; charset=utf-8"),
}


class ValidationError(ValueError):
    """Raised when a request payload does not satisfy the contract."""


def parse_title(payload: object) -> str:
    """Return the trimmed title or raise ``ValidationError``."""
    if not isinstance(payload, dict):
        raise ValidationError("body must be a JSON object")

    title = payload.get("title")
    if not isinstance(title, str):
        raise ValidationError("title is required and must be a string")

    title = title.strip()
    if not title:
        raise ValidationError("title must not be empty")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValidationError(
            f"title must be at most {MAX_TITLE_LENGTH} characters"
        )
    return title


def parse_done(payload: object) -> bool:
    """Return the ``done`` flag or raise ``ValidationError``."""
    if not isinstance(payload, dict):
        raise ValidationError("body must be a JSON object")

    done = payload.get("done")
    if not isinstance(done, bool):
        raise ValidationError("done is required and must be a boolean")
    return done


class TaskStore:
    """Thread-safe in-memory task store."""

    def __init__(self) -> None:
        self._tasks: list[dict[str, object]] = []
        self._next_id = 1
        self._lock = threading.Lock()

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


def make_handler(
    store: TaskStore, frontend_dir: Path = DEFAULT_FRONTEND_DIR
) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to a store and frontend directory."""

    class TaskRequestHandler(BaseHTTPRequestHandler):
        server_version = "TeamworkTasks/1.0"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # -- helpers ---------------------------------------------------

        def _send_json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_error_json(self, status: HTTPStatus, message: str) -> None:
            self._send_json(status, {"error": message})

        def _read_json_body(self) -> object:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ValidationError("request body required")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ValidationError("invalid Content-Length") from exc
            if length < 0 or length > MAX_BODY_BYTES:
                self.close_connection = True
                raise ValidationError("request body too large")
            if length == 0:
                raise ValidationError("request body required")

            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("body must be valid JSON") from exc

        def _serve_static(self, path: str) -> None:
            route = STATIC_ROUTES.get(path)
            if route is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return

            filename, content_type = route
            try:
                body = (frontend_dir / filename).read_bytes()
            except OSError:
                self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        # -- verbs -----------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = urlsplit(self.path).path
            if path == "/api/tasks":
                self._send_json(HTTPStatus.OK, {"tasks": store.list_tasks()})
                return
            if path in STATIC_ROUTES:
                self._serve_static(path)
                return
            self._send_error_json(HTTPStatus.NOT_FOUND, "not found")

        def do_HEAD(self) -> None:  # noqa: N802 - http.server API
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            path = urlsplit(self.path).path
            if path != "/api/tasks":
                # The body was not consumed; do not reuse this connection.
                self.close_connection = True
                self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return
            try:
                title = parse_title(self._read_json_body())
            except ValidationError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_json(HTTPStatus.CREATED, {"task": store.create_task(title)})

        def do_PATCH(self) -> None:  # noqa: N802 - http.server API
            path = urlsplit(self.path).path
            prefix = "/api/tasks/"
            if not path.startswith(prefix):
                self.close_connection = True
                self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return

            raw_id = path[len(prefix) :]
            try:
                task_id = int(raw_id)
            except ValueError:
                self.close_connection = True
                self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return

            try:
                done = parse_done(self._read_json_body())
            except ValidationError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return

            task = store.set_done(task_id, done)
            if task is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "task not found")
                return
            self._send_json(HTTPStatus.OK, {"task": task})

        def log_message(self, format: str, *args: object) -> None:
            # Keep stdout/stderr clean for --print-port consumers and tests.
            pass

    return TaskRequestHandler


def make_server(
    host: str = "127.0.0.1",
    port: int = 0,
    frontend_dir: Path = DEFAULT_FRONTEND_DIR,
) -> ThreadingHTTPServer:
    """Create a configured HTTP server (not yet serving)."""
    server = ThreadingHTTPServer((host, port), make_handler(TaskStore(), frontend_dir))
    server.daemon_threads = True
    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="In-memory task manager backend")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default: %(default)s)")
    parser.add_argument(
        "--print-port",
        action="store_true",
        help="print the bound port to stdout once listening",
    )
    parser.add_argument(
        "--frontend-dir",
        type=Path,
        default=DEFAULT_FRONTEND_DIR,
        help="directory containing the static frontend files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = make_server(args.host, args.port, args.frontend_dir)
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
