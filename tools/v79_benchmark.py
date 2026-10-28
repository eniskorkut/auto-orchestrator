#!/usr/bin/env python3
"""V7.9 benchmark corpus and honest measurement harness.

The corpus is deterministic and checked by tests. This tool never invents
timings, token counts or cost: ``--list``/``--validate`` produce no metrics,
and ``--run`` records only what a real command reported.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from pathlib import Path

CORPUS = [
    {
        "id": "small-bugfix",
        "category": "bugfix",
        "risk": "low",
        "request": "Fix the failing pagination boundary test.",
        "acceptance": ["the failing test passes", "no unrelated file changes"],
        "test_command": "python3 -m unittest",
    },
    {
        "id": "medium-backend",
        "category": "backend_feature",
        "risk": "medium",
        "request": "Add an idempotent order endpoint with tests.",
        "acceptance": ["endpoint exists", "tests cover duplicate submission"],
        "test_command": "python3 -m unittest",
    },
    {
        "id": "frontend-integration",
        "category": "frontend_backend",
        "risk": "medium",
        "request": "Connect the settings page to the new API and add a test.",
        "acceptance": ["UI calls the API", "error path is tested"],
        "test_command": "npm test",
    },
    {
        "id": "auth-negative",
        "category": "authentication",
        "risk": "high",
        "request": "Implement JWT refresh with expiry and invalid-token tests.",
        "acceptance": ["refresh works", "expired token rejected",
                       "invalid token rejected"],
        "test_command": "python3 -m unittest",
    },
    {
        "id": "database-migration",
        "category": "migration",
        "risk": "high",
        "request": "Add a reversible migration for the sessions table.",
        "acceptance": ["up and down migration pass", "no data loss"],
        "test_command": "python3 -m unittest",
    },
    {
        "id": "api-compat",
        "category": "api_compatibility",
        "risk": "high",
        "request": "Add optional pagination without breaking the old response.",
        "acceptance": ["old fields remain", "new fields are optional"],
        "test_command": "python3 -m unittest",
    },
    {
        "id": "concurrency-bug",
        "category": "concurrency",
        "risk": "high",
        "request": "Fix the double-write race in the claim path.",
        "acceptance": ["exactly one claim wins", "race test is deterministic"],
        "test_command": "python3 -m unittest",
    },
    {
        "id": "repair-failing-tests",
        "category": "repair",
        "risk": "medium",
        "request": "Repair the three failing tests without weakening them.",
        "acceptance": ["all tests pass", "assertions are unchanged"],
        "test_command": "python3 -m unittest",
    },
    {
        "id": "multi-module",
        "category": "multi_module",
        "risk": "high",
        "request": "Move the shared model to a package and update both modules.",
        "acceptance": ["both modules import the package", "tests pass"],
        "test_command": "python3 -m unittest",
    },
    {
        "id": "security-sensitive",
        "category": "security",
        "risk": "high",
        "request": "Reject path traversal in the file download handler.",
        "acceptance": ["traversal is rejected", "normal download still works"],
        "test_command": "python3 -m unittest",
    },
    {
        "id": "grill-ambiguous",
        "category": "requirements",
        "risk": "medium",
        "request": "Improve authentication somehow.",
        "acceptance": ["critical questions answered", "contract approved"],
        "test_command": "python3 -m unittest",
    },
    {
        "id": "handoff-continuation",
        "category": "handoff",
        "risk": "medium",
        "request": "Complete a half-finished README task in a second IDE.",
        "acceptance": ["same run id", "next action preserved",
                       "no duplicate requirement"],
        "test_command": "python3 -m unittest",
    },
]

MODES = ("solo", "fast", "balanced", "rigorous", "auto")


def validate_corpus(corpus=None) -> dict:
    items = corpus if corpus is not None else CORPUS
    errors = []
    seen = set()
    categories = set()
    for item in items:
        if not isinstance(item, dict):
            errors.append("corpus item is not an object")
            continue
        for field in ("id", "category", "risk", "request", "acceptance",
                      "test_command"):
            if not item.get(field):
                errors.append(f"{item.get('id')}: missing {field}")
        if item.get("id") in seen:
            errors.append(f"duplicate id: {item.get('id')}")
        seen.add(item.get("id"))
        categories.add(item.get("category"))
        if item.get("risk") not in ("low", "medium", "high"):
            errors.append(f"{item.get('id')}: invalid risk")
    return {"total": len(items), "categories": sorted(categories),
            "errors": errors, "ok": not errors}


def _run_command(root: Path, command: str, timeout: int) -> dict:
    started = time.time()
    try:
        process = subprocess.run(
            shlex.split(command), cwd=str(root), capture_output=True,
            text=True, timeout=timeout)
        return {
            "exit_code": process.returncode,
            "seconds": round(time.time() - started, 3),
            "stdout_tail": (process.stdout or "")[-2000:],
            "stderr_tail": (process.stderr or "")[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "seconds": round(time.time() - started, 3),
                "error": f"timeout after {timeout}s"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--run", metavar="ID")
    parser.add_argument("--project",
                        help="prepared project root for --run; required for a "
                             "real measurement")
    parser.add_argument("--json-out")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps({"schema_version": 1, "kind": "v79-benchmark-corpus",
                          "modes": list(MODES), "tasks": CORPUS},
                         indent=2, ensure_ascii=False))
        return 0
    if args.validate:
        report = validate_corpus()
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    task = next((item for item in CORPUS if item["id"] == args.run), None)
    if task is None:
        parser.error("unknown --run id; use --list")
    if not args.project:
        parser.error("--run requires --project with a prepared fixture; no "
                     "measurement is fabricated")
    root = Path(args.project).resolve()
    if not root.is_dir():
        parser.error("--project must be an existing directory")
    result = {
        "schema_version": 1,
        "kind": "v79-benchmark-run",
        "task": task,
        "mode": "measured" if args.run else "not_run",
        "root": "$PROJECT",
        "command": task["test_command"],
        "measured": _run_command(root, task["test_command"], args.timeout),
        "note": ("Only the reported exit code and wall time are measured. "
                 "Token and cost fields are absent, not zero."),
    }
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
