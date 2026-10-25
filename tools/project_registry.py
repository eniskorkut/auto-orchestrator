#!/usr/bin/env python3
"""Multi-project registry for Auto-Orchestrator V7.1.

The registry is a local, read/write metadata file. It never stores prompts,
source code, raw native agent identifiers or evidence. Project removal only
drops metadata; run logs, Supervisor databases and worktrees are untouched.

Layout (schema version 2):

    {
      "version": 2,
      "projects": [{
        "project_id": "p_<16 hex>",
        "name": "...", "description": "", "root": "/canonical/path",
        "theme": "ai|security|web|custom",
        "created_at": 0, "updated_at": 0,
        "agents": [{"agent_definition_id": "a_<12 hex>", ...}]
      }]
    }

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

SCHEMA_VERSION = 2
HOME_ENV = "AUTO_ORCHESTRATOR_HOME"
DEFAULT_HOME_DIR = ".auto-orchestrator"
REGISTRY_NAME = "projects.json"
BINDINGS_NAME = "agent-bindings.json"
BINDINGS_VERSION = 1
MAX_REGISTRY_BYTES = 4 * 1024 * 1024
MAX_BINDINGS_BYTES = 256 * 1024
MAX_PROJECTS = 1000
MAX_AGENTS = 64
MAX_BINDINGS = 256

PROJECT_ID = re.compile(r"^p_[0-9a-f]{16}$")
AGENT_ID = re.compile(r"^a_[0-9a-f]{12}$")
NATIVE_HASH = re.compile(r"^[0-9a-f]{64}$")
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

THEMES = ("ai", "security", "web", "custom")
PLATFORMS = ("opencode", "codex", "antigravity", "cursor", "unknown")
DEFAULT_COLOR = "#7dd3fc"

PROJECT_FIELDS = {"project_id", "name", "description", "root", "theme",
                  "created_at", "updated_at", "agents"}
AGENT_FIELDS = {"agent_definition_id", "name", "role", "platform", "model",
                "color", "created_at"}
BINDING_FIELDS = {"agent_definition_id", "native_hash", "run_id", "session_id",
                  "task_id", "bound_at"}


class RegistryError(ValueError):
    """Invalid registry, project root, agent or binding input."""


def default_registry_path() -> Path:
    """Return ``$AUTO_ORCHESTRATOR_HOME/projects.json`` or the home fallback."""
    home = os.environ.get(HOME_ENV)
    if home:
        return Path(os.path.expanduser(home)) / REGISTRY_NAME
    return Path.home() / DEFAULT_HOME_DIR / REGISTRY_NAME


def _number(value, default=0):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else default
    return default


def _path_key(path: Path) -> str:
    """Cross-platform identity for duplicate detection and project IDs."""
    value = os.path.normpath(os.path.abspath(os.fspath(path)))
    return os.path.normcase(value)


def _same_root(left: Path, right: Path) -> bool:
    """Path identity plus inode identity for case-insensitive volumes."""
    if _path_key(left) == _path_key(right):
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def project_id_for(root: Path | str) -> str:
    """Deterministic opaque project ID for an already canonical root.

    ``normcase`` makes the ID stable on case-insensitive Windows volumes. The
    registry always passes a ``resolve(strict=True)`` path here.
    """
    key = _path_key(Path(root))
    digest = hashlib.sha256(("auto-orchestrator|project|" + key).encode("utf-8")).hexdigest()
    return "p_" + digest[:16]


def canonical_root(value) -> Path:
    """Validate and canonicalise a project root.

    Rejects a symlink final component, missing paths and non-directories.
    Parent symlinks are resolved by ``Path.resolve(strict=True)``.
    """
    if not isinstance(value, (str, os.PathLike)):
        raise RegistryError("Project root must be a filesystem path")
    raw = Path(os.fspath(value)).expanduser()
    if not raw.is_absolute():
        raw = Path(os.path.abspath(os.fspath(raw)))
    try:
        if raw.is_symlink():
            raise RegistryError("Project root must not be a symbolic link")
    except OSError as exc:
        raise RegistryError("Project root is unreadable") from exc
    try:
        resolved = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RegistryError("Project root does not exist") from exc
    except OSError as exc:
        raise RegistryError("Project root is unreadable") from exc
    if not resolved.is_dir():
        raise RegistryError("Project root must be a directory")
    if resolved.is_symlink():
        raise RegistryError("Project root must not be a symbolic link")
    return resolved


def _checked_text(value, label, *, required=True, limit=1000):
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise RegistryError(f"{label} must be a string")
    value = value.strip()
    if required and not value:
        raise RegistryError(f"{label} is required")
    if len(value) > limit:
        raise RegistryError(f"{label} is too long")
    if any(ord(char) < 32 for char in value):
        raise RegistryError(f"{label} contains control characters")
    return value


def _checked_safe_id(value, label, *, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise RegistryError(f"{label} is invalid")
    return value


def _checked_native_hash(value):
    if not isinstance(value, str) or not NATIVE_HASH.fullmatch(value):
        raise RegistryError("native_hash must be 64 lowercase hex characters")
    return value


def _validate_agent(raw: dict, seen: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise RegistryError("Agent definition must be an object")
    agent_id = raw.get("agent_definition_id")
    if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id):
        raise RegistryError("Invalid agent definition id")
    if agent_id in seen:
        raise RegistryError("Duplicate agent definition id")
    seen.add(agent_id)
    platform = raw.get("platform", "unknown")
    if platform not in PLATFORMS:
        raise RegistryError("Unsupported agent platform")
    model = raw.get("model")
    if model is not None:
        model = _checked_text(model, "Agent model", required=False, limit=120) or None
    color = raw.get("color", DEFAULT_COLOR)
    if not isinstance(color, str) or not COLOR.fullmatch(color):
        raise RegistryError("Agent color must be a #rrggbb value")
    return {
        "agent_definition_id": agent_id,
        "name": _checked_text(raw.get("name"), "Agent name", limit=120),
        "role": _checked_text(raw.get("role"), "Agent role", limit=120),
        "platform": platform,
        "model": model,
        "color": color,
        "created_at": _number(raw.get("created_at"), 0),
    }


def _validate_project(raw: dict, seen_ids: set[str],
                      seen_roots: dict[str, Path]) -> dict:
    if not isinstance(raw, dict):
        raise RegistryError("Project entry must be an object")
    project_id = raw.get("project_id")
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
        raise RegistryError("Invalid project id")
    if project_id in seen_ids:
        raise RegistryError("Duplicate project id")
    seen_ids.add(project_id)
    root_value = raw.get("root")
    if not isinstance(root_value, str) or not root_value:
        raise RegistryError("Project root must be a string")
    root = Path(root_value)
    if not root.is_absolute():
        raise RegistryError("Project root must be absolute")
    key = _path_key(root)
    if any(_same_root(root, previous) for previous in seen_roots.values()):
        raise RegistryError("Duplicate canonical project root")
    seen_roots[key] = root
    if project_id_for(root) != project_id:
        raise RegistryError("Project id does not match the canonical root")
    if root.is_symlink():
        raise RegistryError("Project root must not be a symbolic link")
    if root.exists():
        try:
            canonical = root.resolve(strict=True)
        except OSError as exc:
            raise RegistryError("Project root is unreadable") from exc
        if _path_key(canonical) != key:
            raise RegistryError("Project root is not canonical")
        if not canonical.is_dir():
            raise RegistryError("Project root must be a directory")
    theme = raw.get("theme", "custom")
    if theme not in THEMES:
        raise RegistryError("Unsupported project theme")
    agents_raw = raw.get("agents", [])
    if not isinstance(agents_raw, list) or len(agents_raw) > MAX_AGENTS:
        raise RegistryError("Project agents must be a bounded array")
    agent_ids: set[str] = set()
    agents = [_validate_agent(agent, agent_ids) for agent in agents_raw]
    return {
        "project_id": project_id,
        "name": _checked_text(raw.get("name"), "Project name", limit=120),
        "description": _checked_text(raw.get("description", ""), "Project description",
                                    required=False, limit=1000),
        "root": str(root),
        "theme": theme,
        "created_at": _number(raw.get("created_at"), 0),
        "updated_at": _number(raw.get("updated_at"), 0),
        "agents": agents,
    }


def _personal_id(prefix: str, length: int, existing: set[str]) -> str:
    while True:
        candidate = prefix + secrets.token_hex(length // 2)
        if candidate not in existing:
            return candidate


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write ``payload`` atomically, mode 0600 where POSIX supports it."""
    if path.is_symlink():
        raise RegistryError("Refusing to write through a symbolic link")
    parent = path.parent
    created_parent = not parent.exists()
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if created_parent and os.name != "nt":
            parent.chmod(0o700)
    except OSError as exc:
        raise RegistryError("Registry directory is not writable") from exc
    if parent.is_symlink():
        raise RegistryError("Refusing to write into a symbolic link directory")
    fd = None
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                         dir=str(parent))
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            try:
                path.chmod(0o600)
            except OSError:
                pass
        try:
            directory = os.open(str(parent), os.O_RDONLY)
        except OSError:
            directory = None
        if directory is not None:
            try:
                os.fsync(directory)
            except OSError:
                pass
            finally:
                os.close(directory)
    except OSError as exc:
        raise RegistryError("Registry write failed") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _migrate(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise RegistryError("Registry must be a JSON object")
    version = payload.get("version")
    if version == SCHEMA_VERSION:
        return payload
    projects = payload.get("projects")
    if not isinstance(projects, list):
        raise RegistryError("Registry projects must be an array")
    if version not in (None, 1):
        raise RegistryError("Unsupported registry version")
    migrated = []
    for raw in projects:
        if not isinstance(raw, dict):
            raise RegistryError("Project entry must be an object")
        root = raw.get("root")
        if not isinstance(root, str) or not root:
            raise RegistryError("Legacy project is missing its root")
        try:
            canonical = canonical_root(root)
        except RegistryError:
            canonical = Path(os.path.abspath(os.path.expanduser(root)))
        item = dict(raw)
        item["root"] = str(canonical)
        item["project_id"] = project_id_for(canonical)
        item.setdefault("name", canonical.name or "project")
        item.setdefault("description", "")
        item.setdefault("theme", "custom")
        item.setdefault("created_at", 0)
        item.setdefault("updated_at", item.get("created_at", 0))
        agents = []
        used: set[str] = set()
        raw_agents = item.get("agents", [])
        if not isinstance(raw_agents, list):
            raise RegistryError("Legacy agents must be an array")
        for agent in raw_agents:
            if not isinstance(agent, dict):
                raise RegistryError("Legacy agent must be an object")
            entry = dict(agent)
            if not isinstance(entry.get("agent_definition_id"), str) \
                    or not AGENT_ID.fullmatch(entry["agent_definition_id"]) \
                    or entry["agent_definition_id"] in used:
                entry["agent_definition_id"] = _personal_id("a_", 12, used)
            used.add(entry["agent_definition_id"])
            entry.setdefault("name", "Agent")
            entry.setdefault("role", "member")
            entry.setdefault("platform", "unknown")
            entry.setdefault("model", None)
            entry.setdefault("color", DEFAULT_COLOR)
            entry.setdefault("created_at", 0)
            agents.append(entry)
        item["agents"] = agents
        migrated.append(item)
    return {"version": SCHEMA_VERSION, "projects": migrated}


def _mutating(method):
    """Serialize read-modify-write registry operations across threads/processes."""
    def wrapper(self, *args, **kwargs):
        with self._mutation():
            return method(self, *args, **kwargs)
    return wrapper


class ProjectRegistry:
    """Read/write access to the version 2 project registry."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path).expanduser() if path is not None else default_registry_path()
        if not self.path.is_absolute():
            self.path = Path(os.path.abspath(os.fspath(self.path)))
        self._mutex = threading.RLock()
        self._lock_depth = 0

    @contextlib.contextmanager
    def _mutation(self):
        with self._mutex:
            lock_handle = None
            if self._lock_depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                lock_path = self.path.with_name(self.path.name + ".lock")
                lock_handle = open(lock_path, "a+b")
                if os.name != "nt":
                    os.chmod(lock_path, 0o600)
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                elif msvcrt is not None:
                    if lock_handle.tell() == 0:
                        lock_handle.write(b"0")
                        lock_handle.flush()
                    lock_handle.seek(0)
                    msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if lock_handle is not None:
                    try:
                        if fcntl is not None:
                            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                        elif msvcrt is not None:
                            lock_handle.seek(0)
                            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                    finally:
                        lock_handle.close()

    # -- registry -------------------------------------------------------
    def load(self) -> dict:
        if self.path.is_symlink():
            raise RegistryError("Registry path is a symbolic link")
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "projects": []}
        if not self.path.is_file():
            raise RegistryError("Registry path is not a regular file")
        try:
            size = self.path.stat().st_size
        except OSError as exc:
            raise RegistryError("Registry is unreadable") from exc
        if size > MAX_REGISTRY_BYTES:
            raise RegistryError("Registry is too large")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RegistryError("Registry is unreadable") from exc
        except (ValueError, RecursionError) as exc:
            raise RegistryError("Registry is not valid JSON") from exc
        payload = _migrate(data)
        projects = payload.get("projects")
        if not isinstance(projects, list) or len(projects) > MAX_PROJECTS:
            raise RegistryError("Registry projects must be a bounded array")
        seen_ids: set[str] = set()
        seen_roots: dict[str, Path] = {}
        validated = [_validate_project(item, seen_ids, seen_roots) for item in projects]
        return {"version": SCHEMA_VERSION, "projects": validated}

    @_mutating
    def save(self, payload: dict) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("projects"), list):
            raise RegistryError("Registry payload is invalid")
        seen_ids: set[str] = set()
        seen_roots: dict[str, Path] = {}
        if len(payload["projects"]) > MAX_PROJECTS:
            raise RegistryError("Registry projects must be a bounded array")
        projects = [_validate_project(item, seen_ids, seen_roots)
                    for item in payload["projects"]]
        encoded = (json.dumps({"version": SCHEMA_VERSION, "projects": projects},
                              indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_REGISTRY_BYTES:
            raise RegistryError("Registry is too large")
        _atomic_write(self.path, encoded)

    def projects(self) -> list[dict]:
        return self.load()["projects"]

    # Friendly aliases used by callers and the CLI vocabulary.
    def list(self) -> list[dict]:
        return self.projects()

    def show(self, project_id: str) -> dict:
        return self.get(project_id)

    def get(self, project_id: str) -> dict:
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise RegistryError("Invalid project id")
        for project in self.projects():
            if project["project_id"] == project_id:
                return project
        raise RegistryError("Unknown project")

    def find_by_root(self, root) -> dict | None:
        canonical = canonical_root(root)
        for project in self.projects():
            if _same_root(canonical, Path(project["root"])):
                return project
        return None

    # -- project metadata ----------------------------------------------
    @_mutating
    def add(self, root, *, name=None, description="", theme="custom", now=None) -> tuple[dict, bool]:
        canonical = canonical_root(root)
        data = self.load()
        for project in data["projects"]:
            if _same_root(canonical, Path(project["root"])):
                return project, False
        if len(data["projects"]) >= MAX_PROJECTS:
            raise RegistryError("Registry has too many projects")
        project_id = project_id_for(canonical)
        if any(project["project_id"] == project_id for project in data["projects"]):
            raise RegistryError("Project id collision for distinct roots")
        if theme not in THEMES:
            raise RegistryError("Unsupported project theme")
        stamp = time.time() if now is None else _number(now, 0)
        project = {
            "project_id": project_id,
            "name": _checked_text(name if name is not None else (canonical.name or "project"),
                                  "Project name", limit=120),
            "description": _checked_text(description, "Project description",
                                         required=False, limit=1000),
            "root": str(canonical),
            "theme": theme,
            "created_at": stamp,
            "updated_at": stamp,
            "agents": [],
        }
        data["projects"].append(project)
        self.save(data)
        return project, True

    @_mutating
    def update(self, project_id: str, *, name=None, description=None, theme=None,
               now=None) -> dict:
        data = self.load()
        target = next((p for p in data["projects"] if p["project_id"] == project_id), None)
        if target is None:
            raise RegistryError("Unknown project")
        if name is not None:
            target["name"] = _checked_text(name, "Project name", limit=120)
        if description is not None:
            target["description"] = _checked_text(description, "Project description",
                                                  required=False, limit=1000)
        if theme is not None:
            if theme not in THEMES:
                raise RegistryError("Unsupported project theme")
            target["theme"] = theme
        target["updated_at"] = time.time() if now is None else _number(now, 0)
        self.save(data)
        return target

    @_mutating
    def remove(self, project_id: str) -> dict:
        """Remove project metadata only. Evidence and worktrees are untouched."""
        data = self.load()
        target = next((p for p in data["projects"] if p["project_id"] == project_id), None)
        if target is None:
            raise RegistryError("Unknown project")
        data["projects"] = [p for p in data["projects"] if p["project_id"] != project_id]
        self.save(data)
        return target

    # -- agents ---------------------------------------------------------
    @_mutating
    def add_agent(self, project_id: str, *, name, role, platform="unknown",
                  model=None, color=DEFAULT_COLOR, now=None) -> dict:
        if platform not in PLATFORMS:
            raise RegistryError("Unsupported agent platform")
        if not isinstance(color, str) or not COLOR.fullmatch(color):
            raise RegistryError("Agent color must be a #rrggbb value")
        if model is not None:
            model = _checked_text(model, "Agent model", required=False, limit=120) or None
        data = self.load()
        target = next((p for p in data["projects"] if p["project_id"] == project_id), None)
        if target is None:
            raise RegistryError("Unknown project")
        if len(target["agents"]) >= MAX_AGENTS:
            raise RegistryError("Project has too many agents")
        used = {agent["agent_definition_id"] for agent in target["agents"]}
        agent = {
            "agent_definition_id": _personal_id("a_", 12, used),
            "name": _checked_text(name, "Agent name", limit=120),
            "role": _checked_text(role, "Agent role", limit=120),
            "platform": platform,
            "model": model,
            "color": color,
            "created_at": time.time() if now is None else _number(now, 0),
        }
        target["agents"].append(agent)
        target["updated_at"] = time.time() if now is None else _number(now, 0)
        self.save(data)
        return agent

    def get_agent(self, project_id: str, agent_definition_id: str) -> dict:
        if not isinstance(agent_definition_id, str) or not AGENT_ID.fullmatch(agent_definition_id):
            raise RegistryError("Invalid agent definition id")
        project = self.get(project_id)
        for agent in project["agents"]:
            if agent["agent_definition_id"] == agent_definition_id:
                return agent
        raise RegistryError("Unknown agent definition")

    # -- explicit project-local bindings --------------------------------
    def load_bindings(self, root=None, *, agents=None) -> dict:
        if root is None:
            raise RegistryError("A project root is required")
        return load_bindings(root, agents=agents)

    def write_bindings(self, root, payload: dict) -> None:
        write_bindings(root, payload)

    @_mutating
    def bind_agent(self, project_id: str, agent_definition_id: str, native_hash: str,
                   *, run_id, session_id=None, task_id=None, now=None) -> tuple[dict, bool]:
        project = self.get(project_id)
        if not Path(project["root"]).is_dir():
            raise RegistryError("Project root is unavailable")
        agent = self.get_agent(project_id, agent_definition_id)
        digest = _checked_native_hash(native_hash)
        run = _checked_safe_id(run_id, "run_id")
        session = _checked_safe_id(session_id, "session_id", required=False)
        task = _checked_safe_id(task_id, "task_id", required=False)
        path = bindings_path(project["root"])
        payload = load_bindings(project["root"])
        stamp = time.time() if now is None else _number(now, 0)
        for binding in payload["bindings"]:
            if binding["agent_definition_id"] == agent_definition_id:
                if (binding["native_hash"] == digest and binding["run_id"] == run
                        and binding["session_id"] == session and binding["task_id"] == task):
                    return binding, False
                break
        for binding in payload["bindings"]:
            if binding["native_hash"] == digest \
                    and binding["agent_definition_id"] != agent_definition_id:
                raise RegistryError("Native hash is already bound to another agent")
        replacement = {
            "agent_definition_id": agent_definition_id,
            "native_hash": digest,
            "run_id": run,
            "session_id": session,
            "task_id": task,
            "bound_at": stamp,
        }
        payload["bindings"] = [binding for binding in payload["bindings"]
                               if binding["agent_definition_id"] != agent_definition_id]
        payload["bindings"].append(replacement)
        write_bindings(project["root"], payload)
        return replacement, True

    @_mutating
    def unbind_agent(self, project_id: str, agent_definition_id: str) -> bool:
        project = self.get(project_id)
        if not Path(project["root"]).is_dir():
            raise RegistryError("Project root is unavailable")
        if not isinstance(agent_definition_id, str) \
                or not AGENT_ID.fullmatch(agent_definition_id):
            raise RegistryError("Invalid agent definition id")
        payload = load_bindings(project["root"])
        remaining = [binding for binding in payload["bindings"]
                     if binding["agent_definition_id"] != agent_definition_id]
        if len(remaining) == len(payload["bindings"]):
            return False
        payload["bindings"] = remaining
        write_bindings(project["root"], payload)
        return True

    # -- discovery / migration -----------------------------------------
    def discover(self, base=".", *, max_depth=3, now=None) -> list[dict]:
        start = canonical_root(base)
        created = []
        for root in _project_roots(start, max_depth=max_depth):
            try:
                project, is_new = self.add(root, now=now)
            except RegistryError:
                continue
            if is_new:
                created.append(project)
        return created

    @_mutating
    def migrate(self) -> dict:
        data = self.load()
        self.save(data)
        return {"version": SCHEMA_VERSION, "projects": len(data["projects"]),
                "registry": str(self.path)}


def _project_roots(base: Path, *, max_depth: int) -> list[Path]:
    """Find project roots with an existing ``.auto-orchestrator`` directory."""
    skip = {"node_modules", "vendor", "dist", "build", "__pycache__", "venv", ".venv"}
    found: list[Path] = []
    stack = [(base, 0)]
    while stack:
        directory, depth = stack.pop()
        marker = directory / ".auto-orchestrator"
        try:
            if marker.is_dir() and not marker.is_symlink():
                found.append(directory)
        except OSError:
            continue
        if depth >= max_depth:
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or entry.is_symlink():
                continue
            if entry.name in skip or entry.name.startswith("."):
                continue
            stack.append((Path(entry.path), depth + 1))
    return sorted(found, key=lambda path: _path_key(path))


def bindings_path(root) -> Path:
    return Path(root) / DEFAULT_HOME_DIR / BINDINGS_NAME


def _validate_binding(raw: dict, *, agents: set[str] | None, hashes: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise RegistryError("Binding must be an object")
    if set(raw) - BINDING_FIELDS:
        raise RegistryError("Binding contains unsupported fields")
    agent_id = raw.get("agent_definition_id")
    if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id):
        raise RegistryError("Invalid binding agent id")
    if agents is not None and agent_id not in agents:
        raise RegistryError("Binding references an unknown agent")
    digest = _checked_native_hash(raw.get("native_hash"))
    if digest in hashes:
        raise RegistryError("Duplicate native hash binding")
    hashes.add(digest)
    return {
        "agent_definition_id": agent_id,
        "native_hash": digest,
        "run_id": _checked_safe_id(raw.get("run_id"), "run_id"),
        "session_id": _checked_safe_id(raw.get("session_id"), "session_id", required=False),
        "task_id": _checked_safe_id(raw.get("task_id"), "task_id", required=False),
        "bound_at": _number(raw.get("bound_at"), 0),
    }


def load_bindings(root, *, agents: list[str] | None = None) -> dict:
    """Read explicit project-local bindings; malformed files fail closed."""
    path = bindings_path(root)
    directory = path.parent
    if directory.is_symlink():
        raise RegistryError("Refusing to read bindings from a symbolic link directory")
    if path.is_symlink():
        raise RegistryError("Bindings file must not be a symbolic link")
    if not path.exists():
        return {"version": BINDINGS_VERSION, "bindings": []}
    if not path.is_file():
        raise RegistryError("Bindings path is not a regular file")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RegistryError("Bindings file is unreadable") from exc
    if size > MAX_BINDINGS_BYTES:
        raise RegistryError("Bindings file is too large")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RegistryError("Bindings file is unreadable") from exc
    except (ValueError, RecursionError) as exc:
        raise RegistryError("Bindings file is not valid JSON") from exc
    if not isinstance(data, dict) or data.get("version") != BINDINGS_VERSION:
        raise RegistryError("Unsupported bindings version")
    if set(data) - {"version", "bindings"}:
        raise RegistryError("Bindings file contains unsupported fields")
    raw_bindings = data.get("bindings")
    if not isinstance(raw_bindings, list) or len(raw_bindings) > MAX_BINDINGS:
        raise RegistryError("Bindings must be a bounded array")
    allowed = set(agents) if agents is not None else None
    hashes: set[str] = set()
    seen_agents: set[str] = set()
    bindings = []
    for raw in raw_bindings:
        binding = _validate_binding(raw, agents=allowed, hashes=hashes)
        if binding["agent_definition_id"] in seen_agents:
            raise RegistryError("Duplicate agent binding")
        seen_agents.add(binding["agent_definition_id"])
        bindings.append(binding)
    return {"version": BINDINGS_VERSION, "bindings": bindings}


def write_bindings(root, payload: dict) -> None:
    root = Path(root)
    if not root.is_dir():
        raise RegistryError("Project root is unavailable")
    path = bindings_path(root)
    directory = path.parent
    if directory.is_symlink():
        raise RegistryError("Refusing to write bindings into a symbolic link directory")
    if path.is_symlink():
        raise RegistryError("Refusing to write through a symbolic link")
    if not isinstance(payload, dict) or not isinstance(payload.get("bindings"), list):
        raise RegistryError("Bindings payload is invalid")
    if len(payload["bindings"]) > MAX_BINDINGS:
        raise RegistryError("Bindings must be a bounded array")
    hashes: set[str] = set()
    agents: set[str] = set()
    validated = []
    for raw in payload["bindings"]:
        binding = _validate_binding(raw, agents=None, hashes=hashes)
        if binding["agent_definition_id"] in agents:
            raise RegistryError("Duplicate agent binding")
        agents.add(binding["agent_definition_id"])
        validated.append(binding)
    encoded = (json.dumps({"version": BINDINGS_VERSION, "bindings": validated},
                          indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_BINDINGS_BYTES:
        raise RegistryError("Bindings file is too large")
    _atomic_write(path, encoded)


def _json_out(payload) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", type=Path, default=None,
                        help="Registry path (default: $AUTO_ORCHESTRATOR_HOME/projects.json)")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--registry", type=Path, default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", parents=[common], help="List registered projects")

    show = commands.add_parser("show", parents=[common], help="Show one project")
    show.add_argument("project_id")

    add = commands.add_parser("add", parents=[common], help="Register a project root")
    add.add_argument("--root", type=Path, required=True)
    add.add_argument("--name")
    add.add_argument("--description", default="")
    add.add_argument("--theme", choices=THEMES, default="custom")

    update = commands.add_parser("update", parents=[common], help="Update project metadata")
    update.add_argument("project_id")
    update.add_argument("--name")
    update.add_argument("--description")
    update.add_argument("--theme", choices=THEMES)

    remove = commands.add_parser("remove", parents=[common], help="Remove project metadata only")
    remove.add_argument("project_id")
    remove.add_argument("--force", action="store_true",
                        help="Acknowledge that evidence is not deleted")

    agent = commands.add_parser("add-agent", parents=[common], help="Add an agent definition")
    agent.add_argument("project_id")
    agent.add_argument("--name", required=True)
    agent.add_argument("--role", required=True)
    agent.add_argument("--platform", choices=PLATFORMS, default="unknown")
    agent.add_argument("--model")
    agent.add_argument("--color", default=DEFAULT_COLOR)

    bind = commands.add_parser("bind-agent", parents=[common], help="Write an explicit local binding")
    bind.add_argument("project_id")
    bind.add_argument("agent_definition_id")
    bind.add_argument("--native-hash", required=True)
    bind.add_argument("--run-id", required=True)
    bind.add_argument("--session-id")
    bind.add_argument("--task-id")

    unbind = commands.add_parser("unbind-agent", parents=[common], help="Remove one explicit binding")
    unbind.add_argument("project_id")
    unbind.add_argument("agent_definition_id")

    discover = commands.add_parser("discover", parents=[common],
                                   help="Register projects with evidence")
    discover.add_argument("base", nargs="?", default=".")
    discover.add_argument("--max-depth", type=int, default=3)

    commands.add_parser("migrate", parents=[common],
                        help="Rewrite the registry in schema version 2")

    args = parser.parse_args(argv)
    try:
        registry = ProjectRegistry(args.registry)
        if args.command == "list":
            payload = registry.load()
            if args.json:
                _json_out(payload)
            else:
                for project in payload["projects"]:
                    print(f"{project['project_id']}  {project['theme']:<8}  "
                          f"{project['name']}\t{project['root']}")
            return 0
        if args.command == "show":
            project = registry.get(args.project_id)
            if args.json:
                _json_out(project)
            else:
                print(json.dumps(project, indent=2, ensure_ascii=False))
            return 0
        if args.command == "add":
            project, created = registry.add(args.root, name=args.name,
                                            description=args.description, theme=args.theme)
            if args.json:
                _json_out({"created": created, "project": project})
            else:
                print(("Registered " if created else "Already registered ")
                      + project["project_id"] + " " + project["root"])
            return 0
        if args.command == "update":
            project = registry.update(args.project_id, name=args.name,
                                      description=args.description, theme=args.theme)
            _json_out(project) if args.json else print(f"Updated {project['project_id']}")
            return 0
        if args.command == "remove":
            if not args.force:
                parser.error("remove requires --force (metadata only; evidence is not deleted)")
            project = registry.remove(args.project_id)
            _json_out(project) if args.json else print(
                f"Removed metadata for {project['project_id']}; evidence untouched")
            return 0
        if args.command == "add-agent":
            agent = registry.add_agent(args.project_id, name=args.name, role=args.role,
                                       platform=args.platform, model=args.model,
                                       color=args.color)
            _json_out(agent) if args.json else print(
                f"Added {agent['agent_definition_id']} ({agent['name']})")
            return 0
        if args.command == "bind-agent":
            binding, created = registry.bind_agent(
                args.project_id, args.agent_definition_id, args.native_hash,
                run_id=args.run_id, session_id=args.session_id, task_id=args.task_id)
            if args.json:
                _json_out({"created": created, "binding": binding})
            else:
                print(("Bound " if created else "Already bound ")
                      + binding["agent_definition_id"])
            return 0
        if args.command == "unbind-agent":
            removed = registry.unbind_agent(args.project_id, args.agent_definition_id)
            print("Unbound" if removed else "No binding found")
            return 0
        if args.command == "discover":
            created = registry.discover(args.base, max_depth=max(0, min(8, args.max_depth)))
            if args.json:
                _json_out({"created": len(created), "projects": created})
            else:
                print(f"Discovered {len(created)} new project(s)")
                for project in created:
                    print(f"  {project['project_id']}  {project['root']}")
            return 0
        if args.command == "migrate":
            result = registry.migrate()
            _json_out(result) if args.json else print(
                f"Registry schema {result['version']} with {result['projects']} project(s)")
            return 0
    except (RegistryError, OSError, ValueError) as exc:
        print(f"REGISTRY ERROR: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
