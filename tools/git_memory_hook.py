#!/usr/bin/env python3
"""Opt-in Git post-commit memory capture.

The hook is explicitly installed by the user, preserves any existing hook body,
and only calls the local universal Teamwork CLI. It never interpolates user or
repository text into a shell command.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

BEGIN = "# auto-orchestrator:memory-capture:begin"
END = "# auto-orchestrator:memory-capture:end"
BLOCK = f"""{BEGIN}
if command -v git >/dev/null 2>&1; then
  ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
  cd "$ROOT" || exit 0
  if [ -f "$ROOT/tools/universal_teamwork.py" ]; then
    if command -v python3.11 >/dev/null 2>&1; then
      PY=python3.11
    elif command -v python3 >/dev/null 2>&1; then
      PY=python3
    else
      exit 0
    fi
    "$PY" "$ROOT/tools/universal_teamwork.py" --root "$ROOT" \\
      --platform "${{AO_PLATFORM:-unknown}}" memory capture >/dev/null 2>&1 || true
  fi
fi
{END}
"""


class HookError(ValueError):
    pass


def hooks_dir(root) -> Path:
    root = Path(root).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HookError("Git repository is unavailable") from exc
    if result.returncode != 0:
        raise HookError("not a Git repository")
    value = result.stdout.strip()
    if not value:
        raise HookError("Git hooks directory is unavailable")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def hook_path(root) -> Path:
    return hooks_dir(root) / "post-commit"


def status(root) -> dict:
    path = hook_path(root)
    if path.is_symlink():
        raise HookError("post-commit hook must not be a symlink")
    if not path.is_file():
        return {"path": str(path), "installed": False, "managed": False}
    text = path.read_text(encoding="utf-8", errors="replace")
    return {"path": str(path), "installed": True,
            "managed": BEGIN in text and END in text}


def install(root, *, dry_run=False) -> dict:
    path = hook_path(root)
    if path.is_symlink():
        raise HookError("post-commit hook must not be a symlink")
    existing = (path.read_text(encoding="utf-8", errors="replace")
                if path.is_file() else "")
    if BEGIN in existing or END in existing:
        if existing.count(BEGIN) != 1 or existing.count(END) != 1:
            raise HookError("managed hook markers are inconsistent")
        updated = re.sub(
            re.escape(BEGIN) + r".*?" + re.escape(END),
            BLOCK.rstrip("\n"), existing, flags=re.DOTALL)
        state = "unchanged" if updated == existing else "updated"
    else:
        prefix = existing
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        updated = prefix + BLOCK
        state = "installed"
    if not dry_run and state != "unchanged":
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
        try:
            temporary.write_text(updated, encoding="utf-8")
            if not path.exists():
                temporary.chmod(0o755)
            os.replace(temporary, path)
            try:
                path.chmod(path.stat().st_mode | 0o111)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return {"path": str(path), "state": state,
            "installed": True, "managed": True}


def uninstall(root, *, dry_run=False) -> dict:
    path = hook_path(root)
    if path.is_symlink():
        raise HookError("post-commit hook must not be a symlink")
    if not path.is_file():
        return {"path": str(path), "state": "missing", "installed": False}
    text = path.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        return {"path": str(path), "state": "preserved",
                "installed": True, "managed": False}
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise HookError("managed hook markers are inconsistent")
    updated = re.sub(
        r"\n?" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?",
        "\n", text, flags=re.DOTALL)
    empty = updated.strip() in ("", "#!/bin/sh", "#!/usr/bin/env sh")
    if not dry_run:
        if empty:
            path.unlink()
        else:
            if not updated.endswith("\n"):
                updated += "\n"
            path.write_text(updated, encoding="utf-8")
    return {"path": str(path),
            "state": ("would-remove" if dry_run else "removed")
            if empty else ("would-update" if dry_run else "updated"),
            "installed": not empty,
            "managed": False}
