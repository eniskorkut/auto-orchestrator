#!/usr/bin/env python3
"""Bounded, read-only repository profile for adaptive teamwork planning.

The profile contains counts and relative structural facts only. It never reads
source contents, prompts, secrets, environment values or absolute paths.
"""
from __future__ import annotations

import os
from pathlib import Path

MAX_FILES = 5000
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".auto-orchestrator", "node_modules", "vendor",
    "dist", "build", ".venv", "venv", "__pycache__", ".pytest_cache",
}
LANGUAGE_SUFFIX = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".java": "java",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".swift": "swift",
    ".sql": "sql",
}
TEST_MARKERS = ("test_", "_test.", ".test.", ".spec.")


class RepoProfileError(ValueError):
    pass


def profile_repository(root: Path, *, max_files: int = MAX_FILES) -> dict:
    root = Path(root).resolve()
    if not root.is_dir():
        raise RepoProfileError("repository root does not exist")
    if type(max_files) is not int or not 1 <= max_files <= 20000:
        raise RepoProfileError("max_files must be 1..20000")
    files = 0
    tests = 0
    languages = {}
    modules = set()
    backend = False
    frontend = False
    has_manifest = False
    has_agents = False
    truncated = False
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIP_DIRS and not name.startswith(".")
        )
        for filename in sorted(filenames):
            path = Path(current) / filename
            if path.is_symlink():
                continue
            files += 1
            if files > max_files:
                truncated = True
                break
            relative = path.relative_to(root)
            parts = relative.parts
            if any(part in ("backend", "server", "api") for part in parts):
                backend = True
            if any(part in ("frontend", "web", "ui", "client") for part in parts):
                frontend = True
            if len(parts) > 1:
                modules.add(parts[0])
            suffix = path.suffix.lower()
            language = LANGUAGE_SUFFIX.get(suffix)
            if language:
                languages[language] = languages.get(language, 0) + 1
            lower = filename.lower()
            if any(marker in lower for marker in TEST_MARKERS):
                tests += 1
            if lower in ("package.json", "pyproject.toml", "cargo.toml",
                         "go.mod", "pom.xml", "build.gradle"):
                has_manifest = True
            if lower == "agents.md":
                has_agents = True
        if truncated:
            break
    test_commands = []
    if tests:
        test_commands.append("python3.11 -m unittest discover -s tests -q")
    if "javascript" in languages or "typescript" in languages:
        test_commands.append("npm test -- --runInBand")
    return {
        "version": 1,
        "files": min(files, max_files),
        "truncated": truncated,
        "modules": min(len(modules), 200),
        "tests": tests,
        "languages": sorted(languages, key=lambda name: (-languages[name], name)),
        "language_files": dict(sorted(languages.items())),
        "backend": backend,
        "frontend": frontend,
        "has_manifest": has_manifest,
        "has_agents_md": has_agents,
        "test_commands": test_commands[:4],
        "limits": [
            "Counts are bounded; hidden/build/vendor directories are skipped.",
            "No source, prompt, secret or absolute path is included.",
        ],
    }
