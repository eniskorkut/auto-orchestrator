#!/usr/bin/env python3
"""Portable, observation-based task supervisor. Does NOT spawn or kill native IDE agents."""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
ROLES = {"explorer", "implementer", "tester", "reviewer"}
ACTIVE = {"running", "cancel_requested", "timed_out"}
TERMINAL = {"completed", "failed", "cancelled"}

class SupervisorError(ValueError):
    pass

def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise SupervisorError("Invalid task/run identifier")
    return value

def scope(value):
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise SupervisorError("Scopes must be relative POSIX paths")
    parts = value.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in parts) or any("*" in p or "?" in p for p in parts):
        raise SupervisorError("Scope contains unsafe path component")
    return value.rstrip("/")

def intersects(left, right):
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")

def conflicts(a, b):
    # Protect readers from concurrent writes as well as writer/writer races.
    return any(intersects(x,y) for x in a["write"] for y in b["write"] + b["read"]) or any(
        intersects(x,y) for x in b["write"] for y in a["read"])

def validate_plan(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list) or not 1 <= len(plan["tasks"]) <= 64:
        raise SupervisorError("Plan requires 1..64 tasks")
    max_parallel = plan.get("max_parallel", 3)
    if type(max_parallel) is not int or not 1 <= max_parallel <= 3:
        raise SupervisorError("max_parallel must be 1..3")
    tasks = {}
    for item in plan["tasks"]:
        if not isinstance(item,dict):
            raise SupervisorError("Each task must be an object")
        tid = identifier(item.get("id"))
        if tid in tasks:
            raise SupervisorError("Duplicate task id")
        role = item.get("role")
        if role not in ROLES:
            raise SupervisorError("Unknown role")
        objective = item.get("objective")
        if not isinstance(objective,str) or not 1 <= len(objective) <= 1500:
            raise SupervisorError("Objective must be 1..1500 characters")
        deps = item.get("depends_on", [])
        if not isinstance(deps,list) or any(not isinstance(d,str) for d in deps) or len(set(deps)) != len(deps):
            raise SupervisorError("Invalid dependencies")
        read = item.get("read_scope", [])
        write = item.get("write_scope", [])
        if not isinstance(read,list) or not isinstance(write,list) or len(read)+len(write)>40:
            raise SupervisorError("Invalid file scope")
        read = [scope(x) for x in read]
        write = [scope(x) for x in write]
        timeout = item.get("timeout_seconds", 900)
        retries = item.get("max_retries", 2)
        if type(timeout) not in (int,float) or not 1 <= timeout <= 86400:
            raise SupervisorError("timeout_seconds out of bounds")
        if type(retries) is not int or not 0 <= retries <= 2:
            raise SupervisorError("max_retries must be 0..2")
        tasks[tid] = {"id":tid,"role":role,"objective":objective,"depends_on":deps,
                      "read":read,"write":write,"timeout_seconds":float(timeout),"max_retries":retries}
    visited = set()
    visiting = set()
    def visit(tid):
        if tid in visiting:
            raise SupervisorError("Dependency cycle detected")
        if tid in visited:
            return
        visiting.add(tid)
        for dependency in tasks[tid]["depends_on"]:
            if dependency not in tasks or dependency == tid:
                raise SupervisorError("Invalid dependency reference")
            visit(dependency)
        visiting.remove(tid)
        visited.add(tid)
    for tid in tasks:
        visit(tid)
    return tasks, max_parallel

def validate_result(value):
    if not isinstance(value,dict) or not isinstance(value.get("summary"),str) or len(value["summary"])>2000:
        raise SupervisorError("Result needs a summary")
    for key in ("changed_files","tests","findings"):
        if not isinstance(value.get(key), list) or len(value[key])>100:
            raise SupervisorError(f"Result requires {key} array")
    for name in value["changed_files"]:
        scope(name)
    if any(x.get("status")=="failed" for x in value["tests"]):
        raise SupervisorError("Cannot complete a task with failing tests")
    if not all(isinstance(x,dict) and isinstance(x.get("name"),str) and
               x.get("status") in ("passed","failed","skipped") for x in value["tests"]):
        raise SupervisorError("Tests need name and passed/failed/skipped status")
    if not all(isinstance(x,str) and len(x)<=1000 for x in value["findings"]):
        raise SupervisorError("Findings must be short strings")
    return value

class Supervisor:
    def __init__(self, root):
        p = Path(root)/".auto-orchestrator"/"supervisor.sqlite3"
        p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.conn=sqlite3.connect(p,timeout=20,isolation_level=None)
        # The lease database stores active native task tokens. Restrict access
        # to the current user on POSIX; Windows access follows the user's ACL.
        if __import__('os').name != 'nt':
            p.chmod(0o600)
        self.conn.row_factory=sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=20000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, created REAL NOT NULL, max_parallel INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (
          run_id TEXT NOT NULL, id TEXT NOT NULL, role TEXT NOT NULL, objective TEXT NOT NULL,
          dependencies TEXT NOT NULL, read_scope TEXT NOT NULL, write_scope TEXT NOT NULL,
          timeout_seconds REAL NOT NULL, max_retries INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL DEFAULT 'pending', lease TEXT, started REAL, finished REAL,
          termination_confirmed INTEGER NOT NULL DEFAULT 0, result TEXT, reason TEXT,
          PRIMARY KEY(run_id,id), FOREIGN KEY(run_id) REFERENCES runs(id));
        """)
    def close(self):
        self.conn.close()

    def transaction(self, fn):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            result=fn()
            self.conn.execute("COMMIT")
            return result
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
    def init(self, run, plan):
        identifier(run)
        tasks, limit=validate_plan(plan)
        def op():
            self.conn.execute("INSERT INTO runs VALUES (?,?,?)",(run,time.time(),limit))
            for item in tasks.values():
                self.conn.execute("""INSERT INTO tasks
                  (run_id,id,role,objective,dependencies,read_scope,write_scope,timeout_seconds,max_retries)
                  VALUES (?,?,?,?,?,?,?,?,?)""",
                  (run,item["id"],item["role"],item["objective"],json.dumps(item["depends_on"]),
                   json.dumps(item["read"]),json.dumps(item["write"]),item["timeout_seconds"],item["max_retries"]))
            return {"run_id":run,"tasks":len(tasks),"max_parallel":limit}
        try:
            return self.transaction(op)
        except sqlite3.IntegrityError as exc:
            raise SupervisorError("Run already exists") from exc
    def _tasks(self,run):
        identifier(run)
        if not self.conn.execute("SELECT 1 FROM runs WHERE id=?",(run,)).fetchone():
            raise SupervisorError("Run does not exist")
        return {r["id"]:dict(r) for r in self.conn.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY rowid",(run,))}
    def _reap(self,run,rows,now):
        timed_out=[]
        for task in rows.values():
            if task["state"]=="running" and now-task["started"]>=task["timeout_seconds"]:
                self.conn.execute("UPDATE tasks SET state='timed_out', reason=? WHERE run_id=? AND id=?",
                                  ("Timeout: native cancellation must be confirmed",run,task["id"]))
                task["state"]="timed_out"
                timed_out.append(task["id"])
        return timed_out
    def claim(self,run,now=None):
        now=time.time() if now is None else now
        def op():
            rows=self._tasks(run)
            timed_out=self._reap(run,rows,now)
            limit=self.conn.execute("SELECT max_parallel FROM runs WHERE id=?",(run,)).fetchone()[0]
            active=[r for r in rows.values() if r["state"] in ACTIVE]
            available=max(0,limit-len(active))
            selected=[]
            for task in rows.values():
                if len(selected)>=available:
                    break
                if task["state"]!="pending" or not all(rows[x]["state"]=="completed" for x in json.loads(task["dependencies"])):
                    continue
                left={"read":json.loads(task["read_scope"]),"write":json.loads(task["write_scope"])}
                if any(conflicts(left,{"read":json.loads(b["read_scope"]),"write":json.loads(b["write_scope"])})
                       for b in active):
                    continue
                lease='ao_'+secrets.token_hex(24)
                attempts=task["attempts"]+1
                self.conn.execute("UPDATE tasks SET state='running', lease=?, attempts=?, started=?, finished=NULL WHERE run_id=? AND id=?",
                                  (lease,attempts,now,run,task["id"]))
                obj={"id":task["id"],"role":task["role"],"objective":task["objective"],
                     "depends_on":json.loads(task["dependencies"]),"read_scope":left["read"],
                     "write_scope":left["write"],"attempt":attempts,"lease":lease}
                selected.append(obj)
                active.append(task | {"state":"running"})
            return {"claimed":selected,"timed_out":timed_out,
                    "native_cancellation_required":timed_out}
        return self.transaction(op)
    def change(self,run,task_id,lease,action,now=None,result=None,reason=None):
        now=time.time() if now is None else now
        identifier(task_id)
        if action=="completed":
            validate_result(result)
        def op():
            rows=self._tasks(run)
            if task_id not in rows:
                raise SupervisorError("Unknown task")
            task=rows[task_id]
            if action=="completed":
                allowed=json.loads(task["write_scope"])
                if any(not any(intersects(file_path,base) and
                                   (file_path==base or file_path.startswith(base+"/")) for base in allowed)
                       for file_path in result["changed_files"]):
                    raise SupervisorError("Changed files exceed assigned write scope")
            if task["lease"]!=lease or not lease:
                raise SupervisorError("Stale or invalid attempt lease")
            if action in ("completed","failed"):
                if task["state"]!="running":
                    raise SupervisorError("Task is not running")
            elif action=="cancelled":
                if task["state"] not in ("running","cancel_requested","timed_out"):
                    raise SupervisorError("No native task to acknowledge")
            else:
                raise SupervisorError("Invalid transition")
            self.conn.execute("""UPDATE tasks SET state=?, finished=?, termination_confirmed=1,
              result=?, reason=? WHERE run_id=? AND id=?""",
              (action,now,json.dumps(result) if result is not None else None,reason,run,task_id))
            return {"id":task_id,"state":action,"attempt":task["attempts"]}
        return self.transaction(op)
    def cancel(self,run,task_id):
        identifier(task_id)
        def op():
            rows=self._tasks(run)
            if task_id not in rows:
                raise SupervisorError("Unknown task")
            task=rows[task_id]
            if task["state"]=="pending":
                self.conn.execute("UPDATE tasks SET state='cancelled', finished=?, termination_confirmed=1 WHERE run_id=? AND id=?",
                                  (time.time(),run,task_id))
                return {"id":task_id,"native_cancellation_required":False}
            if task["state"] in ("running","cancel_requested","timed_out"):
                self.conn.execute("UPDATE tasks SET state='cancel_requested' WHERE run_id=? AND id=?",(run,task_id))
                return {"id":task_id,"native_cancellation_required":True,"lease":task["lease"]}
            raise SupervisorError("Cannot cancel a finished task")
        return self.transaction(op)
    def retry(self,run,task_id,reason):
        if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:
            raise SupervisorError("Retry requires a short actionable reason")
        def op():
            rows=self._tasks(run)
            if task_id not in rows:
                raise SupervisorError("Unknown task")
            task=rows[task_id]
            if task["state"] not in ("failed","cancelled") or not task["termination_confirmed"]:
                raise SupervisorError("Retry requires confirmed native termination")
            if task["attempts"]>=task["max_retries"]+1:
                raise SupervisorError("Retry limit reached")
            self.conn.execute("""UPDATE tasks SET state='pending', lease=NULL, started=NULL, finished=NULL,
              result=NULL, reason=?, termination_confirmed=0 WHERE run_id=? AND id=?""",(reason,run,task_id))
            return {"id":task_id,"state":"pending","attempts_used":task["attempts"]}
        return self.transaction(op)
    def status(self,run):
        rows=self._tasks(run)
        return {"run_id":run,"tasks":[{"id":t["id"],"state":t["state"],"role":t["role"],
                 "attempts":t["attempts"],"max_retries":t["max_retries"],"reason":t["reason"],
                 "result":json.loads(t["result"]) if t["result"] else None} for t in rows.values()],
                "complete":all(t["state"]=="completed" for t in rows.values()),
                "unconfirmed_native_tasks":[t["id"] for t in rows.values() if t["state"] in ACTIVE]}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",default=".")
    sub=p.add_subparsers(dest="command",required=True)
    init=sub.add_parser("init");init.add_argument("--run-id",required=True);init.add_argument("--plan",required=True)
    claim=sub.add_parser("claim");claim.add_argument("--run-id",required=True)
    status=sub.add_parser("status");status.add_argument("--run-id",required=True)
    for name in ("completed","failed","cancelled"):
        q=sub.add_parser(name);q.add_argument("--run-id",required=True);q.add_argument("--task-id",required=True)
        q.add_argument("--lease",required=True);q.add_argument("--result");q.add_argument("--reason")
    cancel=sub.add_parser("cancel");cancel.add_argument("--run-id",required=True);cancel.add_argument("--task-id",required=True)
    retry=sub.add_parser("retry");retry.add_argument("--run-id",required=True);retry.add_argument("--task-id",required=True)
    retry.add_argument("--reason",required=True)
    a=p.parse_args(argv)
    try:
        s=Supervisor(a.root)
        if a.command=="init": result=s.init(a.run_id,json.loads(Path(a.plan).read_text(encoding="utf-8")))
        elif a.command=="claim": result=s.claim(a.run_id)
        elif a.command=="status": result=s.status(a.run_id)
        elif a.command=="cancel": result=s.cancel(a.run_id,a.task_id)
        elif a.command=="retry": result=s.retry(a.run_id,a.task_id,a.reason)
        else:
            payload=json.loads(Path(a.result).read_text(encoding="utf-8")) if a.result else None
            result=s.change(a.run_id,a.task_id,a.lease,a.command,result=payload,reason=a.reason)
        print(json.dumps(result,indent=2))
    except (SupervisorError,OSError,ValueError,sqlite3.Error) as exc:
        p.exit(1,"SUPERVISOR ERROR: "+str(exc)+"\n")
if __name__=="__main__":
    main()
