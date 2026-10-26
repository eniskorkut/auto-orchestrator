#!/usr/bin/env python3
"""Universal Teamwork and Grill-Me command surface.

The same durable project memory is read from every IDE. Execution delegates to
the existing OpenCode managed bridge on this machine; Codex/Cursor/Antigravity
native execution is reported as unsupported unless the corresponding adapter is
actually available. No fake cross-provider dispatch is performed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from tools import teamwork_chat
    from tools.grill_me import GrillError, GrillMeEngine
    from tools.project_identity import ensure_project_identity
    from tools.project_memory import MemoryError, ProjectMemory
    from tools.run_owner import atomic_write_json
except ModuleNotFoundError:
    import teamwork_chat  # type: ignore
    from grill_me import GrillError, GrillMeEngine  # type: ignore
    from project_identity import ensure_project_identity  # type: ignore
    from project_memory import MemoryError, ProjectMemory  # type: ignore
    from run_owner import atomic_write_json  # type: ignore

DEFAULT_PLATFORM = os.environ.get("AO_PLATFORM", "unknown")


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


def _chat_args(root, *, command, request=None, request_file=None,
               request_stdin=False, mode="auto", run_id=None, model=None,
               max_seconds=7200, origin_platform="unknown", provider=None):
    return type("Args", (), {
        "root": str(root), "command": command, "request": request,
        "request_file": request_file, "request_stdin": request_stdin,
        "mode": mode, "run_id": run_id, "model": model,
        "max_seconds": max_seconds, "origin_platform": origin_platform,
        "provider": provider,
    })()


def start(args) -> dict:
    root = _root(args.root)
    platform = _platform(args)
    identity = ensure_project_identity(root)
    chat_request = args.request
    if args.interview_id:
        # The approved interview does not store the raw request. Require the
        # original request again and verify its hash so a long or multi-line
        # task is never silently truncated to the stored summary.
        chat_request = _request_text(root, args)
        with GrillMeEngine(root, identity=identity) as engine:
            interview = engine.get_interview(args.interview_id)
            if interview["status"] != "approved":
                raise UniversalError("interview is not approved")
            if not engine.verify_request(args.interview_id, chat_request):
                raise UniversalError(
                    "request does not match the approved interview; "
                    "use the original request text")
            summary = engine.summary(args.interview_id)
        if summary["blocking_questions"]:
            raise UniversalError("interview still has blocking questions")
    elif args.grill:
        with GrillMeEngine(root, identity=identity) as engine:
            result = engine.start(
                _request_text(root, args), origin_platform=platform,
                mode=args.grill_mode)
        return {"status": "interview_required",
                "platform": platform,
                "interview": result["interview"],
                "question": result["question"],
                "note": "Answer or skip with the grill commands, then approve and start."}
    if not args.interview_id and not args.grill:
        _request_text(root, args)
    result = teamwork_chat.start(_chat_args(
        root, command="start", request=chat_request,
        request_file=args.request_file if args.interview_id is None else None,
        request_stdin=False,
        mode=args.mode, run_id=args.run_id, model=args.model,
        max_seconds=args.max_seconds, origin_platform=platform,
        provider=getattr(args, "provider", None)))
    result["platform"] = platform
    return result


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
    identity = ensure_project_identity(root)
    with ProjectMemory(root, identity=identity) as memory:
        return {
            "platform": _platform(args),
            "identity": {k: identity.get(k) for k in
                         ("project_id", "project_uuid", "instance_id", "root")},
            "state": memory.context_state(),
            "summary": memory.context_summary(limit=args.limit),
            "handoff": memory.latest_handoff(),
            "decisions": memory.list_decisions(status="confirmed", limit=20),
            "blocking": memory.list_requirements(status="unresolved_blocking"),
        }


def handoff(args) -> dict:
    root = _root(args.root)
    identity = ensure_project_identity(root)
    with ProjectMemory(root, identity=identity) as memory:
        latest = memory.latest_handoff()
        return {
            "platform": _platform(args),
            "project_id": identity["project_id"],
            "handoff": latest,
            "summary": memory.context_summary(limit=args.limit),
            "state": memory.context_state(),
        }


def memory_command(args) -> dict:
    root = _root(args.root)
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
    active = teamwork_chat._active(root)
    run_id = args.run_id or active.get("run_id")
    if not run_id:
        raise UniversalError("no active teamwork run")
    run_id = teamwork_chat._safe_run_id(run_id)
    path = root / ".auto-orchestrator" / "teamwork" / f"pause-{run_id}.json"
    atomic_write_json(path, {"run_id": run_id, "requested_at": time.time(),
                             "requested_by": _platform(args)})
    return {"run_id": run_id, "status": "pause_requested",
            "native_cancel_ack": False,
            "note": "Active children finish; no new task is dispatched."}


def grill(args) -> dict:
    root = _root(args.root)
    platform = _platform(args)
    identity = ensure_project_identity(root)
    with GrillMeEngine(root, identity=identity) as engine:
        if args.grill_command == "start":
            return engine.start(_request_text(root, args),
                                origin_platform=platform, mode=args.grill_mode)
        if args.grill_command == "resume":
            return engine.resume(args.interview_id)
        if args.grill_command == "next":
            return {"question": engine.next_question(args.interview_id)}
        if args.grill_command == "answer":
            return engine.answer(args.question_id, option=args.option,
                                 text=args.text)
        if args.grill_command == "skip":
            return engine.skip(args.question_id)
        if args.grill_command == "back":
            return engine.back(args.question_id)
        if args.grill_command == "cancel":
            return {"interview": engine.cancel()}
        if args.grill_command == "summary":
            return engine.summary(args.interview_id)
        if args.grill_command == "approve":
            return engine.approve(args.interview_id)
        if args.grill_command == "status":
            return engine.status()
    raise UniversalError("unknown grill command")


def delegate(args, command):
    root = _root(args.root)
    platform = _platform(args)
    namespace = _chat_args(root, command=command, run_id=args.run_id,
                           origin_platform=platform)
    return getattr(teamwork_chat, command)(namespace)


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
                         choices=["auto", "fast", "balanced", "rigorous"])
    start_p.add_argument("--grill", action="store_true")
    start_p.add_argument("--grill-mode", default="auto",
                         choices=["off", "auto", "required", "plan-only"])
    start_p.add_argument("--interview-id")
    start_p.add_argument("--run-id")
    start_p.add_argument("--model", default=teamwork_chat.DEFAULT_MODEL)
    start_p.add_argument("--provider", choices=["opencode", "codex"],
                         help="explicit managed execution provider; required "
                              "to opt in from Codex/Cursor/Antigravity")
    start_p.add_argument("--max-seconds", type=float, default=7200)

    context_p = sub.add_parser("context")
    context_p.add_argument("--limit", type=int, default=2400)
    handoff_p = sub.add_parser("handoff")
    handoff_p.add_argument("--limit", type=int, default=2400)
    pause_p = sub.add_parser("pause")
    pause_p.add_argument("--run-id")
    for name in ("status", "agents", "milestones", "cancel", "resume"):
        command = sub.add_parser(name)
        command.add_argument("--run-id")

    memory_p = sub.add_parser("memory")
    memory_sub = memory_p.add_subparsers(dest="memory_command", required=True)
    memory_capture = memory_sub.add_parser("capture")
    memory_capture.add_argument("--platform")
    memory_summary = memory_sub.add_parser("summary")
    memory_summary.add_argument("--limit", type=int, default=2400)
    memory_export = memory_sub.add_parser("export")
    memory_export.add_argument("--path", required=True)
    memory_import = memory_sub.add_parser("import")
    memory_import.add_argument("--path", required=True)
    memory_import.add_argument("--apply", action="store_true")
    memory_import.add_argument("--platform")

    grill_p = sub.add_parser("grill")
    grill_sub = grill_p.add_subparsers(dest="grill_command", required=True)
    gs = grill_sub.add_parser("start")
    gsource = gs.add_mutually_exclusive_group(required=True)
    gsource.add_argument("--request")
    gsource.add_argument("--request-file")
    gsource.add_argument("--request-stdin", action="store_true")
    gs.add_argument("--grill-mode", default="auto",
                    choices=["off", "auto", "required", "plan-only"])
    gs.add_argument("--platform")
    for name in ("next", "resume", "summary", "approve", "status", "cancel"):
        gp = grill_sub.add_parser(name)
        gp.add_argument("--interview-id")
        gp.add_argument("--platform")
    ga = grill_sub.add_parser("answer")
    ga.add_argument("--question-id", required=True)
    ga.add_argument("--option")
    ga.add_argument("--text")
    ga.add_argument("--platform")
    for name in ("skip", "back"):
        gp = grill_sub.add_parser(name)
        gp.add_argument("--question-id", required=True)
        gp.add_argument("--platform")

    args = parser.parse_args(argv)
    try:
        action = {
            "start": start, "context": context, "handoff": handoff,
            "pause": pause, "grill": grill, "memory": memory_command,
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
