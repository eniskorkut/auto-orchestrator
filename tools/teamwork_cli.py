#!/usr/bin/env python3
"""CLI for the V7.3 Teamwork milestone engine and managed runner."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from tools import assumption_guard, requirements_contract
    from tools.teamwork import TeamworkEngine, TeamworkError
    from tools.teamwork_planner import TeamworkPlannerError, plan_request
    from tools.teamwork_runner import RunnerError, TeamworkRunner
    from tools.repo_profile import RepoProfileError, profile_repository
except ModuleNotFoundError:  # python tools/teamwork_cli.py
    import assumption_guard  # type: ignore
    import requirements_contract  # type: ignore
    from teamwork import TeamworkEngine, TeamworkError  # type: ignore
    from teamwork_planner import TeamworkPlannerError, plan_request  # type: ignore
    from teamwork_runner import RunnerError, TeamworkRunner  # type: ignore
    from repo_profile import RepoProfileError, profile_repository  # type: ignore


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON must be an object")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="generate a deterministic milestone plan")
    plan.add_argument("--request", required=True)
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--project-id")
    plan.add_argument("--model")
    plan.add_argument("--pattern")
    plan.add_argument("--max-parallel", type=int, default=3)
    plan.add_argument("--mode", choices=["auto", "fast", "balanced", "rigorous"],
                      default=None)
    plan.add_argument("--repo", type=Path,
                      help="repository root to profile for adaptive planning")
    plan.add_argument("--out", type=Path)
    plan.add_argument("--platform", default="opencode")
    plan.add_argument("--grill-mode", default="auto",
                      choices=["off", "auto", "required", "plan-only"])
    plan.add_argument("--interview-id")

    init = sub.add_parser("init")
    init.add_argument("--plan", type=Path, required=True)

    status = sub.add_parser("status")
    status.add_argument("--run-id", required=True)

    nxt = sub.add_parser("next")
    nxt.add_argument("--run-id", required=True)

    handoff = sub.add_parser("handoff")
    handoff.add_argument("--run-id", required=True)
    handoff.add_argument("--milestone-id", required=True)

    reopen = sub.add_parser("reopen")
    reopen.add_argument("--run-id", required=True)
    reopen.add_argument("--task-id", required=True)
    reopen.add_argument("--reason", required=True)

    run = sub.add_parser("run", help="run a plan through a managed provider")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--model")
    run.add_argument("--max-seconds", type=float, default=7200)
    run.add_argument("--recover", action="store_true")
    run.add_argument("--owner-token")

    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            guard = assumption_guard.evaluate(
                args.root.resolve(), args.request, platform=args.platform,
                mode=args.grill_mode, interview_id=args.interview_id)
            if guard["status"] != "allowed":
                print(json.dumps({
                    "status": "interview_required",
                    "interview": guard.get("interview"),
                    "question": guard.get("question"),
                    "reasons": guard.get("reasons") or [],
                    "assumption_guard": guard.get("receipt"),
                    "note": "No plan was written.",
                }, indent=2, ensure_ascii=False))
                return 2
            profile = (profile_repository(args.repo)
                       if args.repo is not None else None)
            plan_kwargs = {
                "run_id": args.run_id, "project_id": args.project_id,
                "max_parallel": args.max_parallel, "model": args.model,
                "pattern": args.pattern, "mode": args.mode,
                "repo_profile": profile,
            }
            if args.interview_id:
                if not plan_kwargs["project_id"]:
                    try:
                        from tools.project_identity import ensure_project_identity
                    except ModuleNotFoundError:
                        from project_identity import ensure_project_identity  # type: ignore
                    plan_kwargs["project_id"] = ensure_project_identity(
                        args.root.resolve())["project_id"]
                contract = requirements_contract.load_current(args.root.resolve())
                if contract is None or contract.get("interview_id") != args.interview_id:
                    contract = requirements_contract.build_from_interview(
                        args.root.resolve(), args.interview_id)
                plan_kwargs["requirements_contract"] = contract
            value = plan_request(args.request, **plan_kwargs)
            value["assumption_guard"] = guard["receipt"]
            text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(text, encoding="utf-8")
                print(f"Plan written: {args.out}")
            else:
                print(text, end="")
            return 0
        if args.command == "init":
            engine = TeamworkEngine(args.root)
            try:
                result = engine.init(_read(args.plan))
            finally:
                engine.close()
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.command == "run":
            plan_value = _read(args.plan)
            runner = TeamworkRunner(args.root, plan_value, model=args.model,
                                    max_seconds=args.max_seconds,
                                    recover_orphans=args.recover,
                                    owner_token=args.owner_token)
            try:
                result = runner.run()
            finally:
                runner.close()
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result.get("status") == "passed" else 1
        engine = TeamworkEngine(args.root)
        try:
            if args.command == "status":
                result = engine.status(args.run_id)
            elif args.command == "next":
                result = engine.next(args.run_id)
            elif args.command == "handoff":
                result = engine.record_handoff(args.run_id, args.milestone_id)
            else:
                result = engine.reopen(args.run_id, args.task_id, args.reason)
        finally:
            engine.close()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TeamworkError, TeamworkPlannerError,
            RunnerError, RepoProfileError,
            assumption_guard.AssumptionGuardError) as exc:
        print(f"TEAMWORK ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
