#!/usr/bin/env python3
"""Universal Teamwork and Grill-Me command surface.

The same durable project memory is read from every IDE. Execution delegates to
the existing OpenCode managed bridge on this machine; Codex/Cursor/Antigravity
native execution is reported as unsupported unless the corresponding adapter is
actually available. No fake cross-provider dispatch is performed.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import inspect
import json
import os
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from tools import assumption_guard, teamwork_chat, test_evidence
    from tools.grill_me import GrillError, GrillMeEngine
    from tools.project_identity import ensure_project_identity, read_project_identity
    from tools.project_memory import MemoryError, ProjectMemory
    from tools.run_owner import atomic_write_json, read_json, teamwork_dir
    from tools.teamwork import TeamworkError
    try:
        from tools import context_selector
    except ModuleNotFoundError:  # older flat installation
        context_selector = None  # type: ignore
except ModuleNotFoundError:
    import assumption_guard  # type: ignore
    import test_evidence  # type: ignore
    import git_memory_hook  # type: ignore
    import teamwork_chat  # type: ignore
    from grill_me import GrillError, GrillMeEngine  # type: ignore
    from project_identity import ensure_project_identity, read_project_identity  # type: ignore
    from project_memory import MemoryError, ProjectMemory  # type: ignore
    from run_owner import atomic_write_json, read_json, teamwork_dir  # type: ignore
    from teamwork import TeamworkError  # type: ignore
    try:
        import context_selector  # type: ignore
    except ModuleNotFoundError:  # older flat installation
        context_selector = None  # type: ignore

DEFAULT_PLATFORM = os.environ.get("AO_PLATFORM", "unknown")
NATIVE_PLATFORMS = ("opencode", "codex", "cursor", "antigravity")
NATIVE_OWNER_TTL = 14_400  # 4h; refreshed by owner commands and `ide heartbeat`
IDE_HANDOFF_COMMANDS = ("status", "activate", "heartbeat", "prepare",
                        "accept", "release", "recover")
FORBIDDEN_NATIVE_FIELDS = frozenset({
    "model", "recommended", "recommendation", "recommendations", "suggested",
    "suggestion", "preselected", "preselection", "llm", "confidence",
    "auto_selected", "selected_by_model", "agent_choice", "default_option",
})


class UniversalError(ValueError):
    pass


def _root(value):
    root = Path(value).resolve()
    if not root.is_dir():
        raise UniversalError("root directory does not exist")
    return root


def _platform(args):
    value = getattr(args, "platform", None) or DEFAULT_PLATFORM
    if value not in ("opencode", "codex", "cursor", "antigravity", "unknown"):
        raise UniversalError("platform must be opencode, codex, cursor, antigravity or unknown")
    return value


def _native_platform(args):
    platform = _platform(args)
    if platform not in NATIVE_PLATFORMS:
        raise UniversalError(
            "native execution requires platform opencode, codex, cursor or "
            "antigravity")
    return platform


def _run_id(value):
    return teamwork_chat._safe_run_id(value)


def _chat_args(root, *, command, request=None, request_file=None,
               request_stdin=False, mode="auto", run_id=None, model=None,
               max_seconds=7200, origin_platform="unknown", provider=None,
               interview_id=None, grill_mode="auto", guard_receipt=None,
               codex_sandbox="read-only", native=False, plan=None,
               integrity_policy="development"):
    return type("Args", (), {
        "root": str(root), "command": command, "request": request,
        "request_file": request_file, "request_stdin": request_stdin,
        "mode": mode, "run_id": run_id, "model": model,
        "max_seconds": max_seconds, "origin_platform": origin_platform,
        "provider": provider, "interview_id": interview_id,
        "grill_mode": grill_mode, "guard_receipt": guard_receipt,
        "codex_sandbox": codex_sandbox, "native": native, "plan": plan,
        "integrity_policy": integrity_policy,
    })()


def _ide_handoff_module():
    """Import the optional ownership module without making it mandatory."""
    try:
        from tools import ide_handoff  # type: ignore
        return ide_handoff
    except ModuleNotFoundError:
        pass
    try:
        import ide_handoff  # type: ignore
        return ide_handoff
    except ModuleNotFoundError:
        return None


def _handoff_kwargs(function, values):
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return {key: value for key, value in values.items()
                if value is not None}
    if any(item.kind is inspect.Parameter.VAR_KEYWORD
           for item in parameters.values()):
        return {key: value for key, value in values.items()
                if value is not None}
    return {key: value for key, value in values.items()
            if key in parameters and value is not None}


def _handoff_call(module, name, root, **values):
    function = getattr(module, name, None)
    if not callable(function):
        raise UniversalError(f"ide_handoff.{name} is not available")
    try:
        result = function(str(root), **_handoff_kwargs(function, values))
    except UniversalError:
        raise
    except Exception as exc:  # noqa: BLE001 - boundary to optional module
        raise UniversalError(f"ide_handoff.{name} failed: {exc}") from exc
    if not isinstance(result, dict):
        raise UniversalError(f"ide_handoff.{name} returned an invalid result")
    return result


def _owner_view(payload):
    payload = payload if isinstance(payload, dict) else {}
    owner = payload.get("owner")
    platform = None
    owner_id = None
    active = None
    if isinstance(owner, dict):
        platform = (owner.get("platform") or owner.get("ide")
                    or owner.get("origin_platform"))
        owner_id = (owner.get("owner_id") or owner.get("session_id")
                    or owner.get("id") or owner.get("name"))
        if "active" in owner:
            active = bool(owner.get("active"))
        elif "alive" in owner:
            active = bool(owner.get("alive"))
    elif isinstance(owner, str):
        platform = owner
        owner_id = owner
    if platform is None:
        platform = payload.get("owner_platform") or payload.get("platform")
    if owner_id is None:
        owner_id = payload.get("owner_id") or payload.get("session_id")
    fence = payload.get("fence", payload.get("owner_fence",
                                            payload.get("lease_fence")))
    if active is None:
        if "active" in payload:
            active = bool(payload.get("active"))
        elif "held" in payload:
            active = bool(payload.get("held"))
        else:
            active = bool(owner)
    return {
        "owner": copy.deepcopy(owner),
        "owner_platform": platform,
        "owner_id": owner_id,
        "fence": fence,
        "active": active,
        "run_id": payload.get("run_id"),
        "status": payload.get("status"),
        "state": payload.get("state") or payload.get("status"),
        "ok": payload.get("ok"),
        "handoff": payload.get("handoff"),
        "reasons": copy.deepcopy(payload.get("reasons") or []),
    }


def _credential_path(root, platform):
    return teamwork_dir(root) / f"ide-owner-{platform}.json"


def _load_credential(root, platform):
    value = read_json(_credential_path(root, platform), {})
    if not isinstance(value, dict):
        return {}
    token = value.get("owner_token")
    fence = value.get("fence")
    if not isinstance(token, str) or len(token) != 48:
        return {}
    return {"owner_token": token, "fence": fence,
            "owner_id": value.get("owner_id"), "platform": platform}


def _store_credential(root, platform, result):
    token = result.get("owner_token")
    if not isinstance(token, str) or len(token) != 48:
        return
    owner = result.get("owner") if isinstance(result.get("owner"), dict) else {}
    atomic_write_json(_credential_path(root, platform), {
        "version": 1,
        "platform": platform,
        "owner_token": token,
        "fence": result.get("fence"),
        "owner_id": owner.get("owner_id"),
        "stored_at": time.time(),
    })


def _credential(root, platform, explicit=None):
    if isinstance(explicit, str) and explicit:
        return {"owner_token": explicit,
                "fence": None, "owner_id": None, "platform": platform}
    return _load_credential(root, platform)


def _ownership_status(root, platform):
    module = _ide_handoff_module()
    if module is None:
        return {
            "available": False, "platform": platform, "owner": None,
            "owner_platform": None, "owner_id": None, "fence": None,
            "active": False, "foreign": False, "read_only": True,
            "note": "ide_handoff is not installed; ownership is unguarded",
        }
    try:
        payload = _handoff_call(module, "status", root)
    except UniversalError as exc:
        return {
            "available": True, "platform": platform, "owner": None,
            "owner_platform": None, "owner_id": None, "fence": None,
            "active": False, "foreign": False, "read_only": True,
            "error": str(exc),
        }
    view = _owner_view(payload)
    view.update({
        "available": True,
        "platform": platform,
        "read_only": True,
        "foreign": bool(view["active"] and view["owner_platform"]
                        and view["owner_platform"] != platform),
        "snapshot": payload,
    })
    return view


def _claim_ownership(root, platform, *, run_id=None, owner_id=None,
                     ttl_seconds=NATIVE_OWNER_TTL, action="write",
                     owner_token=None):
    """Call status/activate and refuse a live owner from another IDE.

    Same-IDE ownership is not re-activated (that would be a conflict in the
    handoff state machine). The caller keeps using the existing fence; a
    stored owner token is available for the explicit handoff subcommands.
    """
    module = _ide_handoff_module()
    if module is None:
        return {
            "available": False, "platform": platform, "owner": None,
            "owner_platform": None, "owner_id": owner_id, "fence": None,
            "active": False, "claimed": False,
            "note": "ide_handoff is not installed; ownership guard unavailable",
        }
    current = _owner_view(_handoff_call(module, "status", root))
    current_state = current.get("state") or current.get("status")
    if (current["active"] and current["owner_platform"]
            and current["owner_platform"] != platform):
        raise UniversalError(
            f"project ownership is held by {current['owner_platform']} "
            f"at fence {current['fence']}; {action} from {platform} is "
            "rejected. Use `ide status` and ask the owner to hand off or "
            "release the project")
    if (owner_id is not None and current["active"]
            and current["owner_id"] is not None
            and str(current["owner_id"]) != str(owner_id)):
        raise UniversalError(
            "project ownership is held by a different session of "
            f"{platform}; current fence {current['fence']}. Use the "
            "owning session or hand off explicitly")
    if current["active"]:
        if current_state == "handoff_prepared":
            if action not in ("ide release", "ide prepare"):
                raise UniversalError(
                    "a handoff is prepared; accept it in the target IDE before "
                    f"{action}")
        elif current_state not in ("active", "transferred"):
            raise UniversalError(
                f"ownership state {current_state} requires explicit recovery "
                f"before {action}")
        if (owner_id is not None and current["owner_id"] is not None
                and str(current["owner_id"]) != str(owner_id)):
            raise UniversalError(
                "project ownership is held by a different session of "
                f"{platform}; current fence {current['fence']}. Use the "
                "owning session or hand off explicitly")
        stored = _credential(root, platform, owner_token)
        if (not isinstance(stored.get("owner_token"), str)
                or not isinstance(stored.get("fence"), int)):
            # A matching platform name is not an authorization. A second
            # session of the same IDE must present the owner credential or
            # wait for an explicit handoff.
            raise UniversalError(
                "project ownership is held by this IDE platform but no owner "
                "credential is available to this session; use `ide status`, "
                "the owning session, or an explicit handoff/recovery")
        if (current["owner_id"] is not None
                and stored.get("owner_id") is not None
                and str(current["owner_id"]) != str(stored["owner_id"])):
            raise UniversalError(
                "project ownership credential belongs to a different session "
                f"of {platform}; current fence {current['fence']}")
        fence = current["fence"]
        if action != "ide heartbeat":
            # Any real command from the current owner refreshes its native
            # heartbeat, so a long working session does not look stale. The
            # heartbeat is a liveness signal only; it never grants a fence.
            refreshed = _handoff_call(
                module, "heartbeat", root,
                owner_token=stored["owner_token"], fence=stored["fence"])
            if refreshed.get("ok") and isinstance(
                    refreshed.get("fence"), int):
                _remember_handoff_result(root, platform, refreshed)
                fence = refreshed["fence"]
            elif refreshed.get("ok") is not True:
                raise UniversalError(
                    "the stored owner credential is stale; do not retry a "
                    "write without an explicit `ide status` or recovery")
        return {
            "available": True, "platform": platform, "claimed": False,
            "owner": current["owner"],
            "owner_platform": current["owner_platform"],
            "owner_id": current["owner_id"], "fence": fence,
            "active": True, "state": current_state,
            "credential_present": True,
        }
    if (current.get("ok") is False
            and current_state not in ("inactive", "released")):
        reasons = "; ".join(
            item.get("message") or item.get("code") or str(item)
            for item in (current.get("reasons") or []))
        raise UniversalError(
            f"ownership cannot be activated from state {current_state}: "
            f"{reasons or 'blocked'}")
    activated = _handoff_call(
        module, "activate", root,
        ide=platform, platform=platform, origin_platform=platform,
        run_id=run_id, owner=owner_id or platform,
        owner_id=owner_id or platform, session_id=owner_id,
        ttl_seconds=ttl_seconds, ttl=ttl_seconds)
    if activated.get("ok") is False:
        reasons = "; ".join(
            item.get("message") or item.get("code") or str(item)
            for item in (activated.get("reasons") or []))
        raise UniversalError(
            f"ide_handoff rejected activation for {platform}: "
            f"{reasons or activated.get('status') or 'blocked'}")
    _store_credential(root, platform, activated)
    view = _owner_view(activated)
    if (view["active"] and view["owner_platform"]
            and view["owner_platform"] != platform):
        raise UniversalError(
            f"ide_handoff returned a foreign owner {view['owner_platform']} "
            f"after activation at fence {view['fence']}")
    return {
        "available": True, "platform": platform, "claimed": True,
        "owner": view["owner"], "owner_platform": view["owner_platform"],
        "owner_id": view["owner_id"], "fence": view["fence"],
        "active": view["active"], "state": view.get("state"),
        "credential_present": bool(activated.get("owner_token")),
        "activation_ok": True,
    }


def _with_ownership(result, ownership):
    result = result if isinstance(result, dict) else {"result": result}
    ownership = ownership if isinstance(ownership, dict) else {}
    result["ownership"] = ownership
    result["owner"] = ownership.get("owner")
    result["owner_platform"] = ownership.get("owner_platform")
    result["fence"] = ownership.get("fence")
    return result


def _plan_start(args) -> dict:
    root = _root(args.root)
    platform = _native_platform(args)
    run_id = _run_id(getattr(args, "plan", None) or "")
    request = getattr(args, "request", None)
    request_file = getattr(args, "request_file", None)
    supplied = any((isinstance(request, str) and request.strip(),
                    isinstance(request_file, str) and request_file.strip(),
                    bool(getattr(args, "request_stdin", False))))
    ownership = _claim_ownership(root, platform, run_id=run_id,
                                 action="start an approved native plan")
    try:
        plan = teamwork_chat.load_native_plan(root, run_id)
        if supplied:
            supplied_text = _request_text(root, args)
            if supplied_text != plan.get("request"):
                raise UniversalError(
                    "the supplied request does not match the approved plan")
        # An approved plan is project state, not another IDE's live session.
        # After a sequential handoff the new owner continues the same plan;
        # ownership, not the authoring platform, decides who may execute it.
        authored_by = plan.get("native_platform")
        result = teamwork_chat.start_native_plan(
            _chat_args(root, command="start", run_id=run_id, plan=run_id,
                       origin_platform=platform, native=True))
    except teamwork_chat.ChatError as exc:
        raise UniversalError(str(exc)) from exc
    result["platform"] = platform
    if authored_by and authored_by != platform:
        result["plan_authored_by"] = authored_by
        result["continued_by"] = platform
        result["handoff_continuation"] = True
    return _with_ownership(result, ownership)


def start(args) -> dict:
    root = _root(args.root)
    platform = _platform(args)
    native = bool(getattr(args, "native", False))
    plan_id = getattr(args, "plan", None)
    if plan_id and native:
        raise UniversalError("start accepts either --native or --plan, not both")
    if plan_id:
        return _plan_start(args)
    if native:
        platform = _native_platform(args)
        if getattr(args, "provider", None):
            raise UniversalError(
                "--provider is a managed execution option; --native starts no "
                "provider and uses this IDE's own subagents")
    ownership = _claim_ownership(
        root, platform, run_id=getattr(args, "run_id", None),
        action="start a Teamwork plan")
    identity = ensure_project_identity(root)
    interview_id = getattr(args, "interview_id", None)
    grill_mode = getattr(args, "grill_mode", "auto") or "auto"
    if getattr(args, "grill", False) and not interview_id:
        with GrillMeEngine(root, identity=identity) as engine:
            result = engine.start(
                _request_text(root, args), origin_platform=platform,
                mode=grill_mode)
        return _with_ownership({
                "status": "interview_required",
                "platform": platform,
                "interview": result["interview"],
                "question": result["question"],
                "note": "Answer or skip with the grill commands, then approve and start."},
                ownership)
    # Read the exact request once. The approved interview does not store the
    # raw request, so its hash is verified against this text.
    request = _request_text(root, args)
    if interview_id:
        with GrillMeEngine(root, identity=identity) as engine:
            interview = engine.get_interview(interview_id)
            if interview["status"] != "approved":
                raise UniversalError("interview is not approved")
            if not engine.verify_request(interview_id, request):
                raise UniversalError(
                    "request does not match the approved interview; "
                    "use the original request text")
            summary = engine.summary(interview_id)
        if summary["blocking_questions"]:
            raise UniversalError("interview still has blocking questions")
    try:
        guard = assumption_guard.evaluate(
            root, request, platform=platform, mode=grill_mode,
            interview_id=interview_id)
    except assumption_guard.AssumptionGuardError as exc:
        raise UniversalError(str(exc)) from exc
    if guard["status"] != "allowed":
        return _with_ownership({
                "status": "interview_required",
                "platform": platform,
                "interview": guard.get("interview"),
                "question": guard.get("question"),
                "reasons": guard.get("reasons") or [],
                "contradictions": guard.get("contradictions") or [],
                "assumption_guard": guard["receipt"],
                "guard_receipt": guard["receipt"],
                "note": ("Critical ambiguity: answer/approve the persistent "
                         "Grill-Me interview, then start with --interview-id. "
                         "No plan or runner state was written.")},
                ownership)
    result = teamwork_chat.start(_chat_args(
        root, command="start", request=request,
        request_file=None, request_stdin=False,
        mode=args.mode, run_id=args.run_id, model=args.model,
        max_seconds=args.max_seconds, origin_platform=platform,
        provider=getattr(args, "provider", None),
        interview_id=interview_id, grill_mode=grill_mode,
        guard_receipt=guard["receipt"],
        codex_sandbox=getattr(args, "codex_sandbox", "read-only"),
        native=native,
        integrity_policy=getattr(args, "integrity_policy",
                                 "development") or "development"))
    result["platform"] = platform
    result.setdefault("assumption_guard", guard["receipt"])
    return _with_ownership(result, ownership)


def plan(args) -> dict:
    root = _root(args.root)
    platform = _native_platform(args)
    ownership = _claim_ownership(
        root, platform, run_id=getattr(args, "run_id", None),
        action="write a native plan")
    try:
        result = teamwork_chat.prepare_native_plan(_chat_args(
            root, command="plan", request=getattr(args, "request", None),
            request_file=getattr(args, "request_file", None),
            request_stdin=bool(getattr(args, "request_stdin", False)),
            mode=getattr(args, "mode", "auto") or "auto",
            run_id=getattr(args, "run_id", None),
            model=getattr(args, "model", None),
            max_seconds=getattr(args, "max_seconds", 7200),
            origin_platform=platform,
            interview_id=getattr(args, "interview_id", None),
            grill_mode=getattr(args, "grill_mode", "auto") or "auto",
            native=True,
            integrity_policy=getattr(args, "integrity_policy",
                                     "development") or "development"))
    except teamwork_chat.ChatError as exc:
        raise UniversalError(str(exc)) from exc
    result["platform"] = platform
    return _with_ownership(result, ownership)


def approve_plan(args) -> dict:
    root = _root(args.root)
    platform = _native_platform(args)
    run_id = _run_id(getattr(args, "run_id", None) or "")
    ownership = _claim_ownership(
        root, platform, run_id=run_id,
        action="approve a native plan")
    try:
        plan = teamwork_chat.load_native_plan(root, run_id)
        # Approval is bound to the plan digest and the explicit approver, not
        # to the IDE that wrote it. A new owner may approve a pending plan
        # after a handoff; ownership gating above already decided who writes.
        result = teamwork_chat.approve_native_plan(
            root, run_id, getattr(args, "approver", None), platform=platform)
    except teamwork_chat.ChatError as exc:
        raise UniversalError(str(exc)) from exc
    result["platform"] = platform
    result["execution"] = "native"
    result["plan_authored_by"] = plan.get("native_platform")
    return _with_ownership(result, ownership)


def _request_text(root, args) -> str:
    text = None
    count = sum((args.request is not None, args.request_file is not None,
                 bool(args.request_stdin)))
    if count != 1:
        raise UniversalError("provide exactly one request source")
    if args.request is not None:
        text = args.request
    elif args.request_file is not None:
        text = teamwork_chat._read_request_file(root, args.request_file)
    else:
        text = sys.stdin.read(teamwork_chat.MAX_REQUEST + 1)
    if not text or not text.strip() or len(text) > teamwork_chat.MAX_REQUEST or "\x00" in text:
        raise UniversalError("request is empty, too long or contains NUL")
    return text


def context(args) -> dict:
    root = _root(args.root)
    platform = _platform(args)
    ownership = _ownership_status(root, platform)
    identity = ensure_project_identity(root)
    with ProjectMemory(root, identity=identity) as memory:
        bundle = memory.context_bundle(limit=8)
        if context_selector is not None:
            selection = context_selector.select_context(
                memory, getattr(args, "request", None) or "",
                budget_tokens=getattr(args, "selection_budget", 900) or 900,
                modules=getattr(args, "module", None))
        else:  # older flat installation without the selector module
            selection = {"status": "unavailable", "items": [], "refs": [],
                         "token_estimate": 0, "reason":
                         "context selector module is not installed"}
        return {
            "platform": platform,
            "ownership": ownership,
            "owner": ownership.get("owner"),
            "owner_platform": ownership.get("owner_platform"),
            "fence": ownership.get("fence"),
            "identity": {k: identity.get(k) for k in
                         ("project_id", "project_uuid", "instance_id", "root")},
            "state": memory.context_state(),
            "summary": memory.context_summary(limit=args.limit),
            "handoff": memory.handoff_view(memory.latest_handoff()),
            "decisions": memory.list_decisions(status="confirmed", limit=20),
            "blocking": memory.list_requirements(status="unresolved_blocking"),
            "active_interview": bundle.get("active_interview"),
            "last_milestone": bundle.get("last_milestone"),
            "stale": bundle.get("stale"),
            "requirements_contract": bundle.get("contract"),
            "contract_ref": bundle.get("contract_ref"),
            "verification": bundle.get("verification"),
            "selection": selection,
            "selected_context": selection,
            "open_tasks": {
                "next_tasks": (bundle.get("handoff") or {}).get("payload", {}).get("next_tasks", []),
                "next_milestone": (bundle.get("handoff") or {}).get("payload", {}).get("next_milestone"),
            },
        }


def handoff(args) -> dict:
    root = _root(args.root)
    platform = _platform(args)
    ownership = _ownership_status(root, platform)
    identity = ensure_project_identity(root)
    with ProjectMemory(root, identity=identity) as memory:
        latest = memory.handoff_view(memory.latest_handoff())
        return {
            "platform": platform,
            "ownership": ownership,
            "owner": ownership.get("owner"),
            "owner_platform": ownership.get("owner_platform"),
            "fence": ownership.get("fence"),
            "project_id": identity["project_id"],
            "handoff": latest,
            "handoff_verified": bool((latest or {}).get("verified")),
            "summary": memory.context_summary(limit=args.limit),
            "state": memory.context_state(),
            "requirements_contract": memory.contract_ref(),
        }


def memory_command(args) -> dict:
    root = _root(args.root)
    if args.memory_command in ("install-hook", "uninstall-hook", "hook-status"):
        try:
            if args.memory_command == "install-hook":
                return git_memory_hook.install(root, dry_run=args.dry_run)
            if args.memory_command == "uninstall-hook":
                return git_memory_hook.uninstall(root, dry_run=args.dry_run)
            return git_memory_hook.status(root)
        except git_memory_hook.HookError as exc:
            raise UniversalError(str(exc)) from exc
    identity = ensure_project_identity(root)
    with ProjectMemory(root, identity=identity) as memory:
        if args.memory_command == "capture":
            observed = memory.capture_git_observation(platform=_platform(args))
            memory.write_context_files()
            return {"platform": _platform(args), "observation": observed,
                    "state": memory.context_state()}
        if args.memory_command == "summary":
            return {"state": memory.context_state(),
                    "summary": memory.context_summary(limit=args.limit)}
        if args.memory_command == "export":
            return memory.export_bundle(args.path)
        if args.memory_command == "import":
            return memory.import_bundle(args.path, apply=args.apply)
    raise UniversalError("unknown memory command")


def pause(args) -> dict:
    root = _root(args.root)
    platform = _platform(args)
    active = teamwork_chat._active(root)
    run_id = args.run_id or active.get("run_id")
    if not run_id:
        raise UniversalError("no active teamwork run")
    run_id = teamwork_chat._safe_run_id(run_id)
    ownership = _claim_ownership(root, platform, run_id=run_id,
                                 action="pause a run")
    path = root / ".auto-orchestrator" / "teamwork" / f"pause-{run_id}.json"
    atomic_write_json(path, {"run_id": run_id, "requested_at": time.time(),
                             "requested_by": platform})
    return _with_ownership({
            "run_id": run_id, "status": "pause_requested",
            "native_cancel_ack": False,
            "note": "Active children finish; no new task is dispatched."},
            ownership)


GRILL_READ_ONLY = frozenset({"next", "summary", "status"})




def _readonly_memory_brief(root, project_id):
    """Read the brief fields with SQLite mode=ro; never create or migrate."""
    empty = {"interview": None, "requirements": {"confirmed": 0,
             "blocking": 0, "blocking_ids": []}, "decisions": []}
    path = root / ".auto-orchestrator" / "memory.sqlite3"
    if path.is_symlink() or not path.is_file():
        return empty
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                     timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
    except (sqlite3.Error, OSError, ValueError):
        return empty
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        interview = None
        if "interviews" in tables:
            row = connection.execute(
                "SELECT interview_id,request_summary,status FROM interviews "
                "WHERE project_id=? AND status IN ('open','blocked','ready') "
                "ORDER BY updated_at DESC LIMIT 1",
                (project_id,)).fetchone()
            if row is not None:
                interview = {"interview_id": row["interview_id"],
                             "objective": row["request_summary"],
                             "status": row["status"]}
                if "interview_questions" in tables:
                    question = connection.execute(
                        "SELECT question_id,question_key FROM "
                        "interview_questions WHERE interview_id=? AND "
                        "status='open' ORDER BY ordinal LIMIT 1",
                        (row["interview_id"],)).fetchone()
                    if question is not None:
                        interview["question_id"] = question["question_id"]
                        interview["question_key"] = question["question_key"]
        requirements = dict(empty["requirements"])
        if "requirements" in tables:
            for row in connection.execute(
                    "SELECT status,COUNT(*) AS total FROM requirements "
                    "WHERE project_id=? GROUP BY status", (project_id,)):
                if row["status"] == "confirmed":
                    requirements["confirmed"] = int(row["total"])
                elif row["status"] == "unresolved_blocking":
                    requirements["blocking"] = int(row["total"])
            requirements["blocking_ids"] = [
                row["requirement_id"] for row in connection.execute(
                    "SELECT requirement_id FROM requirements WHERE "
                    "project_id=? AND status='unresolved_blocking' "
                    "ORDER BY updated_at DESC LIMIT 10", (project_id,))]
        decisions = []
        if "decisions" in tables:
            decisions = [
                {"decision_id": row["decision_id"], "topic": row["topic"],
                 "decision": row["decision"]}
                for row in connection.execute(
                    "SELECT decision_id,topic,decision FROM decisions WHERE "
                    "project_id=? AND status='confirmed' ORDER BY updated_at "
                    "DESC LIMIT 10", (project_id,))]
        return {"interview": interview, "requirements": requirements,
                "decisions": decisions}
    except sqlite3.Error:
        return empty
    finally:
        connection.close()

def brief(args) -> dict:
    """Read-only continuity brief for a new IDE session.

    It never takes ownership and never writes project state. The caller still
    needs an explicit handoff/release/accept to gain write authority.
    """
    root = _root(args.root)
    platform = _platform(args)
    identity = read_project_identity(root) or {
        "project_id": None, "project_uuid": None}
    ownership_full = _ownership_status(root, platform)
    ownership = {key: ownership_full.get(key) for key in (
        "available", "platform", "state", "status", "owner_platform",
        "owner_id", "fence", "active", "foreign", "read_only")}
    ownership["previous_owner"] = (
        (ownership_full.get("snapshot") or {}).get("previous_owner"))
    module = _ide_handoff_module()
    snapshot = None
    run_state = {}
    if module is not None:
        try:
            payload = _handoff_call(module, "status", root)
            snapshot = ((payload.get("result") or {}).get("snapshot")
                        if isinstance(payload.get("result"), dict)
                        else payload.get("snapshot"))
        except UniversalError:
            snapshot = None
        try:
            run_state = module._run_state(root, identity)
        except Exception:  # noqa: BLE001 - optional read-only helper
            run_state = {}
    memory_view = _readonly_memory_brief(root, identity["project_id"])
    interview = memory_view["interview"]
    requirements = memory_view["requirements"]
    decisions = memory_view["decisions"]
    blocking = requirements["blocking_ids"]
    verified = None
    run_id = (snapshot or {}).get("run_id") or run_state.get("run_id")
    if isinstance(run_id, str):
        directory = root / ".auto-orchestrator" / "verification"
        if directory.is_dir():
            files = sorted(directory.glob(f"{run_id}-*.json"))
            if files:
                try:
                    record = json.loads(files[-1].read_text(encoding="utf-8"))
                    verified = {
                        "verified": bool(record.get("verified")),
                        "aggregate_status": (record.get("result") or {}).get(
                            "aggregate_status"),
                        "commit": record.get("commit"),
                        "exit_code": record.get("exit_code"),
                        "evidence": files[-1].relative_to(root).as_posix(),
                    }
                except (OSError, ValueError):
                    verified = None
    next_action = (snapshot or {}).get("next_action")
    steps = []
    if ownership.get("foreign"):
        steps.append(
            f"Ownership is held by {ownership.get('owner_platform')}; ask that "
            "session to hand off or inspect read-only.")
    elif ownership.get("active"):
        steps.append("This IDE already holds ownership; continue the run.")
    else:
        steps.append("Take ownership with `ide activate` before any write.")
    if next_action:
        steps.append("Next action from the handoff: " + str(next_action)[:400])
    if interview and interview.get("question_id"):
        steps.append("Answer the open Grill-Me question: "
                     + str(interview["question_id"]))
    if run_state.get("failed_tests"):
        steps.append("Re-run the failing tests: " + ", ".join(
            run_state["failed_tests"][:5]))
    if blocking:
        steps.append("Resolve blocking requirements: " + ", ".join(
            str(item) for item in blocking[:5]))
    if verified is not None and not verified["verified"]:
        steps.append("Verification is not complete; run native-verify after "
                     "committing the change.")
    return {
        "platform": platform,
        "project_id": identity["project_id"],
        "project_uuid": identity["project_uuid"],
        "ownership": ownership,
        "active_ide": ownership.get("owner_platform"),
        "previous_ide": (snapshot or {}).get("source_ide")
        if snapshot else (ownership.get("previous_owner") or {}).get("ide"),
        "run": {
            "run_id": run_id,
            "mode": run_state.get("mode") or (snapshot or {}).get("mode"),
            "status": run_state.get("run_status")
            or (snapshot or {}).get("run_status"),
            "milestone_id": run_state.get("milestone_id")
            or (snapshot or {}).get("milestone_id"),
            "task_id": run_state.get("task_id")
            or (snapshot or {}).get("task_id"),
            "completed_tasks": run_state.get("completed_tasks")
            or (snapshot or {}).get("completed_tasks") or [],
            "pending_tasks": run_state.get("pending_tasks")
            or (snapshot or {}).get("pending_tasks") or [],
            "failed_tests": run_state.get("failed_tests")
            or (snapshot or {}).get("failed_tests") or [],
        },
        "objective": ((interview or {}).get("objective") or ""),
        "next_action": next_action,
        "open_question": (snapshot or {}).get("open_question")
        or (interview or {}).get("question_id"),
        "requirements": {
            "confirmed": requirements["confirmed"],
            "blocking": len(blocking),
            "blocking_ids": list(blocking[:10]),
        },
        "confirmed_decisions": list(decisions)[:10],
        "verification": verified,
        "resume_steps": steps,
        "read_only": True,
        "note": ("This brief does not grant write authority and does not "
                 "replay another IDE's chat session."),
    }

def grill(args) -> dict:
    root = _root(args.root)
    platform = _platform(args)
    if args.grill_command == "import-native":
        return import_native_grill(args)
    if args.grill_command in GRILL_READ_ONLY:
        ownership = _ownership_status(root, platform)
    else:
        ownership = _claim_ownership(
            root, platform, action=f"grill {args.grill_command}")
    identity = ensure_project_identity(root)
    with GrillMeEngine(root, identity=identity) as engine:
        if args.grill_command == "start":
            result = engine.start(_request_text(root, args),
                                  origin_platform=platform,
                                  mode=args.grill_mode)
        elif args.grill_command == "resume":
            result = engine.resume(args.interview_id)
        elif args.grill_command == "next":
            result = {"question": engine.next_question(args.interview_id)}
        elif args.grill_command == "answer":
            result = engine.answer(args.question_id, option=args.option,
                                   text=args.text,
                                   reason=getattr(args, "reason", None))
        elif args.grill_command == "skip":
            result = engine.skip(args.question_id)
        elif args.grill_command == "back":
            result = engine.back(args.question_id,
                                 reason=getattr(args, "reason", None))
        elif args.grill_command == "cancel":
            result = {"interview": engine.cancel()}
        elif args.grill_command == "summary":
            result = engine.summary(args.interview_id)
        elif args.grill_command == "approve":
            result = engine.approve(args.interview_id)
        elif args.grill_command == "status":
            result = engine.status()
        else:
            raise UniversalError("unknown grill command")
    return _with_ownership(result, ownership)


def _read_native_import_file(path_value) -> dict:
    if isinstance(path_value, dict):
        return path_value
    if not path_value:
        raise UniversalError("grill import-native requires --file")
    try:
        path = Path(path_value)
    except (TypeError, ValueError) as exc:
        raise UniversalError("native import path is invalid") from exc
    if path.is_symlink():
        raise UniversalError("native import file must not be a symlink")
    try:
        resolved = path.resolve()
        if not resolved.is_file():
            raise UniversalError("native import file is missing")
        if resolved.stat().st_size > 262144:
            raise UniversalError("native import file is too large")
        raw = resolved.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except UniversalError:
        raise
    except (OSError, ValueError) as exc:
        raise UniversalError("native import file is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise UniversalError("native import must be a JSON object")
    return payload


def _reject_native_import_fields(value, path="payload"):
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).strip().lower()
            if lowered in FORBIDDEN_NATIVE_FIELDS:
                raise UniversalError(
                    f"native import rejected {key!r} at {path}; only explicit "
                    "user-confirmed answers may be imported, never "
                    "recommendations or model output")
            _reject_native_import_fields(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_native_import_fields(item, f"{path}[{index}]")


def _bounded_text(value, field, limit, required=True):
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise UniversalError(f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise UniversalError(f"{field} is required")
    if len(value) > limit or any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
        raise UniversalError(f"{field} is not a bounded single field")
    return value


def _native_import_answers(payload):
    answers = payload.get("answers")
    decisions = payload.get("decisions")
    if answers is not None and decisions is not None:
        raise UniversalError("use either answers or decisions, not both")
    rows = answers if answers is not None else decisions
    if not isinstance(rows, list) or not rows:
        raise UniversalError(
            "native import needs a non-empty answers array of explicit "
            "user-confirmed answers")
    if len(rows) > 50:
        raise UniversalError("native import accepts at most 50 answers")
    normalized = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise UniversalError(f"answers[{index}] must be an object")
        if row.get("confirmed") is not True:
            raise UniversalError(
                f"answers[{index}] is not explicitly user-confirmed")
        confirmed_by = row.get("confirmed_by")
        if not isinstance(confirmed_by, str) or confirmed_by.strip().lower() != "user":
            raise UniversalError(
                f"answers[{index}] confirmed_by must be 'user'")
        topic = (row.get("topic") or row.get("question_key")
                 or row.get("key"))
        topic = _bounded_text(topic, f"answers[{index}].topic", 300)
        decision = _bounded_text(row.get("decision"),
                                 f"answers[{index}].decision", 2000)
        rationale = _bounded_text(row.get("rationale", ""),
                                  f"answers[{index}].rationale", 2000,
                                  required=False)
        alternatives = row.get("alternatives") or []
        if not isinstance(alternatives, list) or len(alternatives) > 20:
            raise UniversalError(
                f"answers[{index}].alternatives must be a short array")
        alternatives = [_bounded_text(item,
                                      f"answers[{index}].alternatives",
                                      1000) for item in alternatives]
        requirements = row.get("requirements") or []
        if not isinstance(requirements, list) or len(requirements) > 20:
            raise UniversalError(
                f"answers[{index}].requirements must be a short array")
        requirements = [_bounded_text(item,
                                      f"answers[{index}].requirements",
                                      2000) for item in requirements]
        normalized.append({
            "topic": topic, "decision": decision, "rationale": rationale,
            "alternatives": alternatives, "requirements": requirements,
        })
    return normalized


def import_native_grill(args) -> dict:
    root = _root(args.root)
    platform = _platform(args)
    payload = _read_native_import_file(getattr(args, "file", None))
    _reject_native_import_fields(payload)
    declared = payload.get("platform") or payload.get("origin_platform")
    if declared is not None and declared != "antigravity":
        raise UniversalError(
            "grill import-native only imports Antigravity native interviews")
    if platform not in ("antigravity", "unknown"):
        raise UniversalError(
            "grill import-native is an Antigravity native bridge; run it with "
            "--platform antigravity")
    if platform == "unknown" and declared != "antigravity":
        raise UniversalError(
            "grill import-native needs --platform antigravity or a payload "
            "platform field of 'antigravity'")
    platform = "antigravity"
    answers = _native_import_answers(payload)
    interview_ref = payload.get("interview_id")
    if interview_ref is not None:
        interview_ref = _bounded_text(interview_ref, "interview_id", 120)
    source_ref = f"native:antigravity:{interview_ref or 'import'}"
    ownership = _claim_ownership(
        root, platform, action="import a native Antigravity interview")
    identity = ensure_project_identity(root)
    decisions = []
    requirements = []
    with ProjectMemory(root, identity=identity) as memory:
        for answer in answers:
            decision = memory.add_decision(
                topic=answer["topic"], decision=answer["decision"],
                rationale=answer["rationale"],
                alternatives=answer["alternatives"],
                source="user", source_ref=(
                    f"{source_ref}:{answer['topic']}"),
                status="confirmed", confirmed_by="user")
            decisions.append({
                "decision_id": decision.get("decision_id"),
                "topic": decision.get("topic"),
                "decision": decision.get("decision"),
                "status": decision.get("status"),
            })
            for requirement_text in answer["requirements"]:
                requirement = memory.add_requirement(
                    requirement_text, category="confirmed",
                    status="confirmed", source="user",
                    source_ref=f"{source_ref}:{answer['topic']}")
                requirements.append({
                    "requirement_id": requirement.get("requirement_id"),
                    "text": requirement.get("text"),
                    "status": requirement.get("status"),
                })
        memory.write_context_files()
    return _with_ownership({
        "status": "imported",
        "execution": "native",
        "platform": platform,
        "interview_id": interview_ref,
        "source": "antigravity-native",
        "decisions": decisions,
        "requirements": requirements,
        "note": ("Only explicitly user-confirmed native answers were stored. "
                 "No recommended, model-generated or unconfirmed field was "
                 "imported, and no native /grill-me command was replaced."),
    }, ownership)


def delegate(args, command):
    root = _root(args.root)
    platform = _platform(args)
    if command in ("cancel", "resume"):
        ownership = _claim_ownership(root, platform,
                                     run_id=getattr(args, "run_id", None),
                                     action=command)
    else:
        ownership = _ownership_status(root, platform)
    namespace = _chat_args(root, command=command, run_id=args.run_id,
                           origin_platform=platform)
    result = getattr(teamwork_chat, command)(namespace)
    return _with_ownership(result, ownership)


def _require_ide_platform(args):
    platform = _platform(args)
    if platform not in NATIVE_PLATFORMS:
        raise UniversalError(
            "ide commands require an explicit platform: opencode, codex, "
            "cursor or antigravity")
    return platform


def _ide_result(command, platform, payload, ownership, *, read_only=False,
                show_token=False):
    view = _owner_view(payload)
    safe_payload = copy.deepcopy(payload)
    if not show_token:
        safe_payload.pop("owner_token", None)
        safe_payload.pop("token", None)
    return {
        "command": command,
        "platform": platform,
        "available": True,
        "ok": payload.get("ok"),
        "status": view.get("status") or payload.get("state") or "ok",
        "state": view.get("state"),
        "result": safe_payload,
        "owner": view["owner"] or ownership.get("owner"),
        "owner_platform": (view["owner_platform"]
                           or ownership.get("owner_platform")),
        "owner_id": view["owner_id"] or ownership.get("owner_id"),
        "fence": (view["fence"] if view["fence"] is not None
                  else ownership.get("fence")),
        "active": view["active"],
        "foreign": bool(view["active"] and view["owner_platform"]
                        and view["owner_platform"] != platform),
        "credential_present": ownership.get("credential_present", False),
        "ownership": dict(ownership, read_only=read_only),
    }


def _reason_text(payload):
    return "; ".join(
        item.get("message") or item.get("code") or str(item)
        for item in (payload.get("reasons") or [])) or "blocked"


def _remember_handoff_result(root, platform, payload):
    _store_credential(root, platform, payload)
    credential = _load_credential(root, platform)
    if (credential and payload.get("fence") is not None
            and credential.get("fence") != payload.get("fence")):
        credential["fence"] = payload.get("fence")
        atomic_write_json(_credential_path(root, platform), {
            "version": 1, "platform": platform,
            "owner_token": credential["owner_token"],
            "fence": payload.get("fence"),
            "owner_id": credential.get("owner_id"),
            "stored_at": time.time(),
        })


def _resolve_handoff(args):
    value = getattr(args, "handoff", None)
    if not value:
        raise UniversalError("--handoff requires a handoff id or snapshot path")
    digest = getattr(args, "digest", None)
    try:
        path = Path(value)
        if path.is_file() and not path.is_symlink():
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(snapshot, dict):
                raise UniversalError("handoff snapshot is not a JSON object")
            return (path.stem, digest or snapshot.get("digest"))
    except UniversalError:
        raise
    except (OSError, ValueError):
        pass
    return (value, digest)


def ide(args) -> dict:
    root = _root(args.root)
    platform = _require_ide_platform(args)
    command = args.ide_command
    if command not in IDE_HANDOFF_COMMANDS:
        raise UniversalError("unknown ide command")
    module = _ide_handoff_module()
    if module is None:
        if command == "status":
            return {
                "command": command, "platform": platform,
                "available": False, "status": "unavailable",
                "result": None, "ownership": _ownership_status(root, platform),
                "owner": None, "fence": None,
                "note": "ide_handoff is not installed; no ownership state exists",
            }
        raise UniversalError(
            "ide_handoff is not installed; IDE ownership and handoff commands "
            "are unavailable")
    if command == "status":
        payload = _handoff_call(module, "status", root)
        view = _owner_view(payload)
        return _ide_result(
            command, platform, payload,
            dict(view, available=True, platform=platform),
            read_only=True)
    if command == "activate":
        current = _owner_view(_handoff_call(module, "status", root))
        if current["active"]:
            if (current["owner_platform"]
                    and current["owner_platform"] != platform):
                raise UniversalError(
                    f"project ownership is held by "
                    f"{current['owner_platform']} at fence {current['fence']}")
            return _ide_result(
                command, platform,
                _handoff_call(module, "status", root),
                dict(current, available=True, platform=platform,
                     credential_present=bool(
                         _credential(root, platform,
                                     getattr(args, "owner_token", None)))),
                read_only=True)
        payload = _handoff_call(
            module, "activate", root, ide=platform,
            session_id=getattr(args, "owner_id", None),
            owner_id=getattr(args, "owner_id", None),
            ttl=_validate_ttl(getattr(args, "ttl_seconds", None)))
        if payload.get("ok") is False:
            raise UniversalError(
                f"ide_handoff.activate failed: {_reason_text(payload)}")
        _remember_handoff_result(root, platform, payload)
        return _ide_result(
            command, platform, payload,
            {"available": True, "platform": platform,
             "owner": payload.get("owner"), "fence": payload.get("fence"),
             "credential_present": bool(payload.get("owner_token")),
             "claimed": True},
            show_token=bool(getattr(args, "show_token", False)))
    if command == "accept":
        handoff_id, digest = _resolve_handoff(args)
        if not isinstance(digest, str) or len(digest) != 64:
            raise UniversalError(
                "ide accept requires --digest or a snapshot JSON path with "
                "a digest")
        payload = _handoff_call(
            module, "accept", root, handoff_id=handoff_id, digest=digest,
            ide=platform, session_id=getattr(args, "owner_id", None))
        if payload.get("ok") is False:
            raise UniversalError(
                f"ide_handoff.accept failed: {_reason_text(payload)}")
        _remember_handoff_result(root, platform, payload)
        status_payload = _handoff_call(module, "status", root)
        return _ide_result(
            command, platform, payload,
            dict(_owner_view(status_payload), available=True,
                 platform=platform, claimed=True,
                 credential_present=bool(
                     _credential(root, platform,
                                 getattr(args, "owner_token", None)))),
            read_only=False,
            show_token=bool(getattr(args, "show_token", False)))
    if command == "recover":
        evidence = None
        evidence_path = getattr(args, "evidence", None)
        if evidence_path:
            evidence = _read_evidence_file(evidence_path, "recovery evidence")
        payload = _handoff_call(
            module, "recover", root,
            actor=getattr(args, "owner_id", None) or platform,
            reason=getattr(args, "reason", None) or
            "explicit operator recovery",
            evidence=evidence,
            apply=bool(getattr(args, "apply", False)),
            discard_handoff=bool(getattr(args, "discard_handoff", False)))
        status_payload = _handoff_call(module, "status", root)
        return _ide_result(
            command, platform, payload,
            dict(_owner_view(status_payload), available=True,
                 platform=platform), read_only=True)
    action = f"ide {command}"
    ownership = _claim_ownership(
        root, platform, run_id=getattr(args, "run_id", None),
        owner_id=getattr(args, "owner_id", None),
        ttl_seconds=getattr(args, "ttl_seconds", None) or NATIVE_OWNER_TTL,
        action=action,
        owner_token=getattr(args, "owner_token", None))
    credential = _credential(root, platform,
                             getattr(args, "owner_token", None))
    token = credential.get("owner_token")
    fence = getattr(args, "fence", None)
    if fence is None:
        fence = credential.get("fence")
    if fence is None:
        fence = ownership.get("fence")
    if not token:
        raise UniversalError(
            "an owner token is required for this handoff command; run "
            "`ide activate` and keep the returned owner_token, or pass "
            "--owner-token")
    if type(fence) is not int:
        raise UniversalError(
            "a fence is required for this handoff command; pass --fence or "
            "use the fence returned by ide activate")
    values = {
        "owner_token": token,
        "fence": fence,
        "target_ide": getattr(args, "target_platform", None),
        "next_action": (getattr(args, "next_action", None)
                        or getattr(args, "summary", None)),
        "allow_dirty": bool(getattr(args, "allow_dirty", False)),
        "idle_confirmation": (
            {"user": "user_confirmed", "owner": "owner_confirmed"}.get(
                getattr(args, "confirm_idle", None))),
        "handoff_id": (getattr(args, "handoff", None)
                       if command == "release" else None),
        "reason": getattr(args, "reason", None),
    }
    function_name = "prepare_handoff" if command == "prepare" else command
    payload = _handoff_call(module, function_name, root, **values)
    if payload.get("ok") is False:
        raise UniversalError(
            f"ide_handoff.{function_name} failed: {_reason_text(payload)}")
    _remember_handoff_result(root, platform, payload)
    return _ide_result(command, platform, payload, ownership)



# -- native verification ------------------------------------------------
SHELL_METACHARACTERS = (";", "|", "&", "$", "`", "\n", "\r")
REVIEW_ROLES = ("reviewer", "auditor", "challenger", "success-auditor")


def _verification_command(value, contract, plan):
    """Resolve a command and enforce a typed test-runner allowlist.

    Substring checks are not enough: ``python -c "print('test')"`` must never
    count as a test command. Only real runner shapes are accepted.
    """
    command = value
    if not command and isinstance(contract, dict):
        command = contract.get("verification_command")
    if not command and isinstance(plan, dict):
        command = plan.get("verification_command")
    if not isinstance(command, str) or not command.strip():
        raise UniversalError(
            "native-verify needs a verification command on the contract, the "
            "plan or --verification-command")
    command = command.strip()
    if len(command) > 500 or any(char in command for char in SHELL_METACHARACTERS):
        raise UniversalError("verification command is unsafe or too long")
    try:
        argv = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as exc:
        raise UniversalError("verification command is not parseable") from exc
    if os.name == "nt":
        argv = [token.strip('"') for token in argv]
    if not argv:
        raise UniversalError("verification command is empty")
    executable = Path(argv[0]).name.lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    allowed = False
    if executable in ("python", "python3", "python3.11", "python3.12",
                      "python3.13", "py"):
        allowed = (len(argv) >= 3 and argv[1] == "-m"
                   and argv[2] in ("unittest", "pytest"))
    elif executable in ("pytest", "py.test"):
        allowed = True
    elif executable == "node":
        allowed = len(argv) >= 2 and argv[1] == "--test"
    elif executable == "npm":
        allowed = (len(argv) >= 2 and argv[1] == "test") or (
            len(argv) >= 3 and argv[1] == "run"
            and argv[2] in ("test", "check"))
    elif executable == "go":
        allowed = len(argv) >= 2 and argv[1] == "test"
    elif executable == "cargo":
        allowed = len(argv) >= 2 and argv[1] == "test"
    elif executable == "make":
        allowed = len(argv) >= 2 and argv[1] in ("test", "check")
    if not allowed:
        raise UniversalError(
            "native-verify only runs a recognized test runner "
            "(unittest/pytest/node --test/npm test/go test/cargo test/"
            "make test); agent prose or an interpreter expression is not "
            "verification")
    resolved = argv[0]
    if not os.path.isabs(resolved):
        resolved = shutil.which(argv[0])
        if not resolved:
            raise UniversalError("verification executable was not found")
    elif not os.path.isfile(resolved):
        raise UniversalError("verification executable does not exist")
    argv[0] = resolved
    return command, argv


def _run_bounded(argv, *, cwd, env, timeout):
    """Run a child with a bounded transcript and process-group cleanup."""
    process = subprocess.Popen(
        argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, start_new_session=(os.name != "nt"))
    chunks = {"stdout": b"", "stderr": b"", "total": 0}
    lock = threading.Lock()

    def reader(stream, key):
        tail = b""
        while True:
            block = stream.read(65536)
            if not block:
                break
            with lock:
                chunks["total"] += len(block)
            tail = (tail + block)[-200_000:]
        with lock:
            chunks[key] = tail

    threads = [
        threading.Thread(target=reader, args=(process.stdout, "stdout"),
                         daemon=True),
        threading.Thread(target=reader, args=(process.stderr, "stderr"),
                         daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    for thread in threads:
        thread.join(timeout=5)
    stdout = chunks["stdout"].decode("utf-8", "replace")
    stderr = chunks["stderr"].decode("utf-8", "replace")
    return {
        "exit_code": 124 if timed_out else process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "output_bytes": chunks["total"],
        "output_tail": (stdout + stderr)[-200_000:],
    }


def _git_changed_paths(root):
    try:
        from tools import ide_handoff
    except ModuleNotFoundError:
        import ide_handoff  # type: ignore
    state = ide_handoff._git_state(root)
    return [item["path"] for item in state.get("changed_files") or []]


def _load_native_reviews(root, run_id, task_id, commit, contract_sha=None):
    directory = Path(root) / ".auto-orchestrator" / "reviews"
    if directory.is_symlink() or not directory.is_dir():
        return []
    reviews = []
    for path in sorted(directory.glob(f"{run_id}-{task_id}-*.json"))[-20:]:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        reviewed = value.get("reviewed_task_id") or value.get("task_id")
        if value.get("run_id") != run_id or reviewed != task_id:
            continue
        if value.get("commit") and value["commit"] != commit:
            continue
        if (contract_sha and value.get("contract_sha256")
                and value["contract_sha256"] != contract_sha):
            continue
        reviews.append(value)
    return reviews


def _native_report_identity(root, run_id, task_id):
    directory = Path(root) / ".auto-orchestrator" / "teamwork" / "native-reports"
    if directory.is_symlink() or not directory.is_dir():
        return None
    files = sorted(directory.glob(f"{run_id}-{task_id}-*.json"))
    if not files:
        return None
    try:
        value = json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    owner = value.get("owner") if isinstance(value.get("owner"), dict) else {}
    return owner.get("owner_id") or value.get("platform")


def _review_projection(reviews):
    result = []
    for item in reviews:
        role = item.get("role")
        if role in REVIEW_ROLES and item.get("verdict") in ("pass", "fail"):
            level = item.get("independence_level") or "declared"
            result.append({
                "reviewed_task_id": item.get("reviewed_task_id")
                or item.get("task_id"),
                "reviewer_task_id": item.get("reviewer_task_id"),
                "reviewer_agent_id": item.get("reviewer_agent_id"),
                "reviewer_session_id": item.get("reviewer_session_id"),
                "role": role,
                "verdict": item["verdict"],
                "independent": level in ("observed", "accepted"),
                "independence_level": level,
                "verification_status": item.get("verification_status"),
                "commit": item.get("commit") or item.get("commit_sha"),
                "contract_sha256": item.get("contract_sha256"),
                "reviewer": item.get("reviewer"),
                "criterion_ids": list(item.get("criterion_ids") or []),
            })
    return result


def native_verify(args) -> dict:
    root = _root(args.root)
    platform = _native_platform(args)
    run_id = _run_id(getattr(args, "run_id", None) or "")
    task_id = _run_id(getattr(args, "task_id", None) or "")
    ownership = _claim_ownership(root, platform, run_id=run_id,
                                 action="run native verification")
    try:
        plan = teamwork_chat.load_native_plan(root, run_id)
        approval = teamwork_chat.verify_plan_approval(root, plan, run_id)
        contract = teamwork_chat._plan_contract(root, plan)
    except teamwork_chat.ChatError as exc:
        raise UniversalError(str(exc)) from exc
    if contract is None:
        raise UniversalError(
            "native verification requires an approved requirements contract")
    command, argv = _verification_command(
        getattr(args, "verification_command", None), contract, plan)
    timeout = getattr(args, "timeout", None) or 900
    if type(timeout) not in (int, float) or timeout <= 0 or timeout > 3600:
        raise UniversalError("timeout must be between 1 and 3600 seconds")
    verification_run_id = "vr_" + secrets.token_hex(8)
    run_dir = (Path(root) / ".auto-orchestrator" / "verification" / "runs" /
               verification_run_id)
    run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    results_file = getattr(args, "results_file", None)
    results_path = None
    results_sha256 = None
    results_error = None
    if results_file:
        name = str(results_file)
        if (not name or name in (".", "..") or os.path.isabs(name)
                or "/" in name or "\\" in name):
            raise UniversalError(
                "--results-file must be a plain filename; the runner writes "
                "it inside the per-verification output directory")
        results_path = run_dir / name
        if results_path.exists() or results_path.is_symlink():
            raise UniversalError(
                "verification result file already exists before the run; "
                "stale evidence is not accepted")
        # Substitute after tokenization so spaces in the project path do not
        # split the executable or arguments.
        argv = [token.replace("{results_file}", str(results_path)).replace(
            "{output_dir}", str(run_dir)) for token in argv]
    env = dict(os.environ)
    env["AO_VERIFICATION_RUN_ID"] = verification_run_id
    env["AO_VERIFICATION_OUTPUT_DIR"] = str(run_dir)
    if results_path is not None:
        env["AO_VERIFICATION_RESULTS"] = str(results_path)
    started = time.time()
    changed_before = _git_changed_paths(root)
    run = _run_bounded(argv, cwd=root, env=env, timeout=float(timeout))
    finished = time.time()
    exit_code = run["exit_code"]
    output = run["stdout"] + run["stderr"]
    structured = None
    if results_path is not None:
        try:
            if results_path.is_symlink():
                raise test_evidence.TestEvidenceError(
                    "verification result must not be a symlink")
            if not results_path.is_file():
                raise test_evidence.TestEvidenceError(
                    "verification command produced no result file")
            stat = results_path.stat()
            if stat.st_size <= 0:
                raise test_evidence.TestEvidenceError(
                    "verification result file is empty")
            if stat.st_mtime + 2 < started:
                raise test_evidence.TestEvidenceError(
                    "verification result file is stale")
            payload = results_path.read_bytes()
            results_sha256 = hashlib.sha256(payload).hexdigest()
            structured = test_evidence.parse_file(
                results_path, getattr(args, "results_format", None))
            if not structured["tests"]:
                raise test_evidence.TestEvidenceError(
                    "verification result has no tests")
            if structured["totals"]["failed"] and exit_code == 0:
                raise test_evidence.TestEvidenceError(
                    "structured results report failures but the command "
                    "exited 0")
        except test_evidence.TestEvidenceError as exc:
            results_error = str(exc)
            structured = None
    try:
        from tools import completion_engine
    except ModuleNotFoundError:
        import completion_engine  # type: ignore
    head = completion_engine._head_sha(root)
    if not head:
        raise UniversalError("native verification requires a git commit")
    if structured is not None:
        passed_signals = structured["passed_names"]
        all_signals = [item["name"] for item in structured["tests"]]
    else:
        passed_signals = completion_engine.parse_passed_test_signals(output)
        all_signals = completion_engine.parse_test_signals(output)
    changed_after = _git_changed_paths(root)
    # The verification command itself must not become implementation evidence.
    changed = changed_before
    reviews = _load_native_reviews(
        root, run_id, task_id, head,
        (contract or {}).get("sha256") if isinstance(contract, dict) else None)
    implementer_identity = _native_report_identity(root, run_id, task_id)
    result = completion_engine.evaluate_completion(
        root, contract, plan=plan, run_id=run_id, task_id=task_id,
        changed_files=changed, commits=[head],
        test_results=[{
            "command": command,
            "exit_code": exit_code,
            "signals": passed_signals,
            "all_signals": all_signals,
            "source": "native-verify",
        }],
        reviews=_review_projection(reviews),
        commit_sha=head, head_sha=head, expected_commit=head,
        implementer_identity=implementer_identity)
    integrity_policy = plan.get("integrity_policy") or "development"
    merged_tree_clean = not changed_before and not changed_after
    if not merged_tree_clean:
        # Merged-tree gate: a dirty worktree means the test result is not
        # bound to the commit. Keep the evidence, but never claim verified
        # completion.
        result["verified_completion"] = False
        result["fully_verified"] = False
        result["merged_tree_clean"] = False
        reasons = list(result.get("reasons") or [])
        if "dirty worktree" not in " ".join(str(item) for item in reasons):
            reasons.append(
                "dirty worktree: commit the change and re-run native-verify")
        result["reasons"] = reasons
    if integrity_policy != "development":
        result["verified_completion"] = False
        result["fully_verified"] = False
        result["integrity_policy"] = integrity_policy
        reasons = list(result.get("reasons") or [])
        reasons.append(
            f"integrity policy '{integrity_policy}' does not certify "
            "production completion")
        result["reasons"] = reasons
    evidence_failed = results_path is not None and structured is None
    verified = (bool(result.get("verified_completion"))
                and merged_tree_clean and integrity_policy == "development"
                and not evidence_failed)
    status = "verified" if verified else (
        "results_missing" if evidence_failed else
        "command_failed" if exit_code != 0 else
        "dirty_tree" if not merged_tree_clean else
        "policy_unverified" if integrity_policy != "development" else
        (result.get("aggregate_status") or "needs_review"))
    record = {
        "version": 2,
        "kind": "native-verification",
        "verification_run_id": verification_run_id,
        "run_id": run_id,
        "task_id": task_id,
        "platform": platform,
        "owner_id": ownership.get("owner_id"),
        "command": command,
        "argv": argv,
        "exit_code": exit_code,
        "timed_out": run["timed_out"],
        "output_bytes": run["output_bytes"],
        "started_at": started,
        "finished_at": finished,
        "duration_seconds": round(finished - started, 3),
        "commit": head,
        "contract_id": (contract or {}).get("contract_id"),
        "contract_version": (contract or {}).get("contract_version"),
        "contract_sha256": (contract or {}).get("sha256"),
        "plan_sha256": approval.get("plan_sha256"),
        "integrity_policy": integrity_policy,
        "results_format": structured["format"] if structured else "stdout",
        "results_file": str(results_path) if results_path else None,
        "results_sha256": results_sha256,
        "results_error": results_error,
        "results_totals": structured["totals"] if structured else None,
        "failed_tests": structured["failed_names"] if structured else [],
        "skipped_tests": structured["skipped_names"] if structured else [],
        "passed_signals": passed_signals[:completion_engine.MAX_TEST_SIGNALS],
        "observed_signals": all_signals[:completion_engine.MAX_TEST_SIGNALS],
        "output_tail": output[-4000:],
        "output_sha256": __import__("hashlib").sha256(
            output.encode("utf-8", "replace")).hexdigest(),
        "changed_files": changed,
        "merged_tree_clean": merged_tree_clean,
        "post_run_changes": sorted(set(changed_after) - set(changed_before)),
        "reviews": _review_projection(reviews),
        "review_count": len(reviews),
        "result": result,
        "status": status,
        "verified": verified,
        "completion_recorded": False,
        "note": ("This command ran a real test command. It does not mark the "
                 "task complete; Supervisor acceptance and independent review "
                 "remain separate."),
        "recorded_at": time.time(),
    }
    directory = Path(root) / ".auto-orchestrator" / "verification"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{run_id}-{task_id}-{time.time_ns()}.json"
    atomic_write_json(path, record)
    try:
        from tools import project_identity
        identity = project_identity.ensure_project_identity(root)
        with ProjectMemory(root, identity=identity) as memory:
            observation = memory.record_observation(
                kind="native-verification", commit_sha=head,
                files=changed, platform=platform,
                test_evidence=json.dumps({
                    "command": command,
                    "exit_code": exit_code,
                    "passed_signals": passed_signals[:200],
                    "verified": record["verified"],
                    "contract_id": record["contract_id"],
                }, ensure_ascii=False))
    except Exception:
        observation = None
    return _with_ownership({
        "status": record["status"],
        "verified_completion": record["verified"],
        "aggregate_status": result.get("aggregate_status"),
        "run_id": run_id,
        "task_id": task_id,
        "command": command,
        "exit_code": exit_code,
        "commit": head,
        "contract_id": record["contract_id"],
        "contract_sha256": record["contract_sha256"],
        "criteria": result.get("criteria"),
        "review_count": len(reviews),
        "verification_run_id": verification_run_id,
        "results_error": results_error,
        "evidence": str(path),
        "observation_id": (observation or {}).get("observation_id"),
        "completion_recorded": False,
        "note": record["note"],
    }, ownership)


def native_audit(args) -> dict:
    root = _root(args.root)
    platform = _native_platform(args)
    run_id = _run_id(getattr(args, "run_id", None) or "")
    task_id = _run_id(getattr(args, "task_id", None) or "")
    reviewer = getattr(args, "reviewer", None)
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 120:
        raise UniversalError("native-audit requires a reviewer identity")
    verdict = getattr(args, "verdict", None)
    if verdict not in ("pass", "fail"):
        raise UniversalError("native-audit verdict must be pass or fail")
    role = getattr(args, "role", None) or "reviewer"
    if role not in REVIEW_ROLES:
        raise UniversalError("native-audit role is not supported")
    ownership = _claim_ownership(root, platform, run_id=run_id,
                                 action="record a native audit")
    try:
        plan = teamwork_chat.load_native_plan(root, run_id)
        approval = teamwork_chat.verify_plan_approval(root, plan, run_id)
        contract = teamwork_chat._plan_contract(root, plan)
    except teamwork_chat.ChatError as exc:
        raise UniversalError(str(exc)) from exc
    try:
        from tools import completion_engine
    except ModuleNotFoundError:
        import completion_engine  # type: ignore
    head = completion_engine._head_sha(root)
    if not head:
        raise UniversalError("native-audit requires a git commit")
    commit = getattr(args, "commit", None) or head
    if not isinstance(commit, str) or commit != head:
        raise UniversalError(
            "native-audit must bind to the current HEAD; the code changed")
    evidence_refs = []
    for value in getattr(args, "evidence", None) or []:
        path = Path(value)
        if path.is_symlink() or not path.is_file():
            raise UniversalError("native-audit evidence must be a regular file")
        evidence_refs.append(str(path.resolve().relative_to(root.resolve())))
    criteria = list(getattr(args, "criteria", None) or [])
    integrity_policy = plan.get("integrity_policy") or "development"
    level = getattr(args, "independence", None) or "declared"
    accepted_by = (getattr(args, "accepted_by", None) or "").strip()
    if level not in ("declared", "accepted"):
        raise UniversalError(
            "native-audit independence must be declared or accepted; "
            "observed requires a verified native lifecycle record")
    if level == "accepted" and not accepted_by:
        raise UniversalError(
            "an accepted review requires --accepted-by with the human or "
            "trusted tool identity")
    reviewer_session = getattr(args, "reviewer_session_id", None)
    reviewer_agent = getattr(args, "reviewer_agent_id", None)
    record = {
        "version": 2,
        "review_id": "rv_" + secrets.token_hex(8),
        "run_id": run_id,
        "reviewed_task_id": task_id,
        "reviewer_task_id": getattr(args, "reviewer_task_id", None),
        "task_id": task_id,
        "role": role,
        "reviewer": reviewer.strip(),
        "reviewer_agent_id": reviewer_agent,
        "reviewer_session_id": reviewer_session,
        "verdict": verdict,
        "criterion_ids": criteria,
        "criteria": criteria,
        "commit": head,
        "commit_sha": head,
        "contract_id": (contract or {}).get("contract_id"),
        "contract_sha256": (contract or {}).get("sha256"),
        "plan_sha256": approval.get("plan_sha256"),
        "integrity_policy": integrity_policy,
        "evidence_refs": evidence_refs,
        "independence_level": level,
        "independence": "user_accepted" if level == "accepted"
        else "declared_role_separation",
        "independent": level == "accepted",
        "verification_status": "user_accepted" if level == "accepted"
        else "declared_only",
        "accepted_by": accepted_by or None,
        "platform": platform,
        "reviewed_at": time.time(),
    }
    directory = Path(root) / ".auto-orchestrator" / "reviews"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{run_id}-{task_id}-{time.time_ns()}.json"
    atomic_write_json(path, record)
    return _with_ownership({
        "status": "recorded",
        "verdict": verdict,
        "role": role,
        "run_id": run_id,
        "task_id": task_id,
        "commit": head,
        "review": str(path),
        "independence_level": level,
        "verification_status": record["verification_status"],
        "note": ("A declared review is audit evidence, not proof of "
                 "independent execution. Only an explicit accepted review "
                 "satisfies a review-required criterion; native-verify still "
                 "runs the real tests."),
    }, ownership)

def _validate_ttl(value):
    if value is None:
        return NATIVE_OWNER_TTL
    if type(value) not in (int, float) or value <= 0 or value > 86400:
        raise UniversalError("ttl_seconds must be a positive number up to 86400")
    return int(value)


def _read_evidence_file(path_value, label):
    if path_value is None or path_value == "":
        raise UniversalError(f"{label} JSON file is required")
    if isinstance(path_value, (list, dict)):
        return path_value
    if isinstance(path_value, str) and path_value.lstrip().startswith(("[", "{")):
        if len(path_value) > 262144:
            raise UniversalError(f"{label} JSON is too large")
        try:
            return json.loads(path_value)
        except ValueError as exc:
            raise UniversalError(f"{label} JSON is invalid") from exc
    try:
        path = Path(path_value)
    except (TypeError, ValueError) as exc:
        raise UniversalError(f"{label} path is invalid") from exc
    if path.is_symlink():
        raise UniversalError(f"{label} file must not be a symlink")
    try:
        resolved = path.resolve()
        if not resolved.is_file():
            raise UniversalError(f"{label} file is missing")
        if resolved.stat().st_size > 262144:
            raise UniversalError(f"{label} file is too large")
        return json.loads(resolved.read_text(encoding="utf-8"))
    except UniversalError:
        raise
    except (OSError, ValueError) as exc:
        raise UniversalError(f"{label} file is not valid JSON") from exc


def _validate_native_tests(value):
    if not isinstance(value, list):
        raise UniversalError(
            "tests JSON must be an array of test objects; prose is not test "
            "evidence")
    if len(value) > 100:
        raise UniversalError("tests JSON accepts at most 100 entries")
    normalized = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise UniversalError(
                f"tests[{index}] must be an object; prose is not evidence")
        name = item.get("name") or item.get("command") or item.get("test_id")
        if not isinstance(name, str) or not name.strip():
            raise UniversalError(f"tests[{index}] needs a name or command")
        status = item.get("status")
        if status not in ("passed", "failed", "skipped"):
            raise UniversalError(
                f"tests[{index}].status must be passed, failed or skipped")
        if "exit_code" in item:
            if type(item["exit_code"]) is not int:
                raise UniversalError(
                    f"tests[{index}].exit_code must be an integer")
            if status == "passed" and item["exit_code"] != 0:
                raise UniversalError(
                    f"tests[{index}] is passed but its exit code is not 0")
        normalized.append(dict(item))
    return normalized


def native_report(args) -> dict:
    root = _root(args.root)
    platform = _native_platform(args)
    run_id = _run_id(getattr(args, "run_id", None) or "")
    task_id = _run_id(getattr(args, "task_id", None) or "")
    status = getattr(args, "status", None)
    verdict = getattr(args, "verdict", None)
    if status not in ("passed", "failed", "blocked"):
        raise UniversalError("native-report status must be passed, failed or blocked")
    if verdict not in ("passed", "failed", "needs_review"):
        raise UniversalError(
            "native-report verdict must be passed, failed or needs_review")
    tests = _validate_native_tests(
        _read_evidence_file(getattr(args, "tests", None), "tests"))
    if status == "passed":
        if verdict == "failed":
            raise UniversalError(
                "a passed native task cannot carry a failed verdict")
        if not tests:
            raise UniversalError(
                "a passed native task needs non-empty tests JSON; prose is "
                "not test evidence")
        if any(item.get("status") != "passed" for item in tests):
            raise UniversalError(
                "a passed native task cannot have failing or skipped tests")
    if status == "failed" and verdict == "passed":
        raise UniversalError(
            "a failed native task cannot carry a passed verdict")
    if status == "blocked" and verdict == "passed":
        raise UniversalError(
            "a blocked native task cannot carry a passed verdict")
    ownership = _claim_ownership(root, platform, run_id=run_id,
                                 action="record a native task report")
    try:
        plan = teamwork_chat.load_native_plan(root, run_id)
        # Evidence is attributed to the reporting platform, but the approved
        # plan may be continued by a new owner after a sequential handoff.
        approval = teamwork_chat.verify_plan_approval(root, plan, run_id)
    except teamwork_chat.ChatError as exc:
        raise UniversalError(str(exc)) from exc
    tasks = [item for item in plan.get("tasks", [])
             if isinstance(item, dict)]
    task = next((item for item in tasks if item.get("id") == task_id), None)
    if task is None:
        raise UniversalError("native-report task id is not in the approved plan")
    try:
        state = teamwork_chat._engine_status(root, run_id)
    except TeamworkError as exc:
        raise UniversalError(
            "native-plan state is not initialized; run start --plan first") from exc
    engine_task = None
    for milestone in state.get("milestones", []):
        for item in milestone.get("tasks", []):
            if item.get("id") == task_id:
                engine_task = item
                break
    if engine_task is None:
        raise UniversalError("native-report task id is not in engine state")
    summary = getattr(args, "summary", None)
    if summary is not None:
        summary = _bounded_text(summary, "summary", 2000, required=False)
    directory = teamwork_dir(root) / "native-reports"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    report_path = (directory /
                   f"{run_id}-{task_id}-{time.time_ns()}.json")
    payload = {
        "version": 1,
        "kind": "native-report",
        "run_id": run_id,
        "task_id": task_id,
        "status": status,
        "verdict": verdict,
        "tests": tests,
        "summary": summary or "",
        "platform": platform,
        "origin_platform": platform,
        "execution": "native",
        "execution_provider": platform,
        "approval": approval,
        "plan_sha256": approval["plan_sha256"],
        "owner": ownership.get("owner"),
        "fence": ownership.get("fence"),
        "engine_task_state": engine_task.get("state"),
        "recorded_at": time.time(),
        "native_session_id": None,
        "verified": False,
        "completion_recorded": False,
        "note": ("Stored as native evidence only. This command does not mark "
                 "the task verified, does not claim a native session id and "
                 "does not replace Supervisor acceptance or review evidence."),
    }
    atomic_write_json(report_path, payload)
    return _with_ownership({
        "status": status,
        "record_status": "recorded",
        "verdict": verdict,
        "run_id": run_id,
        "task_id": task_id,
        "report": str(report_path),
        "tests_count": len(tests),
        "verified": False,
        "completion_recorded": False,
        "native_session_id": None,
        "note": payload["note"],
    }, ownership)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--platform", default=DEFAULT_PLATFORM)
    sub = parser.add_subparsers(dest="command", required=True)

    start_p = sub.add_parser("start")
    source = start_p.add_mutually_exclusive_group(required=False)
    source.add_argument("--request")
    source.add_argument("--request-file")
    source.add_argument("--request-stdin", action="store_true")
    start_p.add_argument("--mode", default="auto",
                         choices=["solo", "auto", "fast", "balanced", "rigorous"])
    start_p.add_argument("--grill", action="store_true")
    start_p.add_argument("--grill-mode", default="auto",
                         choices=["off", "auto", "required", "plan-only"])
    start_p.add_argument("--interview-id")
    start_p.add_argument("--run-id")
    start_p.add_argument("--model", default=teamwork_chat.DEFAULT_MODEL)
    start_p.add_argument("--codex-sandbox",
                         choices=["read-only", "workspace-write"],
                         default="read-only",
                         help="explicit Codex sandbox; workspace-write is opt-in")
    start_p.add_argument("--provider", choices=["opencode", "codex"],
                         help="explicit managed execution provider; required "
                              "to opt in from Codex/Cursor/Antigravity")
    start_p.add_argument("--max-seconds", type=float, default=7200)
    start_p.add_argument("--integrity-policy", default="development",
                         choices=["development", "demo", "benchmark"])
    start_p.add_argument("--native", action="store_true",
                         help="write a native plan/engine state; never spawn "
                              "a provider, runner or PID")
    start_p.add_argument("--plan", metavar="RUN_ID",
                         help="start an already approved native plan by run id")

    plan_p = sub.add_parser("plan")
    plan_source = plan_p.add_mutually_exclusive_group(required=True)
    plan_source.add_argument("--request")
    plan_source.add_argument("--request-file")
    plan_source.add_argument("--request-stdin", action="store_true")
    plan_p.add_argument("--mode", default="auto",
                        choices=["solo", "auto", "fast", "balanced", "rigorous"])
    plan_p.add_argument("--run-id")
    plan_p.add_argument("--model", default=teamwork_chat.DEFAULT_MODEL)
    plan_p.add_argument("--interview-id")
    plan_p.add_argument("--grill-mode", default="auto",
                        choices=["off", "auto", "required", "plan-only"])
    plan_p.add_argument("--max-seconds", type=float, default=7200)
    plan_p.add_argument("--integrity-policy", default="development",
                        choices=["development", "demo", "benchmark"])

    approve_p = sub.add_parser("approve-plan")
    approve_p.add_argument("--run-id", required=True)
    approve_p.add_argument("--approver", required=True)

    verify_p = sub.add_parser("native-verify")
    verify_p.add_argument("--run-id", required=True)
    verify_p.add_argument("--task-id", required=True)
    verify_p.add_argument("--verification-command", dest="verification_command")
    verify_p.add_argument("--results-file",
                          help="JUnit XML or pytest JSON from the real run")
    verify_p.add_argument("--results-format", choices=["junit", "pytest-json"])
    verify_p.add_argument("--timeout", type=float, default=900)

    audit_p = sub.add_parser("native-audit")
    audit_p.add_argument("--run-id", required=True)
    audit_p.add_argument("--task-id", required=True)
    audit_p.add_argument("--reviewer", required=True)
    audit_p.add_argument("--verdict", required=True,
                         choices=["pass", "fail"])
    audit_p.add_argument("--role", default="reviewer",
                         choices=list(REVIEW_ROLES))
    audit_p.add_argument("--criteria", action="append")
    audit_p.add_argument("--reviewer-task-id")
    audit_p.add_argument("--reviewer-agent-id")
    audit_p.add_argument("--reviewer-session-id")
    audit_p.add_argument("--independence", choices=["declared", "accepted"],
                         default="declared")
    audit_p.add_argument("--accepted-by")
    audit_p.add_argument("--commit")
    audit_p.add_argument("--evidence", action="append")

    report_p = sub.add_parser("native-report")
    report_p.add_argument("--run-id", required=True)
    report_p.add_argument("--task-id", required=True)
    report_p.add_argument("--status", required=True,
                          choices=["passed", "failed", "blocked"])
    report_p.add_argument("--tests", required=True,
                          help="path to a JSON array of test evidence objects")
    report_p.add_argument("--verdict", required=True,
                          choices=["passed", "failed", "needs_review"])
    report_p.add_argument("--summary")

    context_p = sub.add_parser("context")
    context_p.add_argument("--limit", type=int, default=2400)
    context_p.add_argument("--request")
    context_p.add_argument("--selection-budget", type=int, default=900)
    context_p.add_argument("--module", action="append")
    brief_p = sub.add_parser("brief")
    brief_p.add_argument("--run-id")
    handoff_p = sub.add_parser("handoff")
    handoff_p.add_argument("--limit", type=int, default=2400)
    pause_p = sub.add_parser("pause")
    pause_p.add_argument("--run-id")
    for name in ("status", "agents", "milestones", "cancel", "resume"):
        command = sub.add_parser(name)
        command.add_argument("--run-id")

    ide_p = sub.add_parser("ide")
    ide_sub = ide_p.add_subparsers(dest="ide_command", required=True)
    ide_status = ide_sub.add_parser("status")
    ide_status.add_argument("--platform", default=argparse.SUPPRESS)
    ide_activate = ide_sub.add_parser("activate")
    ide_activate.add_argument("--run-id")
    ide_activate.add_argument("--owner-id")
    ide_activate.add_argument("--owner-token")
    ide_activate.add_argument("--ttl-seconds", type=float,
                             default=NATIVE_OWNER_TTL)
    ide_activate.add_argument("--show-token", action="store_true",
                              help="print the owner token; it is stored in a "
                                   "0600 credential file otherwise")
    ide_activate.add_argument("--platform", default=argparse.SUPPRESS)
    for name in ("heartbeat", "prepare", "accept", "release", "recover"):
        ide_command = ide_sub.add_parser(name)
        ide_command.add_argument("--run-id")
        ide_command.add_argument("--owner-id")
        ide_command.add_argument("--owner-token")
        ide_command.add_argument("--fence", type=int)
        ide_command.add_argument("--platform", default=argparse.SUPPRESS)
        if name == "prepare":
            ide_command.add_argument("--summary")
            ide_command.add_argument("--next-action")
            ide_command.add_argument("--target-platform", required=True,
                                     choices=list(NATIVE_PLATFORMS))
            ide_command.add_argument("--allow-dirty", action="store_true")
            ide_command.add_argument("--confirm-idle", choices=["user", "owner"],
                                     help="explicit drain acknowledgement; "
                                          "required for prepare")
        if name == "accept":
            ide_command.add_argument("--handoff", required=True,
                                     help="handoff id or snapshot JSON path")
            ide_command.add_argument("--digest")
            ide_command.add_argument("--show-token", action="store_true")
        if name in ("release", "recover"):
            ide_command.add_argument("--reason")
        if name == "release":
            ide_command.add_argument("--handoff")
            ide_command.add_argument("--confirm-idle", choices=["user", "owner"],
                                     help="explicit drain acknowledgement; a "
                                          "prepared handoff inherits it")
        if name == "recover":
            ide_command.add_argument("--evidence")
            ide_command.add_argument("--apply", action="store_true")
            ide_command.add_argument(
                "--discard-handoff", action="store_true",
                help="abandon an unaccepted handoff so a new owner can "
                     "activate; requires --apply and evidence")

    memory_p = sub.add_parser("memory")
    memory_sub = memory_p.add_subparsers(dest="memory_command", required=True)
    memory_capture = memory_sub.add_parser("capture")
    memory_capture.add_argument("--platform", default=argparse.SUPPRESS)
    memory_summary = memory_sub.add_parser("summary")
    memory_summary.add_argument("--limit", type=int, default=2400)
    memory_export = memory_sub.add_parser("export")
    memory_export.add_argument("--path", required=True)
    memory_import = memory_sub.add_parser("import")
    memory_import.add_argument("--path", required=True)
    memory_import.add_argument("--apply", action="store_true")
    memory_import.add_argument("--platform", default=argparse.SUPPRESS)
    memory_install = memory_sub.add_parser("install-hook")
    memory_install.add_argument("--dry-run", action="store_true")
    memory_uninstall = memory_sub.add_parser("uninstall-hook")
    memory_uninstall.add_argument("--dry-run", action="store_true")
    memory_sub.add_parser("hook-status")

    grill_p = sub.add_parser("grill")
    grill_sub = grill_p.add_subparsers(dest="grill_command", required=True)
    gs = grill_sub.add_parser("start")
    gsource = gs.add_mutually_exclusive_group(required=True)
    gsource.add_argument("--request")
    gsource.add_argument("--request-file")
    gsource.add_argument("--request-stdin", action="store_true")
    gs.add_argument("--grill-mode", default="auto",
                    choices=["off", "auto", "required", "plan-only"])
    gs.add_argument("--platform", default=argparse.SUPPRESS)
    for name in ("next", "resume", "summary", "approve", "status", "cancel"):
        gp = grill_sub.add_parser(name)
        gp.add_argument("--interview-id")
        gp.add_argument("--platform", default=argparse.SUPPRESS)
    ga = grill_sub.add_parser("answer")
    ga.add_argument("--question-id", required=True)
    ga.add_argument("--option")
    ga.add_argument("--text")
    ga.add_argument("--reason",
                    help="bounded provenance for an answer change")
    ga.add_argument("--platform", default=argparse.SUPPRESS)
    for name in ("skip", "back"):
        gp = grill_sub.add_parser(name)
        gp.add_argument("--question-id", required=True)
        if name == "back":
            gp.add_argument("--reason")
        gp.add_argument("--platform", default=argparse.SUPPRESS)
    gi = grill_sub.add_parser("import-native")
    gi.add_argument("--file", required=True,
                    help="JSON export of explicitly user-confirmed Antigravity "
                         "answers")
    gi.add_argument("--platform", default=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    try:
        action = {
            "start": start, "plan": plan, "approve-plan": approve_plan,
            "native-report": native_report, "native-verify": native_verify,
            "native-audit": native_audit, "context": context,
            "handoff": handoff, "pause": pause, "grill": grill,
            "brief": brief,
            "memory": memory_command, "ide": ide,
            "status": lambda a: delegate(a, "status"),
            "agents": lambda a: delegate(a, "agents"),
            "milestones": lambda a: delegate(a, "milestones"),
            "cancel": lambda a: delegate(a, "cancel"),
            "resume": lambda a: delegate(a, "resume"),
        }[args.command]
        result = action(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (UniversalError, GrillError, MemoryError, ValueError,
            teamwork_chat.ChatError, OSError) as exc:
        print(f"UNIVERSAL TEAMWORK ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
