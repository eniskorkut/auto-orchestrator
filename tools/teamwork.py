#!/usr/bin/env python3
"""V7.3 teamwork milestone engine.

The engine is a thin, deterministic coordination layer over the existing
Supervisor SQLite database. It does not spawn, signal or cancel native agents.
Supervisor task states are the only source of build/verification truth; a
milestone passes only when every task completed and every required
verification role has an explicit ``result.verdict == "pass"``.

All persisted state is additive in ``.auto-orchestrator/supervisor.sqlite3`` so
restarting the process reloads the same milestone state. Handoff documents are
versioned, content-addressed JSON files written atomically.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path

try:
    from tools.supervisor import (
        ROLES,
        Supervisor,
        SupervisorError,
        identifier,
        validate_plan,
    )
except ModuleNotFoundError:  # Running as ``python tools/teamwork.py``.
    from supervisor import (  # type: ignore
        ROLES,
        Supervisor,
        SupervisorError,
        identifier,
        validate_plan,
    )

PATTERNS = ("iterative_coding", "distributed_coding", "review_and_repair", "research")
PHASES = ("build", "verify", "repair")
MILESTONE_STATUSES = ("planned", "ready", "running", "verifying", "passed",
                      "failed", "blocked", "repairing", "cancelled")
TERMINAL_MILESTONE_STATUSES = frozenset({"passed", "cancelled"})
ACTIVE_TASK_STATES = frozenset({"running", "cancel_requested", "timed_out"})
FAILED_TASK_STATES = frozenset({"failed", "cancelled"})
SESSION_STATUSES = frozenset({"starting", "running", "completed", "failed",
                              "cancelled"})

MAX_MILESTONES = 64
MAX_TASKS_PER_MILESTONE = 64
MAX_ACCEPTANCE = 10
MAX_ACCEPTANCE_TEXT = 500
MAX_REPAIR = 64
MAX_VERIFY_COMMANDS = 20
MAX_VERIFY_COMMAND_LENGTH = 1000

HANDOFF_DIR = Path(".auto-orchestrator") / "handoffs"
HANDOFF_FIELDS = (
    "milestone_id", "completed_tasks", "decisions", "changed_files", "commits",
    "verified_tests", "review_findings", "open_issues", "next_milestone",
    "next_tasks", "context_summary", "evidence_refs",
)


class TeamworkError(ValueError):
    """Invalid milestone plan or illegal teamwork transition."""


def _identifier(value, field="identifier"):
    try:
        return identifier(value)
    except SupervisorError as exc:
        raise TeamworkError(f"Invalid {field}") from exc


def _text(value, field, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise TeamworkError(f"{field} must be 1..{limit} characters")
    if "\x00" in value:
        raise TeamworkError(f"{field} contains a NUL character")
    return value


def _short_string(value, field, limit):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise TeamworkError(f"{field} must be 1..{limit} characters")
    if any(ord(ch) < 32 for ch in value):
        raise TeamworkError(f"{field} must be a single-line value")
    return value


def _optional_string(value, field, limit):
    if value is None:
        return None
    return _short_string(value, field, limit)


def _optional_number(value, field):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TeamworkError(f"{field} must be numeric")
    if not math.isfinite(float(value)):
        raise TeamworkError(f"{field} must be finite")
    return float(value)


def _string_list(value, field, *, minimum=0, maximum=64, item_limit=1000,
                 unique=False, allow_empty=False):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise TeamworkError(f"{field} must contain {minimum}..{maximum} items")
    cleaned = []
    for item in value:
        if not isinstance(item, str) or len(item) > item_limit:
            raise TeamworkError(f"{field} contains an invalid string")
        if item == "" and not allow_empty:
            raise TeamworkError(f"{field} contains an empty string")
        if "\x00" in item:
            raise TeamworkError(f"{field} contains a NUL character")
        cleaned.append(item)
    if unique and len(set(cleaned)) != len(cleaned):
        raise TeamworkError(f"{field} must be unique")
    return cleaned


def _validate_policy(value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise TeamworkError("verification_policy must be an object")
    require_roles = value.get("require_roles", [])
    require_roles = _string_list(require_roles, "require_roles", maximum=16,
                                 item_limit=80, unique=True)
    for role in require_roles:
        if role not in ROLES:
            raise TeamworkError(f"Unknown verification role: {role}")
    max_repair = value.get("max_repair", 2)
    if type(max_repair) is not int or not 0 <= max_repair <= MAX_REPAIR:
        raise TeamworkError(f"max_repair must be 0..{MAX_REPAIR}")
    return {"require_roles": list(require_roles), "max_repair": max_repair}


def _validate_acceptance(value):
    if value is None:
        return []
    return _string_list(value, "milestone acceptance", maximum=MAX_ACCEPTANCE,
                        item_limit=MAX_ACCEPTANCE_TEXT, unique=True)


def _validate_task_metadata(item):
    phase = item.get("phase", "build")
    if phase not in PHASES:
        raise TeamworkError(f"Unknown task phase: {phase!r}")
    provider = item.get("provider")
    if provider is not None:
        if (not isinstance(provider, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", provider)):
            raise TeamworkError("provider must be a short identifier")
    model = item.get("model")
    if model is not None:
        model = _short_string(model, "model", 200)
    commands = item.get("verify_commands", [])
    if commands is None:
        commands = []
    commands = _string_list(commands, "verify_commands", maximum=MAX_VERIFY_COMMANDS,
                            item_limit=MAX_VERIFY_COMMAND_LENGTH)
    return {
        "phase": phase,
        "provider": provider,
        "model": model,
        "verify_commands": list(commands),
    }


def validate_teamwork_plan(plan):
    """Validate a V7.1/V7.2 plan plus optional V7.3 milestones.

    Returns a normalized dictionary. Existing plans without ``milestones``
    remain valid and are mapped to one derived milestone, ``M1``.
    """
    if not isinstance(plan, dict):
        raise TeamworkError("Plan must be an object")
    run_id = _identifier(plan.get("run_id"), "run_id")
    pattern = plan.get("pattern")
    if pattern is not None and pattern not in PATTERNS:
        raise TeamworkError("Unknown teamwork pattern")
    try:
        tasks, max_parallel = validate_plan(plan)
    except SupervisorError as exc:
        raise TeamworkError(str(exc)) from exc

    raw_tasks = plan["tasks"]
    metadata = {}
    for item in raw_tasks:
        task_id = _identifier(item.get("id"), "task id")
        metadata[task_id] = _validate_task_metadata(item)

    raw_milestones = plan.get("milestones", None)
    has_milestones = raw_milestones is not None
    if has_milestones:
        if not isinstance(raw_milestones, list) or not 1 <= len(raw_milestones) <= MAX_MILESTONES:
            raise TeamworkError(f"Plan requires 1..{MAX_MILESTONES} milestones")
    elif any(item.get("milestone") is not None for item in raw_tasks):
        raise TeamworkError("Task milestones require a milestones plan")

    milestone_order = []
    milestone_by_id = {}
    if not has_milestones:
        milestone_order = ["M1"]
        milestone_by_id["M1"] = {
            "id": "M1",
            "objective": "All tasks from the legacy plan",
            "depends_on": [],
            "acceptance": [],
            "verification_policy": {"require_roles": [], "max_repair": 2},
            "task_ids": [],
        }
    else:
        for item in raw_milestones:
            if not isinstance(item, dict):
                raise TeamworkError("Each milestone must be an object")
            milestone_id = _identifier(item.get("id"), "milestone id")
            if milestone_id in milestone_by_id:
                raise TeamworkError("Duplicate milestone id")
            raw_objective = item.get("objective")
            objective = (f"Milestone {milestone_id}" if raw_objective is None
                         else _text(raw_objective, "milestone objective", 1500))
            depends_on = item.get("depends_on", [])
            depends_on = _string_list(depends_on, "milestone depends_on", maximum=64,
                                      item_limit=80, unique=True)
            acceptance = _validate_acceptance(item.get("acceptance"))
            policy = _validate_policy(item.get("verification_policy"))
            milestone_by_id[milestone_id] = {
                "id": milestone_id,
                "objective": objective,
                "depends_on": list(depends_on),
                "acceptance": list(acceptance),
                "verification_policy": policy,
                "task_ids": [],
            }
            milestone_order.append(milestone_id)
        for milestone in milestone_by_id.values():
            for dependency in milestone["depends_on"]:
                if dependency not in milestone_by_id:
                    raise TeamworkError("Unknown milestone dependency")
                if dependency == milestone["id"]:
                    raise TeamworkError("Milestone dependency cycle detected")

    task_milestones = {}
    if not has_milestones:
        for task_id in tasks:
            task_milestones[task_id] = "M1"
            milestone_by_id["M1"]["task_ids"].append(task_id)
    else:
        for item in raw_tasks:
            task_id = item["id"]
            milestone_id = item.get("milestone")
            if milestone_id is None:
                raise TeamworkError("Every task requires a milestone")
            milestone_id = _identifier(milestone_id, "task milestone")
            if milestone_id not in milestone_by_id:
                raise TeamworkError(f"Unknown milestone for task {task_id}")
            task_milestones[task_id] = milestone_id
            milestone_by_id[milestone_id]["task_ids"].append(task_id)

    for milestone in milestone_by_id.values():
        if not 1 <= len(milestone["task_ids"]) <= MAX_TASKS_PER_MILESTONE:
            raise TeamworkError(
                f"Milestone {milestone['id']} requires "
                f"1..{MAX_TASKS_PER_MILESTONE} tasks")

    # Milestone dependency cycles.
    visiting = set()
    visited = set()

    def visit_milestone(milestone_id):
        if milestone_id in visiting:
            raise TeamworkError("Milestone dependency cycle detected")
        if milestone_id in visited:
            return
        visiting.add(milestone_id)
        for dependency in milestone_by_id[milestone_id]["depends_on"]:
            visit_milestone(dependency)
        visiting.remove(milestone_id)
        visited.add(milestone_id)

    for milestone_id in milestone_order:
        visit_milestone(milestone_id)

    ancestors = {}

    def ancestor_set(milestone_id):
        if milestone_id in ancestors:
            return ancestors[milestone_id]
        result = set()
        for dependency in milestone_by_id[milestone_id]["depends_on"]:
            result.add(dependency)
            result.update(ancestor_set(dependency))
        ancestors[milestone_id] = result
        return result

    for milestone_id in milestone_order:
        ancestor_set(milestone_id)

    # A task may only depend on another task in the same milestone or in a
    # milestone that is a transitive prerequisite of its own milestone.
    for task_id, task in tasks.items():
        own_milestone = task_milestones[task_id]
        for dependency in task["depends_on"]:
            dependency_milestone = task_milestones[dependency]
            if dependency_milestone == own_milestone:
                continue
            if dependency_milestone not in ancestors[own_milestone]:
                raise TeamworkError(
                    f"Task {task_id} depends on a task in an unordered or later "
                    f"milestone ({dependency_milestone})")

    normalized_tasks = {}
    for task_id, task in tasks.items():
        normalized_tasks[task_id] = {
            **task,
            "milestone": task_milestones[task_id],
            **metadata[task_id],
        }
    work_mode = plan.get("work_mode", "legacy")
    if not isinstance(work_mode, str) or len(work_mode) > 40:
        raise TeamworkError("work_mode must be a short string")
    risk_assessment = plan.get("risk_assessment")
    if risk_assessment is not None and not isinstance(risk_assessment, dict):
        raise TeamworkError("risk_assessment must be an object")
    request = plan.get("request")
    if request is not None and (not isinstance(request, str) or len(request) > 20000):
        raise TeamworkError("request must be a bounded string")
    repo_profile = plan.get("repo_profile")
    if repo_profile is not None and not isinstance(repo_profile, dict):
        raise TeamworkError("repo_profile must be an object")
    plan_version = plan.get("plan_version", 1)
    if plan_version not in (1, 2):
        raise TeamworkError("plan_version must be 1 or 2")
    integrity_policy = plan.get("integrity_policy")
    if integrity_policy is not None and integrity_policy not in (
            "development", "demo", "benchmark"):
        raise TeamworkError("integrity_policy is invalid")
    for field in ("scope_contract", "adaptive_decision"):
        value = plan.get(field)
        if value is not None and not isinstance(value, dict):
            raise TeamworkError(f"{field} must be an object")
    for field in ("file_ownership", "ownership_conflicts"):
        value = plan.get(field)
        if value is not None and (not isinstance(value, list)
                                  or len(value) > 200):
            raise TeamworkError(f"{field} must be a bounded array")
    normalized = {
        "run_id": run_id,
        "pattern": pattern,
        "work_mode": work_mode,
        "plan_version": plan_version,
        "integrity_policy": integrity_policy,
        "scope_contract": plan.get("scope_contract"),
        "adaptive_decision": plan.get("adaptive_decision"),
        "file_ownership": plan.get("file_ownership"),
        "ownership_conflicts": plan.get("ownership_conflicts"),
        "risk_assessment": risk_assessment,
        "request": request,
        "repo_profile": repo_profile,
        "max_parallel": max_parallel,
        "milestones": [milestone_by_id[mid] for mid in milestone_order],
        "tasks": normalized_tasks,
    }
    # Guard metadata is opaque to milestone coordination but must survive
    # validation so the planner artifact, snapshot and status agree.
    for key in ("requirements_contract", "contract_ref", "assumption_guard"):
        if key in plan:
            normalized[key] = plan[key]
    return normalized


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _handoff_bytes(payload):
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _parse_json(value, default):
    if value is None:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise TeamworkError("Persisted teamwork state is invalid") from exc
    return parsed


class TeamworkEngine:
    """Persisted milestone state machine over ``Supervisor``."""

    def __init__(self, root: Path):
        self.supervisor = Supervisor(root)
        self.root = Path(self.supervisor.root)
        self.conn = self.supervisor.conn
        self._create_schema()

    def close(self):
        self.supervisor.close()

    # -- persistence -----------------------------------------------------
    def _create_schema(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS milestones (
          run_id TEXT NOT NULL,
          id TEXT NOT NULL,
          objective TEXT NOT NULL,
          dependencies TEXT NOT NULL,
          acceptance TEXT NOT NULL,
          verification_policy TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'planned',
          started_at REAL,
          completed_at REAL,
          repair_iterations INTEGER NOT NULL DEFAULT 0,
          handoff_path TEXT,
          last_error TEXT,
          PRIMARY KEY(run_id,id));
        CREATE TABLE IF NOT EXISTS milestone_tasks (
          run_id TEXT NOT NULL,
          milestone_id TEXT NOT NULL,
          task_id TEXT NOT NULL,
          phase TEXT NOT NULL,
          PRIMARY KEY(run_id,task_id));
        CREATE INDEX IF NOT EXISTS milestone_tasks_milestone
          ON milestone_tasks(run_id,milestone_id);
        CREATE TABLE IF NOT EXISTS teamwork_handoffs (
          run_id TEXT NOT NULL,
          milestone_id TEXT NOT NULL,
          version INTEGER NOT NULL,
          path TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          created_at REAL NOT NULL,
          PRIMARY KEY(run_id,milestone_id,version));
        CREATE TABLE IF NOT EXISTS managed_sessions (
          run_id TEXT NOT NULL,
          task_id TEXT NOT NULL,
          attempt INTEGER NOT NULL,
          provider TEXT NOT NULL,
          session_id TEXT NOT NULL,
          status TEXT NOT NULL,
          started_at REAL,
          updated_at REAL,
          finished_at REAL,
          events_path TEXT,
          model TEXT,
          cost REAL,
          tokens TEXT,
          error TEXT,
          cancel_state TEXT,
          resume_of INTEGER,
          tool_count INTEGER NOT NULL DEFAULT 0,
          pid INTEGER,
          pid_start_token TEXT,
          pid_uid INTEGER,
          PRIMARY KEY(run_id,task_id,attempt));
        CREATE TABLE IF NOT EXISTS teamwork_plans (
          run_id TEXT PRIMARY KEY,
          pattern TEXT,
          plan TEXT NOT NULL,
          created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS requirements_verification (
          run_id TEXT PRIMARY KEY,
          payload TEXT NOT NULL,
          updated_at REAL NOT NULL);
        """)
        session_columns = {row["name"] for row in self.conn.execute(
            "PRAGMA table_info(managed_sessions)")}
        if "tool_count" not in session_columns:
            try:
                self.conn.execute(
                    "ALTER TABLE managed_sessions ADD COLUMN tool_count INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        for column, kind in (
            ("pid", "INTEGER"),
            ("pid_start_token", "TEXT"),
            ("pid_uid", "INTEGER"),
        ):
            if column not in session_columns:
                try:
                    self.conn.execute(
                        f"ALTER TABLE managed_sessions ADD COLUMN {column} {kind}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise

    def _transaction(self, fn):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            value = fn()
            self.conn.execute("COMMIT")
            return value
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def _run_exists(self, run_id):
        return self.conn.execute(
            "SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is not None

    def _require_run(self, run_id):
        run_id = _identifier(run_id, "run_id")
        if not self._run_exists(run_id):
            raise TeamworkError("Run does not exist")
        return run_id

    def _delete_run(self, run_id):
        def op():
            for table in ("teamwork_handoffs", "teamwork_plans",
                          "requirements_verification", "milestone_tasks",
                          "milestones"):
                self.conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
            self.conn.execute("DELETE FROM native_bindings WHERE run_id=?", (run_id,))
            self.conn.execute("DELETE FROM tasks WHERE run_id=?", (run_id,))
            self.conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
        try:
            self._transaction(op)
        except sqlite3.Error:
            pass

    def _snapshot(self, normalized):
        snapshot = {
            "run_id": normalized["run_id"],
            "pattern": normalized["pattern"],
            "work_mode": normalized.get("work_mode", "legacy"),
            "risk_assessment": normalized.get("risk_assessment"),
            "request": normalized.get("request"),
            "repo_profile": normalized.get("repo_profile"),
            "max_parallel": normalized["max_parallel"],
            "milestones": [{
                "id": milestone["id"],
                "objective": milestone["objective"],
                "depends_on": milestone["depends_on"],
                "acceptance": milestone["acceptance"],
                "verification_policy": milestone["verification_policy"],
            } for milestone in normalized["milestones"]],
            "tasks": [{
                "id": task_id,
                "role": task["role"],
                "objective": task["objective"],
                "depends_on": task["depends_on"],
                "milestone": task["milestone"],
                "phase": task["phase"],
                "provider": task["provider"],
                "model": task["model"],
                "verify_commands": task["verify_commands"],
                "department": task["department"],
            } for task_id, task in normalized["tasks"].items()],
        }
        for key in ("requirements_contract", "contract_ref", "assumption_guard"):
            if key in normalized:
                snapshot[key] = normalized[key]
        return snapshot

    def _load_plan_snapshot(self, run_id):
        row = self.conn.execute(
            "SELECT plan FROM teamwork_plans WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise TeamworkError("Teamwork plan snapshot is missing")
        value = _parse_json(row["plan"], {})
        if not isinstance(value, dict):
            raise TeamworkError("Teamwork plan snapshot is invalid")
        return value

    def _load_milestones(self, run_id):
        rows = list(self.conn.execute(
            "SELECT * FROM milestones WHERE run_id=? ORDER BY rowid", (run_id,)))
        if not rows:
            raise TeamworkError("No teamwork milestones for run")
        mappings = list(self.conn.execute(
            "SELECT milestone_id,task_id,phase FROM milestone_tasks "
            "WHERE run_id=? ORDER BY rowid", (run_id,)))
        by_id = {}
        for row in rows:
            value = {
                "id": row["id"],
                "objective": row["objective"],
                "depends_on": _parse_json(row["dependencies"], []),
                "acceptance": _parse_json(row["acceptance"], []),
                "verification_policy": _parse_json(row["verification_policy"], {}),
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "repair_iterations": row["repair_iterations"],
                "handoff_path": row["handoff_path"],
                "last_error": row["last_error"],
                "task_ids": [],
                "phases": {},
            }
            by_id[value["id"]] = value
        for mapping in mappings:
            milestone = by_id.get(mapping["milestone_id"])
            if milestone is None:
                raise TeamworkError("Milestone task mapping is invalid")
            milestone["task_ids"].append(mapping["task_id"])
            milestone["phases"][mapping["task_id"]] = mapping["phase"]
        return list(by_id.values())

    # -- initialization --------------------------------------------------
    def init(self, plan):
        normalized = validate_teamwork_plan(plan)
        request = normalized.get("request")
        receipt = normalized.get("assumption_guard")
        contract = normalized.get("requirements_contract")
        if request:
            if receipt is not None:
                try:
                    try:
                        from tools import assumption_guard
                    except ModuleNotFoundError:
                        import assumption_guard  # type: ignore
                    assumption_guard.validate_receipt(self.root, request, receipt)
                except Exception as exc:
                    raise TeamworkError(
                        f"Assumption Guard receipt is invalid: {exc}") from exc
            if contract is not None:
                try:
                    try:
                        from tools import requirements_contract
                    except ModuleNotFoundError:
                        import requirements_contract  # type: ignore
                    requirements_contract.validate_contract(contract)
                except Exception as exc:
                    raise TeamworkError(
                        f"Requirements contract is invalid: {exc}") from exc
            if receipt is None and contract is None:
                raise TeamworkError(
                    "A new development plan requires an Assumption Guard receipt "
                    "or an approved requirements contract")
        run_id = normalized["run_id"]
        snapshot = self._snapshot(normalized)
        canonical = _canonical(snapshot)
        existing = self.conn.execute(
            "SELECT plan FROM teamwork_plans WHERE run_id=?", (run_id,)).fetchone()
        if self._run_exists(run_id):
            if existing is not None and existing["plan"] == canonical:
                return self.status(run_id)
            raise TeamworkError("Run already exists")
        try:
            self.supervisor.init(run_id, plan)
        except SupervisorError as exc:
            raise TeamworkError(str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise TeamworkError("Run already exists") from exc

        def insert():
            self.conn.execute(
                "INSERT INTO teamwork_plans (run_id,pattern,plan,created_at) "
                "VALUES (?,?,?,?)",
                (run_id, normalized["pattern"], canonical, time.time()))
            for milestone in normalized["milestones"]:
                self.conn.execute("""
                  INSERT INTO milestones
                    (run_id,id,objective,dependencies,acceptance,verification_policy,
                     status,started_at,completed_at,repair_iterations,handoff_path,
                     last_error)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (run_id, milestone["id"], milestone["objective"],
                      _canonical(milestone["depends_on"]),
                      _canonical(milestone["acceptance"]),
                      _canonical(milestone["verification_policy"]),
                      "planned", None, None, 0, None, None))
            for task_id, task in normalized["tasks"].items():
                self.conn.execute("""
                  INSERT INTO milestone_tasks (run_id,milestone_id,task_id,phase)
                  VALUES (?,?,?,?)
                """, (run_id, task["milestone"], task_id, task["phase"]))

        try:
            self._transaction(insert)
        except BaseException:
            self._delete_run(run_id)
            raise
        return self.status(run_id)

    # -- state machine ---------------------------------------------------
    @staticmethod
    def _verification_state(milestone, tasks_by_id):
        policy = milestone.get("verification_policy") or {}
        require_roles = list(policy.get("require_roles") or [])
        satisfied = []
        missing = []
        verdicts = {}
        for role in require_roles:
            passed = False
            role_verdicts = []
            for task_id in milestone["task_ids"]:
                task = tasks_by_id.get(task_id)
                if task is None or task.get("role") != role:
                    continue
                result = task.get("result")
                verdict = result.get("verdict") if isinstance(result, dict) else None
                if verdict is not None:
                    role_verdicts.append(verdict)
                if task.get("state") == "completed" and verdict == "pass":
                    passed = True
            verdicts[role] = role_verdicts
            if passed:
                satisfied.append(role)
            else:
                missing.append(role)
        return {
            "require_roles": require_roles,
            "satisfied_roles": satisfied,
            "missing_roles": missing,
            "verdicts": verdicts,
            "satisfied": not missing,
        }

    def _compute_status(self, milestone, tasks_by_id, dependency_statuses, now):
        task_states = [
            tasks_by_id[task_id].get("state", "pending")
            for task_id in milestone["task_ids"]
            if task_id in tasks_by_id
        ]
        dependency_values = list(dependency_statuses.values())
        all_dependencies_passed = all(value == "passed" for value in dependency_values)
        dependency_failed = any(
            value in ("failed", "cancelled", "blocked") for value in dependency_values)
        all_cancelled = bool(task_states) and all(
            value == "cancelled" for value in task_states)
        any_failed = any(value in FAILED_TASK_STATES for value in task_states)
        all_completed = bool(task_states) and all(
            value == "completed" for value in task_states)
        any_non_pending = any(value != "pending" for value in task_states)
        any_active = any(value in ACTIVE_TASK_STATES for value in task_states)
        verification = self._verification_state(milestone, tasks_by_id)
        rejected = []
        for task_id in milestone["task_ids"]:
            task = tasks_by_id.get(task_id)
            if task is None:
                continue
            task_result = task.get("result")
            verdict = (task_result.get("verdict")
                       if isinstance(task_result, dict) else None)
            if task.get("state") == "completed" and verdict not in (None, "pass"):
                label = (task.get("role")
                         if task.get("role") in verification["require_roles"]
                         else task_id)
                if label not in rejected:
                    rejected.append(label)

        last_error = None
        if all_cancelled:
            status = "cancelled"
        elif any_failed:
            status = "failed"
            failed = next(
                (tasks_by_id[task_id] for task_id in milestone["task_ids"]
                 if tasks_by_id.get(task_id, {}).get("state") in FAILED_TASK_STATES),
                None)
            if failed is not None:
                reason = failed.get("reason")
                if isinstance(reason, str) and reason.strip():
                    last_error = reason
                else:
                    last_error = f"Task {failed['id']} {failed['state']}"
            else:
                last_error = "A milestone task failed"
        elif not all_dependencies_passed:
            if dependency_failed or any_non_pending:
                status = "blocked"
                if dependency_failed:
                    blocked = [milestone_id for milestone_id, value
                               in dependency_statuses.items() if value != "passed"]
                    last_error = "Blocked by milestone dependency: " + ", ".join(blocked)
                else:
                    last_error = "Tasks started before dependencies passed"
            else:
                status = "planned"
        elif all_completed:
            if rejected:
                status = "failed"
                last_error = "Verification verdict failed: " + ", ".join(rejected)
            elif verification["satisfied"]:
                status = "passed"
            else:
                status = "verifying"
        elif milestone["status"] == "repairing":
            status = "repairing"
        elif any_active or any_non_pending:
            status = "running"
        else:
            status = "ready"

        started_at = milestone["started_at"]
        if (status in ("running", "verifying", "repairing", "passed", "failed",
                       "cancelled")
                and started_at is None):
            started_at = now
        if status in ("planned", "ready"):
            started_at = None
        if status == "blocked" and any_non_pending and started_at is None:
            started_at = now

        completed_at = milestone["completed_at"]
        if status in ("passed", "failed", "cancelled"):
            if completed_at is None:
                completed_at = now
        else:
            completed_at = None

        if status == "repairing":
            last_error = milestone["last_error"] or "Repair iteration open"
        elif status == "blocked":
            pass
        elif status == "failed":
            pass
        else:
            last_error = None

        return {
            "status": status,
            "started_at": started_at,
            "completed_at": completed_at,
            "last_error": last_error,
        }

    def refresh(self, run_id):
        run_id = self._require_run(run_id)
        milestones = self._load_milestones(run_id)
        supervisor_state = self.supervisor.status(run_id)
        tasks_by_id = {task["id"]: task for task in supervisor_state["tasks"]}
        milestone_by_id = {milestone["id"]: milestone for milestone in milestones}
        for milestone in milestones:
            for task_id in milestone["task_ids"]:
                if task_id not in tasks_by_id:
                    raise TeamworkError("Milestone references an unknown Supervisor task")
        now = time.time()
        computed = {}
        resolving = set()

        def resolve(milestone_id):
            if milestone_id in computed:
                return computed[milestone_id]["status"]
            if milestone_id in resolving:
                raise TeamworkError("Milestone dependency cycle detected")
            if milestone_id not in milestone_by_id:
                raise TeamworkError("Unknown milestone dependency")
            resolving.add(milestone_id)
            milestone = milestone_by_id[milestone_id]
            dependencies = {
                dependency: resolve(dependency)
                for dependency in milestone["depends_on"]
            }
            computed[milestone_id] = self._compute_status(
                milestone, tasks_by_id, dependencies, now)
            resolving.remove(milestone_id)
            return computed[milestone_id]["status"]

        for milestone in milestones:
            resolve(milestone["id"])

        def persist():
            for milestone in milestones:
                desired = computed[milestone["id"]]
                if (milestone["status"] == desired["status"]
                        and milestone["started_at"] == desired["started_at"]
                        and milestone["completed_at"] == desired["completed_at"]
                        and milestone["last_error"] == desired["last_error"]):
                    continue
                self.conn.execute("""
                  UPDATE milestones SET status=?,started_at=?,completed_at=?,last_error=?
                  WHERE run_id=? AND id=?
                """, (desired["status"], desired["started_at"],
                      desired["completed_at"], desired["last_error"],
                      run_id, milestone["id"]))

        self._transaction(persist)
        return self._status_payload(run_id)

    def _status_payload(self, run_id):
        run_id = self._require_run(run_id)
        milestones = self._load_milestones(run_id)
        supervisor_state = self.supervisor.status(run_id)
        tasks_by_id = {task["id"]: task for task in supervisor_state["tasks"]}
        managed_rows = self._managed_sessions(run_id)
        managed_by_task = {}
        for row in managed_rows:
            managed_by_task[row["task_id"]] = row
        snapshot = self._load_plan_snapshot(run_id)
        snapshot_tasks = {
            task["id"]: task for task in snapshot.get("tasks", [])
            if isinstance(task, dict) and isinstance(task.get("id"), str)
        }
        milestone_payloads = []
        all_task_payloads = []
        passed = 0
        for milestone in milestones:
            policy = milestone.get("verification_policy") or {}
            require_roles = list(policy.get("require_roles") or [])
            max_repair = policy.get("max_repair", 2)
            verification = self._verification_state(milestone, tasks_by_id)
            task_payloads = []
            for task_id in milestone["task_ids"]:
                task = tasks_by_id.get(task_id)
                if task is None:
                    raise TeamworkError("Milestone references an unknown Supervisor task")
                meta = snapshot_tasks.get(task_id, {})
                payload = {
                    "id": task["id"],
                    "task_id": task["id"],
                    "role": task["role"],
                    "state": task["state"],
                    "milestone": milestone["id"],
                    "phase": milestone["phases"].get(task_id, "build"),
                    "attempts": task.get("attempts", 0),
                    "max_retries": task.get("max_retries", 0),
                    "department": task.get("department"),
                    "reason": task.get("reason"),
                    "result": task.get("result"),
                    "native_stop_observed": bool(task.get("native_stop_observed")),
                    "review_attested": bool(task.get("review_attested")),
                    "provider": meta.get("provider"),
                    "model": meta.get("model"),
                    "verify_commands": list(meta.get("verify_commands") or []),
                    "depends_on": list(meta.get("depends_on") or []),
                    "waiting_on": [dep for dep in (meta.get("depends_on") or [])
                                   if tasks_by_id.get(dep, {}).get("state") != "completed"],
                    "duration_seconds": (round(task["finished"] - task["started"], 3)
                                         if task.get("started") is not None
                                         and task.get("finished") is not None else None),
                    "managed": ({
                        "provider": managed_by_task[task_id].get("provider"),
                        "status": managed_by_task[task_id].get("status"),
                        "model": managed_by_task[task_id].get("model"),
                        "cost": managed_by_task[task_id].get("cost"),
                        "tool_count": managed_by_task[task_id].get("tool_count"),
                        "session_hash": (hashlib.sha256(
                            str(managed_by_task[task_id].get("session_id", "")).encode()
                        ).hexdigest()[:12]),
                    } if task_id in managed_by_task else None),
                }
                task_payloads.append(payload)
                all_task_payloads.append(payload)
            if milestone["status"] == "passed":
                passed += 1
            milestone_payloads.append({
                "id": milestone["id"],
                "milestone_id": milestone["id"],
                "objective": milestone["objective"],
                "dependencies": list(milestone["depends_on"]),
                "acceptance": list(milestone["acceptance"]),
                "status": milestone["status"],
                "started_at": milestone["started_at"],
                "completed_at": milestone["completed_at"],
                "repair_iterations": milestone["repair_iterations"],
                "max_repair": max_repair,
                "verification_policy": {
                    "require_roles": require_roles,
                    "max_repair": max_repair,
                },
                "require_roles": require_roles,
                "satisfied_roles": verification["satisfied_roles"],
                "missing_roles": verification["missing_roles"],
                "verdicts": verification["verdicts"],
                "handoff_path": milestone["handoff_path"],
                "last_error": milestone["last_error"],
                "tasks": task_payloads,
            })
        total = len(milestone_payloads)
        verified_progress = (float(passed) / total) if total else 0.0
        statuses = [milestone["status"] for milestone in milestone_payloads]
        if total and passed == total:
            overall = "passed"
        elif "failed" in statuses:
            overall = "failed"
        elif "cancelled" in statuses and all(
                value == "cancelled" for value in statuses):
            overall = "cancelled"
        elif any(value in ("running", "verifying", "repairing") for value in statuses):
            overall = "running"
        elif "ready" in statuses:
            overall = "ready"
        elif "blocked" in statuses and not any(
                value in ("ready", "planned") for value in statuses):
            overall = "blocked"
        else:
            overall = "planned"
        active_sessions = sum(1 for row in managed_rows
                              if row.get("status") in ("starting", "running"))
        queued_tasks = sum(1 for task in all_task_payloads
                           if task.get("state") == "pending")
        repairing = any(m.get("status") == "repairing" for m in milestone_payloads)
        costs = [row.get("cost") for row in managed_rows
                 if isinstance(row.get("cost"), (int, float))]
        tool_counts = [row.get("tool_count") for row in managed_rows
                       if isinstance(row.get("tool_count"), int)]
        token_input = 0
        token_output = 0
        token_cache = 0
        token_seen = False
        for row in managed_rows:
            tokens = row.get("tokens")
            if not isinstance(tokens, dict):
                continue
            token_seen = True
            for key, target in (("input", "input"), ("output", "output")):
                value = tokens.get(key)
                if isinstance(value, (int, float)):
                    if target == "input":
                        token_input += int(value)
                    else:
                        token_output += int(value)
            cache = tokens.get("cache_read")
            if isinstance(cache, (int, float)):
                token_cache += int(cache)
        run_row = self.conn.execute(
            "SELECT max_parallel FROM runs WHERE id=?", (run_id,)).fetchone()
        payload = {
            **{key: snapshot[key] for key in
               ("requirements_contract", "contract_ref", "assumption_guard")
               if key in snapshot},
            "run_id": run_id,
            "pattern": snapshot.get("pattern"),
            "work_mode": snapshot.get("work_mode", "legacy"),
            "risk_assessment": snapshot.get("risk_assessment"),
            "request": snapshot.get("request"),
            "max_parallel": run_row["max_parallel"] if run_row else None,
            "status": overall,
            "complete": bool(total) and passed == total,
            "milestones": milestone_payloads,
            "milestone_ids": [milestone["id"] for milestone in milestone_payloads],
            "tasks": all_task_payloads,
            "task_ids": [task["id"] for task in all_task_payloads],
            "task_count": len(all_task_payloads),
            "milestone_count": total,
            "passed_milestones": passed,
            "verified_progress": verified_progress,
            "estimated_progress": None,
            "progress": {
                "passed": passed,
                "total": total,
                "verified": passed,
                "denominator": total,
            },
            "managed_sessions": managed_rows,
            "scheduler": {
                "active_sessions": active_sessions,
                "queued_tasks": queued_tasks,
                "repairing": repairing,
                "max_parallel": run_row["max_parallel"] if run_row else None,
            },
            "usage": {
                "cost_total_usd": round(sum(costs), 9) if costs else None,
                "cost_measured": bool(costs),
                "tool_count_total": sum(tool_counts) if tool_counts else None,
                "tokens": {
                    "input": token_input if token_seen else None,
                    "output": token_output if token_seen else None,
                    "cache_read": token_cache if token_seen else None,
                },
                "tokens_measured": token_seen,
            },
        }
        # Additive V7.7 requirements projection. This never changes milestone
        # pass/fail state or verified_progress; a full evaluation with real
        # command output is written by the runner at finalization.
        persisted_requirements = self._requirements_verification(run_id)
        contract = snapshot.get("requirements_contract")
        if persisted_requirements is not None:
            payload["requirements"] = persisted_requirements
        elif isinstance(contract, dict):
            try:
                try:
                    from tools import completion_engine
                except ModuleNotFoundError:
                    import completion_engine  # type: ignore
                payload["requirements"] = completion_engine.requirements_projection(
                    contract, state=payload, run_id=run_id)
            except Exception as exc:  # status must never fail on a projection
                payload["requirements"] = {
                    "status": "blocked", "aggregate_status": "blocked",
                    "fully_verified": False, "verified_completion": False,
                    "run_passed": False, "criteria": [],
                    "reasons": [f"requirements projection unavailable: {exc}"[:500]],
                    "projection": True,
                }
        else:
            payload["requirements"] = None
        return payload

    def status(self, run_id):
        return self.refresh(run_id)

    def next(self, run_id):
        state = self.status(run_id)
        milestones = state["milestones"]
        selected = next(
            (milestone for milestone in milestones
             if milestone["status"] in ("ready", "running", "verifying", "repairing")),
            None,
        )
        pending_tasks = []
        if selected is not None:
            pending_tasks = [task for task in selected["tasks"]
                             if task["state"] == "pending"]
        return {
            "run_id": state["run_id"],
            "status": state["status"],
            "next": selected["id"] if selected else None,
            "id": selected["id"] if selected else None,
            "next_milestone": selected["id"] if selected else None,
            "milestone_id": selected["id"] if selected else None,
            "milestone": selected,
            "milestone_detail": selected,
            "milestones": state["milestones"],
            "tasks": pending_tasks,
            "pending_tasks": [task["id"] for task in pending_tasks],
            "ready": [milestone["id"] for milestone in milestones
                      if milestone["status"] == "ready"],
            "active": [milestone["id"] for milestone in milestones
                       if milestone["status"] in ("running", "verifying", "repairing")],
            "failed": [milestone["id"] for milestone in milestones
                       if milestone["status"] == "failed"],
            "blocked": [milestone["id"] for milestone in milestones
                        if milestone["status"] == "blocked"],
            "repair_required": [milestone["id"] for milestone in milestones
                                if milestone["status"] == "failed"],
            "complete": state["complete"],
            "verified_progress": state["verified_progress"],
            "estimated_progress": None,
        }

    def begin_verification(self, run_id, milestone_id):
        run_id = self._require_run(run_id)
        milestone_id = _identifier(milestone_id, "milestone id")
        state = self.refresh(run_id)
        milestone = next(
            (item for item in state["milestones"] if item["id"] == milestone_id),
            None,
        )
        if milestone is None:
            raise TeamworkError("Unknown milestone")
        if milestone["status"] == "passed":
            return self._verification_response(state, milestone)
        if milestone["status"] in ("failed", "cancelled", "blocked", "planned"):
            raise TeamworkError("Milestone cannot enter verification from its current state")
        if any(task["state"] != "completed" for task in milestone["tasks"]):
            raise TeamworkError("Verification requires every milestone task to be completed")
        by_id = {item["id"]: item for item in state["milestones"]}
        for dependency in milestone["dependencies"]:
            if by_id.get(dependency, {}).get("status") != "passed":
                raise TeamworkError("Milestone dependencies must pass before verification")
        if milestone["status"] != "verifying":
            def op():
                self.conn.execute("""
                  UPDATE milestones
                  SET status='verifying', completed_at=NULL,
                      started_at=COALESCE(started_at,?)
                  WHERE run_id=? AND id=?
                """, (time.time(), run_id, milestone_id))
            self._transaction(op)
            state = self.refresh(run_id)
            milestone = next(
                (item for item in state["milestones"] if item["id"] == milestone_id),
                None,
            )
        return self._verification_response(state, milestone)

    @staticmethod
    def _verification_response(state, milestone):
        return {
            "run_id": state["run_id"],
            "milestone_id": milestone["id"],
            "status": milestone["status"],
            "milestone": milestone,
            "satisfied_roles": milestone["satisfied_roles"],
            "missing_roles": milestone["missing_roles"],
            "verified_progress": state["verified_progress"],
            "estimated_progress": None,
        }

    # -- repair ----------------------------------------------------------
    def reopen(self, run_id, task_id, reason, *, count_repair=True):
        run_id = self._require_run(run_id)
        task_id = _identifier(task_id, "task id")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise TeamworkError("Reopen requires a short actionable reason")
        state = self.refresh(run_id)
        milestone = next(
            (item for item in state["milestones"]
             if any(task["id"] == task_id for task in item["tasks"])),
            None,
        )
        if milestone is None:
            raise TeamworkError("Task does not belong to a milestone")
        # A failed verification can reopen an earlier build milestone. A
        # completed run may not be rewritten, and cancelled milestones are
        # final. The repair counter remains the hard bound.
        if milestone["status"] in TERMINAL_MILESTONE_STATUSES:
            raise TeamworkError("Cannot reopen a task in a terminal milestone")
        task = next(task for task in milestone["tasks"] if task["id"] == task_id)
        if task["state"] not in ("completed", "failed"):
            raise TeamworkError("Reopen requires a completed or failed task")
        if count_repair and milestone["repair_iterations"] >= milestone["max_repair"]:
            raise TeamworkError("Milestone repair limit reached")
        try:
            reopened = self.supervisor.reopen(run_id, task_id, reason)
        except SupervisorError as exc:
            raise TeamworkError(str(exc)) from exc

        def op():
            if count_repair:
                self.conn.execute("""
                  UPDATE milestones
                  SET status='repairing', completed_at=NULL,
                      repair_iterations=repair_iterations+1, last_error=?
                  WHERE run_id=? AND id=?
                """, (reason, run_id, milestone["id"]))
            else:
                self.conn.execute("""
                  UPDATE milestones SET status='repairing', completed_at=NULL,
                      last_error=? WHERE run_id=? AND id=?
                """, (reason, run_id, milestone["id"]))
        self._transaction(op)
        state = self.refresh(run_id)
        milestone = next(
            (item for item in state["milestones"] if item["id"] == milestone["id"]),
            None,
        )
        return {
            "run_id": run_id,
            "task_id": task_id,
            "milestone_id": milestone["id"] if milestone else None,
            "state": reopened["state"],
            "task_state": reopened["state"],
            "reopened": True,
            "attempts_used": reopened.get("attempts_used"),
            "status": milestone["status"] if milestone else None,
            "milestone_status": milestone["status"] if milestone else None,
            "repair_iterations": milestone["repair_iterations"] if milestone else None,
            "max_repair": milestone["max_repair"] if milestone else None,
            "reason": reason,
            "verified_progress": state["verified_progress"],
            "estimated_progress": None,
            "native_cancellation_required": False,
            "native_cancel_ack": False,
            "local_signal_only": True,
        }

    # -- handoffs --------------------------------------------------------
    def _handoff_dir(self):
        path = self.root / HANDOFF_DIR
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise TeamworkError("Handoff directory must not be a symlink")
        return path

    def _handoff_content(self, run_id, milestone):
        tasks = milestone["tasks"]
        completed = [task for task in tasks if task["state"] == "completed"]
        decisions = []
        changed_files = []
        commits = []
        verified_tests = []
        review_findings = []
        open_issues = []
        evidence_refs = []
        required_roles = set(milestone["require_roles"])
        seen = {key: set() for key in (
            "decisions", "changed_files", "commits", "verified_tests",
            "review_findings", "open_issues")}

        def add(bucket, values, key):
            if not isinstance(values, list):
                return
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    continue
                value = value[:1000]
                if value not in seen[key]:
                    seen[key].add(value)
                    bucket.append(value)

        for task in completed:
            result = task.get("result")
            result = result if isinstance(result, dict) else {}
            add(decisions, result.get("decisions"), "decisions")
            add(changed_files, result.get("changed_files"), "changed_files")
            add(commits, result.get("commits"), "commits")
            add(open_issues, result.get("open_issues"), "open_issues")
            tests = result.get("tests")
            if isinstance(tests, list):
                for test in tests:
                    if (isinstance(test, dict) and test.get("status") == "passed"
                            and isinstance(test.get("name"), str)
                            and test["name"] and test["name"] not in seen["verified_tests"]):
                        seen["verified_tests"].add(test["name"])
                        verified_tests.append(test["name"])
            if task["phase"] == "verify" or task["role"] in required_roles:
                add(review_findings, result.get("findings"), "review_findings")
            evidence_refs.append(f"supervisor://{run_id}/tasks/{task['id']}")
            result_digest = hashlib.sha256(
                _canonical(result).encode("utf-8")).hexdigest()[:12]
            evidence_refs.append(
                f"supervisor://{run_id}/tasks/{task['id']}#{result_digest}")

        milestone_ids = [item["id"] for item in self._load_milestones(run_id)]
        try:
            index = milestone_ids.index(milestone["id"])
        except ValueError:
            index = -1
        next_milestone = None
        next_tasks = []
        if index >= 0:
            statuses = {row["id"]: row["status"] for row in self._load_milestones(run_id)}
            for candidate in milestone_ids[index + 1:]:
                if statuses.get(candidate) != "passed":
                    next_milestone = candidate
                    break
        if next_milestone:
            snapshot = self._load_plan_snapshot(run_id) or {}
            for task in snapshot.get("tasks", []):
                if (isinstance(task, dict)
                        and task.get("milestone") == next_milestone):
                    next_tasks.append({
                        "id": task.get("id"),
                        "role": task.get("role"),
                        "objective": task.get("objective"),
                        "depends_on": task.get("depends_on", []),
                    })
        evidence_refs.append(f"supervisor://{run_id}/milestones/{milestone['id']}")
        return {
            "milestone_id": milestone["id"],
            "completed_tasks": [task["id"] for task in completed],
            "decisions": decisions,
            "changed_files": changed_files,
            "commits": commits,
            "verified_tests": verified_tests,
            "review_findings": review_findings,
            "open_issues": open_issues,
            "next_milestone": next_milestone,
            "next_tasks": next_tasks[:32],
            "context_summary": (
                f"{milestone['id']} passed with {len(completed)}/{len(tasks)} "
                f"completed task(s); {len(changed_files)} changed file(s); "
                f"{len(verified_tests)} verified test(s); "
                f"{len(review_findings)} review finding(s)."
            ),
            "evidence_refs": evidence_refs,
        }

    def _response_with_handoff(self, run_id, milestone_id, row, payload, unchanged):
        return {
            **payload,
            "run_id": run_id,
            "milestone_id": milestone_id,
            "version": row["version"],
            "path": row["path"],
            "relative_path": str(Path(row["path"]).relative_to(self.root)),
            "sha256": row["sha256"],
            "unchanged": unchanged,
            "handoff": payload,
        }

    def record_handoff(self, run_id, milestone_id):
        run_id = self._require_run(run_id)
        milestone_id = _identifier(milestone_id, "milestone id")
        state = self.refresh(run_id)
        milestone = next(
            (item for item in state["milestones"] if item["id"] == milestone_id),
            None,
        )
        if milestone is None:
            raise TeamworkError("Unknown milestone")
        if milestone["status"] != "passed":
            raise TeamworkError("Handoff requires a passed milestone")
        payload = self._handoff_content(run_id, milestone)
        data = _handoff_bytes(payload)
        digest = hashlib.sha256(data).hexdigest()
        latest = self.conn.execute("""
          SELECT * FROM teamwork_handoffs
          WHERE run_id=? AND milestone_id=? ORDER BY version DESC LIMIT 1
        """, (run_id, milestone_id)).fetchone()
        if latest is not None and latest["sha256"] == digest:
            path = Path(latest["path"])
            if path.is_file() and not path.is_symlink():
                try:
                    current = path.read_bytes()
                except OSError as exc:
                    raise TeamworkError("Existing handoff is unreadable") from exc
                if current == data:
                    if milestone.get("handoff_path") != latest["path"]:
                        def update():
                            self.conn.execute(
                                "UPDATE milestones SET handoff_path=? WHERE run_id=? AND id=?",
                                (latest["path"], run_id, milestone_id))
                        self._transaction(update)
                    return self._response_with_handoff(
                        run_id, milestone_id, latest, payload, True)

        version = (latest["version"] if latest is not None else 0) + 1
        directory = self._handoff_dir()
        path = directory / f"{run_id}-{milestone_id}.v{version}.json"
        while path.exists():
            version += 1
            path = directory / f"{run_id}-{milestone_id}.v{version}.json"
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.tmp-", dir=str(directory))
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        try:
            def insert():
                self.conn.execute("""
                  INSERT INTO teamwork_handoffs
                    (run_id,milestone_id,version,path,sha256,created_at)
                  VALUES (?,?,?,?,?,?)
                """, (run_id, milestone_id, version, str(path), digest, time.time()))
                self.conn.execute(
                    "UPDATE milestones SET handoff_path=? WHERE run_id=? AND id=?",
                    (str(path), run_id, milestone_id))
            self._transaction(insert)
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        row = self.conn.execute("""
          SELECT * FROM teamwork_handoffs
          WHERE run_id=? AND milestone_id=? AND version=?
        """, (run_id, milestone_id, version)).fetchone()
        return self._response_with_handoff(run_id, milestone_id, row, payload, False)

    def handoff(self, run_id, milestone_id):
        run_id = self._require_run(run_id)
        milestone_id = _identifier(milestone_id, "milestone id")
        row = self.conn.execute("""
          SELECT * FROM teamwork_handoffs
          WHERE run_id=? AND milestone_id=? ORDER BY version DESC LIMIT 1
        """, (run_id, milestone_id)).fetchone()
        if row is None:
            raise TeamworkError("No handoff recorded for this milestone")
        path = Path(row["path"])
        if path.is_symlink():
            raise TeamworkError("Handoff path must not be a symlink")
        directory = (self.root / HANDOFF_DIR).resolve()
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise TeamworkError("Handoff file is missing") from exc
        if resolved.parent != directory:
            raise TeamworkError("Handoff path is outside the handoff directory")
        if resolved.stat().st_size > 1_048_576:
            raise TeamworkError("Handoff file is too large")
        try:
            data = resolved.read_bytes()
            payload = json.loads(data.decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise TeamworkError("Handoff file is invalid") from exc
        required = [field for field in HANDOFF_FIELDS if field != "next_tasks"]
        if not isinstance(payload, dict) or any(
                field not in payload for field in required):
            raise TeamworkError("Handoff file is missing required fields")
        # V7.4 handoffs predate next_tasks; preserve digest verification on the
        # original bytes and add the optional field for readers.
        payload.setdefault("next_tasks", [])
        digest = hashlib.sha256(data).hexdigest()
        if digest != row["sha256"]:
            raise TeamworkError("Handoff digest does not match the recorded content")
        return self._response_with_handoff(
            run_id, milestone_id, row, payload, True)

    # -- managed sessions ------------------------------------------------
    def _session_dict(self, row):
        value = dict(row)
        tokens = value.get("tokens")
        if tokens is not None:
            try:
                value["tokens"] = json.loads(tokens)
            except (TypeError, ValueError):
                pass
        return value

    def record_managed_session(self, run_id, task_id, attempt=1, *, provider,
                               session_id, status="running", started_at=None,
                               finished_at=None, events_path=None, model=None,
                               cost=None, tokens=None, error=None,
                               cancel_state=None, resume_of=None, tool_count=0,
                               pid=None, pid_start_token=None, pid_uid=None):
        run_id = self._require_run(run_id)
        task_id = _identifier(task_id, "task id")
        if type(attempt) is not int or not 1 <= attempt <= 1000:
            raise TeamworkError("attempt must be 1..1000")
        provider = _short_string(provider, "provider", 80)
        session_id = _short_string(session_id, "session_id", 256)
        if status not in SESSION_STATUSES:
            raise TeamworkError("Unknown managed session status")
        started_at = _optional_number(started_at, "started_at")
        finished_at = _optional_number(finished_at, "finished_at")
        cost = _optional_number(cost, "cost")
        if type(tool_count) is not int or not 0 <= tool_count <= 100000:
            raise TeamworkError("tool_count must be a bounded integer")
        events_path = _optional_string(events_path, "events_path", 500)
        model = _optional_string(model, "model", 200)
        if pid is not None and (type(pid) is not int or not 1 <= pid <= 2**31 - 1):
            raise TeamworkError("pid must be a positive bounded integer")
        pid_start_token = _optional_string(pid_start_token, "pid_start_token", 200)
        if pid_uid is not None and (type(pid_uid) is not int or pid_uid < 0):
            raise TeamworkError("pid_uid must be a non-negative integer")
        cancel_state = _optional_string(cancel_state, "cancel_state", 80)
        if error is not None and (not isinstance(error, str) or len(error) > 2000):
            raise TeamworkError("error must be at most 2000 characters")
        if resume_of is not None:
            if type(resume_of) is int and resume_of >= 1:
                pass
            elif isinstance(resume_of, str) and 1 <= len(resume_of) <= 256:
                pass
            else:
                raise TeamworkError(
                    "resume_of must be a positive attempt number or session id")
        state = self.supervisor.status(run_id)
        if task_id not in {task["id"] for task in state["tasks"]}:
            raise TeamworkError("Unknown task for managed session")
        now = time.time()
        if status in ("completed", "failed", "cancelled") and finished_at is None:
            finished_at = now
        tokens_value = None
        if tokens is not None:
            if isinstance(tokens, bool) or not isinstance(tokens, (int, float, dict, list)):
                raise TeamworkError("tokens must be a number, object or list")
            tokens_value = json.dumps(tokens, sort_keys=True, separators=(",", ":"))
        existing = self.conn.execute("""
          SELECT * FROM managed_sessions WHERE run_id=? AND task_id=? AND attempt=?
        """, (run_id, task_id, attempt)).fetchone()
        if existing is not None and started_at is None:
            started_at = existing["started_at"]
        if existing is not None:
            if pid is None:
                pid = existing["pid"]
            if pid_start_token is None:
                pid_start_token = existing["pid_start_token"]
            if pid_uid is None:
                pid_uid = existing["pid_uid"]
        if started_at is None:
            started_at = now

        def op():
            if existing is None:
                self.conn.execute("""
                  INSERT INTO managed_sessions
                    (run_id,task_id,attempt,provider,session_id,status,started_at,
                     updated_at,finished_at,events_path,model,cost,tokens,error,
                     cancel_state,resume_of,tool_count,pid,pid_start_token,pid_uid)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (run_id, task_id, attempt, provider, session_id, status,
                      started_at, now, finished_at, events_path, model, cost,
                      tokens_value, error, cancel_state, resume_of, tool_count,
                      pid, pid_start_token, pid_uid))
            else:
                self.conn.execute("""
                  UPDATE managed_sessions
                  SET provider=?,session_id=?,status=?,started_at=?,updated_at=?,
                      finished_at=?,events_path=?,model=?,cost=?,tokens=?,error=?,
                      cancel_state=?,resume_of=?,tool_count=?,pid=?,
                      pid_start_token=?,pid_uid=?
                  WHERE run_id=? AND task_id=? AND attempt=?
                """, (provider, session_id, status, started_at, now, finished_at,
                      events_path, model, cost, tokens_value, error, cancel_state,
                      resume_of, tool_count, pid, pid_start_token, pid_uid,
                      run_id, task_id, attempt))
        self._transaction(op)
        row = self.conn.execute("""
          SELECT * FROM managed_sessions WHERE run_id=? AND task_id=? AND attempt=?
        """, (run_id, task_id, attempt)).fetchone()
        return self._session_dict(row)

    def _managed_sessions(self, run_id):
        rows = self.conn.execute("""
          SELECT * FROM managed_sessions WHERE run_id=?
          ORDER BY task_id, attempt
        """, (run_id,))
        return [self._session_dict(row) for row in rows]

    def managed_sessions(self, run_id):
        run_id = self._require_run(run_id)
        return self._managed_sessions(run_id)

    # -- requirements verification --------------------------------------
    def record_requirements(self, run_id, payload):
        """Persist the runner's final completion evaluation for status readers."""
        run_id = self._require_run(run_id)
        if not isinstance(payload, dict):
            raise TeamworkError("requirements verification must be an object")
        encoded = _canonical(payload)
        if len(encoded.encode("utf-8")) > 1_048_576:
            raise TeamworkError("requirements verification is too large")

        def op():
            self.conn.execute("""
              INSERT INTO requirements_verification (run_id,payload,updated_at)
              VALUES (?,?,?)
              ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload,
                updated_at=excluded.updated_at
            """, (run_id, encoded, time.time()))
        self._transaction(op)
        return json.loads(encoded)

    def _requirements_verification(self, run_id):
        row = self.conn.execute(
            "SELECT payload FROM requirements_verification WHERE run_id=?",
            (run_id,)).fetchone()
        if row is None:
            return None
        value = _parse_json(row["payload"], None)
        return value if isinstance(value, dict) else None

    # Friendly aliases for callers that use shorter names.
    record_session = record_managed_session
    sessions = managed_sessions
