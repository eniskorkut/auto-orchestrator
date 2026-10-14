#!/usr/bin/env python3
"""Safe per-user installation for Codex, OpenCode V1/V2, Antigravity IDE/CLI."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEGIN = '<!-- auto-orchestrator:begin -->'
END = '<!-- auto-orchestrator:end -->'
GUIDANCE = """<!-- auto-orchestrator:begin -->
For non-trivial coding tasks consult the installed auto-orchestrator skill. Delegate independent work to native subagents automatically (max three), protect shared files, and verify integrated results. Trivial tasks use one agent. Never invent model execution, tests, or human time savings.
<!-- auto-orchestrator:end -->"""
MANIFEST = 'auto-orchestrator-global-manifest.json'

class InstallError(ValueError):
    pass

def sha(data:bytes)->str:
    return hashlib.sha256(data).hexdigest()

def skill_files()->dict[str,bytes]:
    root=ROOT/'.agents/skills/auto-orchestrator'
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}

def installed_map(home:Path,platforms:set[str],opencode_version:str,codex_model:str)->dict[Path,bytes]:
    files={}
    skills=skill_files()
    def skills_at(base):
        for path,content in skills.items(): files[base/path]=content
    if 'codex' in platforms:
        codex=home/'.codex'
        skills_at(codex/'skills/auto-orchestrator')
        for source in (ROOT/'.codex/agents').glob('*.toml'):
            files[codex/'agents'/source.name]=source.read_bytes()
        # Global runtime adapters. Existing hook files belong to the user and are left untouched.
        bridge=codex/'auto-orchestrator/tools/hook_bridge.py'
        files[bridge]=(ROOT/'tools/hook_bridge.py').read_bytes()
        files[codex/'auto-orchestrator/tools/perf.py']=(ROOT/'tools/perf.py').read_bytes()
        for name in ('supervisor.py','router.py','worktree.py'):
            files[codex/'auto-orchestrator/tools'/name]=(ROOT/'tools'/name).read_bytes()
        hooks_path=codex/'hooks.json'
        if not hooks_path.exists():
            hooks=json.loads((ROOT/'.codex/hooks.json').read_text(encoding='utf-8'))
            for group in hooks['hooks'].values():
                for item in group:
                    for h in item.get('hooks',[]):
                        h['command']=h['command'].replace('python tools/hook_bridge.py','python "'+str(bridge)+'"')
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
        plugin=ROOT/('adapters/opencode-v2/auto_orchestrator.js' if opencode_version=='v2' else '.opencode/plugins/auto_orchestrator.js')
        files[oc/'plugins/auto_orchestrator.js']=plugin.read_bytes()
        for name in ('supervisor.py','router.py','worktree.py'):
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
        for name in ('supervisor.py','router.py','worktree.py'):
            files[gemini/'config/auto-orchestrator/tools'/name]=(ROOT/'tools'/name).read_bytes()
        hooks_path=gemini/'config/hooks.json'
        if not hooks_path.exists():
            hooks=json.loads((ROOT/'.agents/hooks.json').read_text(encoding='utf-8'))
            for group in hooks.values():
                for entries in group.values():
                    if not isinstance(entries,list): continue
                    for h in entries:
                        if isinstance(h,dict) and isinstance(h.get('command'),str):
                            h['command']=h['command'].replace('python tools/hook_bridge.py','python "'+str(bridge)+'"')
            files[hooks_path]=(json.dumps(hooks,indent=2)+'\n').encode('utf-8')
        files[gemini/'config/rules/auto-orchestrator.md']=('# Auto-Orchestrator\n'+GUIDANCE.replace(BEGIN,'').replace(END,'').strip()+'\n').encode()
    return files

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
    old=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {'files':{},'guidance':[]}
    old_files=old.get('files',{})
    notes=[]
    if uninstall:
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
            if manifest_path.exists():manifest_path.unlink()
        return notes+['removed '+str(len(remove_files))+' managed files (dry-run='+str(dry_run)+')']
    if not platforms: raise InstallError('Choose a platform')
    files=installed_map(home,platforms,opencode_version,codex_model)
    guides=global_guidance_paths(home,platforms)
    for name in ('.codex/hooks.json','.gemini/config/hooks.json'):
        p=home/name
        if p.is_file() and p not in files and str(p) not in old_files and ((name.startswith('.codex') and 'codex' in platforms) or
                                                (name.startswith('.gemini') and 'antigravity' in platforms)):
            notes.append('preserved existing hooks, manual integration needed: '+str(p))
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
        manifest_path.parent.mkdir(parents=True,exist_ok=True)
        manifest_path.write_text(json.dumps({'files':new_files,'guidance':new_guidance},indent=2),encoding='utf-8')
    return notes+[('would install' if dry_run else 'installed')+' '+str(len(files))+' files',
            'Restart IDE and trust hooks; native subagent/model execution still requires manual testing.']

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',type=Path,default=Path.home())
    parser.add_argument('--platform',choices=['all','codex','opencode','antigravity'],default='all')
    parser.add_argument('--opencode-version',choices=['v1','v2'],default='v2')
    parser.add_argument('--codex-model',choices=['inherit','luna-max'],default='inherit')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--uninstall',action='store_true')
    args=parser.parse_args(argv)
    platforms={'codex','opencode','antigravity'} if args.platform=='all' else {args.platform}
    try:print('\n'.join(execute(args.home,platforms,args.opencode_version,args.codex_model,args.dry_run,args.uninstall)))
    except (InstallError,OSError,ValueError) as exc:
        print('INSTALL ERROR: '+str(exc),file=sys.stderr)
        return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
