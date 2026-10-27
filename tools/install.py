#!/usr/bin/env python3
"""Install Auto-Orchestrator V2 to an existing project safely (stdlib only)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
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
MANIFEST_RELATIVE = Path('.auto-orchestrator/install-manifest.json')
MANIFEST_VERSION = 1
V761_MODULES = ('requirements_contract.py', 'assumption_guard.py',
                'git_memory_hook.py')
V77_MODULES = ('completion_engine.py', 'context_selector.py',
               'grill_llm.py', 'grill_benchmark.py')
V78_MODULES = ('ide_handoff.py',)
# Installer-generated runtime and integration files may be upgraded in place
# only while the manifest still proves the file is ours. Everything else keeps
# the conservative opt-in rule.
MANAGED_PREFIXES = (
    'tools/',
    '.agents/skills/',
    '.agents/agents/',
    '.codex/agents/',
    '.codex/skills/',
    '.cursor/agents/',
    '.opencode/agents/',
    '.opencode/commands/',
    '.opencode/skills/',
    '.gemini/',
)


def _managed_relative(relative: str) -> bool:
    return any(relative == prefix.rstrip('/') or relative.startswith(prefix)
               for prefix in MANAGED_PREFIXES)


def _prunable_for_selection(relative: str, selected: set[str]) -> bool:
    """Do not remove another platform's integration on a scoped install."""
    owners = (
        ('.codex/', 'codex'),
        ('.opencode/', 'opencode'),
        ('.cursor/', 'cursor'),
        ('.gemini/', 'antigravity'),
    )
    for prefix, platform in owners:
        if relative.startswith(prefix):
            return platform in selected
    # Shared .agents files remain for other platforms; only the selected
    # platform's integration files and common tools may be pruned.
    return relative.startswith('tools/')


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
    content=source.read_bytes().decode('utf-8')
    if source in (ROOT/'.codex/hooks.json', ROOT/'.agents/hooks.json', ROOT/'.cursor/hooks.json'):
        command=shell_arg(Path(sys.executable))+' '+shell_arg((target/'tools/hook_bridge.py').resolve())
        content=content.replace('python tools/hook_bridge.py',json.dumps(command)[1:-1])
    return content


def write_file(dest: Path, text: str, dry_run: bool, force: bool = False,
               managed: bool = False, backup: bool = False) -> str:
    existed = dest.is_file()
    if existed:
        old_bytes = dest.read_bytes()
        new_bytes = text.encode('utf-8')
        if (old_bytes == new_bytes
                or old_bytes.replace(b'\r\n', b'\n')
                == new_bytes.replace(b'\r\n', b'\n')):
            return 'unchanged'
        old = old_bytes.decode('utf-8', errors='replace')
        if not force and not managed:
            raise Conflict(f'Existing file differs: {dest}. No overwrite (use --force-generated for generated profiles).')
        if not dry_run:
            if force or backup:
                shutil.copy2(dest, dest.with_name(dest.name + '.bak'))
            dest.write_bytes(text.encode('utf-8'))
    elif dest.exists():
        raise Conflict(f'Not a normal file: {dest}')
    elif not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(text.encode('utf-8'))
    return 'updated' if existed else 'created'


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
    lines = old.splitlines()
    if ('.auto-orchestrator/*' in lines
            and '!.auto-orchestrator/project.json' in lines):
        return 'unchanged'
    # Migrate the old blanket ignore. The small portable manifest must be
    # committable; the rest of the local state stays ignored.
    kept = [line for line in lines if line.strip() != '.auto-orchestrator/']
    new = ('\n'.join(kept).rstrip()
           + '\n.auto-orchestrator/*\n!.auto-orchestrator/project.json\n')
    if not dry_run:
        dest.write_text(new, encoding='utf-8')
    return 'updated'


def remove_guidance(dest: Path, dry_run: bool) -> str:
    """Remove only the managed block, preserving all surrounding user text."""
    if not dest.is_file():
        return 'missing'
    old = dest.read_text(encoding='utf-8')
    if old.count(START) != old.count(END) or old.count(START) > 1:
        raise Conflict(f'Invalid managed markers in {dest}')
    if START not in old:
        return 'unchanged'
    prefix = old[:old.index(START)]
    suffix = old[old.index(END) + len(END):]
    new = (prefix + suffix).rstrip()
    if new:
        new += '\n'
    if not dry_run:
        dest.write_text(new, encoding='utf-8')
    return 'updated'


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _manifest_path(target: Path) -> Path:
    return target / MANIFEST_RELATIVE


def _load_manifest(target: Path) -> dict:
    path = _manifest_path(target)
    guard_dest(target, path)
    if path.is_symlink():
        raise Conflict('Install manifest must not be a symlink')
    if not path.is_file():
        return {'version': MANIFEST_VERSION, 'files': {}, 'created': [],
                'guidance': [], 'guidance_created': [], 'gitignore': {},
                'platforms': []}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise Conflict('Install manifest is invalid; refusing unsafe uninstall') from exc
    if not isinstance(value, dict) or value.get('version') != MANIFEST_VERSION:
        raise Conflict('Unsupported install manifest; refusing unsafe uninstall')
    files = value.get('files', {})
    created = value.get('created', [])
    guidance = value.get('guidance', [])
    guidance_created = value.get('guidance_created', [])
    gitignore = value.get('gitignore', {})
    platforms = value.get('platforms', [])
    if (not isinstance(files, dict)
            or not all(isinstance(key, str) and isinstance(item, str)
                       for key, item in files.items())
            or not isinstance(created, list)
            or not all(isinstance(item, str) for item in created)
            or not isinstance(guidance, list)
            or not all(isinstance(item, str) for item in guidance)
            or not isinstance(guidance_created, list)
            or not all(isinstance(item, str) for item in guidance_created)
            or not isinstance(gitignore, dict)
            or (gitignore and not isinstance(gitignore.get('path'), str))
            or not isinstance(platforms, list)
            or not all(isinstance(item, str) for item in platforms)):
        raise Conflict('Install manifest structure is invalid')
    return {'version': MANIFEST_VERSION, 'files': files,
            'created': created, 'guidance': guidance,
            'guidance_created': guidance_created, 'gitignore': gitignore,
            'platforms': platforms}


def _write_manifest(target: Path, value: dict, dry_run: bool) -> None:
    if dry_run:
        return
    path = _manifest_path(target)
    guard_dest(target, path)
    text = json.dumps(value, indent=2, sort_keys=True) + '\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.tmp-{os.getpid()}')
    try:
        temporary.write_text(text, encoding='utf-8')
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _runtime_sources() -> list[Path]:
    sources = [ROOT/'tools/perf.py', ROOT/'tools/hook_bridge.py',
               ROOT/'tools/supervisor.py', ROOT/'tools/router.py', ROOT/'tools/router_eval.py', ROOT/'tools/worktree.py', ROOT/'tools/benchmark.py', ROOT/'tools/quality_gate.py', ROOT/'tools/review_gate.py',
               ROOT/'tools/dashboard.py', ROOT/'tools/project_registry.py',
               ROOT/'tools/event_stream.py', ROOT/'tools/event_model.py',
               ROOT/'tools/office_model.py', ROOT/'tools/capabilities.py',
               ROOT/'tools/telemetry.py', ROOT/'tools/project_planner.py',
               ROOT/'tools/executor.py', ROOT/'tools/teamwork.py',
               ROOT/'tools/teamwork_planner.py', ROOT/'tools/managed_exec.py',
               ROOT/'tools/teamwork_runner.py', ROOT/'tools/teamwork_benchmark.py',
               ROOT/'tools/teamwork_cli.py', ROOT/'tools/teamwork_chat.py',
               ROOT/'tools/scheduler.py', ROOT/'tools/run_owner.py',
               ROOT/'tools/repo_profile.py', ROOT/'tools/project_identity.py',
               ROOT/'tools/project_memory.py', ROOT/'tools/repo_facts.py',
               ROOT/'tools/grill_me.py', ROOT/'tools/universal_teamwork.py']
    # V7.6.1 modules ship when present. A partial source tree must not make a
    # previously working installer unusable. V7.8 adds the read-only IDE
    # handoff module on the same optional basis.
    sources.extend(ROOT/'tools'/name for name in V761_MODULES + V77_MODULES + V78_MODULES
                   if (ROOT/'tools'/name).is_file())
    return sources


def install(target: Path, platform: str, codex_model: str, dry_run: bool, force_generated: bool, opencode_version: str = 'v2') -> list[tuple[str,str]]:
    if not target.is_dir():
        raise Conflict(f'Target project directory does not exist: {target}')
    if (target / 'tools' / 'install.py').resolve() == Path(__file__).resolve():
        raise Conflict('Install into a separate target project (do not self-install).')
    target = target.resolve()
    selected = {'codex','opencode','antigravity','cursor'} if platform == 'all' else {platform}
    def destination(source: Path) -> Path:
        if source == ROOT/'adapters/opencode-v2/auto_orchestrator.js':
            return target/'.opencode/plugins/auto_orchestrator.js'
        return target/source.relative_to(ROOT)
    sources = [ROOT/'.agents/skills/auto-orchestrator/SKILL.md']
    sources.extend(sorted((ROOT/'.agents/skills/auto-orchestrator/references').glob('*.md')))
    if selected & {'codex', 'opencode', 'cursor'}:
        sources.append(ROOT/'.agents/skills/grill-me/SKILL.md')
    if 'antigravity' in selected:
        sources.append(ROOT/'.agents/skills/ao-grill-me/SKILL.md')
    sources.extend(_runtime_sources())
    sources.extend(sorted(p for p in (ROOT/'tools/dashboard_assets').rglob('*') if p.is_file()))
    if 'codex' in selected:
        sources.extend(sorted((ROOT/'.codex/agents').glob('*.toml')))
        sources.extend(sorted(p for p in (ROOT/'.codex/skills').rglob('*') if p.is_file()))
        sources.append(ROOT/'.codex/hooks.json')
    if 'opencode' in selected:
        sources.extend(sorted((ROOT/'.opencode/agents').glob('*.md')))
        commands = ROOT/'.opencode/commands'
        if commands.is_dir():
            sources.extend(sorted(commands.glob('*.md')))
        skills = ROOT/'.opencode/skills'
        if skills.is_dir():
            sources.extend(sorted(p for p in skills.rglob('*') if p.is_file()))
        sources.append(ROOT/('adapters/opencode-v2/auto_orchestrator.js' if opencode_version == 'v2' else '.opencode/plugins/auto_orchestrator.js'))
    if 'antigravity' in selected:
        sources.extend(sorted((ROOT/'.agents/agents').glob('*.md')))
        sources.append(ROOT/'.agents/hooks.json')
    if 'cursor' in selected:
        sources.extend(sorted((ROOT/'.cursor/agents').glob('*.md')))
        sources.append(ROOT/'.cursor/hooks.json')

    outputs: list[tuple[Path, str]] = [
        (destination(source), rendered(source, target)) for source in sources]
    if 'antigravity' in selected:
        grill = ROOT/'.agents/skills/ao-grill-me/SKILL.md'
        if grill.is_file():
            outputs.append((
                target/'.gemini/antigravity-cli/skills/ao-grill-me/SKILL.md',
                grill.read_bytes().decode('utf-8')))

    manifest_path = _manifest_path(target)
    manifest = _load_manifest(target)
    old_files = manifest['files']
    old_created = set(manifest['created'])

    def managed(relative: str, dest: Path) -> bool:
        if not dest.is_file() or relative not in old_files:
            return False
        try:
            return _sha256_file(dest) == old_files[relative]
        except OSError:
            return False

    # Preflight all non-managed destinations, so no partial install on conflict.
    for dest, text in outputs:
        relative = dest.relative_to(target).as_posix()
        guard_dest(target, dest)
        if dest.exists() and not dest.is_file():
            raise Conflict(f'Not a normal file: {dest}')
        if dest.is_file() and not force_generated:
            old = dest.read_text(encoding='utf-8')
            # Line-ending differences are not user content changes.
            old_normalized = old.replace('\r\n', '\n')
            text_normalized = text.replace('\r\n', '\n')
            # Runtime modules, dashboard assets, skills, commands and agent
            # profiles may upgrade in place only when the manifest proves the
            # file is still installer-generated. User-modified files (or files
            # without a manifest entry) keep the stricter opt-in rule.
            managed_update = _managed_relative(relative) and managed(relative, dest)
            if old_normalized != text_normalized and not managed_update:
                raise Conflict(f'Existing file differs: {dest}; nothing changed.')
    conf = target/'.codex/config.toml'
    guard_dest(target,conf)
    guard_dest(target,target/'AGENTS.md')
    guard_dest(target,target/'.gitignore')
    guard_dest(target, manifest_path)
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
    records: dict[str, dict] = {}
    new_relatives = {dest.relative_to(target).as_posix() for dest, _ in outputs}
    if 'codex' in selected:
        new_relatives.add('.codex/config.toml')
    # Obsolete managed integration files can be pruned only when they are
    # still byte-identical to the manifest. Modified files stay on disk and in
    # the manifest so an uninstall can preserve or report them.
    obsolete: list[str] = []
    for relative, checksum in old_files.items():
        if relative in new_relatives or not _managed_relative(relative):
            continue
        if not _prunable_for_selection(relative, selected):
            continue
        dest = target / relative
        guard_dest(target, dest)
        if dest.is_file() and _sha256_file(dest) == checksum:
            obsolete.append(relative)
    for dest, text in outputs:
        relative = dest.relative_to(target).as_posix()
        existed = dest.is_file()
        managed_update = _managed_relative(relative) and managed(relative, dest)
        state = write_file(dest, text, dry_run, force_generated,
                           managed_update, backup=managed_update)
        results.append((relative, state))
        records[relative] = {
            'sha256': _sha256_bytes(text.encode('utf-8')),
            'created': bool(not existed or relative in old_created),
        }
    if 'codex' in selected:
        existed = conf.is_file()
        state = write_file(conf, MODEL_SETTING[codex_model], dry_run, True,
                           managed('.codex/config.toml', conf))
        results.append(('.codex/config.toml', state))
        records['.codex/config.toml'] = {
            'sha256': _sha256_bytes(MODEL_SETTING[codex_model].encode('utf-8')),
            'created': bool(not existed or '.codex/config.toml' in old_created),
        }
    for relative in obsolete:
        if not dry_run:
            (target / relative).unlink()
        results.append((relative,
                        'would-remove-obsolete' if dry_run else 'removed-obsolete'))
    g_existed = g.is_file()
    ignore_path = target/'.gitignore'
    ignore_existed = ignore_path.is_file()
    ignore_lines = (ignore_path.read_text(encoding='utf-8').splitlines()
                    if ignore_existed else [])
    ignore_had_lines = ('.auto-orchestrator/*' in ignore_lines
                        and '!.auto-orchestrator/project.json' in ignore_lines)
    guidance_state = patch_guidance(g, dry_run)
    results.append(('AGENTS.md', guidance_state))
    results.append(('.gitignore', patch_ignore(ignore_path, dry_run)))

    new_files = dict(old_files)
    for relative in obsolete:
        new_files.pop(relative, None)
    new_created = set(old_created)
    for relative, record in records.items():
        new_files[relative] = record['sha256']
        if record['created']:
            new_created.add(relative)
        else:
            new_created.discard(relative)
    new_created.difference_update(obsolete)
    new_guidance = set(manifest['guidance'])
    if g.is_file() or not dry_run:
        new_guidance.add('AGENTS.md')
    guidance_created = set(manifest.get('guidance_created', []))
    if not g_existed:
        guidance_created.add('AGENTS.md')
    old_gitignore = manifest.get('gitignore')
    old_gitignore = old_gitignore if isinstance(old_gitignore, dict) else {}
    gitignore_info = {
        'path': '.gitignore',
        'created': bool(not ignore_existed or old_gitignore.get('created')),
        'added': bool(not ignore_had_lines or old_gitignore.get('added')),
    }
    manifest_value = {
        'version': MANIFEST_VERSION,
        'files': new_files,
        'created': sorted(new_created),
        'guidance': sorted(new_guidance),
        'guidance_created': sorted(guidance_created),
        'gitignore': gitignore_info,
        'platforms': sorted(set(manifest.get('platforms', [])) | selected),
        'updated_at': time.time(),
    }
    _write_manifest(target, manifest_value, dry_run)
    return results


def uninstall(target: Path, dry_run: bool = False) -> list[tuple[str, str]]:
    """Remove only files this project installer created, preserving user edits."""
    if not target.is_dir():
        raise Conflict(f'Target project directory does not exist: {target}')
    target = target.resolve()
    manifest_path = _manifest_path(target)
    guard_dest(target, manifest_path)
    if not manifest_path.is_file():
        # Without a manifest ownership is unknown; remove nothing.
        return []
    manifest = _load_manifest(target)
    # Validate every recorded path before deleting anything, so one corrupt
    # manifest entry cannot cause a partial uninstall.
    for relative in (set(manifest['created']) | set(manifest['guidance'])):
        guard_dest(target, target / relative)
    for relative in set(manifest['guidance']):
        path = target / relative
        if path.is_file():
            text = path.read_text(encoding='utf-8')
            if text.count(START) != text.count(END) or text.count(START) > 1:
                raise Conflict(f'Invalid managed markers in {path}; nothing was removed.')
    gitignore_preflight = manifest.get('gitignore') or {}
    if isinstance(gitignore_preflight.get('path'), str):
        guard_dest(target, target / gitignore_preflight['path'])
    results: list[tuple[str, str]] = []
    for relative in sorted(set(manifest['created'])):
        dest = target / relative
        guard_dest(target, dest)
        if not dest.is_file():
            results.append((relative, 'missing'))
            continue
        expected = manifest['files'].get(relative)
        if expected is not None and _sha256_file(dest) != expected:
            results.append((relative, 'preserved'))
            continue
        if not dry_run:
            dest.unlink()
        results.append((relative, 'would-remove' if dry_run else 'removed'))
    guidance_created = set(manifest.get('guidance_created', []))
    for relative in sorted(set(manifest['guidance'])):
        dest = target / relative
        guard_dest(target, dest)
        if not dest.is_file():
            results.append((relative, 'missing'))
            continue
        state = remove_guidance(dest, dry_run)
        if (not dry_run and relative in guidance_created
                and dest.is_file() and not dest.read_text(encoding='utf-8').strip()):
            dest.unlink()
            state = 'removed'
        results.append((relative, 'would-update'
                        if dry_run and state == 'updated' else state))
    gitignore = manifest.get('gitignore') or {}
    if ((gitignore.get('created') or gitignore.get('added'))
            and isinstance(gitignore.get('path'), str)):
        dest = target / gitignore['path']
        guard_dest(target, dest)
        if dest.is_file():
            lines = dest.read_text(encoding='utf-8').splitlines()
            kept = [line for line in lines if line.strip() not in (
                '.auto-orchestrator/*', '!.auto-orchestrator/project.json')]
            if any(line.strip() for line in kept):
                if not dry_run:
                    dest.write_text('\n'.join(kept).rstrip() + '\n',
                                    encoding='utf-8')
                results.append((gitignore['path'],
                                'would-update' if dry_run else 'updated'))
            else:
                if not dry_run:
                    dest.unlink()
                results.append((gitignore['path'],
                                'would-remove' if dry_run else 'removed'))
    preserved = any(state == 'preserved' for _, state in results)
    if not dry_run and not preserved:
        manifest_path.unlink()
    results.append((MANIFEST_RELATIVE.as_posix(),
                    ('preserved' if preserved else
                     ('would-remove' if dry_run else 'removed'))))
    return results


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',required=True,type=Path)
    p.add_argument('--platform',choices=['all','codex','opencode','antigravity','cursor'],default='all')
    p.add_argument('--codex-model',choices=['inherit','luna-max'],default='inherit')
    p.add_argument('--opencode-version',choices=['v1','v2'],default='v2')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--force-generated',action='store_true',help='Back up and replace generated profiles/skill files; not existing Codex config')
    p.add_argument('--uninstall',action='store_true',help='Remove files recorded by this project installer')
    args=p.parse_args(argv)
    try:
        if args.uninstall:
            results = uninstall(args.target.resolve(),args.dry_run)
            if not results:
                print('No project install manifest was found; nothing was removed.')
                return 0
            for path,state in results:
                print(f'{state:13s} {path}')
            print('DRY RUN — no files written' if args.dry_run else 'Uninstalled project-local files recorded in the install manifest.')
            return 0
        for path,state in install(args.target.resolve(),args.platform,args.codex_model,args.dry_run,args.force_generated,args.opencode_version):
            print(f'{state:9s} {path}')
    except (Conflict,OSError) as e:
        print(f'INSTALL ERROR: {e}',file=sys.stderr)
        return 1
    print('DRY RUN — no files written' if args.dry_run else 'Installed. Restart your client/session if it does not discover the skill.')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
