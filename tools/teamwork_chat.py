#!/usr/bin/env python3
"""Single-chat bridge for V7.4 Teamwork.

The OpenCode command file calls this module. ``start`` writes a plan, initializes
the durable run and launches the managed runner in a detached process; the main
chat receives a run id immediately. Status commands read the same persisted
Supervisor/milestone state. Child sessions remain separate managed sessions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    from tools.repo_profile import RepoProfileError, profile_repository
    from tools.teamwork import TeamworkEngine, TeamworkError
    from tools.teamwork_planner import TeamworkPlannerError, plan_request
except ModuleNotFoundError:  # python tools/teamwork_chat.py
    from repo_profile import RepoProfileError, profile_repository  # type: ignore
    from teamwork import TeamworkEngine, TeamworkError  # type: ignore
    from teamwork_planner import TeamworkPlannerError, plan_request  # type: ignore

STATE_DIR = Path(".auto-orchestrator") / "teamwork"
ACTIVE = "active.json"
DEFAULT_MODEL = os.environ.get("AO_TEAMWORK_MODEL",
                               "opencode-go/deepseek-v4.1-flash")


class ChatError(ValueError):
    pass


def _root(value: str) -> Path:
    root = Path(value).resolve()
    if not root.is_dir():
        raise ChatError("root directory does not exist")
    return root


def _state_path(root: Path, name: str = ACTIVE) -> Path:
    path = root / STATE_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _active(root: Path) -> dict:
    value = _read_json(_state_path(root), {})
    return value if isinstance(value, dict) else {}


def _engine_status(root: Path, run_id: str) -> dict:
    engine = TeamworkEngine(root)
    try:
        return engine.status(run_id)
    finally:
        engine.close()


def start(args) -> dict:
    root = _root(args.root)
    active = _active(root)
    if _pid_alive(active.get("pid")):
        raise ChatError(
            f"active teamwork run {active.get('run_id')} is already running; "
            "use status/resume instead of starting a duplicate"
        )
    mode = args.mode
    if mode not in ("auto", "fast", "balanced", "rigorous"):
        raise ChatError("mode must be auto, fast, balanced or rigorous")
    run_id = args.run_id or f"team-{int(time.time())}-{os.getpid()}"
    plan_path = root / ".auto-orchestrator" / "plans" / f"{run_id}.json"
    if plan_path.exists():
        raise ChatError("run id already exists; choose another --run-id")
    profile = profile_repository(root)
    plan = plan_request(args.request, run_id=run_id, model=args.model,
                        mode=mode, repo_profile=profile)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    engine = TeamworkEngine(root)
    try:
        engine.init(plan)
    finally:
        engine.close()
    log = _state_path(root, f"{run_id}.log")
    cli = Path(__file__).with_name("teamwork_cli.py")
    command = [sys.executable, str(cli), "--root", str(root), "run",
               "--plan", str(plan_path), "--max-seconds", str(args.max_seconds)]
    if args.model:
        command.extend(["--model", args.model])
    with log.open("ab") as handle:
        process = subprocess.Popen(
            command, cwd=root, stdin=subprocess.DEVNULL,
            stdout=handle, stderr=subprocess.STDOUT,
            start_new_session=True if os.name != "nt" else False,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                           if os.name == "nt" else 0),
        )
    state = {
        "run_id": run_id, "mode": plan["work_mode"], "pid": process.pid,
        "plan": str(plan_path), "log": str(log), "started_at": time.time(),
        "request": args.request, "profile": profile, "model": args.model,
    }
    _write_json(_state_path(root), state)
    return {
        "status": "started", "run_id": run_id, "mode": plan["work_mode"],
        "pid": process.pid, "plan": str(plan_path), "log": str(log),
        "risk_assessment": plan.get("risk_assessment"),
        "child_sessions": "separate managed OpenCode sessions",
        "note": "Use status/agents/milestones; cancel is a local signal only.",
    }


def status(args) -> dict:
    root = _root(args.root)
    active = _active(root)
    run_id = args.run_id or active.get("run_id")
    if not run_id:
        raise ChatError("no active teamwork run")
    try:
        state = _engine_status(root, run_id)
    except TeamworkError as exc:
        raise ChatError(str(exc)) from exc
    running = [item for item in state.get("managed_sessions", [])
               if item.get("status") in ("starting", "running")]
    role_by_task = {}
    for milestone in state.get("milestones", []):
        for task in milestone.get("tasks", []):
            role_by_task[task.get("id")] = task.get("role")
    current = next((item for item in state.get("milestones", [])
                    if item.get("status") in ("running", "verifying", "repairing")),
                   None)
    return {
        "run_id": run_id,
        "mode": state.get("work_mode", active.get("mode")),
        "status": state.get("status"),
        "verified_progress": state.get("verified_progress"),
        "estimated_progress": None,
        "current_milestone": current.get("id") if current else None,
        "milestones": [{k: item.get(k) for k in
                        ("id", "objective", "status", "repair_iterations",
                         "max_repair", "satisfied_roles", "missing_roles")}
                       for item in state.get("milestones", [])],
        "active_managed_sessions": len(running),
        "active_tasks": [{"task_id": item.get("task_id"),
                          "role": role_by_task.get(item.get("task_id")),
                          "status": item.get("status"),
                          "session_hash": hashlib.sha256(
                              str(item.get("session_id", "")).encode()).hexdigest()[:12]}
                         for item in running],
        "pid_alive": _pid_alive(active.get("pid")),
        "log": active.get("log"),
    }


def agents(args) -> dict:
    root = _root(args.root)
    active = _active(root)
    run_id = args.run_id or active.get("run_id")
    if not run_id:
        raise ChatError("no active teamwork run")
    state = _engine_status(root, run_id)
    latest = {}
    for row in state.get("managed_sessions", []):
        key = (row.get("task_id"), row.get("attempt"))
        if key not in latest or (row.get("updated_at") or 0) > (latest[key].get("updated_at") or 0):
            latest[key] = row
    return {
        "run_id": run_id,
        "agents": [{
            "task_id": row.get("task_id"), "attempt": row.get("attempt"),
            "provider": row.get("provider"), "status": row.get("status"),
            "model": row.get("model"), "tool_count": row.get("tool_count"),
            "cost": row.get("cost"), "tokens": row.get("tokens"),
            "session_hash": (__import__("hashlib").sha256(
                str(row.get("session_id", "")).encode()).hexdigest()[:12]),
        } for row in latest.values()],
    }


def milestones(args) -> dict:
    data = status(args)
    return {"run_id": data["run_id"], "work_mode": data["mode"],
            "verified_progress": data["verified_progress"],
            "milestones": data["milestones"]}


def cancel(args) -> dict:
    root = _root(args.root)
    active = _active(root)
    run_id = args.run_id or active.get("run_id")
    if not run_id:
        raise ChatError("no active teamwork run")
    path = _state_path(root, f"cancel-{run_id}.json")
    _write_json(path, {"run_id": run_id, "requested_at": time.time(),
                       "requested_by": "single-chat"})
    return {
        "run_id": run_id, "status": "cancel_requested",
        "local_signal_only": True, "native_cancel_ack": False,
        "note": "The runner will signal local child processes; no native ACK is claimed.",
    }


def resume(args) -> dict:
    root = _root(args.root)
    active = _active(root)
    run_id = args.run_id or active.get("run_id")
    if not run_id:
        raise ChatError("no active teamwork run")
    plan_path = Path(active.get("plan") or
                     root / ".auto-orchestrator" / "plans" / f"{run_id}.json")
    if not plan_path.is_file():
        raise ChatError("plan artifact is missing")
    if _pid_alive(active.get("pid")):
        return {"run_id": run_id, "status": "already_running",
                "pid": active.get("pid")}
    log = _state_path(root, f"{run_id}.resume.log")
    cli = Path(__file__).with_name("teamwork_cli.py")
    command = [sys.executable, str(cli), "--root", str(root), "run",
               "--plan", str(plan_path), "--max-seconds", "7200", "--recover"]
    if active.get("model"):
        command.extend(["--model", active.get("model")])
    with log.open("ab") as handle:
        process = subprocess.Popen(
            command, cwd=root, stdin=subprocess.DEVNULL,
            stdout=handle, stderr=subprocess.STDOUT,
            start_new_session=True if os.name != "nt" else False,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                           if os.name == "nt" else 0),
        )
    active["pid"] = process.pid
    active["log"] = str(log)
    _write_json(_state_path(root), active)
    return {"run_id": run_id, "status": "resumed", "pid": process.pid,
            "log": str(log), "recover": True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    start_p = sub.add_parser("start")
    start_p.add_argument("--request", required=True)
    start_p.add_argument("--mode", default="auto",
                         choices=["auto", "fast", "balanced", "rigorous"])
    start_p.add_argument("--run-id")
    start_p.add_argument("--model", default=DEFAULT_MODEL)
    start_p.add_argument("--max-seconds", type=float, default=7200)
    for name in ("status", "agents", "milestones", "cancel", "resume"):
        command = sub.add_parser(name)
        command.add_argument("--run-id")
    args = parser.parse_args(argv)
    try:
        action = {"start": start, "status": status, "agents": agents,
                  "milestones": milestones, "cancel": cancel,
                  "resume": resume}[args.command]
        print(json.dumps(action(args), indent=2, ensure_ascii=False))
        return 0
    except (ChatError, OSError, ValueError, TeamworkError,
            TeamworkPlannerError, RepoProfileError) as exc:
        print(f"TEAMWORK CHAT ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
