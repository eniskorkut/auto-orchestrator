#!/usr/bin/env python3
"""Compare measured serial vs orchestrated runs; never invent a baseline."""
from __future__ import annotations
import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

class BenchmarkError(ValueError): pass

def compare(data: list[dict], minimum: int=3)->list[dict]:
    groups=defaultdict(lambda:{'serial':[],'orchestrated':[]})
    for row in data:
        try:
            task,commit,model,mode=row['task'],row['commit'],row['model'],row['mode']
            elapsed=float(row['elapsed_seconds'])
            status=row['status']
            verified=row['verified']
        except (KeyError,TypeError,ValueError) as exc:
            raise BenchmarkError('Every observation needs task, commit, model, mode, elapsed_seconds, status, verified') from exc
        if not all(isinstance(x,str) and x for x in (task,commit,model)):
            raise BenchmarkError('task/commit/model must be nonempty strings')
        if mode not in ('serial','orchestrated') or status not in ('success','failed','cancelled'):
            raise BenchmarkError('Unsupported mode or status')
        if not math.isfinite(elapsed) or elapsed<=0 or not isinstance(verified,bool):
            raise BenchmarkError('Invalid duration or verified flag')
        if verified and status!='success':raise BenchmarkError('Failed runs cannot be verified')
        groups[(task,commit,model)][mode].append((elapsed,status=='success' and verified))
    reports=[]
    for (task,commit,model),modes in sorted(groups.items()):
        a=modes['serial'];b=modes['orchestrated']
        sa=[t for t,ok in a if ok];sb=[t for t,ok in b if ok]
        result={'task':task,'commit':commit,'model':model,'serial_runs':len(a),
                'orchestrated_runs':len(b),'serial_verified':len(sa),'orchestrated_verified':len(sb),
                'serial_success_rate':len(sa)/len(a) if a else None,
                'orchestrated_success_rate':len(sb)/len(b) if b else None}
        if len(a)<minimum or len(b)<minimum or len(sa)<minimum or len(sb)<minimum:
            result.update({'comparison':'insufficient matched verified repeats','seconds_saved':None,'speedup_percent':None})
        else:
            x=statistics.median(sa); y=statistics.median(sb)
            result.update({'comparison':'measured matched runs; not human labor savings',
                           'serial_median_seconds':x,'orchestrated_median_seconds':y,
                           'seconds_saved':round(x-y,2),
                           'speedup_percent':round(100*(x-y)/x,2)})
        reports.append(result)
    return reports

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('input',type=Path)
    p.add_argument('--minimum',type=int,default=3)
    args=p.parse_args(argv)
    try:
        if args.minimum<2:raise BenchmarkError('Minimum must be at least 2')
        observations=json.loads(args.input.read_text(encoding='utf-8'))
        if not isinstance(observations,list):raise BenchmarkError('JSON list required')
        print(json.dumps(compare(observations,args.minimum),indent=2))
    except (OSError,ValueError) as exc:
        p.exit(1,'BENCHMARK ERROR: '+str(exc)+'\n')
    return 0
if __name__=='__main__':raise SystemExit(main())
