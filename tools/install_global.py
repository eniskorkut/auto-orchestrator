#!/usr/bin/env python3
"""Safe per-user installation for Codex, OpenCode V1/V2, Antigravity IDE/CLI."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import os
import shlex
import subprocess
import tomllib
from pathlib import Path
try:
    from tools.hook_integration import HookIntegrationError, merge as merge_hooks, remove as remove_hooks, parse_hooks, pretty
except ModuleNotFoundError:
    from hook_integration import HookIntegrationError, merge as merge_hooks, remove as remove_hooks, parse_hooks, pretty

ROOT = Path(__file__).resolve().parents[1]
BEGIN = '<!-- auto-orchestrator:begin -->'
END = '<!-- auto-orchestrator:end -->'
GUIDANCE = """<!-- auto-orchestrator:begin -->
For non-trivial coding tasks consult the installed auto-orchestrator skill. Delegate independent work to native subagents automatically (max three), protect shared files, and verify integrated results. Trivial tasks use one agent. Never invent model execution, tests, or human time savings.
<!-- auto-orchestrator:end -->"""
MANIFEST = 'auto-orchestrator-global-manifest.json'
V71_MODULES = ('project_registry.py', 'event_stream.py', 'event_model.py',
               'office_model.py', 'capabilities.py', 'telemetry.py',
               'project_planner.py', 'executor.py')
V73_MODULES = ('teamwork.py', 'teamwork_planner.py', 'managed_exec.py',
               'teamwork_runner.py', 'teamwork_benchmark.py',
               'teamwork_cli.py', 'teamwork_chat.py', 'scheduler.py', 'run_owner.py',
               'repo_profile.py')

class InstallError(ValueError):
    pass

def guard_path(home:Path, path:Path)->None:
    """Refuse symlink traversal or mutations outside the requested home."""
    home=home.resolve()
    try:
        parts=path.relative_to(home).parts
    except ValueError as exc:
        raise InstallError('Managed path escapes home: '+str(path)) from exc
    current=home
    for part in parts:
        current=current/part
        if current.is_symlink():
            raise InstallError('Symlink in managed path: '+str(current))
    if not path.resolve(strict=False).is_relative_to(home):
        raise InstallError('Managed path resolves outside home: '+str(path))

def shell_arg(value:Path)->str:
    value=str(value)
    return subprocess.list2cmdline([value]) if os.name=='nt' else shlex.quote(value)

def sha(data:bytes)->str:
    return hashlib.sha256(data).hexdigest()

def skill_files()->dict[str,bytes]:
    root=ROOT/'.agents/skills/auto-orchestrator'
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.is_symlink() and not p.parent.is_symlink()}

def dashboard_files()->dict[str,bytes]:
    """Read the read-only dashboard runtime and its static assets."""
    tools=ROOT/'tools'
    assets=tools/'dashboard_assets'
    result={'dashboard.py':(tools/'dashboard.py').read_bytes()}
    for source in sorted(assets.rglob('*')):
        if source.is_file() and not source.is_symlink():
            result['dashboard_assets/'+source.relative_to(assets).as_posix()]=source.read_bytes()
    return result

def installed_map(home:Path,platforms:set[str],opencode_version:str,codex_model:str)->dict[Path,bytes]:
    files={}
    skills=skill_files()
    def skills_at(base):
        for path,content in skills.items(): files[base/path]=content
    dashboard=dashboard_files()
    def dashboard_at(base):
        for path,content in dashboard.items(): files[base/path]=content
    if 'codex' in platforms:
        codex=home/'.codex'
        skills_at(codex/'skills/auto-orchestrator')
        for source in (ROOT/'.codex/agents').glob('*.toml'):
            files[codex/'agents'/source.name]=source.read_bytes()
        # Global runtime adapters. Existing hook files belong to the user and are left untouched.
        bridge=codex/'auto-orchestrator/tools/hook_bridge.py'
        files[bridge]=(ROOT/'tools/hook_bridge.py').read_bytes()
        files[codex/'auto-orchestrator/tools/perf.py']=(ROOT/'tools/perf.py').read_bytes()
        for name in ('supervisor.py','router.py','router_eval.py','worktree.py','benchmark.py','quality_gate.py','review_gate.py'):
            files[codex/'auto-orchestrator/tools'/name]=(ROOT/'tools'/name).read_bytes()
        dashboard_at(codex/'auto-orchestrator/tools')
        for name in V71_MODULES + V73_MODULES:
            files[codex/'auto-orchestrator/tools'/name]=(ROOT/'tools'/name).read_bytes()
        hooks_path=codex/'hooks.json'
        if not hooks_path.exists():
            hooks=json.loads((ROOT/'.codex/hooks.json').read_text(encoding='utf-8'))
            for group in hooks['hooks'].values():
                for item in group:
                    for h in item.get('hooks',[]):
                        h['command']=h['command'].replace('python tools/hook_bridge.py',shell_arg(Path(sys.executable))+' '+shell_arg(bridge))
            files[hooks_path]=(json.dumps(hooks,indent=2)+'\n').encode('utf-8')
        config=codex/'config.toml'
        if not config.exists():
            lines=['[agents]','enabled = true','max_concurrent_threads_per_session = 3']
            if codex_model=='luna-max':
                lines.extend(['default_subagent_model = "gpt-6-luna"','default_subagent_reasoning_effort = "max"'])
            files[config]=('\n'.join(lines)+'\n').encode()
        elif codex_model=='luna-max':
            old=config.read_text(encoding='utf-8')
            if 'default_subagent_model = "gpt-6-luna"' not in old or 'default_subagent_reasoning_effort = "max"' not in old:
                raise InstallError('Existing Codex config requires manual Luna Max settings; refusing overwrite.')
    if 'opencode' in platforms:
        oc=home/'.config/opencode'
        skills_at(oc/'skills/auto-orchestrator')
        for source in (ROOT/'.opencode/agents').glob('*.md'):
            content=source.read_text(encoding='utf-8')
            if opencode_version=='v2':
                content=content.replace('permission:\n  edit: deny','permissions:\n  - action: edit\n    resource: "*"\n    effect: deny')
            files[oc/'agents'/source.name]=content.encode()
        commands = ROOT/'.opencode/commands'
        if commands.is_dir():
            for source in commands.glob('*.md'):
                files[oc/'commands'/source.name]=source.read_bytes()
        plugin=ROOT/('adapters/opencode-v2/auto_orchestrator.js' if opencode_version=='v2' else '.opencode/plugins/auto_orchestrator.js')
        files[oc/'plugins/auto_orchestrator.js']=plugin.read_bytes()
        files[oc/'auto-orchestrator/tools/perf.py']=(ROOT/'tools/perf.py').read_bytes()
        for name in ('supervisor.py','router.py','router_eval.py','worktree.py','benchmark.py','quality_gate.py','review_gate.py'):
            files[oc/'auto-orchestrator/tools'/name]=(ROOT/'tools'/name).read_bytes()
        dashboard_at(oc/'auto-orchestrator/tools')
        for name in V71_MODULES + V73_MODULES:
            files[oc/'auto-orchestrator/tools'/name]=(ROOT/'tools'/name).read_bytes()
    if 'antigravity' in platforms:
        gemini=home/'.gemini'
        skills_at(gemini/'config/skills/auto-orchestrator')
        skills_at(gemini/'antigravity-cli/skills/auto-orchestrator')
        for source in (ROOT/'.agents/agents').glob('*.md'):
            files[gemini/'config/agents'/source.stem/'agent.md']=source.read_bytes()
        bridge=gemini/'config/auto-orchestrator/tools/hook_bridge.py'
        files[bridge]=(ROOT/'tools/hook_bridge.py').read_bytes()
        files[gemini/'config/auto-orchestrator/tools/perf.py']=(ROOT/'tools/perf.py').read_bytes()
        for name in ('supervisor.py','router.py','router_eval.py','worktree.py','benchmark.py','quality_gate.py','review_gate.py'):
            files[gemini/'config/auto-orchestrator/tools'/name]=(ROOT/'tools'/name).read_bytes()
        dashboard_at(gemini/'config/auto-orchestrator/tools')
        for name in V71_MODULES + V73_MODULES:
            files[gemini/'config/auto-orchestrator/tools'/name]=(ROOT/'tools'/name).read_bytes()
        hooks_path=gemini/'config/hooks.json'
        if not hooks_path.exists():
            hooks=json.loads((ROOT/'.agents/hooks.json').read_text(encoding='utf-8'))
            for group in hooks.values():
                for entries in group.values():
                    if not isinstance(entries,list): continue
                    for h in entries:
                        if isinstance(h,dict) and isinstance(h.get('command'),str):
                            h['command']=h['command'].replace('python tools/hook_bridge.py',shell_arg(Path(sys.executable))+' '+shell_arg(bridge))
            files[hooks_path]=(json.dumps(hooks,indent=2)+'\n').encode('utf-8')
        files[gemini/'config/rules/auto-orchestrator.md']=('# Auto-Orchestrator\n'+GUIDANCE.replace(BEGIN,'').replace(END,'').strip()+'\n').encode()
    if 'cursor' in platforms:
        # Cursor user-level hooks execute under ~/.cursor and their stdin
        # does not attest the workspace. Never enable global telemetry hooks.
        cursor=home/'.cursor'
        skills_at(cursor/'skills/auto-orchestrator')
        for source in (ROOT/'.cursor/agents').glob('*.md'):
            files[cursor/'agents'/source.name]=source.read_bytes()
        for name in ('supervisor.py','router.py','router_eval.py','worktree.py','quality_gate.py','review_gate.py','perf.py','hook_bridge.py'):
            files[cursor/'auto-orchestrator/tools'/name]=(ROOT/'tools'/name).read_bytes()
        dashboard_at(cursor/'auto-orchestrator/tools')
        for name in V71_MODULES + V73_MODULES:
            files[cursor/'auto-orchestrator/tools'/name]=(ROOT/'tools'/name).read_bytes()
    return files

def native_hook_fragment(home: Path, platform: str) -> dict:
    if platform == 'codex':
        source=ROOT/'.codex/hooks.json'
        bridge=home/'.codex/auto-orchestrator/tools/hook_bridge.py'
        data=json.loads(source.read_text(encoding='utf-8'))
        for groups in data['hooks'].values():
            for group in groups:
                for handler in group.get('hooks',[]):
                    handler['command']=handler['command'].replace(
                        'python tools/hook_bridge.py',
                        shell_arg(Path(sys.executable))+' '+shell_arg(bridge))
        return {'hooks':data['hooks']}
    if platform == 'antigravity':
        source=ROOT/'.agents/hooks.json'
        bridge=home/'.gemini/config/auto-orchestrator/tools/hook_bridge.py'
        data=json.loads(source.read_text(encoding='utf-8'))
        for handlers in data['auto-orchestrator'].values():
            for handler in handlers:
                handler['command']=handler['command'].replace(
                    'python tools/hook_bridge.py',
                    shell_arg(Path(sys.executable))+' '+shell_arg(bridge))
        return data
    raise InstallError('Unknown native hook platform')

def global_guidance_paths(home:Path,platforms:set[str])->list[Path]:
    result=[]
    if 'codex' in platforms: result.append(home/'.codex/AGENTS.md')
    if 'opencode' in platforms: result.append(home/'.config/opencode/AGENTS.md')
    return result

def with_guidance(content:str,remove:bool=False)->str:
    if content.count(BEGIN)!=content.count(END) or content.count(BEGIN)>1:
        raise InstallError('Unbalanced AGENTS markers')
    if BEGIN in content:
        a=content.index(BEGIN)
        b=content.index(END)+len(END)
        return content[:a]+('' if remove else GUIDANCE)+content[b:]
    return content if remove else content.rstrip()+'\n\n'+GUIDANCE+'\n'

def execute(home:Path,platforms:set[str],opencode_version:str='v2',codex_model:str='inherit',
            dry_run:bool=False,uninstall:bool=False)->list[str]:
    home=home.resolve()
    manifest_path=home/'.auto-orchestrator'/MANIFEST
    guard_path(home,manifest_path)
    old=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {'files':{},'guidance':[]}
    old_files=old.get('files',{})
    notes=[]
    old_hook_injections=old.get('hook_injections',{})
    if not isinstance(old_hook_injections,dict):
        raise InstallError('Invalid saved hook integration metadata')
    if uninstall:
        for name in list(old_files)+old.get('guidance',[]):
            guard_path(home,Path(name))
        hook_removals=[]
        for name,entry in old_hook_injections.items():
            path=Path(name)
            guard_path(home,path)
            if not path.is_file():
                notes.append('hook file missing, nothing to remove: '+str(path))
                continue
            if not isinstance(entry,dict) or entry.get('platform') not in ('codex','antigravity'):
                raise InstallError('Invalid hook integration manifest entry')
            current=parse_hooks(path.read_text(encoding='utf-8'))
            new,changed,modified=remove_hooks(current,entry['fragment'],entry['platform'])
            if changed:
                hook_removals.append((path,pretty(new)))
            if modified:
                notes.append('preserved user-modified hook entry: '+str(path))
        remove_files=[]
        for name,checksum in old_files.items():
            path=Path(name)
            if path.is_file() and sha(path.read_bytes())==checksum:
                remove_files.append(path)
            elif path.exists():
                notes.append('preserved modified file: '+str(path))
        edits=[]
        for name in old.get('guidance',[]):
            path=Path(name)
            if path.is_file(): edits.append((path,with_guidance(path.read_text(encoding='utf-8'),True)))
        if not dry_run:
            for path in remove_files:path.unlink()
            for path,data in edits:path.write_text(data,encoding='utf-8')
            for path,data in hook_removals:path.write_text(data,encoding='utf-8')
            if manifest_path.exists():manifest_path.unlink()
        return notes+['removed '+str(len(remove_files))+' managed files (dry-run='+str(dry_run)+')']
    if not platforms: raise InstallError('Choose a platform')
    files=installed_map(home,platforms,opencode_version,codex_model)
    # Existing Codex configuration is never silently overwritten; detect
    # settings that could prevent native hooks or desired subagent behavior.
    if 'codex' in platforms:
        cfg=home/'.codex/config.toml'
        if cfg.is_file() and cfg not in files:
            try:
                parsed=tomllib.loads(cfg.read_text(encoding='utf-8'))
            except (tomllib.TOMLDecodeError, UnicodeError) as exc:
                raise InstallError('Existing Codex config.toml is invalid; refusing unsafe assumptions') from exc
            agent_settings=parsed.get('agents',{})
            if not isinstance(agent_settings,dict):
                raise InstallError('Codex [agents] configuration must be a table')
            if agent_settings.get('enabled') is False or parsed.get('features',{}).get('multi_agent') is False:
                notes.append('WARNING Codex multi-agent explicitly disabled in existing config.toml')
            if parsed.get('features',{}).get('hooks') is False:
                notes.append('WARNING Codex hooks explicitly disabled in existing config.toml')
            if agent_settings.get('max_concurrent_threads_per_session') != 3:
                notes.append('WARNING existing Codex config does not guarantee a 3-thread subagent limit')
            current_model=agent_settings.get('default_subagent_model')
            if codex_model=='inherit' and current_model:
                notes.append('WARNING existing Codex config overrides inherited subagent model: '+str(current_model))
    guides=global_guidance_paths(home,platforms)
    for path in [*files,*guides]:
        guard_path(home,path)
    # Existing user-owned hook files are extended in place, never managed as
    # complete files; uninstall removes only the exact entries we injected.
    hook_edits=[]
    hook_injections=dict(old_hook_injections)
    for platform,relative in (('codex','.codex/hooks.json'),('antigravity','.gemini/config/hooks.json')):
        if platform not in platforms:
            continue
        p=home/relative
        guard_path(home,p)
        if not p.is_file() or p in files or str(p) in old_files:
            continue
        existing=parse_hooks(p.read_text(encoding='utf-8'))
        stored=hook_injections.get(str(p))
        fragment=stored['fragment'] if stored else native_hook_fragment(home,platform)
        try:
            merged,changed=merge_hooks(existing,fragment,platform)
        except HookIntegrationError as exc:
            raise InstallError(str(exc)) from exc
        if changed:
            hook_edits.append((p,pretty(merged)))
            notes.append('integrated native hooks: '+str(p))
        if changed or stored:
            hook_injections[str(p)]={'platform':platform,'fragment':fragment}
    # Preflight everything before changing files.
    for path,content in files.items():
        if path.is_file() and path.read_bytes()!=content and old_files.get(str(path))!=sha(path.read_bytes()):
            raise InstallError('Existing unmanaged/modified file: '+str(path))
        if path.exists() and not path.is_file():raise InstallError('Non-file collision: '+str(path))
    edits=[]
    for path in guides:
        before=path.read_text(encoding='utf-8') if path.exists() else ''
        edits.append((path,with_guidance(before)))
    new_files=dict(old_files)
    for path,content in files.items():new_files[str(path)]=sha(content)
    new_guidance=sorted(set(old.get('guidance',[]))|{str(path) for path in guides})
    if not dry_run:
        for path,content in files.items():
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(content)
        for path,content in edits:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(content,encoding='utf-8')
        for path,content in hook_edits:
            path.write_text(content,encoding='utf-8')
        manifest_path.parent.mkdir(parents=True,exist_ok=True)
        manifest_path.write_text(json.dumps({'files':new_files,'guidance':new_guidance,'hook_injections':hook_injections},indent=2),encoding='utf-8')
    return notes+[('would install' if dry_run else 'installed')+' '+str(len(files))+' files',
            'Restart IDE and trust hooks; native subagent/model execution still requires manual testing.']

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',type=Path,default=Path.home())
    parser.add_argument('--platform',choices=['all','codex','opencode','antigravity','cursor'],default='all')
    parser.add_argument('--opencode-version',choices=['v1','v2'],default='v2')
    parser.add_argument('--codex-model',choices=['inherit','luna-max'],default='inherit')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--uninstall',action='store_true')
    args=parser.parse_args(argv)
    platforms={'codex','opencode','antigravity','cursor'} if args.platform=='all' else {args.platform}
    try:print('\n'.join(execute(args.home,platforms,args.opencode_version,args.codex_model,args.dry_run,args.uninstall)))
    except (InstallError,HookIntegrationError,OSError,ValueError) as exc:
        print('INSTALL ERROR: '+str(exc),file=sys.stderr)
        return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
