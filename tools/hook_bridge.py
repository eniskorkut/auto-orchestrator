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
import sys
from pathlib import Path
try:
    from tools.perf import EventError, append_event, load_events, path_for
except ModuleNotFoundError:
    from perf import EventError, append_event, load_events, path_for

POLICY = ('Auto-Orchestrator: for non-trivial coding tasks read '
          '.agents/skills/auto-orchestrator/SKILL.md. Delegate independent work '
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


def handle_codex(payload: dict, root: Path) -> dict:
    event=payload.get('hook_event_name')
    session=payload.get('session_id')
    turn=payload.get('turn_id')
    if not session or not isinstance(session,str):
        return {}
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


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('platform',choices=['codex','antigravity'])
    parser.add_argument('event',nargs='?',default='')
    parser.add_argument('--root',type=Path,default=Path.cwd())
    args=parser.parse_args(argv)
    try:
        payload=json.load(sys.stdin)
        if not isinstance(payload,dict):
            return 0
        result=(handle_codex(payload,args.root) if args.platform=='codex' else
                handle_antigravity(payload,args.root,args.event))
        if result:
            print(json.dumps(result,separators=(',',':')))
    except (EventError,OSError,TypeError,ValueError,KeyError) as exc:
        # Hooks should never block user work. No stdin/prompt content in logs.
        print(f'auto-orchestrator hook warning: {type(exc).__name__}',file=sys.stderr)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
