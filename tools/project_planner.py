#!/usr/bin/env python3
"""Deterministic, acceptance-aware project planner for the existing Supervisor.

The planner does not call an LLM and does not spawn agents. It validates a
human/agent-authored task DAG, rejects unsafe scope overlap, records acceptance
criteria next to the Supervisor plan, and initializes the durable run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from tools.supervisor import Supervisor, SupervisorError, identifier, intersects, scope, validate_plan
    from tools.telemetry import span
except ModuleNotFoundError:  # Running as ``python tools/project_planner.py``.
    from supervisor import Supervisor, SupervisorError, identifier, intersects, scope, validate_plan
    from telemetry import span

PLAN_DIR = Path(".auto-orchestrator") / "plans"
MAX_ACCEPTANCE = 10
MAX_ACCEPTANCE_TEXT = 500
PROJECT_ID = re.compile(r"^p_[0-9a-f]{16}$")


class PlannerError(ValueError):
    pass


def _require_text(value, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PlannerError(f"{field} must be 1..{limit} characters")
    return value


def _normalize_acceptance(item: dict) -> list[str]:
    acceptance = item.get("acceptance")
    if not isinstance(acceptance, list) or not 1 <= len(acceptance) <= MAX_ACCEPTANCE:
        raise PlannerError("Each task requires 1..10 acceptance criteria")
    cleaned = []
    for criterion in acceptance:
        cleaned.append(_require_text(criterion, "acceptance criterion", MAX_ACCEPTANCE_TEXT))
    if len(set(cleaned)) != len(cleaned):
        raise PlannerError("Acceptance criteria must be unique per task")
    return cleaned


def _ancestors(tasks: dict[str, dict]) -> dict[str, set[str]]:
    memo: dict[str, set[str]] = {}

    def visit(task_id: str, stack: set[str]) -> set[str]:
        if task_id in memo:
            return memo[task_id]
        if task_id in stack:
            raise PlannerError("Dependency cycle detected")
        result = set()
        for dependency in tasks[task_id]["depends_on"]:
            result.add(dependency)
            result.update(visit(dependency, stack | {task_id}))
        memo[task_id] = result
        return result

    for task_id in tasks:
        visit(task_id, set())
    return memo


def _overlap(a: list[str], b: list[str]) -> bool:
    return any(intersects(x, y) for x in a for y in b)


def validate_rich_plan(plan: dict) -> dict:
    if not isinstance(plan, dict):
        raise PlannerError("Plan must be an object")
    run_id = identifier(plan.get("run_id"))
    project_id = plan.get("project_id")
    if project_id is not None and (not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id)):
        raise PlannerError("project_id must be p_<16 hex> when supplied")
    try:
        tasks, max_parallel = validate_plan(plan)
    except SupervisorError as exc:
        raise PlannerError(str(exc)) from exc
    normalized = {}
    for task_id, task in tasks.items():
        original = next(item for item in plan["tasks"] if item.get("id") == task_id)
        normalized[task_id] = {**task, "acceptance": _normalize_acceptance(original)}
    ancestors = _ancestors(normalized)
    ids = list(normalized)
    for index, left_id in enumerate(ids):
        for right_id in ids[index + 1:]:
            left, right = normalized[left_id], normalized[right_id]
            ordered = left_id in ancestors[right_id] or right_id in ancestors[left_id]
            if ordered:
                continue
            if _overlap(left["write"], right["write"]):
                raise PlannerError(f"Unordered tasks write overlapping scopes: {left_id}, {right_id}")
            if _overlap(left["write"], right["read"]) or _overlap(right["write"], left["read"]):
                raise PlannerError(f"Unordered tasks read/write overlapping scopes: {left_id}, {right_id}")
    return {
        "version": 1,
        "run_id": run_id,
        "project_id": project_id,
        "max_parallel": max_parallel,
        "native_supervision": bool(plan.get("native_supervision", False)),
        "require_review": bool(plan.get("require_review", False)),
        "created_at": time.time(),
        "tasks": list(normalized.values()),
        **{key: plan[key] for key in
           ("requirements_contract", "contract_ref", "assumption_guard")
           if key in plan},
    }


def supervisor_plan(rich: dict) -> dict:
    return {
        "max_parallel": rich["max_parallel"],
        "native_supervision": rich["native_supervision"],
        "require_review": rich["require_review"],
        "tasks": [{
            "id": task["id"],
            "role": task["role"],
            "objective": task["objective"],
            "department": task.get("department"),
            "depends_on": task["depends_on"],
            "read_scope": task["read"],
            "write_scope": task["write"],
            "timeout_seconds": task["timeout_seconds"],
            "max_retries": task["max_retries"],
        } for task in rich["tasks"]],
    }


def plan_path(root: Path, run_id: str) -> Path:
    identifier(run_id)
    return Path(root).resolve() / PLAN_DIR / f"{run_id}.json"


def write_plan(root: Path, rich: dict) -> Path:
    path = plan_path(root, rich["run_id"])
    if path.exists() or path.is_symlink():
        raise PlannerError("A planner artifact already exists for this run")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise PlannerError("Planner directory must not be a symlink")
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, (json.dumps(rich, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return path


def load_plan(root: Path, run_id: str) -> dict:
    path = plan_path(root, run_id)
    if path.is_symlink() or not path.is_file():
        raise PlannerError("Planner artifact not found")
    if path.stat().st_size > 131072:
        raise PlannerError("Planner artifact is too large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlannerError("Planner artifact is invalid") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise PlannerError("Unsupported planner artifact")
    return value


def initialize(root: Path, plan: dict) -> dict:
    rich = validate_rich_plan(plan)
    root = Path(root).resolve()
    with span("task.plan", project_id=rich.get("project_id"), run_id=rich["run_id"],
              tasks=len(rich["tasks"]), mode="supervisor"):
        path = write_plan(root, rich)
        supervisor = Supervisor(root)
        try:
            supervisor.init(rich["run_id"], supervisor_plan(rich))
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        finally:
            supervisor.close()
    return {"run_id": rich["run_id"], "tasks": len(rich["tasks"]),
            "max_parallel": rich["max_parallel"], "planner_artifact": str(path)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--plan", required=True, type=Path)
    validate.add_argument("--json", action="store_true")
    init = sub.add_parser("init")
    init.add_argument("--plan", required=True, type=Path)
    init.add_argument("--json", action="store_true")
    show = sub.add_parser("show")
    show.add_argument("--run-id", required=True)
    show.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            value = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            rich = validate_rich_plan(value)
            output = {"run_id": rich["run_id"], "tasks": len(rich["tasks"]),
                      "max_parallel": rich["max_parallel"],
                      "acceptance": {task["id"]: task["acceptance"] for task in rich["tasks"]}}
            print(json.dumps(output, indent=2, ensure_ascii=False) if args.json
                  else f"Valid plan: {output['run_id']} ({output['tasks']} tasks)")
        elif args.command == "init":
            value = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            output = initialize(args.root, value)
            print(json.dumps(output, indent=2) if args.json
                  else f"Initialized {output['run_id']} with {output['tasks']} tasks")
        else:
            value = load_plan(args.root, args.run_id)
            print(json.dumps(value, indent=2, ensure_ascii=False) if args.json
                  else f"{value['run_id']}: {len(value['tasks'])} tasks, max_parallel={value['max_parallel']}")
    except (OSError, ValueError, SupervisorError) as exc:
        print(f"PLANNER ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
