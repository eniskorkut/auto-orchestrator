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

# Skill selection must be conservative: a single incidental description word
# must not route backend work to an animation/UI skill.
WORDS=re.compile(r"[^\W_]+",re.UNICODE)
STOP={"agent","agents","skill","skills","task","tasks","code","coding","file","files",
      "implement","implementation","review","reviews","this","that","those","these",
      "with","from","must","and","the","for","using","should","work","works",
      "create","build","make","change","update","new","add","fix","check","best",
      "support","help","manage","process","project","projects","tool","tools",
      "system","systems","feature","features","general","complex","simple"}
# These are domain vetoes, not an exhaustive ontology. No LLM is required.
DOMAIN_TERMS={
    "backend":{"backend","api","endpoint","endpoints","fastapi","django","flask",
               "server","servers","database","databases","postgres","postgresql",
               "mysql","redis","rest","graphql","orm","microservice"},
    "frontend":{"frontend","ui","ux","react","vue","angular","css","html",
                "animation","animations","motion","layout","navbar","component",
                "components","tailwind","figma","responsive","interface"},
    "security":{"security","secure","vulnerability","vulnerabilities","xss",
                "csrf","auth","authentication","authorization","secrets","owasp",
                "threat","injection","permissions","masking"},
    "testing":{"pytest","unittest","vitest","jest","playwright","cypress",
               "e2e","coverage","testing","test","tests","regression"},
    "devops":{"docker","kubernetes","k8s","ci","cd","deployment","deploy",
              "terraform","helm","pipeline","pipelines","observability"},
    "data":{"pandas","analytics","dataset","datasets","dataframe","dataframes",
            "statistics","visualization","csv","etl","warehouse"},
    "docs":{"documentation","docs","readme","manual","changelog","tutorial"},
}
MAX_SKILL_BYTES=64*1024

def tokens(value):
    if not isinstance(value,str):
        return set()
    # CamelCase, slash paths, and hyphenated skill names are split into words.
    value=re.sub(r"([a-z0-9])([A-Z])",r"\1 \2",value)
    value=re.sub(r"[/_.+\-]", " ", value)
    return {w.lower() for w in WORDS.findall(value)
            if w.lower() not in STOP and len(w)>=2}

def domains(words):
    return {domain for domain,terms in DOMAIN_TERMS.items() if words & terms}

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
    """Deterministic, abstaining router; metadata is data, never instructions."""
    if not isinstance(max_skills,int) or not 0<=max_skills<=5:
        raise SupervisorError("max_skills out of range")
    if max_skills==0:
        return []
    objective=tokens(task.get("objective",""))
    scopes=tokens(" ".join(task.get("read",[])+task.get("write",[])))
    query=objective | scopes
    task_domains=domains(objective)  # User objective, not arbitrary filenames.
    explicit=task.get("skills",[])
    if not isinstance(explicit,list) or any(not isinstance(x,str) for x in explicit):
        raise SupervisorError("Explicit skills must be a list of names")
    selected=set(explicit)
    ranked=[]
    for item in skills:
        name=item["name"]
        name_words=tokens(name)
        description_words=tokens(item.get("description",""))
        skill_domains=domains(name_words|description_words)
        manual=name in selected
        # A backend API request must not accidentally select a UI/animation
        # skill through generic terms in its description.
        if not manual and task_domains and skill_domains and not (task_domains & skill_domains):
            continue
        name_hits=objective & name_words
        desc_hits=objective & description_words
        scope_hits=scopes & (name_words|description_words)
        # No match is a valid result. Require name evidence or multiple
        # descriptor hits; a domain-specific singleton also qualifies.
        strong=bool(name_hits) or len(desc_hits)>=2 or (
            bool(desc_hits) and bool(task_domains & skill_domains))
        if not manual and not strong:
            continue
        score=(100 if manual else 0)+6*len(name_hits)+2*len(desc_hits)+len(scope_hits)
        if task_domains & skill_domains:
            score+=3
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
