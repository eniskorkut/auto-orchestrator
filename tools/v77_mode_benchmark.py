#!/usr/bin/env python3
"""Measured V7.7 SOLO/FAST/BALANCED/RIGOROUS planning benchmark.

Planning cells are measured locally (wall-clock and plan shape); only the SOLO
cell that was actually executed by a real agent may carry wall time/tokens. No
unmeasured cell is presented as a win.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

try:
    from tools.teamwork_planner import assess_complexity, plan_request
except ModuleNotFoundError:
    from teamwork_planner import assess_complexity, plan_request  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
PROFILE = {"files": 120, "modules": 4, "tests": 24,
           "languages": ["py", "js"], "backend": True, "frontend": False}
TASKS = [
    ("small-bugfix", "Fix the add function in src/calc.py and keep tests green"),
    ("backend-frontend", "Build a backend API and a frontend UI for notifications"),
    ("multi-module", "Add authentication, database migration and an audit review path"),
]
MODES = ("solo", "fast", "balanced", "rigorous")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--solo-evidence", type=Path, default=None)
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "kind": "v77-mode-benchmark",
              "profile_note": "synthetic repo profile; not a provider benchmark"}
    cells = []
    for task_id, request in TASKS:
        for mode in MODES:
            started = time.perf_counter()
            plan = plan_request(request, run_id=f"bench-{task_id}-{mode}",
                                mode=mode, repo_profile=PROFILE)
            elapsed = (time.perf_counter() - started) * 1000
            assessment = assess_complexity(
                request, repo_profile=PROFILE, requested_mode=mode)
            cells.append({
                "task": task_id, "mode": mode, "planned_mode": plan["work_mode"],
                "tasks": len(plan["tasks"]),
                "milestones": len(plan["milestones"]),
                "max_parallel": plan["max_parallel"],
                "planning_ms": round(elapsed, 2),
                "risk_score": assessment.get("score"),
                "safety_upgrade": assessment.get("safety_upgrade"),
                "auto_floor": assessment.get("auto_floor"),
                "real_run": False,
            })
    report["cells"] = cells
    if args.solo_evidence and args.solo_evidence.is_file():
        try:
            evidence = json.loads(args.solo_evidence.read_text(encoding="utf-8"))
            if evidence.get("status") == "passed" and evidence.get("mode") == "solo":
                for cell in cells:
                    if cell["task"] == "small-bugfix" and cell["mode"] == "solo":
                        cell.update({
                            "real_run": True,
                            "status": evidence.get("status"),
                            "wall_time_seconds": evidence.get("wall_time_seconds"),
                            "tokens": evidence.get("tokens"),
                            "handoff": evidence.get("handoff"),
                            "tests_passed": evidence.get("tests_passed"),
                        })
        except (OSError, ValueError):
            pass
    report["summary"] = {
        "cells": len(cells),
        "real_run_cells": sum(1 for cell in cells if cell["real_run"]),
        "not_run_cells": sum(1 for cell in cells if not cell["real_run"]),
        "note": ("Only cells with real_run=true contain measured agent wall time "
                 "or tokens. Other cells are planning-only and are not quality "
                 "or savings comparisons."),
    }
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    print(text, end="")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
