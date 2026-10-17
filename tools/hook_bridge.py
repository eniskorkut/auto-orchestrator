#!/usr/bin/env python3
"""Codex and Antigravity lifecycle bridge; read stdin, never persist prompts.

Only Codex SubagentStart/SubagentStop expose authoritative subagent runtimes.
Antigravity's invoke_subagent Pre/PostToolUse measure tool dispatch only, so they
must NOT be interpreted as completed agent wall-time or claimed as time saved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
try:
    from tools.perf import EventError, append_event, load_events, path_for
except ModuleNotFoundError:
    from perf import EventError, append_event, load_events, path_for

POLICY = ('Auto-Orchestrator: for non-trivial coding tasks read '
          'auto-orchestrator SKILL.md (project-local or globally installed). Delegate independent work '
          'to native subagents automatically when safe, with max 3 concurrent; '
          'use one agent for trivial work. Verify changes before reporting. '
          'Do not invent subagent timings or human time savings.')


def opaque(*values: object) -> str:
    value = '|'.join(str(v) for v in values)
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


def already_finished(root: Path, run_id: str) -> bool:
    path=path_for(root, run_id)
    return path.exists() and any(x.get('event')=='finish' for x in load_events(path))


def record(root: Path, run_id: str, event: str, **kwargs) -> None:
    path=path_for(root, run_id)
    if event=='start' and path.exists():
        return
    if event!='start' and (not path.exists() or already_finished(root, run_id)):
        return
    if event in ('agent-start','agent-stop'):
        existing=load_events(path)
        agent=kwargs['agent_id']
        if any(x.get('event')==event and x.get('agent_id')==agent for x in existing):
            return
    append_event(root,run_id,event,**kwargs)


def observe_supervised(root: Path, platform: str, native_id: object, event: str) -> None:
    """Only pre-existing Supervisor DBs; no implicit run, claim or completion."""
    db=root/'.auto-orchestrator'/'supervisor.sqlite3'
    if not db.is_file() or db.is_symlink() or not isinstance(native_id,str):
        return
    try:
        try:
            from tools.supervisor import Supervisor, SupervisorError
        except ModuleNotFoundError:
            from supervisor import Supervisor, SupervisorError
        supervisor=Supervisor(root)
        try:
            supervisor.observe_native(platform,native_id,event)
        finally:
            supervisor.close()
    except (SupervisorError,OSError,ValueError,sqlite3.Error):
        pass  # Hooks are advisory; do not interrupt native IDE operations.

def handle_codex(payload: dict, root: Path) -> dict:
    event=payload.get('hook_event_name')
    session=payload.get('session_id')
    turn=payload.get('turn_id')
    if not session or not isinstance(session,str):
        return {}
    agent=payload.get('agent_id')
    if event in ('SubagentStart','SubagentStop') and agent:
        # Supervisor identity matching does not depend on turn-specific
        # performance bookkeeping. Some client hook payloads lack turn_id.
        observe_supervised(root,'codex',agent,'start' if event=='SubagentStart' else 'stop')
    if event=='UserPromptSubmit' and turn:
        run=opaque('codex',session,turn)
        record(root,run,'start',platform='codex')
        return {'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':POLICY}}
    if not turn:
        return {}
    run=opaque('codex',session,turn)
    agent=payload.get('agent_id')
    if event in ('SubagentStart','SubagentStop') and agent:
        record(root,run,'agent-start' if event=='SubagentStart' else 'agent-stop',agent_id=opaque(agent))
    elif event=='Stop':
        record(root,run,'finish',status='success')  # Verified only with explicit CLI event.
    return {}


def handle_antigravity(payload: dict, root: Path, event: str) -> dict:
    session=payload.get('conversationId')
    if not session or not isinstance(session,str):
        return {}
    # A session-scoped duration, not a subagent runtime. A resumed conversation
    # with multiple Stop cycles requires a new session to begin another timing run.
    run=opaque('antigravity',session)
    if event=='SessionStart':
        record(root,run,'start',platform='antigravity')
        return {'injectSteps':[{'ephemeralMessage':POLICY}]}
    if event=='PreInvocation' and payload.get('invocationNum')==0:
        record(root,run,'start',platform='antigravity')
        return {'injectSteps':[{'ephemeralMessage':POLICY}]}
    if event=='Stop':
        if payload.get('fullyIdle') is True:
            record(root,run,'finish',status='success' if payload.get('terminationReason')=='model_stop' else 'failed')
        return {'decision':'allow'}  # Stop requires a decision field.
    return {}


def handle_cursor(payload: dict, root: Path, event: str) -> dict:
    """Cursor project-scoped hook bridge, strictly observation-only."""
    sid=payload.get('session_id') or payload.get('parent_conversation_id')
    if not isinstance(sid,str) or not sid:
        return {}
    run=opaque('cursor',sid)
    if event=='sessionStart':
        record(root,run,'start',platform='cursor')
        return {'additional_context':POLICY}
    if event=='subagentStart':
        # Cursor's docs expose an ID here, but its subagentStop schema does
        # not promise one. Only link authoritative, identical IDs.
        native=payload.get('subagent_id')
        if isinstance(native,str) and native:
            # Start is observable, but Cursor's documented stop payload does
            # not include the same ID. Do not create unmatched perf spans.
            observe_supervised(root,'cursor',native,'start')
        return {}  # Never auto-approve or bypass the IDE's permission chain.
    if event=='subagentStop':
        native=payload.get('subagent_id')
        if isinstance(native,str) and native:
            # Some versions may provide an ID as an extension; only then may
            # Supervisor match and release evidence. No guessing from task text.
            observe_supervised(root,'cursor',native,'stop')
        # Do NOT assume task text/type or transcript path identifies the agent.
        return {}
    if event=='sessionEnd':
        reason=payload.get('reason')
        status={'completed':'success','aborted':'cancelled','error':'failed',
                'window_close':'cancelled','user_close':'cancelled'}.get(reason,'failed')
        record(root,run,'finish',status=status)
    return {}

def root_for_payload(payload: dict, platform: str) -> Path:
    # Native hooks may run from the IDE process directory rather than a
    # workspace directory. Prefer the caller-reported active workspace.
    if platform == 'codex':
        cwd=payload.get('cwd')
        if isinstance(cwd,str) and cwd and Path(cwd).is_dir():
            return Path(cwd).resolve()
    elif platform == 'antigravity':
        paths=payload.get('workspacePaths')
        if isinstance(paths,list) and len(paths)==1 and isinstance(paths[0],str):
            candidate=Path(paths[0])
            if candidate.is_dir():
                return candidate.resolve()
    # User-level hooks may execute from the IDE/global configuration folder.
    # Missing/ambiguous workspace does not authorize telemetry in process.cwd().
    return None

def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('platform',choices=['codex','antigravity','cursor'])
    parser.add_argument('event',nargs='?',default='')
    parser.add_argument('--root',type=Path,default=None)
    args=parser.parse_args(argv)
    try:
        payload=json.load(sys.stdin)
        if not isinstance(payload,dict):
            return 0
        if args.platform=='cursor' and args.root is None:
            # Global Cursor hooks execute in ~/.cursor, not the workspace.
            # Only project-local installs may authorize telemetry.
            return 0
        root=args.root if args.root is not None else root_for_payload(payload,args.platform)
        if root is None:
            return 0
        result=(handle_codex(payload,root) if args.platform=='codex' else
                handle_cursor(payload,root,args.event) if args.platform=='cursor' else
                handle_antigravity(payload,root,args.event))
        if result:
            print(json.dumps(result,separators=(',',':')))
    except (EventError,OSError,TypeError,ValueError,KeyError) as exc:
        # Hooks should never block user work. No stdin/prompt content in logs.
        print(f'auto-orchestrator hook warning: {type(exc).__name__}',file=sys.stderr)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
