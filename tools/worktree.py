#!/usr/bin/env python3
"""Safe Git worktree creation and NON-DESTRUCTIVE integration preview."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
try:
    from tools.supervisor import identifier, SupervisorError
except ModuleNotFoundError:
    from supervisor import identifier, SupervisorError

class WorktreeError(ValueError):
    pass

def git(cwd, *args, check=True):
    process=subprocess.run(["git","-C",str(cwd),*args],capture_output=True,
                           text=True,encoding="utf-8",errors="replace",timeout=30)
    if check and process.returncode:
        raise WorktreeError((process.stderr or process.stdout).strip()[:1200])
    return process

def repository(path):
    repo=git(path,"rev-parse","--show-toplevel").stdout.strip()
    if not repo:
        raise WorktreeError("Not a Git repository")
    return Path(repo).resolve()

def ensure_clean(path):
    if git(path,"status","--porcelain").stdout.strip():
        raise WorktreeError("Working tree contains uncommitted or untracked changes")

def manifest_path(repo,task):
    identifier(task)
    return repo/".auto-orchestrator"/"worktrees"/f"{task}.json"

def read_manifest(repo,task):
    path=manifest_path(repo,task)
    if not path.is_file():
        raise WorktreeError("Unknown managed task worktree")
    data=json.loads(path.read_text(encoding="utf-8"))
    if data.get("repository") != str(repo) or data.get("task") != task:
        raise WorktreeError("Managed worktree metadata does not match repository")
    return data

def common_git_dir(path):
    value=git(path,"rev-parse","--git-common-dir").stdout.strip()
    p=Path(value)
    return (p if p.is_absolute() else Path(path)/p).resolve()

def create(repo_path,task):
    repo=repository(repo_path)
    identifier(task)
    ensure_clean(repo)
    manifest=manifest_path(repo,task)
    if manifest.exists():
        raise WorktreeError("Task worktree already exists")
    base=git(repo,"rev-parse","HEAD").stdout.strip()
    branch="auto-orchestrator/"+task
    dest=repo.parent/(repo.name+"-auto-orchestrator-worktrees")/task
    if dest.exists():
        raise WorktreeError("Target worktree path already exists")
    if git(repo,"show-ref","--verify","--quiet","refs/heads/"+branch,check=False).returncode==0:
        raise WorktreeError("Task branch already exists")
    dest.parent.mkdir(parents=True,exist_ok=True)
    git(repo,"worktree","add","-b",branch,str(dest),base)
    data={"task":task,"repository":str(repo),"base_sha":base,"branch":branch,"path":str(dest)}
    try:
        manifest.parent.mkdir(parents=True,exist_ok=True)
        manifest.write_text(json.dumps(data,indent=2)+"\n",encoding="utf-8")
    except OSError:
        # Avoid destroying a developer's worktree if metadata cannot be written.
        raise WorktreeError("Worktree created but metadata write failed; inspect git worktree list")
    return data

def preview(repo_path,task):
    repo=repository(repo_path)
    data=read_manifest(repo,task)
    wt=Path(data["path"])
    if common_git_dir(wt)!=common_git_dir(repo):
        raise WorktreeError("Task worktree is not attached to expected repository")
    ensure_clean(repo)
    ensure_clean(wt)
    branch=data["branch"]
    base=data["base_sha"]
    if git(repo,"merge-base","--is-ancestor",base,branch,check=False).returncode!=0:
        raise WorktreeError("Task branch lost its original base")
    changed=git(repo,"diff","--name-only",base,branch).stdout.splitlines()
    if not changed:
        return {"task":task,"ready":False,"conflict_free":True,"changed_files":[],"reason":"No committed changes"}
    # merge-tree writes only a synthetic tree object; it never updates index/working files.
    proc=git(repo,"merge-tree","--write-tree","HEAD",branch,check=False)
    return {"task":task,"ready":proc.returncode==0,"conflict_free":proc.returncode==0,
            "changed_files":changed,
            "reason":"Non-destructive preview only; integration requires main-agent review and tests" if proc.returncode==0
                     else "Conflicts detected; do not automatically merge",
            "diagnostic":(proc.stdout+"\n"+proc.stderr)[-1200:] if proc.returncode else ""}

def cleanup(repo_path,task):
    repo=repository(repo_path)
    data=read_manifest(repo,task)
    wt=Path(data["path"])
    if not wt.is_dir():
        raise WorktreeError("Worktree missing; manual reconciliation required")
    ensure_clean(wt)
    git(repo,"worktree","remove",str(wt))
    manifest_path(repo,task).unlink()
    # Keep branch and commits; never force-delete work.
    return {"task":task,"worktree_removed":True,"branch_preserved":data["branch"]}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo",default=".")
    sub=p.add_subparsers(dest="command",required=True)
    for name in ("create","preview","cleanup"):
        sub.add_parser(name).add_argument("--task-id",required=True)
    a=p.parse_args(argv)
    try:
        action={"create":create,"preview":preview,"cleanup":cleanup}[a.command]
        print(json.dumps(action(a.repo,a.task_id),indent=2))
    except (SupervisorError,WorktreeError,OSError,subprocess.TimeoutExpired,ValueError) as exc:
        p.exit(1,"WORKTREE ERROR: "+str(exc)+"\n")
if __name__=="__main__":
    main()
