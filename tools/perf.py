#!/usr/bin/env python3
"""Privacy-preserving subagent wall-time event recorder and qualified report."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')
EVENTS = {'start','agent-start','agent-stop','finish'}

class EventError(ValueError):
    pass

@dataclass
class Summary:
    run_id: str
    platform: str
    status: str
    verified: bool
    elapsed_seconds: float | None
    subagents: int
    summed_agent_seconds: float | None
    estimated_parallelism_seconds: float | None
    has_valid_estimate: bool
    reason: str


def checked_id(value: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise EventError('IDs must use only letters, numbers, dot, hyphen, underscore (max 80 chars).')
    return value


def path_for(root: Path, run_id: str) -> Path:
    return root / '.auto-orchestrator' / 'runs' / (checked_id(run_id) + '.jsonl')


def load_events(path: Path) -> list[dict]:
    if not path.is_file():
        raise EventError(f'Run not found: {path.name}')
    try:
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    except (ValueError, OSError) as exc:
        raise EventError(f'Malformed event log: {exc}') from exc


def append_event(root: Path, run_id: str, event: str, *, platform: str | None = None, agent_id: str | None = None,
                 status: str | None = None, verified: bool = False, at: float | None = None) -> None:
    if event not in EVENTS:
        raise EventError('Unsupported event')
    if agent_id is not None:
        checked_id(agent_id)
    path = path_for(root, run_id)
    now = time.time() if at is None else float(at)
    if not (0 <= now < 1e12):
        raise EventError('Invalid timestamp')
    payload = {'event':event,'time':now}
    if event == 'start':
        if platform not in ('codex','opencode','antigravity'):
            raise EventError('Platform must be codex, opencode, or antigravity')
        payload['platform'] = platform
    elif event in ('agent-start','agent-stop'):
        if not agent_id:
            raise EventError('Agent ID required')
        payload['agent_id'] = agent_id
    elif event == 'finish':
        if status not in ('success','failed','cancelled'):
            raise EventError('Finish status must be success, failed or cancelled')
        if verified and status != 'success':
            raise EventError('Only successful tasks may be verified')
        payload['status'] = status
        payload['verified'] = bool(verified)
    if event != 'start' and not path.exists():
        raise EventError('Start the run first')
    if event == 'start' and path.exists():
        raise EventError('Run ID already exists; choose a fresh run ID')
    if event != 'start' and any(x['event']=='finish' for x in load_events(path)):
        raise EventError('Run already finished')
    path.parent.mkdir(parents=True,exist_ok=True)
    # Each process performs one small append; O_APPEND prevents clobbering concurrent events.
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if event == 'start': flags |= os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (json.dumps(payload,separators=(',',':'))+'\n').encode('utf-8'))
    finally:
        os.close(fd)


def analyze(run_id: str, events: list[dict]) -> Summary:
    checked_id(run_id)
    if not events or events[0].get('event')!='start' or sum(x.get('event')=='start' for x in events)!=1:
        raise EventError('Exactly one start event must come first')
    platform=events[0].get('platform','unknown')
    start=float(events[0]['time'])
    started={}
    spans=[]
    finished=None
    issue=None
    for e in events[1:]:
        name=e.get('event')
        t=float(e['time'])
        if t < start:
            issue='An event occurred before the task start'
        if finished is not None:
            issue='Events occurred after task completion'
        if name=='agent-start':
            aid=e['agent_id']
            if aid in started or any(x[0]==aid for x in spans):
                issue='Duplicate agent ID'
            else:
                started[aid]=t
        elif name=='agent-stop':
            aid=e['agent_id']
            if aid not in started:
                issue='Unmatched agent stop'
            else:
                begun=started.pop(aid)
                if t<begun: issue='Agent stopped before it started'
                spans.append((aid,begun,t))
        elif name=='finish':
            finished=e
        else:
            issue='Unknown event'
    verified = bool(finished and finished.get('verified'))
    status = finished.get('status','unknown') if finished else 'running'
    elapsed=float(finished['time'])-start if finished else None
    summed=sum(e-s for _,s,e in spans) if not issue else None
    if finished and elapsed is not None and elapsed<0:
        issue='Finish occurred before start'
    if started:
        issue='Missing agent stop events'
    if any(s<start or (finished and e>float(finished['time'])) for _,s,e in spans):
        issue='Agent intervals fall outside run interval'
    if len(spans)<2:
        issue=issue or 'Fewer than two completed subagents'
    if not verified or status!='success':
        issue=issue or 'Run not successful and verified'
    if not finished:
        issue=issue or 'Run not finished'
    est=max(0.0,summed-elapsed) if issue is None and summed is not None and elapsed is not None else None
    if est is not None and est<=0:
        issue='No positive estimated parallelism benefit'
    return Summary(run_id, platform, status, verified, elapsed, len(spans), summed, est,
                   issue is None, issue or 'Estimate based on agent overlaps; not a benchmark')


def fmt_seconds(seconds: float | None) -> str:
    if seconds is None: return 'unknown'
    return f'{seconds/60:.1f} min' if seconds>=60 else f'{seconds:.1f} sec'


def display(summary: Summary) -> str:
    base=f'Run: {summary.run_id} | {summary.platform} | {summary.status} | verified={summary.verified}\n'
    base+=f'Elapsed: {fmt_seconds(summary.elapsed_seconds)} | completed subagents: {summary.subagents}\n'
    if summary.has_valid_estimate:
        return base+(f'Estimated parallelism benefit: {fmt_seconds(summary.estimated_parallelism_seconds)} '
            '(not benchmarked human time saved)')
    return base+f'Estimated parallelism benefit: Not available ({summary.reason})'


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='.',type=Path,help='Project directory (default: current directory)')
    commands=parser.add_subparsers(dest='command',required=True)
    for cmd in ('start','agent-start','agent-stop','finish','report'):
        p=commands.add_parser(cmd)
        p.add_argument('--run-id',required=True)
        if cmd=='start':p.add_argument('--platform',required=True,choices=['codex','opencode','antigravity'])
        if cmd in ('agent-start','agent-stop'):p.add_argument('--agent-id',required=True)
        if cmd=='finish':
            p.add_argument('--status',choices=['success','failed','cancelled'],required=True)
            p.add_argument('--verified',action='store_true')
        if cmd!='report':p.add_argument('--time',type=float,help='Import externally measured Unix timestamp / deterministic test data')
        if cmd=='report':p.add_argument('--json',action='store_true')
    a=parser.parse_args(argv)
    try:
        if a.command=='report':
            summary=analyze(a.run_id,load_events(path_for(a.root,a.run_id)))
            print(json.dumps(asdict(summary),indent=2) if a.json else display(summary))
        else:
            append_event(a.root,a.run_id,a.command,platform=getattr(a,'platform',None),agent_id=getattr(a,'agent_id',None),
                         status=getattr(a,'status',None),verified=getattr(a,'verified',False),at=getattr(a,'time',None))
            print(f'Recorded {a.command} for {a.run_id}')
    except (OSError,EventError,KeyError,TypeError,ValueError) as exc:
        print(f'PERF ERROR: {exc}',file=sys.stderr)
        return 1
    return 0

if __name__=='__main__':
    raise SystemExit(main())
