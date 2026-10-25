#!/usr/bin/env python3
"""Controlled project executor over the existing Supervisor.

The executor claims durable tasks, emits an operator dispatch manifest and
records structured results. It does NOT spawn or kill native IDE agents and it
does not merge worktrees. The native client remains the only dispatcher.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

try:
    from tools.project_planner import PlannerError, load_plan
    from tools.supervisor import Supervisor, SupervisorError, identifier
    from tools.telemetry import span
    from tools.worktree import WorktreeError, preview
except ModuleNotFoundError:  # Running as ``python tools/executor.py``.
    from project_planner import PlannerError, load_plan
    from supervisor import Supervisor, SupervisorError, identifier
    from telemetry import span
    from worktree import WorktreeError, preview

DISPATCH_DIR = Path(".auto-orchestrator") / "dispatch"


class ExecutorError(ValueError):
    pass


def _acceptance_map(rich: dict) -> dict[str, list[str]]:
    return {task["id"]: list(task.get("acceptance", [])) for task in rich.get("tasks", [])}


def validate_acceptance(result: dict, criteria: list[str]) -> list[dict]:
    if not criteria:
        raise ExecutorError("Planner artifact has no acceptance criteria for this task")
    entries = result.get("acceptance") if isinstance(result, dict) else None
    if not isinstance(entries, list) or len(entries) != len(criteria):
        raise ExecutorError("Result must report every acceptance criterion")
    expected = list(criteria)
    seen = set()
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ExecutorError("Acceptance entries must be objects")
        criterion = entry.get("criterion")
        status = entry.get("status")
        evidence = entry.get("evidence")
        if criterion not in expected:
            raise ExecutorError("Result contains an unknown acceptance criterion")
        if criterion in seen:
            raise ExecutorError("Acceptance criteria must be reported once")
        if status != "passed":
            raise ExecutorError("Every acceptance criterion must pass before completion")
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1000:
            raise ExecutorError("Passed acceptance criteria need concise evidence")
        seen.add(criterion)
        normalized.append({"criterion": criterion, "status": "passed", "evidence": evidence})
    if seen != set(expected):
        raise ExecutorError("Result is missing an acceptance criterion")
    return normalized


def _dispatch_path(root: Path, run_id: str) -> Path:
    directory = root / DISPATCH_DIR
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ExecutorError("Dispatch directory must not be a symlink")
    return directory / f"{run_id}-{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex[:6]}.json"


def _write_manifest(root: Path, payload: dict) -> Path:
    path = _dispatch_path(root, payload["run_id"])
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise ExecutorError("Could not create dispatch manifest") from exc
    try:
        os.write(fd, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return path


class Executor:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.supervisor = Supervisor(self.root)

    def close(self):
        self.supervisor.close()

    def _plan(self, run_id: str) -> dict:
        return load_plan(self.root, run_id)

    def next(self, run_id: str) -> dict:
        rich = self._plan(run_id)
        criteria = _acceptance_map(rich)
        with span("task.dispatch", project_id=rich.get("project_id"), run_id=run_id,
                  mode="native-ide"):
            claim = self.supervisor.claim(run_id)
        claimed = claim.get("claimed", [])
        tasks = []
        for item in claimed:
            task_criteria = criteria.get(item["id"], [])
            tasks.append({
                "task": {
                    "id": item["id"],
                    "role": item["role"],
                    "objective": item["objective"],
                    "acceptance": task_criteria,
                    "depends_on": item.get("depends_on", []),
                    "read_scope": item.get("read_scope", []),
                    "write_scope": item.get("write_scope", []),
                    "attempt": item.get("attempt"),
                },
                "control": {"lease": item.get("lease"), "attempt": item.get("attempt")},
                "dispatch": {
                    "mode": "native-ide",
                    "instruction": "Dispatch this task through the IDE's native subagent tool. "
                                   "Bind the returned native ID before requesting completion. "
                                   "Do not place the control lease in the subagent prompt.",
                },
            })
        payload = {
            "version": 1,
            "run_id": run_id,
            "project_id": rich.get("project_id"),
            "generated_at": time.time(),
            "claimed": [item["id"] for item in claimed],
            "timed_out": claim.get("timed_out", []),
            "tasks": tasks,
            "status": self.supervisor.status(run_id),
        }
        if tasks:
            payload["manifest"] = str(_write_manifest(self.root, payload))
        return payload

    def bind(self, run_id: str, task_id: str, lease: str, platform: str,
             native_id: str | None = None, native_hash: str | None = None) -> dict:
        return self.supervisor.bind_native(run_id, task_id, lease, platform,
                                           native_id=native_id, native_hash=native_hash)

    def complete(self, run_id: str, task_id: str, lease: str, result: dict,
                 review: dict | None = None) -> dict:
        rich = self._plan(run_id)
        task = next((item for item in rich.get("tasks", []) if item.get("id") == task_id), None)
        if task is None:
            raise ExecutorError("Unknown task in planner artifact")
        criteria = list(task.get("acceptance", []))
        result = dict(result)
        if task.get("role") in ("implementer", "tester") and not result.get("tests"):
            raise ExecutorError("Implementation and test tasks need at least one recorded test")
        result["acceptance"] = validate_acceptance(result, criteria)
        with span("task.verification", project_id=rich.get("project_id"), run_id=run_id,
                  task_id=task_id, mode="acceptance"):
            return self.supervisor.change(run_id, task_id, lease, "completed",
                                          result=result, review=review)

    def fail(self, run_id: str, task_id: str, lease: str, reason: str) -> dict:
        return self.supervisor.change(run_id, task_id, lease, "failed", reason=reason)

    def status(self, run_id: str) -> dict:
        rich = self._plan(run_id)
        state = self.supervisor.status(run_id)
        acceptance = _acceptance_map(rich)
        for task in state["tasks"]:
            task["acceptance"] = acceptance.get(task["id"], [])
            result = task.get("result") or {}
            reported = result.get("acceptance") if isinstance(result, dict) else None
            task["acceptance_reported"] = isinstance(reported, list) and len(reported) == len(task["acceptance"])
        state["planner_artifact"] = str(self.root / ".auto-orchestrator" / "plans" / f"{run_id}.json")
        return state

    def preview(self, run_id: str) -> dict:
        state = self.supervisor.status(run_id)
        rich = self._plan(run_id)
        previews = []
        for task in state["tasks"]:
            if task["state"] != "completed":
                continue
            with span("worktree.integration", project_id=rich.get("project_id"),
                      run_id=run_id, task_id=task["id"], mode="preview"):
                try:
                    previews.append({"task_id": task["id"], "preview": preview(self.root, task["id"])})
                except (WorktreeError, OSError, ValueError) as exc:
                    previews.append({"task_id": task["id"], "error": str(exc)})
        return {"run_id": run_id, "previews": previews,
                "note": "Preview only. The executor never merges, resets or deletes a worktree."}

    def audit(self, run_id: str) -> dict:
        state = self.supervisor.status(run_id)
        rich = self._plan(run_id)
        acceptance = _acceptance_map(rich)
        issues = []
        for task in state["tasks"]:
            task_id = task["id"]
            if task["state"] != "completed":
                if task["state"] not in ("pending", "running"):
                    issues.append(f"{task_id}: terminal state is {task['state']}")
                continue
            result = task.get("result") or {}
            tests = result.get("tests") if isinstance(result, dict) else None
            task_spec = next((item for item in rich.get("tasks", []) if item.get("id") == task_id), {})
            if not isinstance(tests, list) or any(item.get("status") == "failed" for item in tests):
                issues.append(f"{task_id}: result has failing or missing tests")
            elif task_spec.get("role") in ("implementer", "tester") and not tests:
                issues.append(f"{task_id}: implementation/test task has no recorded test")
            reported = result.get("acceptance") if isinstance(result, dict) else None
            expected = acceptance.get(task_id, [])
            if not isinstance(reported, list) or len(reported) != len(expected) or any(
                    not isinstance(item, dict) or item.get("status") != "passed" for item in reported):
                issues.append(f"{task_id}: acceptance evidence is incomplete")
            if state.get("native_supervision") and not task.get("native_stop_observed"):
                issues.append(f"{task_id}: native stop was not observed")
            if state.get("require_review") and not task.get("review_attested"):
                issues.append(f"{task_id}: review attestation is missing")
        if not state.get("complete"):
            issues.append("plan is not complete")
        return {"run_id": run_id, "ok": not issues, "issues": issues, "status": state}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("next", "status", "preview", "audit"):
        command = sub.add_parser(name)
        command.add_argument("--run-id", required=True)
        command.add_argument("--json", action="store_true")
    bind = sub.add_parser("bind")
    bind.add_argument("--run-id", required=True)
    bind.add_argument("--task-id", required=True)
    bind.add_argument("--lease", required=True)
    bind.add_argument("--platform", required=True, choices=["codex", "opencode", "cursor"])
    native = bind.add_mutually_exclusive_group(required=True)
    native.add_argument("--native-id")
    native.add_argument("--native-hash")
    complete = sub.add_parser("complete")
    complete.add_argument("--run-id", required=True)
    complete.add_argument("--task-id", required=True)
    complete.add_argument("--lease", required=True)
    complete.add_argument("--result", required=True, type=Path)
    complete.add_argument("--review", type=Path)
    fail = sub.add_parser("fail")
    fail.add_argument("--run-id", required=True)
    fail.add_argument("--task-id", required=True)
    fail.add_argument("--lease", required=True)
    fail.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    executor = None
    try:
        executor = Executor(args.root)
        if args.command == "next":
            output = executor.next(args.run_id)
            print(json.dumps(output, indent=2, ensure_ascii=False) if args.json
                  else (f"Claimed {len(output['claimed'])} task(s); manifest: {output.get('manifest', 'none')}"))
        elif args.command == "bind":
            output = executor.bind(args.run_id, args.task_id, args.lease, args.platform,
                                   native_id=args.native_id, native_hash=args.native_hash)
            print(json.dumps(output, indent=2))
        elif args.command == "complete":
            result = json.loads(Path(args.result).read_text(encoding="utf-8"))
            review = json.loads(Path(args.review).read_text(encoding="utf-8")) if args.review else None
            output = executor.complete(args.run_id, args.task_id, args.lease, result, review)
            print(json.dumps(output, indent=2))
        elif args.command == "fail":
            output = executor.fail(args.run_id, args.task_id, args.lease, args.reason)
            print(json.dumps(output, indent=2))
        elif args.command == "status":
            output = executor.status(args.run_id)
            print(json.dumps(output, indent=2, ensure_ascii=False) if args.json
                  else f"{args.run_id}: complete={output['complete']}")
        elif args.command == "preview":
            output = executor.preview(args.run_id)
            print(json.dumps(output, indent=2, ensure_ascii=False) if args.json
                  else f"{len(output['previews'])} worktree preview(s); no merge performed")
        else:
            output = executor.audit(args.run_id)
            print(json.dumps(output, indent=2, ensure_ascii=False) if args.json
                  else ("Audit OK" if output["ok"] else "Audit issues:\n- " + "\n- ".join(output["issues"])))
            return 0 if output["ok"] else 1
    except (OSError, ValueError, PlannerError, SupervisorError, WorktreeError) as exc:
        print(f"EXECUTOR ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if executor is not None:
            executor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
