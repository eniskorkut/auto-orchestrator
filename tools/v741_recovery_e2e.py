#!/usr/bin/env python3
"""V7.4.1 crash-recovery E2E.

Starts a real two-implementer FAST run, kills only the runner while both managed
OpenCode sessions are alive, resumes through the single-chat bridge, and requires
the integrated backend tests and browser E2E to pass before calling recovery a
success.

Artifacts are written relative to the repository, with no local absolute paths
in the public evidence file.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from tools import teamwork_chat
    from tools.run_owner import (
        atomic_write_json,
        identity_matches,
        owner_record,
        read_recovery_state,
        wait_for_exit,
    )
    from tools.teamwork import TeamworkEngine
    from tools.v73_e2e import DEFAULT_MODEL, browser_env, prepare_sample
    from tools.v74_e2e import fast_plan
except ModuleNotFoundError:  # python tools/v741_recovery_e2e.py
    import teamwork_chat  # type: ignore
    from run_owner import (  # type: ignore
        atomic_write_json,
        identity_matches,
        owner_record,
        read_recovery_state,
        wait_for_exit,
    )
    from teamwork import TeamworkEngine  # type: ignore
    from v73_e2e import DEFAULT_MODEL, browser_env, prepare_sample  # type: ignore
    from v74_e2e import fast_plan  # type: ignore

ROOT = Path(__file__).resolve().parents[1]


def _sanitize(value):
    """Remove local paths and raw process ids from public evidence."""
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if key in ("pid", "process_group"):
                clean["pid_recorded"] = True
                continue
            if key == "log" and isinstance(item, str):
                clean[key] = "$TMPDIR/opencode/v741-recovery/.auto-orchestrator/teamwork/v741-recovery-e2e.resume.log" if "resume" in item else "$TMPDIR/opencode/v741-recovery/.auto-orchestrator/teamwork/v741-recovery-e2e.log"
                continue
            clean[key] = _sanitize(item)
        return clean
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        value = value.replace(str(ROOT), "$REPO")
        value = value.replace(str(Path.home()), "$HOME")
        value = value.replace("/private" + tempfile.gettempdir(), "$TMPDIR")
        value = value.replace(tempfile.gettempdir(), "$TMPDIR")
        return value
    return value


def _engine_status(root: Path, run_id: str) -> dict:
    engine = TeamworkEngine(root)
    try:
        return engine.status(run_id)
    finally:
        engine.close()


def _sessions(status: dict) -> dict[str, dict]:
    result = {}
    for row in status.get("managed_sessions", []):
        result[row.get("task_id")] = row
    return result


def _write_active(root: Path, run_id: str, plan_path: Path, log: Path,
                  model: str | None, process, token: str) -> dict:
    owner = owner_record(process.pid)
    if owner is None:
        raise RuntimeError("could not verify the runner process identity")
    now = time.time()
    active = {
        "version": 2, "run_id": run_id, "mode": "fast",
        "plan": str(plan_path), "log": str(log), "model": model,
        "owner": owner, "pid": owner["pid"], "runner_token": token,
        "started_at": now, "heartbeat_at": now, "recovery_state": "active",
    }
    atomic_write_json(root / ".auto-orchestrator" / "teamwork" / "active.json",
                      active)
    return active


def _launch(root: Path, plan_path: Path, log: Path, model: str,
            max_seconds: float, token: str):
    cli = ROOT / "tools" / "teamwork_cli.py"
    command = [sys.executable, str(cli), "--root", str(root), "run",
               "--plan", str(plan_path), "--model", model,
               "--max-seconds", str(max_seconds), "--owner-token", token]
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as handle:
        return subprocess.Popen(
            command, cwd=root, stdin=subprocess.DEVNULL,
            stdout=handle, stderr=subprocess.STDOUT,
            start_new_session=True if os.name != "nt" else False,
        )


def _run_command(argv, cwd: Path, env: dict | None = None) -> dict:
    started = time.monotonic()
    result = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900, env={**os.environ, **(env or {})},
    )
    return {
        "command": " ".join(argv),
        "exit_code": result.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_tail": ((result.stdout or "") + (result.stderr or ""))[-3000:],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path,
                        default=Path(tempfile.gettempdir()) / "opencode" / "v741-recovery")
    parser.add_argument("--run-id", default="v741-recovery-e2e")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-seconds", type=float, default=3600)
    parser.add_argument("--wait-seconds", type=float, default=1800)
    parser.add_argument("--json-out", type=Path,
                        default=Path("docs/assets/v741-recovery-e2e.json"))
    args = parser.parse_args(argv)
    root = prepare_sample(args.root, reset=True)
    # The disposable sample has no node_modules of its own; the harness supplies
    # the tool repository's Playwright installation through the supported env.
    harness_browser = browser_env()
    os.environ.setdefault("AO_NODE_PATH", harness_browser.get("NODE_PATH", ""))
    if harness_browser.get("AO_CHROMIUM_EXECUTABLE"):
        os.environ.setdefault("AO_CHROMIUM_EXECUTABLE",
                              harness_browser["AO_CHROMIUM_EXECUTABLE"])
    if harness_browser.get("AO_BROWSER_CHANNEL"):
        os.environ.setdefault("AO_BROWSER_CHANNEL",
                              harness_browser["AO_BROWSER_CHANNEL"])
    plan = fast_plan(args.run_id, args.model)
    plan_path = root / ".auto-orchestrator" / "plans" / f"{args.run_id}.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    engine = TeamworkEngine(root)
    try:
        engine.init(plan)
    finally:
        engine.close()
    token = os.urandom(16).hex()
    log = root / ".auto-orchestrator" / "teamwork" / f"{args.run_id}.log"
    started = time.monotonic()
    process = _launch(root, plan_path, log, args.model, args.max_seconds, token)
    active = _write_active(root, args.run_id, plan_path, log, args.model,
                           process, token)
    timeline = ["active"]
    killed_at = None
    pre_kill = {}
    child_identity = {}
    try:
        deadline = time.monotonic() + args.wait_seconds
        while time.monotonic() < deadline:
            status = _engine_status(root, args.run_id)
            sessions = _sessions(status)
            running = [task_id for task_id in ("backend", "frontend")
                       if sessions.get(task_id, {}).get("status") == "running"
                       and sessions.get(task_id, {}).get("pid")]
            if len(running) == 2:
                pre_kill = {
                    "tasks": {task["id"]: task["state"] for task in status["tasks"]},
                    "sessions": {
                        task_id: {
                            "status": sessions[task_id].get("status"),
                            "pid": sessions[task_id].get("pid"),
                            "tool_count": sessions[task_id].get("tool_count"),
                        } for task_id in ("backend", "frontend")
                    },
                    "milestones": status["milestones"],
                }
                child_identity = {
                    task_id: {
                        "pid": sessions[task_id].get("pid"),
                        "start_token": sessions[task_id].get("pid_start_token"),
                        "uid": sessions[task_id].get("pid_uid"),
                    } for task_id in ("backend", "frontend")
                }
                break
            time.sleep(1)
        if not pre_kill:
            raise RuntimeError("both managed sessions did not become active")
        # Kill only the runner. Child providers are deliberately left alive so
        # recovery must verify ownership and stop them safely.
        process.terminate()
        process.wait(timeout=30)
        killed_at = time.monotonic()
        if not wait_for_exit(active["owner"], 15):
            raise RuntimeError("runner process did not exit after SIGTERM")
        timeline.append("runner_disconnected")
        status = _engine_status(root, args.run_id)
        persisted = {
            "tasks": {task["id"]: task["state"] for task in status["tasks"]},
            "sessions": {
                row.get("task_id"): row.get("status")
                for row in status.get("managed_sessions", [])
            },
            "milestones": status["milestones"],
        }
        children_alive = {
            task_id: identity_matches(identity)
            for task_id, identity in child_identity.items()
        }
        if status.get("status") == "passed":
            raise RuntimeError("run finished before the crash; rerun with a slower task")
        # Resume through the real single-chat bridge. Allow a short grace for
        # the killed process's OS lease to be released.
        result = None
        resume_deadline = time.monotonic() + 30
        while time.monotonic() < resume_deadline:
            result = teamwork_chat.resume(
                type("Args", (), {"root": str(root), "run_id": args.run_id})())
            if result.get("status") == "resumed":
                break
            if result.get("status") != "already_running":
                raise RuntimeError(f"resume was rejected: {result}")
            time.sleep(0.5)
        if not result or result.get("status") != "resumed":
            raise RuntimeError("resume did not acquire the runner lease")
        timeline.append("resuming")
        seen_recovery = set()
        final_status = None
        while time.monotonic() < deadline:
            try:
                chat_status = teamwork_chat.status(
                    type("Args", (), {"root": str(root), "run_id": args.run_id})())
                recovery = chat_status.get("recovery_state")
            except Exception:
                chat_status = None
                recovery = None
            if recovery and recovery not in seen_recovery:
                seen_recovery.add(recovery)
                timeline.append(recovery)
            engine_state = _engine_status(root, args.run_id)
            if engine_state.get("status") in ("passed", "failed", "cancelled"):
                final_status = engine_state
                break
            time.sleep(5)
        if final_status is None:
            raise RuntimeError("recovery did not reach a terminal state")
        backend_tests = _run_command(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
            root,
        )
        browser = _run_command(
            ["node", "e2e/check.cjs"], root, browser_env(),
        )
        # Independent final product verification, even if the tester's managed
        # verification already ran.
        passed = (
            final_status.get("status") == "passed"
            and backend_tests["exit_code"] == 0
            and browser["exit_code"] == 0
            and (root / "backend" / "app.py").is_file()
            and (root / "frontend" / "index.html").is_file()
        )
        sessions = _engine_status(root, args.run_id).get("managed_sessions", [])
        completed_attempts = {}
        duplicate_completed = []
        seen_completed = set()
        for row in sessions:
            if row.get("status") == "completed":
                key = (row.get("task_id"), row.get("attempt"))
                if key in seen_completed:
                    duplicate_completed.append(list(key))
                seen_completed.add(key)
                completed_attempts.setdefault(row.get("task_id"), []).append(
                    row.get("attempt"))
        payload = {
            "schema_version": 1,
            "kind": "v741-crash-recovery-real-e2e",
            "run_id": args.run_id,
            "mode": plan["work_mode"],
            "status": "passed" if passed else "failed",
            "runner_exit_code": process.returncode,
            "wall_time_seconds": round(time.monotonic() - started, 3),
            "crash_at_seconds": round(killed_at - started, 3)
            if killed_at is not None else None,
            "timeline": timeline,
            "pre_kill": pre_kill,
            "persisted_after_crash": persisted,
            "children_alive_after_runner_crash": children_alive,
            "resume_response": result,
            "final": {
                "status": final_status.get("status"),
                "verified_progress": final_status.get("verified_progress"),
                "milestones": [
                    {"id": item.get("id"), "status": item.get("status"),
                     "repair_iterations": item.get("repair_iterations"),
                     "max_repair": item.get("max_repair")}
                    for item in final_status.get("milestones", [])
                ],
                "sessions": [
                    {"task_id": row.get("task_id"), "attempt": row.get("attempt"),
                     "status": row.get("status"),
                     "tool_count": row.get("tool_count"),
                     "cost": row.get("cost"),
                     "pid_verified": bool(row.get("pid") and row.get("pid_start_token")),
                     "cancel_state": row.get("cancel_state")}
                    for row in sessions
                ],
                "completed_attempts": completed_attempts,
                "duplicate_completed_attempts": duplicate_completed,
                "note": "Repaired tasks may have more than one completed attempt; that is expected.",
            },
            "independent_verification": {
                "backend": backend_tests,
                "browser_e2e": browser,
            },
            "recovery_state_file": read_recovery_state(root, args.run_id),
            "native_cancel_ack": False,
            "local_signal_only": True,
            "note": (
                "Real managed OpenCode crash-recovery run. The runner was killed "
                "while both implementers were alive; recovery terminated only "
                "identity-verified owned child processes. No native cancellation "
                "ACK is claimed."
            ),
        }
        text = json.dumps(_sanitize(payload), indent=2, ensure_ascii=False) + "\n"
        print(text, end="")
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(text, encoding="utf-8")
        return 0 if passed else 1
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        # Best-effort cleanup of children that recovery did not stop. Only
        # identities recorded by the managed runner are signalled.
        for identity in child_identity.values():
            if identity_matches(identity):
                try:
                    if os.name == "nt":
                        os.kill(identity["pid"], 15)
                    else:
                        os.kill(identity["pid"], 15)
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
