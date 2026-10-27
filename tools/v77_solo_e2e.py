#!/usr/bin/env python3
"""V7.7 SOLO small-bugfix E2E.

Creates a failing test, starts Teamwork in SOLO mode through the real CLI, lets
the active OpenCode agent fix only src/calc.py, and records the verified result.
No prompts or source files are exported.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from tools.project_identity import ensure_project_identity
    from tools.project_memory import ProjectMemory
    from tools.teamwork import TeamworkEngine
except ModuleNotFoundError:
    from project_identity import ensure_project_identity  # type: ignore
    from project_memory import ProjectMemory  # type: ignore
    from teamwork import TeamworkEngine  # type: ignore

ROOT = Path(__file__).resolve().parents[1]


def _run(root, *args):
    process = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "universal_teamwork.py"),
         "--root", str(root), *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120)
    data = json.loads(process.stdout) if process.stdout.strip() else {}
    return process.returncode, data, process.stderr


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True)


def _sanitize(value, roots):
    text = json.dumps(value, ensure_ascii=False)
    for root in roots:
        text = text.replace(str(root), "$PROJECT")
    text = text.replace(str(Path.home()), "$HOME")
    for temp in (tempfile.gettempdir(), "/private" + tempfile.gettempdir()):
        text = text.replace(str(temp), "$TMPDIR")
    return json.loads(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)
    root = Path(tempfile.mkdtemp(prefix="v77-solo-"))
    report = {"schema_version": 1, "kind": "v77-solo-e2e"}
    started = time.monotonic()
    try:
        _git(root, "init")
        _git(root, "config", "user.email", "e2e@example.invalid")
        _git(root, "config", "user.name", "E2E")
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a - b\n", encoding="utf-8")
        (root / "tests" / "test_calc.py").write_text(
            "import sys, unittest\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n"
            "from calc import add\n\n"
            "class CalcTests(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n", encoding="utf-8")
        (root / ".gitignore").write_text(
            ".auto-orchestrator/*\n!.auto-orchestrator/project.json\n",
            encoding="utf-8")
        ensure_project_identity(root)
        _git(root, "add", ".")
        _git(root, "commit", "-m", "initial failing calculator")
        rc, started_payload, err = _run(
            root, "--platform", "opencode", "start", "--mode", "solo",
            "--request",
            "Fix the add function in src/calc.py so the existing tests pass; "
            "keep all other behavior unchanged and do not change tests.",
            "--max-seconds", "600")
        if rc != 0:
            raise RuntimeError(err or started_payload)
        run_id = started_payload.get("run_id")
        report["start"] = {
            "run_id": run_id, "mode": started_payload.get("mode"),
            "provider": started_payload.get("execution_provider"),
            "plan_only": False,
        }
        final = None
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            rc, status, err = _run(root, "--platform", "opencode", "status",
                                   "--run-id", run_id)
            if rc == 0 and status.get("status") in (
                    "passed", "failed", "cancelled"):
                final = status
                break
            time.sleep(3)
        if final is None:
            raise RuntimeError("SOLO run did not reach a terminal state")
        engine = TeamworkEngine(root)
        try:
            state = engine.status(run_id)
        finally:
            engine.close()
        with ProjectMemory(root) as memory:
            handoff = memory.latest_handoff()
            meta = memory.get_meta()
        report.update({
            "status": final.get("status"),
            "mode": final.get("mode"),
            "requirements": final.get("requirements"),
            "changed_src": (root / "src" / "calc.py").read_text(
                encoding="utf-8").strip().endswith("a + b"),
            "tests_passed": any(
                item.get("status") == "passed"
                for milestone in state["milestones"]
                for task in milestone["tasks"]
                for item in ((task.get("result") or {}).get("tests") or [])),
            "tasks": sum(len(milestone["tasks"])
                         for milestone in state["milestones"]),
            "milestones": [milestone["status"]
                           for milestone in state["milestones"]],
            "managed_sessions": len(state.get("managed_sessions") or []),
            "tokens": ((state.get("managed_sessions") or [{}])[0].get("tokens")
                       if state.get("managed_sessions") else None),
            "handoff": bool(handoff),
            "last_platform": meta.get("last_platform"),
            "wall_time_seconds": round(time.monotonic() - started, 3),
        })
    finally:
        pass
    text = _sanitize(report, [root])
    text = json.dumps(text, indent=2, ensure_ascii=False) + "\n"
    print(text, end="")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
