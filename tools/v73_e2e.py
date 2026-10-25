#!/usr/bin/env python3
"""Build the disposable V7.3 sample project and run it through Teamwork.

The scaffold contains only a README, an API contract and a git repository. The
product code, tests and browser E2E are produced by real managed OpenCode
sessions; this script never writes application code on their behalf.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from tools.teamwork_runner import TeamworkRunner
    from tools.teamwork import TeamworkEngine
    from tools.worktree import read_manifest
except ModuleNotFoundError:  # python tools/v73_e2e.py
    from teamwork_runner import TeamworkRunner  # type: ignore
    from teamwork import TeamworkEngine  # type: ignore
    from worktree import read_manifest  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "opencode-go/deepseek-v4.1-flash"
CONTRACT = """# Teamwork E2E sample contract

The orchestrator builds this project with managed sessions. Do not rewrite this
file or README.md. The product is a small in-memory task manager.

## Backend (Python 3.11 stdlib)

- `GET /api/tasks` -> `200 {"tasks": [{"id": 1, "title": "...", "done": false}]}`
- `POST /api/tasks` body `{"title": "..."}`:
  - 1..80 non-whitespace characters -> `201 {"task": {...}}`
  - invalid -> `400 {"error": "..."}`
- `PATCH /api/tasks/<id>` body `{"done": true|false}` -> `200 {"task": {...}}` or 404.
- `GET /`, `/app.js`, `/styles.css`, `/config.js` serve files from `frontend/`.
- `python3.11 backend/app.py --port 0 --print-port` prints the bound port and
  serves until interrupted.
- No third-party packages. Data is in memory.

## Frontend (no build step)

- `frontend/index.html`, `frontend/styles.css`, `frontend/app.js`,
  `frontend/config.js`.
- `frontend/config.js` sets `window.AO_API_BASE = ""` by default.
- List tasks, create a task, show validation errors, toggle done.
- Requests are relative to `window.AO_API_BASE`.

## Tests

- Backend: `python3.11 -m unittest discover -s tests -v`.
- Browser E2E: CommonJS `e2e/check.cjs`, uses `require("playwright")` supplied
  through `NODE_PATH`, starts the backend, creates a task, rejects an empty
  title, toggles done, asserts the DOM.
"""

TASKS = [
    {
        "id": "explore", "role": "explorer", "milestone": "M1", "phase": "build",
        "objective": "Inspect the scaffold, confirm the API/frontend boundary and test strategy.",
        "acceptance": ["Report names the API contract, ownership boundaries and verification commands."],
        "read_scope": ["README.md", "CONTRACT.md"], "write_scope": [],
        "depends_on": [], "timeout_seconds": 600, "max_retries": 1,
        "verify_commands": [], "department": "research",
    },
    {
        "id": "backend", "role": "implementer", "milestone": "M2", "phase": "build",
        "objective": "Implement the stdlib backend from CONTRACT.md and focused backend unit tests.",
        "acceptance": ["Backend implements create/list/toggle and validation from CONTRACT.md.",
                       "Backend unit tests pass."],
        "read_scope": ["CONTRACT.md"], "write_scope": ["backend/", "tests/"],
        "depends_on": ["explore"], "timeout_seconds": 1800, "max_retries": 2,
        "verify_commands": ["python3.11 -m unittest tests.test_backend -v"],
        "department": "backend",
    },
    {
        "id": "frontend", "role": "implementer", "milestone": "M2", "phase": "build",
        "objective": "Implement the static frontend and browser E2E from CONTRACT.md.",
        "acceptance": ["Frontend lists, creates and toggles tasks using the documented API.",
                       "frontend/config.js exposes AO_API_BASE and the browser E2E script is present."],
        "read_scope": ["CONTRACT.md"], "write_scope": ["frontend/", "e2e/"],
        "depends_on": ["explore"], "timeout_seconds": 1800, "max_retries": 2,
        "verify_commands": ["node --check frontend/app.js"],
        "department": "frontend",
    },
    {
        "id": "integrate", "role": "tester", "milestone": "M2", "phase": "verify",
        "objective": "Run integrated backend unit tests and the browser E2E; reproduce any failure.",
        "acceptance": ["Backend unit tests pass on the integrated tree.",
                       "Browser E2E passes against the integrated tree."],
        "read_scope": ["backend/", "frontend/", "e2e/", "tests/"],
        "write_scope": ["tests/integration/"],
        "depends_on": ["backend", "frontend"], "timeout_seconds": 1800, "max_retries": 2,
        "verify_commands": ["python3.11 -m unittest discover -s tests -v",
                            "node e2e/check.cjs"],
        "department": "qa",
    },
    {
        "id": "critic", "role": "reviewer", "milestone": "M2", "phase": "verify",
        "objective": "Independently review the integrated backend/frontend for logic errors, races and regressions.",
        "acceptance": ["Reviewer returns an explicit verdict and actionable findings."],
        "read_scope": ["backend/", "frontend/", "tests/", "e2e/"], "write_scope": [],
        "depends_on": ["integrate"], "timeout_seconds": 1200, "max_retries": 1,
        "verify_commands": ["python3.11 -m unittest discover -s tests -v"],
        "department": "review",
    },
    {
        "id": "challenger", "role": "challenger", "milestone": "M2", "phase": "verify",
        "objective": "Add adversarial edge-case tests and try to falsify the implementation claims.",
        "acceptance": ["Challenger tests cover empty/trimmed/too-long titles and invalid IDs.",
                       "Challenger tests pass."],
        "read_scope": ["backend/", "frontend/", "tests/"],
        "write_scope": ["tests/challenger/"],
        "depends_on": ["integrate"], "timeout_seconds": 1200, "max_retries": 1,
        "verify_commands": ["python3.11 -m unittest discover -s tests -v"],
        "department": "review",
    },
    {
        "id": "auditor", "role": "auditor", "milestone": "M2", "phase": "verify",
        "objective": "Audit commands, exit codes, changed files and acceptance criteria independently.",
        "acceptance": ["Auditor returns command evidence and an explicit verdict."],
        "read_scope": ["backend/", "frontend/", "tests/", "e2e/"], "write_scope": [],
        "depends_on": ["integrate", "critic", "challenger"],
        "timeout_seconds": 1800, "max_retries": 1,
        "verify_commands": ["python3.11 -m unittest discover -s tests -v",
                            "node e2e/check.cjs"],
        "department": "review",
    },
    {
        "id": "success", "role": "success-auditor", "milestone": "M3", "phase": "verify",
        "objective": "Run final independent E2E and reject success if any acceptance evidence is missing.",
        "acceptance": ["Final backend and browser E2E pass.", "All milestone evidence is present."],
        "read_scope": ["backend/", "frontend/", "tests/", "e2e/"], "write_scope": [],
        "depends_on": ["auditor"], "timeout_seconds": 1800, "max_retries": 1,
        "verify_commands": ["python3.11 -m unittest discover -s tests -v",
                            "node e2e/check.cjs"],
        "department": "review",
    },
]


def make_plan(run_id: str, model: str) -> dict:
    tasks = []
    for task in TASKS:
        item = dict(task)
        item["provider"] = "opencode"
        item["model"] = model
        tasks.append(item)
    return {
        "version": 1,
        "run_id": run_id,
        "max_parallel": 3,
        "pattern": "distributed_coding",
        "milestones": [
            {
                "id": "M1", "objective": "Discover the scaffold and confirm boundaries",
                "depends_on": [], "acceptance": ["Explorer report is present."],
                "verification_policy": {"require_roles": [], "max_repair": 0},
            },
            {
                "id": "M2", "objective": "Build, integrate, test and independently review",
                "depends_on": ["M1"],
                "acceptance": ["Backend and browser E2E pass.", "Critic, challenger and auditor pass."],
                "verification_policy": {
                    "require_roles": ["reviewer", "challenger", "auditor"],
                    "max_repair": 2,
                },
            },
            {
                "id": "M3", "objective": "Final independent success audit",
                "depends_on": ["M2"],
                "acceptance": ["Final success auditor runs the cross-stack E2E and passes."],
                "verification_policy": {"require_roles": ["success-auditor"], "max_repair": 1},
            },
        ],
        "tasks": tasks,
    }


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(root), *args],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=60)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:1200])
    return result


def prepare_sample(root: Path, *, reset: bool = False) -> Path:
    root = Path(root).resolve()
    if reset and root.exists():
        shutil.rmtree(root)
    worktrees = root.parent / (root.name + "-auto-orchestrator-worktrees")
    if reset and worktrees.exists():
        shutil.rmtree(worktrees)
    root.mkdir(parents=True, exist_ok=True)
    if (root / ".git").exists():
        return root
    (root / "README.md").write_text(
        "# teamwork-e2e-sample\n\nDisposable sample built by Auto-Orchestrator V7.3 managed sessions.\n",
        encoding="utf-8")
    (root / "CONTRACT.md").write_text(CONTRACT, encoding="utf-8")
    (root / ".gitignore").write_text(
        ".auto-orchestrator/\n__pycache__/\n*.pyc\nnode_modules/\n", encoding="utf-8")
    git(root, "init")
    git(root, "config", "user.email", "orchestrator@example.invalid")
    git(root, "config", "user.name", "Auto Orchestrator")
    git(root, "add", ".")
    git(root, "commit", "-m", "chore: sample scaffold and API contract")
    return root


def browser_env() -> dict:
    env = {
        "NODE_PATH": str(ROOT / "node_modules"),
        "AO_BROWSER_CHANNEL": os.environ.get("AO_BROWSER_CHANNEL", "chrome"),
    }
    explicit = os.environ.get("AO_CHROMIUM_EXECUTABLE")
    if explicit and Path(explicit).is_file():
        env["AO_CHROMIUM_EXECUTABLE"] = explicit
    elif Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").is_file():
        env["AO_CHROMIUM_EXECUTABLE"] = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return env


def copy_working_sample(source: Path, target: Path) -> Path:
    """Copy only the integrated working tree into a fresh Git repository."""
    source = Path(source).resolve()
    target = Path(target).resolve()
    if target.exists():
        shutil.rmtree(target)
    worktrees = target.parent / (target.name + "-auto-orchestrator-worktrees")
    if worktrees.exists():
        shutil.rmtree(worktrees)

    def ignore(directory, names):
        ignored = set()
        for name in names:
            if name in {".git", ".auto-orchestrator", "__pycache__", "node_modules"}:
                ignored.add(name)
        return ignored

    shutil.copytree(source, target, ignore=ignore)
    git(target, "init")
    git(target, "config", "user.email", "orchestrator@example.invalid")
    git(target, "config", "user.name", "Auto Orchestrator")
    git(target, "add", ".")
    git(target, "commit", "-m", "chore: import working sample for failure recovery")
    return target


def inject_frontend_fault(root: Path) -> bool:
    """Controlled fault injection in an isolated copy, never the source sample."""
    try:
        manifest = read_manifest(root, "frontend")
    except Exception:
        return False
    config = Path(manifest["path"]) / "frontend" / "config.js"
    if not config.is_file():
        return False
    original = config.read_text(encoding="utf-8")
    updated = re.sub(
        r"window\.AO_API_BASE\s*=\s*[^;]+;",
        'window.AO_API_BASE = "http://127.0.0.1:1";',
        original,
        count=1,
    )
    if updated == original:
        return False
    config.write_text(updated, encoding="utf-8")
    git(Path(manifest["path"]), "add", "frontend/config.js")
    git(Path(manifest["path"]), "commit", "-m",
        "test: controlled frontend API fault for repair-loop evidence")
    return True


def run_failure_recovery(source_root: Path, root: Path, *, run_id: str,
                         model: str = DEFAULT_MODEL,
                         max_seconds: float = 7200) -> dict:
    root = copy_working_sample(source_root, root)
    plan = make_plan(run_id, model)
    plan_path = root / ".auto-orchestrator" / "plans" / f"{run_id}.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    injected = {"done": False}

    def after_task(task_id: str, result: dict) -> None:
        if task_id == "frontend" and not injected["done"]:
            injected["done"] = inject_frontend_fault(root)

    runner = TeamworkRunner(root, plan, model=model, browser_env=browser_env(),
                            max_seconds=max_seconds,
                            on_task_complete=after_task)
    try:
        summary = runner.run()
    finally:
        runner.close()
    summary["failure_recovery"] = {
        "injected": injected["done"],
        "injection": "frontend/config.js AO_API_BASE -> unreachable loopback port",
        "repair_iterations": next(
            (item.get("repair_iterations") for item in summary.get("milestones", [])
             if item.get("id") == "M2"), 0),
        "root": str(root),
    }
    return summary


def run_sample(root: Path, *, run_id: str, model: str = DEFAULT_MODEL,
               max_seconds: float = 7200) -> dict:
    root = prepare_sample(root)
    plan = make_plan(run_id, model)
    plan_path = root / ".auto-orchestrator" / "plans" / f"{run_id}.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    runner = TeamworkRunner(root, plan, model=model, browser_env=browser_env(),
                            max_seconds=max_seconds)
    try:
        return runner.run()
    finally:
        runner.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path,
                        default=Path(tempfile.gettempdir()) / "teamwork-e2e-sample")
    parser.add_argument("--run-id", default="teamwork-e2e")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=7200)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--failure-from", type=Path, default=None,
                        help="copy this working sample and run controlled failure recovery")
    args = parser.parse_args(argv)
    try:
        root = prepare_sample(args.root, reset=args.reset)
        if args.prepare_only:
            print(json.dumps({"root": str(root), "plan": str(root / ".auto-orchestrator" /
                                                              "plans" / f"{args.run_id}.json")},
                             indent=2))
            return 0
        if args.failure_from is not None:
            summary = run_failure_recovery(args.failure_from, root,
                                           run_id=args.run_id, model=args.model,
                                           max_seconds=args.max_seconds)
        else:
            summary = run_sample(root, run_id=args.run_id, model=args.model,
                                 max_seconds=args.max_seconds)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0 if summary["status"] == "passed" else 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"V73 E2E ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
