#!/usr/bin/env python3
"""Build compact, scoped subagent briefs and discover relevant installed skills."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
try:
    from tools.supervisor import validate_plan, SupervisorError
except ModuleNotFoundError:
    from supervisor import validate_plan, SupervisorError

WORDS=re.compile(r"[a-z0-9][a-z0-9_.+-]{2,}",re.I)
STOP={"agent","skill","skills","task","code","file","files","implement","review","this",
      "with","from","must","and","the","for","test","tests","using","should","work"}
MAX_SKILL_BYTES=64*1024

def tokens(value):
    return set(w.lower() for w in WORDS.findall(value) if w.lower() not in STOP)

def skill_roots(project:Path, home:Path|None=None):
    home=home or Path.home()
    return [project/".agents/skills",project/".codex/skills",project/".opencode/skills",
            project/".gemini/config/skills",home/".codex/skills",
            home/".config/opencode/skills",home/".gemini/config/skills",
            home/".gemini/antigravity-cli/skills"]

def discover(project:Path, home:Path|None=None):
    found={}
    for root in skill_roots(project,home):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size>MAX_SKILL_BYTES:
                continue
            if not path.resolve().is_relative_to(root.resolve()):
                continue
            content=path.read_text(encoding="utf-8")
            header=content.split("---",2)
            meta=header[1] if len(header)==3 and not header[0].strip() else ""
            match_name=re.search(r"^name:\s*['\"]?([A-Za-z0-9_-]+)",meta,re.M)
            match_description=re.search(r"^description:\s*(.+)$",meta,re.M)
            name=match_name.group(1) if match_name else path.parent.name
            if name=="auto-orchestrator" or name in found:
                continue
            description=(match_description.group(1).strip().strip('"\'') if match_description else "")
            found[name]={"name":name,"description":description[:300],"path":str(path.resolve())}
    return sorted(found.values(),key=lambda x:x["name"])

def route(task:dict, skills:list[dict], max_skills:int=2):
    if not 0<=max_skills<=5:
        raise SupervisorError("max_skills out of range")
    query=tokens(task["objective"]+" "+task["role"]+" "+" ".join(task.get("read",[])+task.get("write",[])))
    ranked=[]
    explicit=set(task.get("skills",[]))
    for item in skills:
        name=item["name"]
        description=item["description"]
        name_tokens=tokens(name.replace("-"," "))
        score=5 if name in explicit else 0
        score += 3*len(query & name_tokens)
        score += len(query & tokens(description))
        if score:
            ranked.append((score,item))
    ranked.sort(key=lambda x:(-x[0],x[1]["name"]))
    return [entry for _,entry in ranked[:max_skills]]

def brief(project:Path,plan:dict,task_id:str,home:Path|None=None,max_skills:int=2):
    tasks,_=validate_plan(plan)
    if task_id not in tasks:
        raise SupervisorError("Task not in plan")
    task=tasks[task_id]
    original=next(x for x in plan["tasks"] if x["id"]==task_id)
    skills=route(task|{"skills":original.get("skills",[])},discover(project,home),max_skills)
    return {
        "task_id":task_id,"role":task["role"],"objective":task["objective"],
        "depends_on":task["depends_on"],"read_scope":task["read"],"write_scope":task["write"],
        "selected_skills":[{"name":s["name"],"path":s["path"]} for s in skills],
        "required_result":{
            "summary":"concise completed work","changed_files":[],
            "tests":[{"name":"test command or check","status":"passed|failed|skipped"}],
            "findings":[]},
        "rules":[
            "Read only the selected skills needed for this bounded task.",
            "Do not access or write outside assigned scopes; no unrelated refactors.",
            "Don't claim native task completion without a successful native tool result.",
            "Return concrete test evidence and do not fabricate measurements.",
            "Never include secrets or whole chat transcripts in the brief or output."],
    }

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,default=Path.cwd())
    s=p.add_subparsers(dest="command",required=True)
    d=s.add_parser("discover")
    b=s.add_parser("brief");b.add_argument("--plan",type=Path,required=True)
    b.add_argument("--task-id",required=True);b.add_argument("--max-skills",type=int,default=2)
    a=p.parse_args(argv)
    try:
        if a.command=="discover": result=discover(a.root)
        else: result=brief(a.root,json.loads(a.plan.read_text(encoding="utf-8")),a.task_id,max_skills=a.max_skills)
        print(json.dumps(result,indent=2))
    except (SupervisorError,OSError,ValueError) as exc:
        p.exit(1,"ROUTER ERROR: "+str(exc)+"\n")
if __name__=="__main__":
    main()
