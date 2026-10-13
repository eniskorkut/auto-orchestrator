#!/usr/bin/env python3
"""Offline readiness check. Does not pretend to run an IDE or subagent."""
import argparse
import json
from pathlib import Path
CHECKS={
'codex':['.codex/skills/auto-orchestrator/SKILL.md','.codex/AGENTS.md','.codex/agents/auto_explorer.toml'],
'opencode':['.config/opencode/skills/auto-orchestrator/SKILL.md','.config/opencode/AGENTS.md','.config/opencode/plugins/auto_orchestrator.js'],
'antigravity':['.gemini/config/skills/auto-orchestrator/SKILL.md','.gemini/config/rules/auto-orchestrator.md']}
def check(home,platform):
    selected=CHECKS if platform=='all' else {platform:CHECKS[platform]}
    return {p:{item:(home/item).is_file() for item in paths} for p,paths in selected.items()}
def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--home',type=Path,default=Path.home())
    ap.add_argument('--platform',choices=['all',*CHECKS],default='all')
    a=ap.parse_args(argv)
    result=check(a.home,a.platform)
    print(json.dumps(result,indent=2))
    ok=all(all(v.values()) for v in result.values())
    print('READY FOR MANUAL IDE SMOKE TEST' if ok else 'MISSING INSTALLED FILES')
    print('Does not prove live delegation, actual model, or benchmark speedup.')
    return 0 if ok else 1
if __name__=='__main__':raise SystemExit(main())
