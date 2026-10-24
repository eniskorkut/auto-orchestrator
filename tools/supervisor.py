#!/usr/bin/env python3
"""Portable, observation-based task supervisor. Does NOT spawn or kill native IDE agents."""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sqlite3
import hashlib
import os
import time
from pathlib import Path
try:
    from tools.review_gate import ReviewError, validate_review
except ModuleNotFoundError:
    from review_gate import ReviewError, validate_review

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
    if type(plan.get("require_review", False)) is not bool:
        raise SupervisorError("require_review must be a boolean")
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
    if not all(isinstance(x,dict) and isinstance(x.get("name"),str) and
               x.get("status") in ("passed","failed","skipped") for x in value["tests"]):
        raise SupervisorError("Tests need name and passed/failed/skipped status")
    if any(x["status"]=="failed" for x in value["tests"]):
        raise SupervisorError("Cannot complete a task with failing tests")
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
        if os.name != 'nt':
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
        # Preserve existing V4 SQLite data in place.
        columns={row["name"] for row in self.conn.execute("PRAGMA table_info(runs)")}
        if "native_supervision" not in columns:
            self.conn.execute("ALTER TABLE runs ADD COLUMN native_supervision INTEGER NOT NULL DEFAULT 0")
        if "require_review" not in columns:
            self.conn.execute("ALTER TABLE runs ADD COLUMN require_review INTEGER NOT NULL DEFAULT 0")
        task_columns={r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        if "reviewed" not in task_columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0")
        if "review_digest" not in task_columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN review_digest TEXT")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS native_bindings (
          run_id TEXT NOT NULL, task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
          platform TEXT NOT NULL, native_hash TEXT NOT NULL,
          started INTEGER NOT NULL DEFAULT 0, stopped INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(run_id,task_id,attempt),
          UNIQUE(platform,native_hash));
        CREATE TABLE IF NOT EXISTS native_pending_starts (
          platform TEXT NOT NULL, native_hash TEXT NOT NULL,
          observed REAL NOT NULL, PRIMARY KEY(platform,native_hash));
        CREATE TABLE IF NOT EXISTS native_pending_stops (
          platform TEXT NOT NULL, native_hash TEXT NOT NULL,
          observed REAL NOT NULL, PRIMARY KEY(platform,native_hash));
        """)
        native_cols={row["name"] for row in self.conn.execute("PRAGMA table_info(native_bindings)")}
        if "started" not in native_cols:
            # Existing V6 bindings are not trustworthy start/stop evidence.
            # Old rows remain bound but must see fresh native start before stop.
            self.conn.execute("ALTER TABLE native_bindings ADD COLUMN started INTEGER NOT NULL DEFAULT 0")
            self.conn.execute("UPDATE native_bindings SET stopped=0")
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
        native_supervision=plan.get("native_supervision", False) if isinstance(plan,dict) else False
        if type(native_supervision) is not bool:
            raise SupervisorError("native_supervision must be a boolean")
        tasks, limit=validate_plan(plan)
        require_review=plan.get("require_review", False)
        def op():
            self.conn.execute("""INSERT INTO runs
              (id,created,max_parallel,native_supervision,require_review) VALUES (?,?,?,?,?)""",
              (run,time.time(),limit,int(native_supervision),int(require_review)))
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
    def change(self,run,task_id,lease,action,now=None,result=None,reason=None,review=None):
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
            settings=self.conn.execute(
                "SELECT native_supervision,require_review FROM runs WHERE id=?",(run,)).fetchone()
            supervised=bool(settings["native_supervision"])
            review_proof=None
            if action=="completed" and settings["require_review"]:
                try:
                    review_proof=validate_review(review,task_id=task_id,
                        attempt=task["attempts"],result=result)
                except ReviewError as exc:
                    raise SupervisorError("Quality review rejected: "+str(exc)) from exc
            if supervised:
                binding=self.conn.execute("""
                    SELECT stopped FROM native_bindings
                    WHERE run_id=? AND task_id=? AND attempt=?
                """,(run,task_id,task["attempts"])).fetchone()
                if not binding or not binding["stopped"]:
                    raise SupervisorError("Native termination evidence required for supervised task transition")
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
              result=?, reason=?, reviewed=?, review_digest=? WHERE run_id=? AND id=?""",
              (action,now,json.dumps(result) if result is not None else None,reason,
               int(review_proof is not None),
               review_proof["result_sha256"] if review_proof else None,run,task_id))
            return {"id":task_id,"state":action,"attempt":task["attempts"]}
        return self.transaction(op)
    @staticmethod
    def native_hash(platform,native_id):
        if platform not in ("codex","cursor","opencode") or not isinstance(native_id,str) or not 4 <= len(native_id) <= 256:
            raise SupervisorError("Unsupported native platform or native ID")
        if any(ord(ch) < 33 or ord(ch) > 126 for ch in native_id):
            raise SupervisorError("Invalid native ID")
        return hashlib.sha256((platform+"|"+native_id).encode()).hexdigest()

    @staticmethod
    def validate_native_hash(platform, digest):
        if platform not in ("codex","cursor","opencode") or not isinstance(digest,str) or not re.fullmatch(r"[0-9a-f]{64}",digest):
            raise SupervisorError("Invalid native platform or hash")
        return digest

    def bind_native(self,run,task_id,lease,platform,native_id=None,native_hash=None):
        """Bind an attempt by native ID or its platform-scoped SHA256 digest."""
        if (native_id is None) == (native_hash is None):
            raise SupervisorError("Exactly one native ID or native hash is required")
        digest=(self.native_hash(platform,native_id) if native_id is not None
                else self.validate_native_hash(platform,native_hash))
        identifier(task_id)
        def op():
            rows=self._tasks(run)
            item=rows.get(task_id)
            if not item or item["state"]!="running" or not lease or item["lease"]!=lease:
                raise SupervisorError("Native binding requires a valid active task lease")
            supervised=self.conn.execute(
                "SELECT native_supervision FROM runs WHERE id=?",(run,)).fetchone()[0]
            if not supervised:
                raise SupervisorError("Enable native_supervision in the run plan first")
            prior=self.conn.execute("""
                SELECT platform,native_hash FROM native_bindings
                WHERE run_id=? AND task_id=? AND attempt=?
            """,(run,task_id,item["attempts"])).fetchone()
            if prior:
                if prior["platform"]!=platform or prior["native_hash"]!=digest:
                    raise SupervisorError("Attempt already bound to a different native agent")
                return {"bound":True,"idempotent":True,"task_id":task_id,"attempt":item["attempts"]}
            try:
                self.conn.execute("""
                INSERT INTO native_bindings (run_id,task_id,attempt,platform,native_hash)
                VALUES (?,?,?,?,?)
                """,(run,task_id,item["attempts"],platform,digest))
            except sqlite3.IntegrityError as exc:
                raise SupervisorError("Native agent already bound to another attempt") from exc
            # Some native clients emit SubagentStart before the parent has
            # obtained the ID and called bind-native. Consume a recent hook
            # observation only if it follows this claim's start timestamp.
            # A stop-only event is never buffered or accepted.
            pending=self.conn.execute("""
              SELECT observed FROM native_pending_starts
              WHERE platform=? AND native_hash=?
            """,(platform,digest)).fetchone()
            accepted_start=bool(pending and item["started"] is not None and (
                item["started"]-2 <= pending["observed"] <= time.time()+2 and
                time.time()-pending["observed"] <= 600))
            stop=self.conn.execute("""
              SELECT observed FROM native_pending_stops
              WHERE platform=? AND native_hash=?
            """,(platform,digest)).fetchone()
            accepted_stop=bool(accepted_start and stop and
                pending["observed"] <= stop["observed"] <= time.time()+2 and
                time.time()-stop["observed"] <= 600)
            if accepted_start:
                self.conn.execute("""
                  UPDATE native_bindings SET started=1, stopped=?
                  WHERE run_id=? AND task_id=? AND attempt=?
                """,(int(accepted_stop),run,task_id,item["attempts"]))
            for table in ("native_pending_starts","native_pending_stops"):
                self.conn.execute(f"""
                  DELETE FROM {table} WHERE platform=? AND native_hash=?
                """,(platform,digest))
            return {"bound":True,"idempotent":False,"task_id":task_id,"attempt":item["attempts"]}
        return self.transaction(op)

    def observe_native(self,platform,native_id,event):
        return self.observe_hash(platform,self.native_hash(platform,native_id),event)

    def observe_hash(self,platform,digest,event):
        """Trusted native lifecycle evidence only; NEVER complete a task here."""
        self.validate_native_hash(platform,digest)
        if event not in ("start","stop"):
            raise SupervisorError("Unknown native observation")
        def op():
            binding=self.conn.execute("""
              SELECT b.run_id,b.task_id,b.attempt,b.started,b.stopped,t.attempts,t.state
              FROM native_bindings b JOIN tasks t ON
                t.run_id=b.run_id AND t.id=b.task_id
              WHERE b.platform=? AND b.native_hash=?
            """,(platform,digest)).fetchone()
            if not binding or binding["attempt"]!=binding["attempts"] or binding["state"] not in ACTIVE:
                if not binding:
                    now=time.time()
                    for table in ("native_pending_starts","native_pending_stops"):
                        self.conn.execute(f"DELETE FROM {table} WHERE observed<?",(now-600,))
                    # Never collect unmatched starts when no supervised attempt is running.
                    active=self.conn.execute("""
                      SELECT 1 FROM tasks t JOIN runs r ON t.run_id=r.id
                      WHERE r.native_supervision=1 AND t.state='running' LIMIT 1
                    """).fetchone()
                    if active and event=="start":
                        self.conn.execute("""
                          INSERT INTO native_pending_starts(platform,native_hash,observed)
                          VALUES (?,?,?)
                          ON CONFLICT(platform,native_hash) DO UPDATE SET observed=excluded.observed
                        """,(platform,digest,now))
                        self.conn.execute("DELETE FROM native_pending_stops WHERE platform=? AND native_hash=?",(platform,digest))
                    elif active and event=="stop":
                        pending=self.conn.execute("""
                          SELECT observed FROM native_pending_starts
                          WHERE platform=? AND native_hash=?
                        """,(platform,digest)).fetchone()
                        if pending and pending["observed"]<=now:
                            self.conn.execute("""
                              INSERT INTO native_pending_stops(platform,native_hash,observed)
                              VALUES (?,?,?)
                              ON CONFLICT(platform,native_hash) DO UPDATE SET observed=excluded.observed
                            """,(platform,digest,now))
                return {"matched":False}
            if event=="start" and not binding["started"]:
                self.conn.execute("""
                  UPDATE native_bindings SET started=1
                  WHERE run_id=? AND task_id=? AND attempt=?
                """,(binding["run_id"],binding["task_id"],binding["attempt"]))
            if event=="stop" and binding["started"] and not binding["stopped"]:
                self.conn.execute("""
                  UPDATE native_bindings SET stopped=1
                  WHERE run_id=? AND task_id=? AND attempt=?
                """,(binding["run_id"],binding["task_id"],binding["attempt"]))
            accepted=bool(binding["started"] or event=="start")
            return {"matched":accepted,"task_id":binding["task_id"],
                    "native_start_observed":accepted,
                    "native_stop_observed":bool(binding["stopped"] or (event=="stop" and binding["started"]))}
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
              result=NULL, reason=?, termination_confirmed=0, reviewed=0, review_digest=NULL WHERE run_id=? AND id=?""",(reason,run,task_id))
            return {"id":task_id,"state":"pending","attempts_used":task["attempts"]}
        return self.transaction(op)
    def status(self,run):
        rows=self._tasks(run)
        settings=self.conn.execute(
            "SELECT native_supervision,require_review FROM runs WHERE id=?",(run,)).fetchone()
        supervised=bool(settings["native_supervision"])
        review_required=bool(settings["require_review"])
        bindings={(row["task_id"],row["attempt"]):bool(row["stopped"]) for row in
                  self.conn.execute("SELECT task_id,attempt,stopped FROM native_bindings WHERE run_id=?",(run,))}
        return {"run_id":run,"native_supervision":supervised,"require_review":review_required,"tasks":[{"id":t["id"],"state":t["state"],"role":t["role"],
                 "native_stop_observed":(t["state"]!="pending" and bindings.get((t["id"],t["attempts"]),False)),
                 "attempts":t["attempts"],"max_retries":t["max_retries"],
                 "review_attested":bool(t["reviewed"]), "review_digest":t["review_digest"],
                 "reason":t["reason"],
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
        if name=="completed": q.add_argument("--review",help="V6.2 structured review JSON (opt-in)")
    bind=sub.add_parser("bind-native");bind.add_argument("--run-id",required=True)
    bind.add_argument("--task-id",required=True);bind.add_argument("--lease",required=True)
    bind.add_argument("--platform",required=True)
    bind_ids=bind.add_mutually_exclusive_group(required=True)
    bind_ids.add_argument("--native-id");bind_ids.add_argument("--native-hash")
    observe=sub.add_parser("observe-native");observe.add_argument("--platform",required=True)
    observe_ids=observe.add_mutually_exclusive_group(required=True)
    observe_ids.add_argument("--native-id");observe_ids.add_argument("--native-hash")
    observe.add_argument("--event",choices=["start","stop"],required=True)
    cancel=sub.add_parser("cancel");cancel.add_argument("--run-id",required=True);cancel.add_argument("--task-id",required=True)
    retry=sub.add_parser("retry");retry.add_argument("--run-id",required=True);retry.add_argument("--task-id",required=True)
    retry.add_argument("--reason",required=True)
    a=p.parse_args(argv)
    try:
        s=Supervisor(a.root)
        if a.command=="init": result=s.init(a.run_id,json.loads(Path(a.plan).read_text(encoding="utf-8")))
        elif a.command=="claim": result=s.claim(a.run_id)
        elif a.command=="status": result=s.status(a.run_id)
        elif a.command=="bind-native": result=s.bind_native(a.run_id,a.task_id,a.lease,a.platform,a.native_id,a.native_hash)
        elif a.command=="observe-native":
            result=(s.observe_native(a.platform,a.native_id,a.event) if a.native_id is not None
                    else s.observe_hash(a.platform,a.native_hash,a.event))
        elif a.command=="cancel": result=s.cancel(a.run_id,a.task_id)
        elif a.command=="retry": result=s.retry(a.run_id,a.task_id,a.reason)
        else:
            payload=json.loads(Path(a.result).read_text(encoding="utf-8")) if a.result else None
            review=json.loads(Path(a.review).read_text(encoding="utf-8")) if getattr(a,"review",None) else None
            result=s.change(a.run_id,a.task_id,a.lease,a.command,result=payload,reason=a.reason,review=review)
        print(json.dumps(result,indent=2))
    except (SupervisorError,OSError,ValueError,sqlite3.Error) as exc:
        p.exit(1,"SUPERVISOR ERROR: "+str(exc)+"\n")
if __name__=="__main__":
    main()
