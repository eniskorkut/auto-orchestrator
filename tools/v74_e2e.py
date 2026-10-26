#!/usr/bin/env python3
"""V7.4 FAST-mode real E2E: parallel backend/frontend, one milestone, tester.

This intentionally uses fewer verification sessions than V7.3. It is not a
substitute for the RIGOROUS E2E; it demonstrates the adaptive fast budget and
true implementer concurrency.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

try:
    from tools.teamwork_chat import ChatError
    from tools.teamwork_runner import TeamworkRunner
    from tools.v73_e2e import (DEFAULT_MODEL, TASKS, browser_env, git,
                               prepare_sample)
except ModuleNotFoundError:
    from teamwork_chat import ChatError  # type: ignore
    from teamwork_runner import TeamworkRunner  # type: ignore
    from v73_e2e import (DEFAULT_MODEL, TASKS, browser_env, git,  # type: ignore
                         prepare_sample)


def fast_plan(run_id: str, model: str) -> dict:
    source = {task["id"]: task for task in TASKS}
    tasks = []
    for task_id in ("backend", "frontend", "integrate"):
        task = dict(source[task_id])
        task["milestone"] = "M1"
        task["provider"] = "opencode"
        task["model"] = model
        task["depends_on"] = (["backend", "frontend"] if task_id == "integrate"
                              else [])
        if task_id == "backend":
            task["verify_commands"] = [
                "python3.11 -m unittest discover -s tests -q"
            ]
        tasks.append(task)
    return {
        "version": 1,
        "run_id": run_id,
        "max_parallel": 2,
        "pattern": "distributed_coding",
        "work_mode": "fast",
        "request": "Build the task manager backend and frontend, integrate and run the browser E2E.",
        "risk_assessment": {
            "mode": "fast", "score": 12, "confidence": "high",
            "reason": "small scaffold with explicit paths and one verification suite",
            "factors": {"layers": 2, "profile_files": 3, "profile_tests": 0},
            "fallback_policy": "BALANCED when repository facts are missing",
        },
        "milestones": [{
            "id": "M1", "objective": "Implement and verify in a fast budget",
            "depends_on": [],
            "acceptance": ["Backend tests pass.", "Browser E2E passes."],
            "verification_policy": {"require_roles": [], "max_repair": 2},
        }],
        "tasks": tasks,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path,
                        default=Path(tempfile.gettempdir()) / "opencode" / "v74-fast-sample")
    parser.add_argument("--run-id", default="v74-fast-e2e")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-seconds", type=float, default=3600)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)
    root = prepare_sample(args.root, reset=True)
    plan = fast_plan(args.run_id, args.model)
    plan_path = root / ".auto-orchestrator" / "plans" / f"{args.run_id}.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    runner = TeamworkRunner(root, plan, model=args.model,
                            browser_env=browser_env(),
                            max_seconds=args.max_seconds)
    try:
        summary = runner.run()
    finally:
        runner.close()
    summary["artifact_root"] = str(root)
    summary["mode"] = "fast"
    text = json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
    print(text, end="")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    return 0 if summary.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
