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
            status=row['status']
            verified=row['verified']
            elapsed=(None if status=='incomplete' else float(row['elapsed_seconds']))
        except (KeyError,TypeError,ValueError) as exc:
            raise BenchmarkError('Every observation needs task, commit, model, mode, status, verified; terminal runs also need elapsed_seconds') from exc
        if not all(isinstance(x,str) and x for x in (task,commit,model)):
            raise BenchmarkError('task/commit/model must be nonempty strings')
        if mode not in ('serial','orchestrated') or status not in ('success','failed','cancelled','incomplete'):
            raise BenchmarkError('Unsupported mode or status')
        if (elapsed is not None and (not math.isfinite(elapsed) or elapsed<=0)) or not isinstance(verified,bool):
            raise BenchmarkError('Invalid duration or verified flag')
        if status=='incomplete' and (verified or row.get('elapsed_seconds') is not None):
            raise BenchmarkError('Incomplete runs must not claim verification or elapsed duration')
        if verified and status!='success':raise BenchmarkError('Failed runs cannot be verified')
        groups[(task,commit,model)][mode].append((elapsed,status=='success' and verified,status))
    reports=[]
    for (task,commit,model),modes in sorted(groups.items()):
        a=modes['serial'];b=modes['orchestrated']
        sa=[t for t,ok,_ in a if ok];sb=[t for t,ok,_ in b if ok]
        result={'task':task,'commit':commit,'model':model,'serial_runs':len(a),
                'orchestrated_runs':len(b),'serial_verified':len(sa),'orchestrated_verified':len(sb),
                'serial_success_rate':len(sa)/len(a) if a else None,
                'orchestrated_success_rate':len(sb)/len(b) if b else None,
                'serial_incomplete':sum(1 for _,_,status in a if status=='incomplete'),
                'orchestrated_incomplete':sum(1 for _,_,status in b if status=='incomplete')}
        if (len(a)<minimum or len(b)<minimum or len(sa)<minimum or len(sb)<minimum
                or len(sa)!=len(a) or len(sb)!=len(b)):
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
