#!/usr/bin/env python3
"""Bounded, read-only repository fact detection for assumption prevention.

Only code/config file names and small text snippets are inspected. Secret-like
files are skipped. Documentation and test-only mentions are reported separately
and never used as proof that a technology is implemented: a README sentence or a
comment must not suppress a critical requirement question.
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
CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json",
    ".toml", ".yaml", ".yml", ".sql", ".go", ".rs", ".java", ".rb", ".php",
    ".sh", ".bash", ".zsh", ".cs", ".kt", ".kts", ".swift", ".c", ".h",
    ".cc", ".cpp", ".hpp",
}
DOC_SUFFIXES = {".md", ".mdx", ".rst", ".txt", ".adoc"}
TEXT_SUFFIXES = CODE_SUFFIXES | DOC_SUFFIXES
TEST_DIRS = {"test", "tests", "spec", "specs", "__tests__", "e2e"}
KEYWORDS = {
    "jwt": ("jwt", "jsonwebtoken", "pyjwt", "access_token", "refresh_token"),
    "session": ("session cookie", "session_cookie", "httponly",
                "express-session", "flask-session", "django.contrib.sessions",
                "sessionmiddleware", "req.session", "request.session",
                "sessionid"),
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
    needle = needle.lower()
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


def _is_test_path(relative: Path) -> bool:
    parts = {part.lower() for part in relative.parts}
    if parts & TEST_DIRS:
        return True
    name = relative.name.lower()
    return (name.startswith("test_") or name.startswith("test-")
            or "_test." in name or "_spec." in name
            or ".test." in name or ".spec." in name)


def _strip_comments(text: str, suffix: str) -> str:
    """Remove comments so prose in code cannot prove a technology exists."""
    suffix = suffix.lower()
    if suffix in {".py", ".pyi", ".rb", ".sh", ".bash", ".zsh", ".toml",
                  ".yaml", ".yml"}:
        if suffix in {".py", ".pyi"}:
            # Docstrings are documentation, not executable evidence.
            text = re.sub(r'""".*?"""', " ", text, flags=re.DOTALL)
            text = re.sub(r"'''.*?'''", " ", text, flags=re.DOTALL)
        lines = []
        for line in text.splitlines():
            index = line.find("#")
            lines.append(line if index < 0 else line[:index])
        return "\n".join(lines)
    if suffix == ".sql":
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
        lines = []
        for line in text.splitlines():
            index = line.find("--")
            lines.append(line if index < 0 else line[:index])
        return "\n".join(lines)
    if suffix in {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs",
                  ".java", ".php", ".cs", ".kt", ".kts", ".swift", ".c", ".h",
                  ".cc", ".cpp", ".hpp"}:
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
        # Do not swallow URLs such as https://example.invalid.
        return re.sub(r"(?<!:)//[^\n]*", " ", text)
    return text


def _match(text: str, relative: Path) -> set[str]:
    lower = text.lower() + "\n" + relative.as_posix().lower()
    return {key for key, needles in KEYWORDS.items()
            if any(_contains(lower, needle) for needle in needles)}


def scan_repository(root) -> dict:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("repository root does not exist")
    files = 0
    code_found = {key: [] for key in KEYWORDS}
    docs_found = {key: [] for key in KEYWORDS}
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
            suffix = path.suffix.lower()
            text = _text_file(path)
            if not text:
                continue
            if suffix in DOC_SUFFIXES:
                for key in _match(text, relative):
                    if len(docs_found[key]) < 5:
                        docs_found[key].append(relative.as_posix())
                continue
            if suffix not in CODE_SUFFIXES:
                continue
            languages.add(suffix.lstrip("."))
            # Test code mentions an expectation, not a production choice.
            if _is_test_path(relative):
                continue
            code_text = _strip_comments(text, suffix)
            for key in _match(code_text, relative):
                if len(code_found[key]) < 5:
                    code_found[key].append(relative.as_posix())
        if files > MAX_FILES:
            break
    code_detected = {key: bool(value) for key, value in code_found.items()}
    docs_detected = {key: bool(value) for key, value in docs_found.items()}
    facts = {
        "files_scanned": min(files, MAX_FILES),
        "truncated": files > MAX_FILES,
        "backend": backend,
        "frontend": frontend,
        "languages": sorted(languages)[:12],
        "code_detected": code_detected,
        "docs_detected": docs_detected,
        "code_evidence": {key: value for key, value in code_found.items() if value},
        "docs_evidence": {key: value for key, value in docs_found.items() if value},
        # Backward-compatible aliases: only executable code/config counts.
        "detected": code_detected,
        "evidence": {key: value for key, value in code_found.items() if value},
        "source": "code_observed",
    }
    return facts
