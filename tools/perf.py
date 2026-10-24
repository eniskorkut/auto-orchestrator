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
EVENTS = {'start','route','agent-start','agent-stop','check','mutation','verified','finish','diagnostic'}
DIAGNOSTIC_REASONS = {'session_ownership_lost','session_deleted_without_terminal',
                      'event_stream_closed','event_stream_error','plugin_unloaded'}

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
    # Observed native tool span overlap, not saved wall-clock time. Background
    # tool-return spans might not represent the native child's full lifetime.
    observed_overlap_seconds: float | None = None
    route_latency_ms: float | None = None
    suggested_delegation: str | None = None
    suggested_max_agents: int | None = None
    observed_native_starts: int = 0
    observed_max_native_concurrency: int = 0
    routing_advice_followed: bool | None = None


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
                 status: str | None = None, verified: bool = False, at: float | None = None,
                 check_id: str | None = None, exit_code: int | None = None,
                 source: str | None = None, policy_hash: str | None = None) -> None:
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
        if platform not in ('codex','opencode','antigravity','cursor'):
            raise EventError('Platform must be codex, opencode, antigravity, or cursor')
        payload['platform'] = platform
    elif event in ('agent-start','agent-stop'):
        if not agent_id:
            raise EventError('Agent ID required')
        payload['agent_id'] = agent_id
    elif event == 'check':
        if not check_id or not SAFE_ID.fullmatch(check_id) or type(exit_code) is not int or source != 'native-tool':
            raise EventError('Check requires ID, native source and integer exit code')
        payload.update(check_id=check_id,exit_code=exit_code,source=source)
        if policy_hash:
            payload['policy_hash']=policy_hash
    elif event == 'mutation':
        pass
    elif event == 'diagnostic':
        if source not in DIAGNOSTIC_REASONS:
            raise EventError('Diagnostic requires a supported reason code')
        payload['reason'] = source
    elif event == 'verified':
        if source == 'quality-gate':
            payload['source']='quality-gate'
        elif source is not None:
            raise EventError('Unsupported verification source')
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
    if event != 'start':
        existing=load_events(path)
        finished=next((x for x in existing if x['event']=='finish'),None)
        # With V5 explicitly enabled, legacy manual flags must not bypass
        # native evidence checks by claiming verification before finish.
        if event in ('verified','finish'):
            try:
                from tools.quality_gate import load_policy
            except ModuleNotFoundError:
                from quality_gate import load_policy
            if existing[0].get('platform')=='opencode' and load_policy(root) is not None:
                if (event=='verified' and not finished) or (event=='finish' and verified):
                    raise EventError('V5 requires native evidence validation after execution finish')
        if finished:
            if event!='verified' or source!='quality-gate' or finished.get('status')!='success':
                raise EventError('Run already finished')
            if any(x['event']=='verified' for x in existing):
                raise EventError('Run already verified')
            try:
                from tools.quality_gate import assess, load_policy
            except ModuleNotFoundError:
                from quality_gate import assess, load_policy
            decision=assess(existing,load_policy(root))
            if not decision['verified']:
                raise EventError('Evidence gate rejected verification: '+decision['reason'])
            payload['check_ids']=decision['check_ids']
    path.parent.mkdir(parents=True,exist_ok=True)
    # Each process performs one small append; O_APPEND prevents clobbering concurrent events.
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if event == 'start': flags |= os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (json.dumps(payload,separators=(',',':'))+'\n').encode('utf-8'))
    finally:
        os.close(fd)


def observed_overlap(spans: list[tuple[str,float,float]]) -> float:
    """Duration counted more than once across overlapping native tool spans."""
    if len(spans) < 2:
        return 0.0
    intervals=sorted((start,end) for _,start,end in spans)
    started,ended=intervals[0]
    union=0.0
    for a,b in intervals[1:]:
        if a>ended:
            union+=ended-started
            started,ended=a,b
        else:
            ended=max(ended,b)
    union+=ended-started
    return max(0.0,sum(b-a for _,a,b in spans)-union)


def analyze(run_id: str, events: list[dict], policy: dict | None = None) -> Summary:
    checked_id(run_id)
    if not events or events[0].get('event')!='start' or sum(x.get('event')=='start' for x in events)!=1:
        raise EventError('Exactly one start event must come first')
    platform=events[0].get('platform','unknown')
    start=float(events[0]['time'])
    started={}
    spans=[]
    finished=None
    issue=None
    verification_events=[]
    route_meta=None
    route_latency=None
    native_starts=0
    max_concurrency=0
    for e in events[1:]:
        name=e.get('event')
        t=float(e['time'])
        if t < start:
            issue='An event occurred before the task start'
        if finished is not None and name != 'verified':
            issue='Unexpected event after execution finish'
        if name=='agent-start':
            aid=e['agent_id']
            if aid in started or any(x[0]==aid for x in spans):
                issue='Duplicate agent ID'
            else:
                started[aid]=t
                native_starts+=1
                max_concurrency=max(max_concurrency,len(started))
        elif name=='agent-stop':
            aid=e['agent_id']
            if aid not in started:
                issue='Unmatched agent stop'
            else:
                begun=started.pop(aid)
                if t<begun: issue='Agent stopped before it started'
                spans.append((aid,begun,t))
        elif name=='verified':
            verification_events.append(e)
            if len(verification_events)>1:
                issue='Duplicate verification'
            if finished is not None and e.get('source')!='quality-gate':
                issue='Untrusted late verification'
            if finished is not None and not (
                isinstance(e.get('check_ids'),list) and e['check_ids'] and
                all(isinstance(x,str) for x in e['check_ids'])
            ):
                issue='Late verification missing gate evidence'
        elif name=='route':
            # Route events report advice, not authoritative dispatch decisions.
            # The latest admitted prompt within the run takes precedence.
            mode=e.get('delegation')
            limit=e.get('max_agents')
            latency=e.get('latency_ms')
            if mode in ('single','consider-parallel') and type(limit) is int and 1<=limit<=3:
                route_meta=(mode,limit)
                route_latency=(latency if type(latency) in (int,float) and
                               0 <= latency <= 60000 else None)
        elif name in ('check','mutation','diagnostic'):
            pass
        elif name=='finish':
            finished=e
        else:
            issue='Unknown event'
    verified = bool(finished and (finished.get('verified') or verification_events))
    if platform=='opencode' and policy is not None and finished and finished.get('verified'):
        issue=issue or 'V5 policy rejects legacy unproven verification flags'
    for proof in verification_events:
        if proof.get('source') == 'quality-gate':
            try:
                from tools.quality_gate import assess
            except ModuleNotFoundError:
                from quality_gate import assess
            decision=assess(events, policy)
            if not decision['verified'] or proof.get('check_ids')!=decision.get('check_ids'):
                issue=issue or 'Invalid or stale automatic verification evidence'
        elif finished is not None and events.index(proof)>events.index(finished):
            issue=issue or 'Late verification lacks trusted evidence'
    if issue:
        verified = False
    status = finished.get('status','unknown') if finished else 'running'
    elapsed=float(finished['time'])-start if finished else None
    summed=sum(e-s for _,s,e in spans) if not issue else None
    if finished and elapsed is not None and elapsed<0:
        issue='Finish occurred before start'
    if started:
        issue='Missing agent stop events'
    if any(s<start or (finished and e>float(finished['time'])) for _,s,e in spans):
        issue='Agent intervals fall outside run interval'
    if issue:
        verified=False
    if len(spans)<2:
        issue=issue or 'Fewer than two completed subagents'
    if not verified or status!='success':
        issue=issue or 'Run not successful and verified'
    if not finished:
        issue=issue or 'Run not finished'
    # Overlap is a separate descriptive measure: it may be positive even when
    # sum(agent durations) - entire parent duration is zero/negative.
    # Never compute it for unverified, malformed, or unfinished executions.
    observed = observed_overlap(spans) if issue is None and len(spans)>=2 else None
    est=max(0.0,summed-elapsed) if issue is None and summed is not None and elapsed is not None else None
    if est is not None and est<=0:
        issue='No positive estimated parallelism benefit'
    adherence=None
    if route_meta:
        # A single-agent recommendation excludes child subagents. Parallel
        # advice only caps observed concurrent native subagents, not total calls.
        adherence=(native_starts==0 if route_meta[0]=='single'
                   else max_concurrency<=route_meta[1])
    return Summary(run_id, platform, status, verified, elapsed, len(spans), summed, est,
                   issue is None, issue or 'Estimate based on agent overlaps; not a benchmark',
                   observed, route_latency,
                   route_meta[0] if route_meta else None,
                   route_meta[1] if route_meta else None,
                   native_starts, max_concurrency, adherence)


def diagnose(run_id: str, events: list[dict]) -> dict:
    """Classify observed run evidence, not the current state of an IDE process."""
    checked_id(run_id)
    if not events or events[0].get('event') != 'start':
        raise EventError('Run start not present')
    starts: set[str] = set()
    terminal = None
    reasons: list[str] = []
    for event in events:
        kind = event.get('event')
        if kind == 'agent-start':
            starts.add(event['agent_id'])
        elif kind == 'agent-stop':
            starts.discard(event['agent_id'])
        elif kind == 'diagnostic' and event.get('reason') in DIAGNOSTIC_REASONS:
            reasons.append(event['reason'])
        elif kind == 'finish':
            terminal = event.get('status')
    if terminal:
        category = 'terminal_recorded'
    elif starts:
        category = 'missing_terminal_open_native_spans'
    elif any(x.startswith('event_stream_') for x in reasons):
        category = 'missing_terminal_subscription_interrupted'
    elif 'session_ownership_lost' in reasons:
        category = 'missing_terminal_session_ownership_lost'
    elif 'plugin_unloaded' in reasons:
        category = 'missing_terminal_plugin_unloaded'
    else:
        category = 'missing_terminal_reason_unknown'
    return {'run_id':run_id,'classification':category,'finished':terminal is not None,
            'terminal_status':terminal,'open_native_spans':len(starts),
            'diagnostics':sorted(set(reasons)),
            'last_event_time':max(float(e['time']) for e in events),
            'note':'No terminal event cannot prove the native execution ended.'}


def fmt_seconds(seconds: float | None) -> str:
    if seconds is None: return 'unknown'
    return f'{seconds/60:.1f} min' if seconds>=60 else f'{seconds:.1f} sec'


def display(summary: Summary) -> str:
    base=f'Run: {summary.run_id} | {summary.platform} | {summary.status} | verified={summary.verified}\n'
    base+=f'Elapsed: {fmt_seconds(summary.elapsed_seconds)} | completed subagents: {summary.subagents}\n'
    if summary.observed_overlap_seconds is not None:
        base+=f'Observed native tool span overlap: {fmt_seconds(summary.observed_overlap_seconds)} (NOT actual time saved)\n'
    if summary.has_valid_estimate:
        return base+(f'Estimated parallelism benefit: {fmt_seconds(summary.estimated_parallelism_seconds)} '
            '(not benchmarked human time saved)')
    return base+f'Estimated parallelism benefit: Not available ({summary.reason})'


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='.',type=Path,help='Project directory (default: current directory)')
    commands=parser.add_subparsers(dest='command',required=True)
    for cmd in ('start','agent-start','agent-stop','verified','finish','report','diagnose','audit'):
        p=commands.add_parser(cmd)
        p.add_argument('--run-id')
        if cmd in ('verified','report','diagnose'): p.add_argument('--latest',action='store_true',help='Use most recent locally recorded run')
        if cmd=='start':p.add_argument('--platform',required=True,choices=['codex','opencode','antigravity'])
        if cmd in ('agent-start','agent-stop'):p.add_argument('--agent-id',required=True)
        if cmd=='finish':
            p.add_argument('--status',choices=['success','failed','cancelled'],required=True)
            p.add_argument('--verified',action='store_true')
        if cmd!='report':p.add_argument('--time',type=float,help='Import externally measured Unix timestamp / deterministic test data')
        if cmd in ('report','diagnose','audit'):p.add_argument('--json',action='store_true')
    a=parser.parse_args(argv)
    try:
        if a.command=='audit':
            run_dir=a.root/'.auto-orchestrator/runs'
            paths=sorted((p for p in run_dir.glob('*.jsonl') if p.is_file() and not p.is_symlink()),
                         key=lambda p: (p.stat().st_mtime_ns,p.name))[-500:]
            results=[diagnose(p.stem,load_events(p)) for p in paths]
            counts={}
            for item in results:
                key=item['classification']
                counts[key]=counts.get(key,0)+1
            output={'run_count':len(results),'categories':counts,'runs':results}
            print(json.dumps(output,indent=2) if a.json else
                  '\n'.join(f"{item['run_id']}: {item['classification']}" for item in results))
            return 0
        if getattr(a,'latest',False):
            if a.run_id:
                raise EventError('Choose either --run-id or --latest')
            paths = sorted((a.root/'.auto-orchestrator/runs').glob('*.jsonl'), key=lambda p: (p.stat().st_mtime_ns, p.name))
            if not paths:
                raise EventError('No recorded run')
            a.run_id = paths[-1].stem
        if not a.run_id:
            raise EventError('Supply --run-id or --latest')
        if a.command=='diagnose':
            result=diagnose(a.run_id,load_events(path_for(a.root,a.run_id)))
            print(json.dumps(result,indent=2) if a.json else
                  f"{result['classification']}; open native spans: {result['open_native_spans']}; "
                  f"diagnostics: {', '.join(result['diagnostics']) or 'none'}")
        elif a.command=='report':
            try:
                from tools.quality_gate import load_policy
            except ModuleNotFoundError:
                from quality_gate import load_policy
            summary=analyze(a.run_id,load_events(path_for(a.root,a.run_id)),load_policy(a.root))
            print(json.dumps(asdict(summary),indent=2) if a.json else display(summary))
        else:
            append_event(a.root,a.run_id,a.command,platform=getattr(a,'platform',None),agent_id=getattr(a,'agent_id',None),
                         status=getattr(a,'status',None),verified=getattr(a,'verified',False),at=getattr(a,'time',None),
                         source='quality-gate' if a.command=='verified' and
                         any(x.get('event')=='finish' for x in load_events(path_for(a.root,a.run_id))) else None)
            print(f'Recorded {a.command} for {a.run_id}')
    except (OSError,EventError,KeyError,TypeError,ValueError) as exc:
        print(f'PERF ERROR: {exc}',file=sys.stderr)
        return 1
    return 0

if __name__=='__main__':
    raise SystemExit(main())
