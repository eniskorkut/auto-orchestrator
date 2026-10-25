#!/usr/bin/env python3
"""Install Auto-Orchestrator V2 to an existing project safely (stdlib only)."""
from __future__ import annotations

import argparse
import shutil
import sys
import os
import shlex
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = '<!-- auto-orchestrator:begin -->'
END = '<!-- auto-orchestrator:end -->'
GUIDANCE = '''<!-- auto-orchestrator:begin -->
## Auto-Orchestrator (installed)
For non-trivial development tasks, consult `.agents/skills/auto-orchestrator/SKILL.md` and use native subagents automatically if safe. Parallelize independent work with a default concurrency limit of three. Follow the skill's context, file-ownership and verification rules. Do not delegate trivial work, fabricate timing data or claim unverified time savings. If subagents are unavailable, complete serially and mention the limitation. Subagents inherit the active model unless Codex-specific configuration says otherwise.
<!-- auto-orchestrator:end -->'''
MODEL_SETTING = {
    'inherit': '[agents]\nenabled = true\nmax_concurrent_threads_per_session = 3\n',
    'luna-max': '[agents]\nenabled = true\nmax_concurrent_threads_per_session = 3\ndefault_subagent_model = "gpt-6-luna"\ndefault_subagent_reasoning_effort = "max"\n',
}

class Conflict(Exception):
    pass

def guard_dest(target:Path, dest:Path)->None:
    try:
        parts=dest.relative_to(target).parts
    except ValueError as exc:
        raise Conflict('Install path is outside target: '+str(dest)) from exc
    current=target
    for part in parts:
        current=current/part
        if current.is_symlink():
            raise Conflict('Symlink in destination path: '+str(current))
    if not dest.resolve(strict=False).is_relative_to(target.resolve()):
        raise Conflict('Install path resolves outside target: '+str(dest))

def shell_arg(value:Path)->str:
    value=str(value)
    return subprocess.list2cmdline([value]) if os.name=='nt' else shlex.quote(value)

def rendered(source:Path, target:Path)->str:
    content=source.read_text(encoding='utf-8')
    if source in (ROOT/'.codex/hooks.json', ROOT/'.agents/hooks.json', ROOT/'.cursor/hooks.json'):
        command=shell_arg(Path(sys.executable))+' '+shell_arg((target/'tools/hook_bridge.py').resolve())
        content=content.replace('python tools/hook_bridge.py',json.dumps(command)[1:-1])
    return content


def write_file(dest: Path, text: str, dry_run: bool, force: bool = False) -> str:
    if dest.is_file():
        old = dest.read_text(encoding='utf-8')
        if old == text:
            return 'unchanged'
        if not force:
            raise Conflict(f'Existing file differs: {dest}. No overwrite (use --force-generated for generated profiles).')
        if not dry_run:
            shutil.copy2(dest, dest.with_name(dest.name + '.bak'))
    elif dest.exists():
        raise Conflict(f'Not a normal file: {dest}')
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding='utf-8')
    return 'updated' if dest.exists() else 'created'


def patch_guidance(dest: Path, dry_run: bool) -> str:
    if dest.exists():
        old = dest.read_text(encoding='utf-8')
        if old.count(START) != old.count(END) or old.count(START) > 1:
            raise Conflict(f'Invalid managed markers in {dest}')
        if START in old:
            prefix = old[:old.index(START)]
            suffix = old[old.index(END) + len(END):]
            new = prefix + GUIDANCE + suffix
        else:
            new = old.rstrip() + '\n\n' + GUIDANCE + '\n'
    else:
        old = ''
        new = GUIDANCE + '\n'
    if old == new:
        return 'unchanged'
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(new, encoding='utf-8')
    return 'updated'


def patch_ignore(dest: Path, dry_run: bool) -> str:
    old = dest.read_text(encoding='utf-8') if dest.exists() else ''
    if '.auto-orchestrator/' in old.splitlines():
        return 'unchanged'
    new = old.rstrip() + '\n.auto-orchestrator/\n'
    if not dry_run:
        dest.write_text(new, encoding='utf-8')
    return 'updated'


def install(target: Path, platform: str, codex_model: str, dry_run: bool, force_generated: bool, opencode_version: str = 'v2') -> list[tuple[str,str]]:
    if not target.is_dir():
        raise Conflict(f'Target project directory does not exist: {target}')
    if (target / 'tools' / 'install.py').resolve() == Path(__file__).resolve():
        raise Conflict('Install into a separate target project (do not self-install).')
    selected = {'codex','opencode','antigravity','cursor'} if platform == 'all' else {platform}
    def destination(source: Path) -> Path:
        if source == ROOT/'adapters/opencode-v2/auto_orchestrator.js':
            return target/'.opencode/plugins/auto_orchestrator.js'
        return target/source.relative_to(ROOT)
    sources = [ROOT/'.agents/skills/auto-orchestrator/SKILL.md']
    sources.extend(sorted((ROOT/'.agents/skills/auto-orchestrator/references').glob('*.md')))
    sources += [ROOT/'tools/perf.py', ROOT/'tools/hook_bridge.py',
                ROOT/'tools/supervisor.py', ROOT/'tools/router.py', ROOT/'tools/router_eval.py', ROOT/'tools/worktree.py', ROOT/'tools/benchmark.py', ROOT/'tools/quality_gate.py', ROOT/'tools/review_gate.py',
                ROOT/'tools/dashboard.py', ROOT/'tools/project_registry.py',
                ROOT/'tools/event_stream.py', ROOT/'tools/event_model.py',
                ROOT/'tools/office_model.py', ROOT/'tools/capabilities.py',
                ROOT/'tools/telemetry.py', ROOT/'tools/project_planner.py',
                ROOT/'tools/executor.py', ROOT/'tools/teamwork.py',
                ROOT/'tools/teamwork_planner.py', ROOT/'tools/managed_exec.py',
                ROOT/'tools/teamwork_runner.py', ROOT/'tools/teamwork_benchmark.py', ROOT/'tools/teamwork_cli.py']
    sources.extend(sorted(p for p in (ROOT/'tools/dashboard_assets').rglob('*') if p.is_file()))
    if 'codex' in selected:
        sources.extend(sorted((ROOT/'.codex/agents').glob('*.toml')))
        sources.append(ROOT/'.codex/hooks.json')
    if 'opencode' in selected:
        sources.extend(sorted((ROOT/'.opencode/agents').glob('*.md')))
        sources.append(ROOT/('adapters/opencode-v2/auto_orchestrator.js' if opencode_version == 'v2' else '.opencode/plugins/auto_orchestrator.js'))
    if 'antigravity' in selected:
        sources.extend(sorted((ROOT/'.agents/agents').glob('*.md')))
        sources.append(ROOT/'.agents/hooks.json')
    if 'cursor' in selected:
        sources.extend(sorted((ROOT/'.cursor/agents').glob('*.md')))
        sources.append(ROOT/'.cursor/hooks.json')
    # Preflight all non-managed destinations, so no partial install on conflict.
    for source in sources:
        dest = destination(source)
        guard_dest(target,dest)
        if dest.exists() and not force_generated and dest.read_text(encoding='utf-8') != rendered(source,target):
            raise Conflict(f'Existing file differs: {dest}; nothing changed.')
    conf = target/'.codex/config.toml'
    guard_dest(target,conf)
    guard_dest(target,target/'AGENTS.md')
    guard_dest(target,target/'.gitignore')
    if 'codex' in selected and conf.exists():
        current = conf.read_text(encoding='utf-8')
        if current not in MODEL_SETTING.values():
            raise Conflict(f'Existing Codex config: {conf}. Edit [agents] manually, preserving your settings; nothing changed.')
    # Check AGENTS marker structure before mutations.
    g = target/'AGENTS.md'
    if g.exists():
        s=g.read_text(encoding='utf-8')
        if s.count(START)!=s.count(END) or s.count(START)>1:
            raise Conflict(f'Invalid markers in {g}; nothing changed.')
    results=[]
    for source in sources:
        dest=destination(source)
        results.append((dest.relative_to(target).as_posix(),write_file(dest,rendered(source,target),dry_run,force_generated)))
    if 'codex' in selected:
        results.append(('.codex/config.toml',write_file(conf,MODEL_SETTING[codex_model],dry_run,force=True)))
    results.append(('AGENTS.md',patch_guidance(g,dry_run)))
    results.append(('.gitignore',patch_ignore(target/'.gitignore',dry_run)))
    return results


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',required=True,type=Path)
    p.add_argument('--platform',choices=['all','codex','opencode','antigravity','cursor'],default='all')
    p.add_argument('--codex-model',choices=['inherit','luna-max'],default='inherit')
    p.add_argument('--opencode-version',choices=['v1','v2'],default='v2')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--force-generated',action='store_true',help='Back up and replace generated profiles/skill files; not existing Codex config')
    args=p.parse_args(argv)
    try:
        for path,state in install(args.target.resolve(),args.platform,args.codex_model,args.dry_run,args.force_generated,args.opencode_version):
            print(f'{state:9s} {path}')
    except (Conflict,OSError) as e:
        print(f'INSTALL ERROR: {e}',file=sys.stderr)
        return 1
    print('DRY RUN — no files written' if args.dry_run else 'Installed. Restart your client/session if it does not discover the skill.')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
