#!/usr/bin/env python3
"""Single-chat bridge for V7.4.1 Teamwork.

User text is never interpolated into a shell command. ``start`` accepts a
request from a validated UTF-8 file inside the project's private request
directory, from stdin, or (for compatibility/tests) from a structured argv
value. The bridge writes an adaptive plan and launches the managed runner with
an argv list; the runner reads the request from the plan, not from a shell.

A project-level advisory lock serializes start/resume/cancel decisions. Owner
records store PID + process start token + UID, so a recycled PID is never
treated as the same runner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

try:
    from tools import assumption_guard
    from tools.project_identity import ensure_project_identity
    from tools.project_memory import MemoryError, ProjectMemory
    from tools.repo_profile import RepoProfileError, profile_repository
    from tools.requirements_contract import (
        ContractError as RequirementsContractError,
        build_from_interview,
        load_contract,
        load_current,
        contract_blocking_requirements,
        validate_contract,
        validate_contract_state,
    )
    from tools.run_owner import (
        OwnershipError,
        atomic_write_json,
        identity_matches,
        lease_held,
        owner_alive,
        owner_record,
        project_lock,
        project_writer_held,
        read_json,
        read_process_identity,
        read_recovery_state,
        teamwork_dir,
        write_recovery_state,
    )
    from tools.teamwork import TeamworkEngine, TeamworkError
    from tools.teamwork_planner import TeamworkPlannerError, plan_request
except ModuleNotFoundError:  # python tools/teamwork_chat.py
    import assumption_guard  # type: ignore
    from project_identity import ensure_project_identity  # type: ignore
    from project_memory import MemoryError, ProjectMemory  # type: ignore
    from repo_profile import RepoProfileError, profile_repository  # type: ignore
    from requirements_contract import (  # type: ignore
        ContractError as RequirementsContractError,
        build_from_interview,
        load_contract,
        load_current,
        contract_blocking_requirements,
        validate_contract,
        validate_contract_state,
    )
    from run_owner import (  # type: ignore
        OwnershipError,
        atomic_write_json,
        identity_matches,
        lease_held,
        owner_alive,
        owner_record,
        project_lock,
        project_writer_held,
        read_json,
        read_process_identity,
        read_recovery_state,
        teamwork_dir,
        write_recovery_state,
    )
    from teamwork import TeamworkEngine, TeamworkError  # type: ignore
    from teamwork_planner import TeamworkPlannerError, plan_request  # type: ignore

STATE_DIR = teamwork_dir(".")
ACTIVE = "active.json"
REQUEST_DIR = "requests"
MAX_REQUEST = 20000
MAX_REQUEST_BYTES = MAX_REQUEST * 4
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
DEFAULT_MODEL = os.environ.get("AO_TEAMWORK_MODEL",
                               "opencode-go/deepseek-v4.1-flash")
TERMINAL_RUNS = ("passed", "failed", "cancelled")
HEARTBEAT_STALE_SECONDS = 300.0
RECOVERY_STATES = {
    "active", "runner_disconnected", "reconciling", "orphaned",
    "retry_ready", "resuming", "verifying", "recovered",
    "recovery_failed", "cancelled",
}


class ChatError(ValueError):
    pass


def _root(value: str) -> Path:
    root = Path(value).resolve()
    if not root.is_dir():
        raise ChatError("root directory does not exist")
    return root


def _state_path(root: Path, name: str = ACTIVE) -> Path:
    return teamwork_dir(root) / name


def _write_json(path: Path, value: dict) -> None:
    atomic_write_json(path, value)


def _active(root: Path) -> dict:
    value = read_json(_state_path(root), {})
    return value if isinstance(value, dict) else {}


def _engine_status(root: Path, run_id: str) -> dict:
    engine = TeamworkEngine(root)
    try:
        return engine.status(run_id)
    finally:
        engine.close()


def _require_repository(root: Path) -> None:
    marker = root / ".git"
    if not marker.exists():
        raise ChatError("root is not a git repository; Teamwork needs isolated worktrees")


def _safe_run_id(value: str) -> str:
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise ChatError(
            "run id must be 1..80 chars of letters, digits, dot, dash or underscore"
        )
    if ".." in value:
        raise ChatError("run id must not contain path traversal")
    return value


def _plan_path(root: Path, run_id: str) -> Path:
    return root / ".auto-orchestrator" / "plans" / f"{_safe_run_id(run_id)}.json"


def _validate_plan_path(root: Path, value, run_id: str) -> Path:
    path = Path(value) if value else _plan_path(root, run_id)
    try:
        resolved = path.resolve()
    except (OSError, ValueError) as exc:
        raise ChatError("plan path is invalid") from exc
    expected = _plan_path(root, run_id).resolve()
    if resolved != expected:
        raise ChatError("plan path is outside the run's plan directory")
    if not resolved.is_file() or resolved.is_symlink():
        raise ChatError("plan artifact is missing or is a symlink")
    return resolved


def _request_text(root: Path, args) -> str:
    sources = [
        getattr(args, "request", None) is not None,
        getattr(args, "request_file", None) is not None,
        bool(getattr(args, "request_stdin", False)),
    ]
    if sum(sources) != 1:
        raise ChatError("provide exactly one of --request, --request-file or --request-stdin")
    if getattr(args, "request", None) is not None:
        text = args.request
    elif getattr(args, "request_file", None) is not None:
        text = _read_request_file(root, args.request_file)
    else:
        text = sys.stdin.read(MAX_REQUEST + 1)
    if not isinstance(text, str):
        raise ChatError("request must be text")
    if "\x00" in text:
        raise ChatError("request must not contain NUL")
    if len(text) > MAX_REQUEST:
        raise ChatError(f"request exceeds {MAX_REQUEST} characters")
    if not text.strip():
        raise ChatError("request must not be empty")
    return text


def _read_request_file(root: Path, value: str) -> str:
    try:
        path = Path(value)
    except (TypeError, ValueError) as exc:
        raise ChatError("request file path is invalid") from exc
    if path.is_symlink():
        raise ChatError("request file must not be a symlink")
    try:
        resolved = path.resolve()
    except (OSError, ValueError) as exc:
        raise ChatError("request file path is invalid") from exc
    allowed = (teamwork_dir(root) / REQUEST_DIR).resolve()
    if resolved != allowed and allowed not in resolved.parents:
        raise ChatError("request file must be inside .auto-orchestrator/teamwork/requests")
    try:
        metadata = resolved.stat()
    except OSError as exc:
        raise ChatError("request file is missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ChatError("request file must be a regular file")
    if metadata.st_size > MAX_REQUEST_BYTES:
        raise ChatError("request file is too large")
    try:
        raw = resolved.read_bytes()
        if os.name == "posix":
            try:
                os.chmod(resolved, 0o600)
            except OSError:
                pass
    except OSError as exc:
        raise ChatError("request file could not be read") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChatError("request file must be valid UTF-8") from exc


def _model(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 200 or "\x00" in value or "\n" in value:
        raise ChatError("model must be a short single-line string")
    return value


def _legacy_pid_alive(active: dict) -> bool:
    pid = active.get("pid")
    return read_process_identity(pid) is not None


def _owner_process_alive(active: dict) -> bool:
    owner = active.get("owner")
    if isinstance(owner, dict):
        return owner_alive(owner)
    # Compatibility with pre-owner V7.4 records. This is reported as
    # unverified and never used to send a signal.
    return _legacy_pid_alive(active)


def _active_connected(active: dict) -> bool:
    if not _owner_process_alive(active):
        return False
    age = _heartbeat_age(active)
    return age is None or age <= HEARTBEAT_STALE_SECONDS


def _owner_verified(active: dict) -> bool:
    if not identity_matches(active.get("owner")):
        return False
    age = _heartbeat_age(active)
    return age is None or age <= HEARTBEAT_STALE_SECONDS


def _heartbeat_age(active: dict) -> float | None:
    value = active.get("heartbeat_at")
    if isinstance(value, (int, float)) and value > 0:
        return max(0.0, round(time.time() - float(value), 3))
    return None


def _recovery_view(root: Path, active: dict, state: dict | None,
                   run_id: str, connected: bool | None = None) -> dict:
    active = active if active.get("run_id") == run_id else {}
    stored = read_recovery_state(root, run_id) or {}
    stored_state = stored.get("state")
    transitions = {"reconciling", "orphaned", "retry_ready", "resuming", "verifying"}
    reported = None
    if state is not None:
        run_status = state.get("status")
        if run_status == "passed":
            reported = (stored_state if stored_state in transitions | {"recovered"}
                        else None)
        elif run_status == "failed":
            reported = "recovery_failed" if stored_state in transitions else None
        elif run_status == "cancelled":
            reported = "cancelled"
    connected = _active_connected(active) if connected is None else connected
    if state is None or state.get("status") not in ("passed", "failed", "cancelled"):
        if not connected:
            reported = "runner_disconnected"
        elif reported is None:
            reported = stored_state if stored_state in RECOVERY_STATES else "active"
    if reported is not None and reported not in RECOVERY_STATES:
        reported = "active" if connected else "runner_disconnected"
    return {
        "state": reported,
        "owner_connected": connected,
        "owner_verified": _owner_verified(active),
        "heartbeat_age_seconds": _heartbeat_age(active),
        "last_transition": stored,
        "local_signal_only": True,
        "native_cancel_ack": False,
    }


def _spawn(root: Path, log: Path, plan_path: Path, *, recover: bool,
           owner_token: str, model: str | None, max_seconds: float):
    cli = Path(__file__).with_name("teamwork_cli.py")
    command = [sys.executable, str(cli), "--root", str(root), "run",
               "--plan", str(plan_path), "--max-seconds", str(max_seconds),
               "--owner-token", owner_token]
    if recover:
        command.append("--recover")
    if model:
        command.extend(["--model", model])
    log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with log.open("ab") as handle:
        return subprocess.Popen(
            command, cwd=root, stdin=subprocess.DEVNULL,
            stdout=handle, stderr=subprocess.STDOUT,
            start_new_session=True if os.name != "nt" else False,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                           if os.name == "nt" else 0),
        )


def _new_active(root: Path, run_id: str, mode: str, plan_path: Path,
                log: Path, model: str | None, token: str, process,
                *, recovery_state: str) -> dict:
    owner = owner_record(process.pid)
    if owner is None:
        try:
            process.terminate()
        except OSError:
            pass
        raise ChatError("runner process ownership could not be verified")
    now = time.time()
    return {
        "version": 2,
        "run_id": run_id,
        "mode": mode,
        "plan": str(plan_path),
        "log": str(log),
        "model": model,
        "owner": owner,
        "pid": owner["pid"],
        "runner_token": token,
        "started_at": now,
        "heartbeat_at": now,
        "recovery_state": recovery_state,
    }


def request_path(args) -> dict:
    """Return a fresh, shell-safe request path inside the project."""
    root = _root(args.root)
    name = f"request-{os.urandom(8).hex()}.md"
    directory = teamwork_dir(root) / REQUEST_DIR
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    relative = (Path(".auto-orchestrator") / "teamwork" / REQUEST_DIR / name)
    return {
        "filename": name,
        "relative_path": relative.as_posix(),
        "absolute_path": str(directory / name),
        "note": "Write the exact request text to this file; use relative_path in the bridge command.",
    }


def _contract_ref(contract: dict) -> dict:
    return {
        "contract_id": contract["contract_id"],
        "contract_version": contract["contract_version"],
        "sha256": contract["sha256"],
        "interview_id": contract.get("interview_id"),
        "source": contract.get("source"),
        "status": contract.get("status"),
    }


def _guard(root: Path, request: str, args) -> dict:
    try:
        return assumption_guard.evaluate(
            root, request,
            platform=getattr(args, "origin_platform", "unknown") or "unknown",
            mode=getattr(args, "grill_mode", "auto") or "auto",
            interview_id=getattr(args, "interview_id", None))
    except assumption_guard.AssumptionGuardError as exc:
        raise ChatError(str(exc)) from exc


def _start_contract(root: Path, interview_id: str) -> dict:
    try:
        current = load_current(root)
        if (current is not None
                and current.get("interview_id") == interview_id
                and current.get("status") == "confirmed"):
            return current
        try:
            return build_from_interview(root, interview_id)
        except RequirementsContractError:
            return assumption_guard.build_requirements_contract(
                root, interview_id)
    except (RequirementsContractError, KeyError, TypeError, ValueError) as exc:
        raise ChatError(f"requirements contract is invalid: {exc}") from exc


def _approved_interview_for_request(root, request):
    """Find an approved interview whose request hash matches this exact text."""
    digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
    try:
        with ProjectMemory(root) as memory:
            row = memory.conn.execute(
                "SELECT interview_id FROM interviews WHERE project_id=? AND "
                "request_hash=? AND status='approved' ORDER BY updated_at DESC "
                "LIMIT 1", (memory.project_id, digest)).fetchone()
            return row["interview_id"] if row is not None else None
    except (MemoryError, OSError, ValueError):
        return None


def _plan_with_contract(request: str, *, contract: dict | None, **kwargs) -> dict:
    """Pass the contract to the planner when it supports it, then attach a ref."""
    import inspect

    parameters = {}
    try:
        parameters = inspect.signature(plan_request).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts = ("requirements_contract" in parameters
               or any(parameter.kind is inspect.Parameter.VAR_KEYWORD
                      for parameter in parameters.values()))
    if contract is not None and accepts:
        kwargs["requirements_contract"] = contract
    plan = plan_request(request, **kwargs)
    attached = plan.get("requirements_contract")
    if contract is not None:
        if not (isinstance(attached, dict)
                and "confirmed_decisions" in attached):
            plan["requirements_contract"] = _contract_ref(contract)
    else:
        plan.setdefault("requirements_contract", None)
    return plan


def _plan_contract(root: Path, plan: dict) -> dict | None:
    """Load and verify a plan's contract; reject stale or invalid contracts."""
    reference = plan.get("requirements_contract")
    if not reference:
        if plan.get("request"):
            receipt = plan.get("assumption_guard")
            if not receipt:
                raise ChatError(
                    "legacy plan has no Assumption Guard receipt; start a new run")
            try:
                assumption_guard.validate_receipt(
                    root, plan["request"], receipt)
            except assumption_guard.AssumptionGuardError as exc:
                raise ChatError(
                    f"plan Assumption Guard receipt is stale: {exc}") from exc
        return None
    if not isinstance(reference, dict):
        raise ChatError("plan requirements contract reference is invalid")
    try:
        if "confirmed_decisions" in reference:
            contract = validate_contract(reference)
        else:
            contract = load_contract(
                root, str(reference.get("contract_id")),
                int(reference.get("contract_version")))
        contract = validate_contract(contract)
    except (RequirementsContractError, KeyError, TypeError, ValueError) as exc:
        raise ChatError(
            "plan requirements contract is missing, stale or invalid") from exc
    if contract.get("status") != "confirmed":
        raise ChatError("plan requirements contract is stale or invalid")
    if reference.get("status") not in (None, "confirmed"):
        raise ChatError("plan requirements contract reference is stale")
    if contract.get("blockers"):
        raise ChatError("plan requirements contract has unresolved blockers")
    expected = reference.get("sha256")
    if expected is not None and contract.get("sha256") != expected:
        raise ChatError("plan requirements contract digest does not match")
    if (reference.get("interview_id") is not None
            and contract.get("interview_id") != reference.get("interview_id")):
        raise ChatError("plan requirements contract belongs to another interview")
    identity = ensure_project_identity(root)
    if contract.get("project_id") != identity["project_id"]:
        raise ChatError("plan requirements contract belongs to another project")
    try:
        current = load_current(root)
    except RequirementsContractError as exc:
        raise ChatError(
            "plan requirements contract pointer is invalid or stale") from exc
    if current is not None and current.get("contract_id") == contract.get("contract_id"):
        # A newer version of *this* contract supersedes the plan. A contract
        # for a different interview/feature does not invalidate this run.
        if current.get("contract_version") != contract.get("contract_version"):
            raise ChatError("plan requirements contract has been superseded")
    try:
        with ProjectMemory(root) as memory:
            if contract.get("interview_id"):
                interview = memory.conn.execute(
                    "SELECT status FROM interviews WHERE project_id=? "
                    "AND interview_id=?",
                    (memory.project_id, contract["interview_id"])).fetchone()
                if interview is None or interview["status"] != "approved":
                    raise ChatError(
                        "plan requirements contract is stale: its interview "
                        "is no longer approved")
            decisions = {item["decision_id"]: item
                         for item in memory.list_decisions(limit=500)}
            for decision in contract.get("confirmed_decisions", []):
                live = decisions.get(decision["decision_ref"])
                if (live is None or live.get("status") != "confirmed"
                        or live.get("decision") != decision.get("decision")):
                    raise ChatError(
                        "plan requirements contract is stale: a confirmed "
                        "decision changed or was superseded")
            if contract_blocking_requirements(memory, contract):
                raise ChatError(
                    "plan requirements contract is stale: unresolved blocking "
                    "requirements exist")
    except ChatError:
        raise
    except (MemoryError, ValueError) as exc:
        raise ChatError("plan requirements contract could not be verified") from exc
    return contract


def start(args) -> dict:
    root = _root(args.root)
    _require_repository(root)
    mode = args.mode
    if mode not in ("auto", "fast", "balanced", "rigorous"):
        raise ChatError("mode must be auto, fast, balanced or rigorous")
    model = _model(getattr(args, "model", None))
    run_id = _safe_run_id(args.run_id or f"team-{int(time.time())}-{os.getpid()}")
    token = os.urandom(16).hex()
    with project_lock(root, timeout=10):
        active = _active(root)
        if project_writer_held(root):
            raise ChatError(
                "another Teamwork writer is active for this project; "
                "wait for it to finish or hand off the project"
            )
        old_run_id = active.get("run_id")
        if _owner_process_alive(active) or (
                isinstance(old_run_id, str) and lease_held(root, old_run_id)):
            raise ChatError(
                f"active teamwork run {old_run_id} still owns a live runner; "
                "use status/resume instead of starting a duplicate"
            )
        if active.get("run_id"):
            try:
                old_state = _engine_status(root, active["run_id"])
            except TeamworkError:
                old_state = None
            if old_state is not None and old_state.get("status") not in TERMINAL_RUNS:
                raise ChatError(
                    f"unfinished teamwork run {active['run_id']} exists; "
                    "use resume instead of starting a duplicate"
                )
        provider = getattr(args, "provider", None)
        codex_sandbox = getattr(args, "codex_sandbox", "read-only")
        platform = getattr(args, "origin_platform", "unknown") or "unknown"
        request = _request_text(root, args)
        guard = _guard(root, request, args)
        if guard["status"] != "allowed":
            question = guard.get("question") or {}
            reasons = "; ".join(guard.get("reasons") or [])
            detail = question.get("question") or reasons or "critical ambiguity"
            error = ChatError(
                "assumption guard requires an approved Grill-Me interview "
                f"before a plan can be written: {detail} "
                f"(interview_id={guard.get('interview_id')})")
            error.guard = guard
            error.interview = guard.get("interview")
            error.question = guard.get("question")
            error.reasons = guard.get("reasons") or []
            error.interview_id = guard.get("interview_id")
            raise error
        received_receipt = getattr(args, "guard_receipt", None)
        receipt = (received_receipt
                   if isinstance(received_receipt, dict)
                   and received_receipt.get("request_hash") == guard["request_hash"]
                   and received_receipt.get("status") == "allowed"
                   else guard["receipt"])
        if codex_sandbox not in ("read-only", "workspace-write"):
            raise ChatError("codex_sandbox must be read-only or workspace-write")
        if provider is None:
            if platform in ("codex", "cursor", "antigravity"):
                raise ChatError(
                    f"no managed execution adapter is selected for {platform}; "
                    "use the platform's native agents, or pass --provider "
                    "opencode explicitly to opt in to the OpenCode bridge"
                )
            provider = "opencode"
        if provider not in ("opencode", "codex"):
            raise ChatError("provider must be opencode or codex")
        plan_path = _plan_path(root, run_id)
        if plan_path.exists():
            raise ChatError("run id already exists; choose another --run-id")
        interview_id = getattr(args, "interview_id", None)
        if not interview_id:
            interview_id = _approved_interview_for_request(root, request)
        contract = _start_contract(root, interview_id) if interview_id else None
        identity = ensure_project_identity(root)
        profile = profile_repository(root)
        try:
            plan = _plan_with_contract(
                request, contract=contract, run_id=run_id, model=model,
                mode=mode, repo_profile=profile,
                project_id=identity["project_id"], provider=provider,
                codex_sandbox=codex_sandbox)
        except (TeamworkPlannerError, ValueError) as exc:
            raise ChatError(str(exc)) from exc
        plan["origin_platform"] = platform
        plan["execution_provider"] = provider
        plan["assumption_guard"] = receipt
        plan["guard_receipt"] = receipt
        plan_only = getattr(args, "grill_mode", "auto") == "plan-only"
        if plan_only:
            plan["plan_only"] = True
        plan_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_json(plan_path, plan)
        engine = TeamworkEngine(root)
        try:
            engine.init(plan)
        finally:
            engine.close()
        if plan_only:
            return {
                "status": "planned", "run_id": run_id,
                "mode": plan["work_mode"], "plan": str(plan_path),
                "plan_only": True, "execution_provider": provider,
                "platform": platform,
                "assumption_guard": receipt,
                "requirements_contract": plan.get("requirements_contract"),
                "note": "plan-only produced a verified plan but started no agent code.",
            }
        log = _state_path(root, f"{run_id}.log")
        process = _spawn(root, log, plan_path, recover=False,
                         owner_token=token, model=model,
                         max_seconds=float(args.max_seconds))
        active = _new_active(root, run_id, plan["work_mode"], plan_path,
                             log, model, token, process,
                             recovery_state="active")
        active["origin_platform"] = plan["origin_platform"]
        _write_json(_state_path(root), active)
        write_recovery_state(root, run_id, "active",
                             mode=plan["work_mode"],
                             note="runner launched; request is stored only in the plan")
    return {
        "status": "started", "run_id": run_id, "mode": plan["work_mode"],
        "pid": active["owner"]["pid"],
        "owner_verified": True,
        "plan": str(plan_path), "log": str(log),
        "risk_assessment": plan.get("risk_assessment"),
        "child_sessions": f"separate managed {provider} sessions",
        "execution_provider": provider,
        "assumption_guard": receipt,
        "requirements_contract": plan.get("requirements_contract"),
        "contract_status": ("confirmed" if plan.get("requirements_contract")
                            else "absent"),
        "note": "Use status/agents/milestones; cancel is a local signal only.",
    }


def status(args) -> dict:
    root = _root(args.root)
    active = _active(root)
    run_id = args.run_id or active.get("run_id")
    if not run_id:
        raise ChatError("no active teamwork run")
    _safe_run_id(run_id)
    try:
        state = _engine_status(root, run_id)
    except TeamworkError as exc:
        raise ChatError(str(exc)) from exc
    selected_active = active if active.get("run_id") == run_id else {}
    runner_lease_held = lease_held(root, run_id)
    connected = _active_connected(selected_active) or runner_lease_held
    sessions = state.get("managed_sessions", [])
    running = [item for item in sessions
               if item.get("status") in ("starting", "running")]
    orphaned = [] if connected else running
    active_running = running if connected else []
    role_by_task = {}
    for milestone in state.get("milestones", []):
        for task in milestone.get("tasks", []):
            role_by_task[task.get("id")] = task.get("role")
    current = next((item for item in state.get("milestones", [])
                    if item.get("status") in ("running", "verifying", "repairing")),
                   None)
    recovery = _recovery_view(root, selected_active, state, run_id,
                              connected=connected)
    reported_status = state.get("status")
    cancel_path = _state_path(root, f"cancel-{run_id}.json")
    pause_path = _state_path(root, f"pause-{run_id}.json")
    pause_requested = pause_path.exists()
    paused = (pause_requested and connected
              and reported_status not in ("passed", "failed", "cancelled"))
    if cancel_path.exists() and reported_status != "passed" and not connected:
        reported_status = "cancelled"
        recovery["state"] = "cancelled"
        recovery["last_transition"] = recovery.get("last_transition") or {}
        recovery["cancelled"] = True
    recovery_state = recovery["state"]
    return {
        "run_id": run_id,
        "mode": state.get("work_mode", active.get("mode")),
        "status": "paused" if paused else reported_status,
        "engine_status": state.get("status"),
        "paused": paused,
        "pause_requested": pause_requested,
        "verified_progress": state.get("verified_progress"),
        "estimated_progress": None,
        "current_milestone": current.get("id") if current else None,
        "milestones": [{k: item.get(k) for k in
                        ("id", "objective", "status", "repair_iterations",
                         "max_repair", "satisfied_roles", "missing_roles")}
                       for item in state.get("milestones", [])],
        "active_managed_sessions": len(active_running),
        "orphaned_managed_sessions": len(orphaned),
        "active_tasks": [{"task_id": item.get("task_id"),
                          "role": role_by_task.get(item.get("task_id")),
                          "status": item.get("status"),
                          "session_hash": hashlib.sha256(
                              str(item.get("session_id", "")).encode()).hexdigest()[:12]}
                         for item in active_running],
        "orphaned_tasks": [{"task_id": item.get("task_id"),
                            "status": "orphaned" if not connected else item.get("status"),
                            "session_hash": hashlib.sha256(
                                str(item.get("session_id", "")).encode()).hexdigest()[:12]}
                           for item in orphaned],
        "owner_connected": connected,
        "owner_process_alive": _owner_process_alive(selected_active) or runner_lease_held,
        "runner_lease_held": runner_lease_held,
        "owner_verified": _owner_verified(selected_active) if connected else False,
        "heartbeat_age_seconds": _heartbeat_age(selected_active),
        "recovery": recovery,
        "recovery_state": recovery_state,
        "pid_alive": _legacy_pid_alive(selected_active),
        "log": selected_active.get("log"),
    }


def agents(args) -> dict:
    root = _root(args.root)
    active = _active(root)
    run_id = args.run_id or active.get("run_id")
    if not run_id:
        raise ChatError("no active teamwork run")
    _safe_run_id(run_id)
    state = _engine_status(root, run_id)
    selected_active = active if active.get("run_id") == run_id else {}
    runner_lease_held = lease_held(root, run_id)
    connected = _active_connected(selected_active) or runner_lease_held
    latest = {}
    for row in state.get("managed_sessions", []):
        key = (row.get("task_id"), row.get("attempt"))
        if key not in latest or (row.get("updated_at") or 0) > (latest[key].get("updated_at") or 0):
            latest[key] = row
    agents = []
    for row in latest.values():
        status_value = row.get("status")
        if not connected and status_value in ("starting", "running"):
            status_value = "orphaned"
        agents.append({
            "task_id": row.get("task_id"), "attempt": row.get("attempt"),
            "provider": row.get("provider"), "status": status_value,
            "model": row.get("model"), "tool_count": row.get("tool_count"),
            "cost": row.get("cost"), "tokens": row.get("tokens"),
            "pid_verified": bool(row.get("pid") and row.get("pid_start_token")),
            "session_hash": hashlib.sha256(
                str(row.get("session_id", "")).encode()).hexdigest()[:12],
        })
    selected_active = active if active.get("run_id") == run_id else {}
    return {"run_id": run_id, "agents": agents,
            "owner_connected": connected,
            "recovery_state": _recovery_view(root, selected_active, state,
                                             run_id,
                                             connected=connected)["state"]}


def milestones(args) -> dict:
    data = status(args)
    return {"run_id": data["run_id"], "work_mode": data["mode"],
            "verified_progress": data["verified_progress"],
            "recovery_state": data["recovery_state"],
            "milestones": data["milestones"]}


def cancel(args) -> dict:
    root = _root(args.root)
    with project_lock(root, timeout=5):
        active = _active(root)
        run_id = args.run_id or active.get("run_id")
        if not run_id:
            raise ChatError("no active teamwork run")
        _safe_run_id(run_id)
        selected = active if active.get("run_id") == run_id else {}
        connected = _active_connected(selected) or lease_held(root, run_id)
        path = _state_path(root, f"cancel-{run_id}.json")
        _write_json(path, {"run_id": run_id, "requested_at": time.time(),
                           "requested_by": "single-chat"})
        write_recovery_state(root, run_id, "reconciling",
                             requested="cancel", owner_connected=connected)
    return {
        "run_id": run_id, "status": "cancel_requested",
        "owner_connected": connected,
        "local_signal_only": True, "native_cancel_ack": False,
        "note": "The runner signals only its owned child processes; no native ACK is claimed.",
    }


def resume(args) -> dict:
    root = _root(args.root)
    _require_repository(root)
    token = os.urandom(16).hex()
    with project_lock(root, timeout=10):
        active = _active(root)
        run_id = args.run_id or active.get("run_id")
        if not run_id:
            raise ChatError("no active teamwork run")
        run_id = _safe_run_id(run_id)
        runner_lease_held = lease_held(root, run_id)
        if project_writer_held(root) and not runner_lease_held:
            return {"run_id": run_id, "status": "already_running",
                    "owner_connected": _active_connected(active),
                    "project_writer_held": True,
                    "note": "another IDE holds the project writer lease; no duplicate writer"}
        try:
            _state_path(root, f"pause-{run_id}.json").unlink()
        except FileNotFoundError:
            pass
        if _owner_process_alive(active) or runner_lease_held:
            return {"run_id": run_id, "status": "already_running",
                    "owner_connected": _active_connected(active) or runner_lease_held,
                    "heartbeat_age_seconds": _heartbeat_age(active),
                    "heartbeat_stale": not _active_connected(active),
                    "runner_lease_held": runner_lease_held,
                    "pid": active.get("pid"),
                    "note": ("recovery did not start a duplicate runner; a live "
                             "owner with a stale heartbeat needs manual inspection"
                             if not _active_connected(active)
                             else "recovery did not start a duplicate runner")}
        plan_path = _validate_plan_path(root, active.get("plan"), run_id)
        try:
            plan = read_json(plan_path, {})
            if not isinstance(plan, dict) or plan.get("run_id") != run_id:
                raise ChatError("plan artifact does not belong to this run")
            state = _engine_status(root, run_id)
        except TeamworkError as exc:
            raise ChatError(str(exc)) from exc
        if state.get("status") in ("passed", "cancelled"):
            return {"run_id": run_id, "status": "already_terminal",
                    "run_status": state.get("status"),
                    "note": "successful or cancelled runs are not restarted"}
        contract = _plan_contract(root, plan)
        try:
            _state_path(root, f"cancel-{run_id}.json").unlink()
        except FileNotFoundError:
            pass
        try:
            _state_path(root, f"pause-{run_id}.json").unlink()
        except FileNotFoundError:
            pass
        log = _state_path(root, f"{run_id}.resume.log")
        process = _spawn(root, log, plan_path, recover=True,
                         owner_token=token, model=active.get("model"),
                         max_seconds=7200)
        previous = active.get("owner")
        updated = _new_active(root, run_id, active.get("mode") or plan.get("work_mode"),
                              plan_path, log, active.get("model"), token, process,
                              recovery_state="resuming")
        updated["previous_owner"] = previous
        updated["resume_count"] = int(active.get("resume_count") or 0) + 1
        _write_json(_state_path(root), updated)
        write_recovery_state(root, run_id, "resuming",
                             previous_owner_verified=bool(previous),
                             note="new runner launched; orphan reconciliation pending")
    return {"run_id": run_id, "status": "resumed",
            "pid": updated["owner"]["pid"], "owner_verified": True,
            "log": str(log), "recover": True,
            "recovery_state": "resuming",
            "assumption_guard": plan.get("assumption_guard"),
            "requirements_contract": (_contract_ref(contract)
                                      if contract is not None else None),
            "contract_status": ("confirmed" if contract is not None
                                else "absent"),
            "native_cancel_ack": False, "local_signal_only": True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    start_p = sub.add_parser("start")
    source = start_p.add_mutually_exclusive_group(required=True)
    source.add_argument("--request")
    source.add_argument("--request-file")
    source.add_argument("--request-stdin", action="store_true")
    start_p.add_argument("--mode", default="auto",
                         choices=["auto", "fast", "balanced", "rigorous"])
    start_p.add_argument("--run-id")
    start_p.add_argument("--model", default=DEFAULT_MODEL)
    start_p.add_argument("--provider", choices=["opencode", "codex"])
    start_p.add_argument("--codex-sandbox",
                         choices=["read-only", "workspace-write"],
                         default="read-only")
    start_p.add_argument("--interview-id")
    start_p.add_argument("--grill-mode", default="auto",
                         choices=["off", "auto", "required", "plan-only"])
    start_p.add_argument("--max-seconds", type=float, default=7200)
    sub.add_parser("request-path")
    for name in ("status", "agents", "milestones", "cancel", "resume"):
        command = sub.add_parser(name)
        command.add_argument("--run-id")
    args = parser.parse_args(argv)
    try:
        action = {"start": start, "status": status, "agents": agents,
                  "milestones": milestones, "cancel": cancel,
                  "resume": resume, "request-path": request_path}[args.command]
        print(json.dumps(action(args), indent=2, ensure_ascii=False))
        return 0
    except (ChatError, OSError, ValueError, TeamworkError,
            TeamworkPlannerError, RepoProfileError, OwnershipError) as exc:
        print(f"TEAMWORK CHAT ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
