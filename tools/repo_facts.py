#!/usr/bin/env python3
"""Bounded, read-only repository fact detection for assumption prevention.

Only file names and small text snippets are inspected. Secret-like files are
skipped. The result is evidence that can resolve a requirement from code; it is
never treated as a user decision.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

MAX_FILES = 2000
MAX_BYTES = 32768
SKIP_DIRS = {
    ".git", ".auto-orchestrator", "node_modules", "vendor", "dist", "build",
    ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
}
SKIP_NAMES = {".env", ".env.local", "auth.json", "credentials", "secrets"}
TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".toml",
    ".yaml", ".yml", ".sql", ".md", ".go", ".rs", ".java", ".rb", ".php",
}
KEYWORDS = {
    "jwt": ("jwt", "jsonwebtoken", "pyjwt", "access_token", "refresh_token"),
    "session": ("session", "httpOnly", "cookie", "express-session", "flask-session"),
    "postgres": ("postgres", "postgresql", "psycopg", "pg8000"),
    "sqlite": ("sqlite", "sqlite3"),
    "mysql": ("mysql", "mariadb"),
    "redis": ("redis",),
    "s3": ("s3://", "boto3", "aws s3", "object storage"),
    "auth": ("auth", "login", "password", "oauth", "sso"),
    "multiuser": ("tenant", "organization", "role", "permission", "rbac"),
}


_WORD_CACHE: dict[str, re.Pattern] = {}


def _contains(lower: str, needle: str) -> bool:
    """Match short words at token boundaries; keep phrases/substrings exact."""
    if " " in needle or not re.fullmatch(r"[a-z0-9_]+", needle):
        return needle in lower
    pattern = _WORD_CACHE.get(needle)
    if pattern is None:
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])")
        _WORD_CACHE[needle] = pattern
    return pattern.search(lower) is not None


def _text_file(path: Path) -> str:
    if path.is_symlink() or path.suffix.lower() not in TEXT_SUFFIXES:
        return ""
    if any(part.lower() in SKIP_NAMES for part in path.parts):
        return ""
    try:
        if path.stat().st_size > MAX_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def scan_repository(root) -> dict:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("repository root does not exist")
    files = 0
    found = {key: [] for key in KEYWORDS}
    backend = False
    frontend = False
    languages = set()
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIP_DIRS and not name.startswith(".")
        )
        for filename in sorted(filenames):
            path = Path(current) / filename
            files += 1
            if files > MAX_FILES:
                break
            relative = path.relative_to(root)
            parts = {part.lower() for part in relative.parts}
            if parts & {"backend", "server", "api"}:
                backend = True
            if parts & {"frontend", "web", "ui", "client"}:
                frontend = True
            if path.suffix.lower() in TEXT_SUFFIXES:
                languages.add(path.suffix.lower().lstrip("."))
            text = _text_file(path)
            if not text:
                continue
            lower = text.lower() + "\n" + relative.as_posix().lower()
            for key, needles in KEYWORDS.items():
                if any(_contains(lower, needle) for needle in needles):
                    if len(found[key]) < 5:
                        found[key].append(str(relative))
        if files > MAX_FILES:
            break
    facts = {
        "files_scanned": min(files, MAX_FILES),
        "truncated": files > MAX_FILES,
        "backend": backend,
        "frontend": frontend,
        "languages": sorted(languages)[:12],
        "detected": {key: bool(value) for key, value in found.items()},
        "evidence": {key: value for key, value in found.items() if value},
        "source": "code_observed",
    }
    return facts
