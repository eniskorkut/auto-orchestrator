#!/usr/bin/env python3
"""Portable project identity and local instance identity.

The portable manifest is deliberately small and contains no prompts, source,
secrets or machine paths. It can be committed so the same logical project keeps
its ID after a clone or move. A separate ignored instance file distinguishes
local working copies.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path

try:
    from tools.run_owner import atomic_write_json, read_json
except ModuleNotFoundError:
    from run_owner import atomic_write_json, read_json  # type: ignore

PROJECT_ID = re.compile(r"^p_[0-9a-f]{16}$")
UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
MANIFEST_VERSION = 1
INSTANCE_VERSION = 1


class ProjectIdentityError(ValueError):
    pass


def identity_path(root: Path) -> Path:
    return Path(root) / ".auto-orchestrator" / "project.json"


def instance_path(root: Path) -> Path:
    return Path(root) / ".auto-orchestrator" / "instance.json"


def project_id_for_uuid(value: str) -> str:
    if not isinstance(value, str) or not UUID.fullmatch(value.lower()):
        raise ProjectIdentityError("invalid project UUID")
    digest = hashlib.sha256(("auto-orchestrator|portable|" + value.lower()).encode()).hexdigest()
    return "p_" + digest[:16]


def _root(value) -> Path:
    try:
        root = Path(value).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise ProjectIdentityError("project root does not exist") from exc
    if not root.is_dir():
        raise ProjectIdentityError("project root must be a directory")
    return root


def _load_manifest(root: Path) -> dict | None:
    path = identity_path(root)
    if path.is_symlink():
        raise ProjectIdentityError("project manifest must not be a symlink")
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > 16_384:
            raise ProjectIdentityError("project manifest is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProjectIdentityError("project manifest is invalid") from exc
    if not isinstance(value, dict):
        raise ProjectIdentityError("project manifest must be an object")
    if value.get("version") != MANIFEST_VERSION:
        raise ProjectIdentityError("unsupported project manifest version")
    uuid_value = value.get("project_uuid")
    project_id = value.get("project_id")
    if not isinstance(uuid_value, str) or not UUID.fullmatch(uuid_value.lower()):
        raise ProjectIdentityError("project manifest UUID is invalid")
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
        raise ProjectIdentityError("project manifest project id is invalid")
    scheme = value.get("id_scheme", "uuid")
    if scheme not in ("uuid", "legacy"):
        raise ProjectIdentityError("project manifest id scheme is invalid")
    if scheme == "uuid" and project_id_for_uuid(uuid_value) != project_id:
        raise ProjectIdentityError("project manifest id does not match UUID")
    name = value.get("name", "")
    if not isinstance(name, str) or len(name) > 120:
        raise ProjectIdentityError("project manifest name is invalid")
    return value


def _ensure_local_instance(root: Path, manifest: dict) -> dict:
    path = instance_path(root)
    existing = read_json(path, None)
    root_hash = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    if isinstance(existing, dict):
        if (existing.get("project_uuid") == manifest["project_uuid"]
                and existing.get("root_hash") == root_hash):
            return existing
        # A copied directory can carry a stale local instance; replace it
        # locally rather than reusing another machine's instance identity.
    value = {
        "version": INSTANCE_VERSION,
        "instance_id": "i_" + secrets.token_hex(8),
        "project_uuid": manifest["project_uuid"],
        "root_hash": root_hash,
        "created_at": time.time(),
    }
    atomic_write_json(path, value)
    return value


def ensure_project_identity(root, *, legacy_project_id: str | None = None) -> dict:
    """Return stable portable and local identities, creating them if needed.

    ``legacy_project_id`` lets an existing registry entry keep its old p_ id
    while a portable UUID is introduced. The manifest then preserves that id
    across clones and moves.
    """
    root = _root(root)
    manifest = _load_manifest(root)
    migrated = False
    if manifest is None:
        if legacy_project_id is not None:
            if not isinstance(legacy_project_id, str) or not PROJECT_ID.fullmatch(
                    legacy_project_id):
                raise ProjectIdentityError("legacy project id is invalid")
            project_id = legacy_project_id
        else:
            project_id = None
        uuid_value = str(uuid.uuid4())
        if project_id is None:
            project_id = project_id_for_uuid(uuid_value)
        manifest = {
            "version": MANIFEST_VERSION,
            "project_uuid": uuid_value,
            "project_id": project_id,
            "id_scheme": "legacy" if legacy_project_id is not None else "uuid",
            "name": root.name[:120] or "project",
            "created_at": time.time(),
        }
        path = identity_path(root)
        if path.parent.is_symlink():
            raise ProjectIdentityError("project state directory must not be a symlink")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_json(path, manifest)
        migrated = True
    instance = _ensure_local_instance(root, manifest)
    return {
        "version": MANIFEST_VERSION,
        "project_id": manifest["project_id"],
        "project_uuid": manifest["project_uuid"],
        "name": manifest.get("name", ""),
        "root": str(root),
        "instance_id": instance.get("instance_id"),
        "manifest": str(identity_path(root)),
        "instance": str(instance_path(root)),
        "migrated": migrated,
        "portable": True,
    }


def read_project_identity(root) -> dict | None:
    root = _root(root)
    manifest = _load_manifest(root)
    if manifest is None:
        return None
    instance = read_json(instance_path(root), {})
    return {
        **manifest,
        "root": str(root),
        "instance_id": instance.get("instance_id") if isinstance(instance, dict) else None,
        "manifest": str(identity_path(root)),
        "instance": str(instance_path(root)),
    }


def export_identity(root) -> dict:
    identity = ensure_project_identity(root)
    return {
        "version": MANIFEST_VERSION,
        "project_uuid": identity["project_uuid"],
        "project_id": identity["project_id"],
        "name": identity["name"],
    }
