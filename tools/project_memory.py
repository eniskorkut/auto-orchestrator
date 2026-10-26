#!/usr/bin/env python3
"""Durable project memory, decision ledger and requirements ledger.

SQLite is the source of truth. Markdown/JSON context files under
``.auto-orchestrator/context`` are deterministic, sanitized projections that an
IDE can load cheaply. No raw prompts, secrets or unconfirmed recommendations
are promoted to confirmed decisions.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
from pathlib import Path

try:
    from tools.project_identity import (
        ProjectIdentityError,
        ensure_project_identity,
        export_identity,
    )
    from tools.run_owner import atomic_write_json, read_json
except ModuleNotFoundError:
    from project_identity import (  # type: ignore
        ProjectIdentityError,
        ensure_project_identity,
        export_identity,
    )
    from run_owner import atomic_write_json, read_json  # type: ignore

SCHEMA_VERSION = 1
DECISION_STATUSES = {
    "confirmed", "code_derived", "unresolved", "superseded", "rejected",
}
REQUIREMENT_STATUSES = {
    "confirmed", "code_derived", "doc_constraint", "unresolved_blocking",
    "unresolved_nonblocking", "conflicting", "superseded",
}
SOURCES = {"user", "code", "docs", "agent", "import", "git"}
DECISION_ID = re.compile(r"^d_[0-9a-f]{16}$")
REQUIREMENT_ID = re.compile(r"^r_[0-9a-f]{16}$")
MAX_TEXT = 4000
MAX_ITEMS = 200


class MemoryError(ValueError):
    pass


def _id(prefix: str) -> str:
    return prefix + secrets.token_hex(8)


def _text(value, field: str, *, required=True, limit=MAX_TEXT) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise MemoryError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise MemoryError(f"{field} is required")
    if len(value) > limit:
        raise MemoryError(f"{field} is too long")
    if "\x00" in value:
        raise MemoryError(f"{field} must not contain NUL")
    return value


def _list(value, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise MemoryError(f"{field} must be a bounded array")
    result = []
    for item in value:
        text = _text(item, field, limit=1000)
        if text not in result:
            result.append(text)
    return result


def _optional_id(value, pattern, field):
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise MemoryError(f"{field} is invalid")
    return value


class ProjectMemory:
    def __init__(self, root, *, identity: dict | None = None):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise MemoryError("project root does not exist")
        self.identity = identity or ensure_project_identity(self.root)
        self.project_id = self.identity["project_id"]
        self.directory = self.root / ".auto-orchestrator"
        if self.directory.is_symlink():
            raise MemoryError("project state directory must not be a symlink")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.directory / "memory.sqlite3"
        self.context_dir = self.directory / "context"
        if self.database.is_symlink() or self.context_dir.is_symlink():
            raise MemoryError("project memory files must not be symlinks")
        self.conn = sqlite3.connect(str(self.database), timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._schema()
        if os.name != "nt":
            for path in (self.directory, self.context_dir):
                try:
                    path.chmod(0o700)
                except OSError:
                    pass
            try:
                self.database.chmod(0o600)
            except OSError:
                pass

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _schema(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS memory_meta (
          project_id TEXT PRIMARY KEY,
          project_uuid TEXT,
          last_platform TEXT,
          last_run_id TEXT,
          last_milestone TEXT,
          head_sha TEXT,
          branch TEXT,
          updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS decisions (
          decision_id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          topic TEXT NOT NULL,
          decision TEXT NOT NULL,
          rationale TEXT NOT NULL,
          alternatives TEXT NOT NULL,
          source TEXT NOT NULL,
          source_ref TEXT,
          confirmed_by TEXT,
          confirmed_at REAL,
          status TEXT NOT NULL,
          supersedes TEXT,
          affected_tasks TEXT NOT NULL,
          updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS decisions_project
          ON decisions(project_id,status,updated_at);
        CREATE TABLE IF NOT EXISTS requirements (
          requirement_id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          text TEXT NOT NULL,
          category TEXT NOT NULL,
          status TEXT NOT NULL,
          source TEXT NOT NULL,
          source_ref TEXT,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS requirements_project
          ON requirements(project_id,status,updated_at);
        CREATE TABLE IF NOT EXISTS project_handoffs (
          handoff_id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          run_id TEXT NOT NULL,
          milestone_id TEXT NOT NULL,
          version INTEGER NOT NULL,
          platform TEXT NOT NULL,
          payload TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          created_at REAL NOT NULL,
          UNIQUE(project_id,run_id,milestone_id,version)
        );
        CREATE TABLE IF NOT EXISTS observations (
          observation_id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          kind TEXT NOT NULL,
          commit_sha TEXT,
          branch TEXT,
          files TEXT NOT NULL,
          test_evidence TEXT,
          platform TEXT,
          created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS interviews (
          interview_id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          origin_platform TEXT NOT NULL,
          request_hash TEXT NOT NULL,
          request_summary TEXT NOT NULL,
          mode TEXT NOT NULL,
          status TEXT NOT NULL,
          approved_at REAL,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          schema_version INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS interviews_project
          ON interviews(project_id,status,updated_at);
        CREATE TABLE IF NOT EXISTS interview_questions (
          interview_id TEXT NOT NULL,
          question_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL,
          question_key TEXT NOT NULL,
          question TEXT NOT NULL,
          options TEXT NOT NULL,
          recommended_option TEXT,
          selected_option TEXT,
          answer_text TEXT,
          answer_source TEXT,
          status TEXT NOT NULL,
          depends_on TEXT NOT NULL,
          created_at REAL NOT NULL,
          answered_at REAL,
          PRIMARY KEY(interview_id,question_id),
          UNIQUE(interview_id,question_key)
        );
        CREATE INDEX IF NOT EXISTS questions_open
          ON interview_questions(interview_id,status,ordinal);
        """)
        self.conn.commit()

    def _transaction(self, fn):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn()
            self.conn.commit()
            return result
        except BaseException:
            self.conn.rollback()
            raise

    # -- decisions -------------------------------------------------------
    def add_decision(self, *, topic, decision, rationale="", alternatives=None,
                     source="agent", source_ref=None, status="code_derived",
                     confirmed_by=None, affected_tasks=None, supersedes=None,
                     decision_id=None) -> dict:
        topic = _text(topic, "topic", limit=300)
        decision = _text(decision, "decision", limit=2000)
        rationale = _text(rationale, "rationale", required=False, limit=2000)
        alternatives = _list(alternatives, "alternatives")
        affected_tasks = _list(affected_tasks, "affected_tasks")
        if source not in SOURCES:
            raise MemoryError("unsupported decision source")
        if status not in DECISION_STATUSES:
            raise MemoryError("unsupported decision status")
        if status == "confirmed":
            if not isinstance(confirmed_by, str) or not confirmed_by.strip():
                raise MemoryError("confirmed decisions require a user confirmation")
            if confirmed_by.strip().lower() in ("agent", "recommended", "auto", "system"):
                raise MemoryError("recommendations cannot be recorded as confirmed")
            confirmed_at = time.time()
        else:
            confirmed_by = None
            confirmed_at = None
        supersedes = _optional_id(supersedes, DECISION_ID, "supersedes")
        decision_id = decision_id or _id("d_")
        if not DECISION_ID.fullmatch(decision_id):
            raise MemoryError("invalid decision id")
        existing = self.conn.execute(
            "SELECT * FROM decisions WHERE project_id=? AND topic=? AND decision=? "
            "AND status<>'superseded'", (self.project_id, topic, decision)
        ).fetchone()
        if existing is not None:
            return self._decision_dict(existing)
        now = time.time()

        def op():
            if supersedes:
                row = self.conn.execute(
                    "SELECT status FROM decisions WHERE project_id=? AND decision_id=?",
                    (self.project_id, supersedes)).fetchone()
                if row is None:
                    raise MemoryError("superseded decision does not exist")
                self.conn.execute(
                    "UPDATE decisions SET status='superseded',updated_at=? "
                    "WHERE project_id=? AND decision_id=?",
                    (now, self.project_id, supersedes))
            self.conn.execute("""
              INSERT INTO decisions(decision_id,project_id,topic,decision,rationale,
                alternatives,source,source_ref,confirmed_by,confirmed_at,status,
                supersedes,affected_tasks,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (decision_id, self.project_id, topic, decision, rationale,
                  json.dumps(alternatives, ensure_ascii=False), source,
                  _text(source_ref, "source_ref", required=False, limit=500) or None,
                  confirmed_by, confirmed_at, status, supersedes,
                  json.dumps(affected_tasks, ensure_ascii=False), now))

        self._transaction(op)
        return self.get_decision(decision_id)

    def get_decision(self, decision_id):
        row = self.conn.execute(
            "SELECT * FROM decisions WHERE project_id=? AND decision_id=?",
            (self.project_id, decision_id)).fetchone()
        if row is None:
            raise MemoryError("decision does not exist")
        return self._decision_dict(row)

    def list_decisions(self, *, status=None, topic=None, limit=100) -> list[dict]:
        clauses = ["project_id=?"]
        values = [self.project_id]
        if status is not None:
            if status not in DECISION_STATUSES:
                raise MemoryError("unsupported decision status")
            clauses.append("status=?")
            values.append(status)
        if topic is not None:
            clauses.append("topic LIKE ?")
            values.append(f"%{_text(topic, 'topic', limit=200)}%")
        values.append(min(max(int(limit), 1), 500))
        rows = self.conn.execute(
            f"SELECT * FROM decisions WHERE {' AND '.join(clauses)} "
            "ORDER BY updated_at DESC LIMIT ?", values).fetchall()
        return [self._decision_dict(row) for row in rows]

    def mark_decisions_superseded(self, *, source_ref, reason=""):
        if not isinstance(source_ref, str) or not source_ref:
            raise MemoryError("source_ref is required")
        self.conn.execute(
            "UPDATE decisions SET status='superseded',updated_at=? "
            "WHERE project_id=? AND source_ref=? AND status<>'superseded'",
            (time.time(), self.project_id, source_ref))
        self.conn.commit()

    @staticmethod
    def _decision_dict(row):
        value = dict(row)
        for key in ("alternatives", "affected_tasks"):
            try:
                value[key] = json.loads(value.get(key) or "[]")
            except ValueError:
                value[key] = []
        return value

    # -- requirements ----------------------------------------------------
    def add_requirement(self, text, *, category="unresolved_blocking",
                        source="user", source_ref=None,
                        status=None, requirement_id=None) -> dict:
        text = _text(text, "requirement")
        if status is None:
            status = category
        if status not in REQUIREMENT_STATUSES:
            raise MemoryError("unsupported requirement status")
        if source not in SOURCES:
            raise MemoryError("unsupported requirement source")
        requirement_id = requirement_id or _id("r_")
        if not REQUIREMENT_ID.fullmatch(requirement_id):
            raise MemoryError("invalid requirement id")
        now = time.time()
        self.conn.execute("""
          INSERT INTO requirements(requirement_id,project_id,text,category,status,
            source,source_ref,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?)
        """, (requirement_id, self.project_id, text, _text(category, "category",
              limit=80), status, source,
              _text(source_ref, "source_ref", required=False, limit=500) or None,
              now, now))
        self.conn.commit()
        return self.get_requirement(requirement_id)

    def get_requirement(self, requirement_id):
        row = self.conn.execute(
            "SELECT * FROM requirements WHERE project_id=? AND requirement_id=?",
            (self.project_id, requirement_id)).fetchone()
        if row is None:
            raise MemoryError("requirement does not exist")
        return dict(row)

    def list_requirements(self, *, status=None, limit=200) -> list[dict]:
        values = [self.project_id]
        sql = "SELECT * FROM requirements WHERE project_id=?"
        if status is not None:
            if status not in REQUIREMENT_STATUSES:
                raise MemoryError("unsupported requirement status")
            sql += " AND status=?"
            values.append(status)
        values.append(min(max(int(limit), 1), 500))
        return [dict(row) for row in self.conn.execute(
            sql + " ORDER BY updated_at DESC LIMIT ?", values)]

    def resolve_requirement(self, requirement_id, *, status="confirmed",
                            source_ref=None):
        if status not in REQUIREMENT_STATUSES:
            raise MemoryError("unsupported requirement status")
        row = self.get_requirement(requirement_id)
        self.conn.execute(
            "UPDATE requirements SET status=?,source_ref=COALESCE(?,source_ref),"
            "updated_at=? WHERE project_id=? AND requirement_id=?",
            (status, source_ref, time.time(), self.project_id, requirement_id))
        self.conn.commit()
        return self.get_requirement(row["requirement_id"])

    def resolve_requirements_by_ref(self, source_ref, *, status="confirmed"):
        if not isinstance(source_ref, str) or not source_ref:
            raise MemoryError("source_ref is required")
        if status not in REQUIREMENT_STATUSES:
            raise MemoryError("unsupported requirement status")
        cursor = self.conn.execute(
            "UPDATE requirements SET status=?,updated_at=? "
            "WHERE project_id=? AND source_ref=? AND status LIKE 'unresolved%'",
            (status, time.time(), self.project_id, source_ref))
        self.conn.commit()
        return cursor.rowcount

    # -- meta / handoff / observations ----------------------------------
    def update_meta(self, **fields) -> dict:
        allowed = {"last_platform", "last_run_id", "last_milestone", "head_sha",
                   "branch"}
        unknown = set(fields) - allowed
        if unknown:
            raise MemoryError("unsupported project memory field")
        current = self.get_meta()
        now = time.time()
        self.conn.execute("""
          INSERT INTO memory_meta(project_id,project_uuid,last_platform,last_run_id,
            last_milestone,head_sha,branch,updated_at)
          VALUES (?,?,?,?,?,?,?,?)
          ON CONFLICT(project_id) DO UPDATE SET
            last_platform=COALESCE(excluded.last_platform,memory_meta.last_platform),
            last_run_id=COALESCE(excluded.last_run_id,memory_meta.last_run_id),
            last_milestone=COALESCE(excluded.last_milestone,memory_meta.last_milestone),
            head_sha=COALESCE(excluded.head_sha,memory_meta.head_sha),
            branch=COALESCE(excluded.branch,memory_meta.branch),
            updated_at=excluded.updated_at
        """, (self.project_id, self.identity["project_uuid"],
              fields.get("last_platform"), fields.get("last_run_id"),
              fields.get("last_milestone"), fields.get("head_sha"),
              fields.get("branch"), now))
        self.conn.commit()
        return self.get_meta()

    def get_meta(self) -> dict:
        row = self.conn.execute(
            "SELECT * FROM memory_meta WHERE project_id=?", (self.project_id,)
        ).fetchone()
        if row is None:
            return {
                "project_id": self.project_id,
                "project_uuid": self.identity["project_uuid"],
                "last_platform": None, "last_run_id": None,
                "last_milestone": None, "head_sha": None, "branch": None,
                "updated_at": None,
            }
        return dict(row)

    def record_handoff(self, *, run_id, milestone_id, payload, platform="unknown",
                       handoff_id=None) -> dict:
        run_id = _text(run_id, "run_id", limit=100)
        milestone_id = _text(milestone_id, "milestone_id", limit=100)
        if not isinstance(payload, dict):
            raise MemoryError("handoff payload must be an object")
        platform = _text(platform, "platform", limit=40)
        row = self.conn.execute(
            "SELECT MAX(version) FROM project_handoffs WHERE project_id=? "
            "AND run_id=? AND milestone_id=?",
            (self.project_id, run_id, milestone_id)).fetchone()
        version = int(row[0] or 0) + 1
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        handoff_id = handoff_id or _id("h_")
        now = time.time()
        self.conn.execute("""
          INSERT INTO project_handoffs(handoff_id,project_id,run_id,milestone_id,
            version,platform,payload,sha256,created_at)
          VALUES (?,?,?,?,?,?,?,?,?)
        """, (handoff_id, self.project_id, run_id, milestone_id, version,
              platform, canonical, digest, now))
        self.conn.commit()
        return self.get_handoff(handoff_id)

    def get_handoff(self, handoff_id):
        row = self.conn.execute(
            "SELECT * FROM project_handoffs WHERE project_id=? AND handoff_id=?",
            (self.project_id, handoff_id)).fetchone()
        if row is None:
            raise MemoryError("handoff does not exist")
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def latest_handoff(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM project_handoffs WHERE project_id=? "
            "ORDER BY created_at DESC, version DESC LIMIT 1",
            (self.project_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def list_handoffs(self, limit=20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM project_handoffs WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (self.project_id, min(max(int(limit), 1), 100))).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            result.append(value)
        return result

    def record_observation(self, *, kind, commit_sha=None, branch=None, files=None,
                           test_evidence=None, platform=None) -> dict:
        kind = _text(kind, "kind", limit=80)
        files = _list(files, "files")
        observation_id = _id("o_")
        now = time.time()
        self.conn.execute("""
          INSERT INTO observations(observation_id,project_id,kind,commit_sha,branch,
            files,test_evidence,platform,created_at)
          VALUES (?,?,?,?,?,?,?,?,?)
        """, (observation_id, self.project_id, kind,
              _text(commit_sha, "commit_sha", required=False, limit=80) or None,
              _text(branch, "branch", required=False, limit=200) or None,
              json.dumps(files, ensure_ascii=False),
              _text(test_evidence, "test_evidence", required=False,
                    limit=2000) or None,
              _text(platform, "platform", required=False, limit=40) or None,
              now))
        self.conn.commit()
        return {"observation_id": observation_id, "kind": kind,
                "files": files, "created_at": now}

    def capture_git_observation(self, *, platform="unknown") -> dict | None:
        try:
            head = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10)
            if head.returncode != 0:
                return None
            branch = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=10)
            changed = subprocess.run(
                ["git", "-C", str(self.root), "show", "--name-only",
                 "--pretty=format:", "HEAD"],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        files = [line.strip() for line in changed.stdout.splitlines()
                 if line.strip() and ".." not in line.split("/")]
        observation = self.record_observation(
            kind="git_commit", commit_sha=head.stdout.strip(),
            branch=branch.stdout.strip(), files=files[:200], platform=platform)
        self.update_meta(head_sha=head.stdout.strip(),
                         branch=branch.stdout.strip(),
                         last_platform=platform)
        return observation

    # -- context ---------------------------------------------------------
    def context_summary(self, *, limit=2400, request=None) -> str:
        meta = self.get_meta()
        decisions = self.list_decisions(status="confirmed", limit=8)
        derived = self.list_decisions(status="code_derived", limit=4)
        unresolved = self.list_requirements(status="unresolved_blocking", limit=6)
        last = self.latest_handoff()
        lines = [
            f"# Project memory — {self.identity.get('name') or 'project'}",
            f"project_id: {self.project_id}",
            f"last_platform: {meta.get('last_platform') or 'unknown'}",
            f"branch: {meta.get('branch') or 'unknown'}",
            f"head: {(meta.get('head_sha') or 'unknown')[:12]}",
        ]
        if last:
            payload = last.get("payload") or {}
            lines.append(f"last_handoff: {payload.get('context_summary', '')}")
        if decisions:
            lines.append("confirmed_decisions:")
            lines.extend(f"- {item['topic']}: {item['decision']}" for item in decisions)
        if derived:
            lines.append("code_derived (not user-confirmed):")
            lines.extend(f"- {item['topic']}: {item['decision']}" for item in derived)
        if unresolved:
            lines.append("open_blocking_requirements:")
            lines.extend(f"- {item['text']}" for item in unresolved)
        text = "\n".join(lines)
        if request:
            text += "\nrequest: " + request[:300]
        return text[:limit]

    def context_state(self) -> dict:
        meta = self.get_meta()
        try:
            head = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            head = ""
        return {
            "project_id": self.project_id,
            "project_uuid": self.identity["project_uuid"],
            "instance_id": self.identity.get("instance_id"),
            "last_platform": meta.get("last_platform"),
            "last_run_id": meta.get("last_run_id"),
            "last_milestone": meta.get("last_milestone"),
            "head_sha": meta.get("head_sha"),
            "current_head": head or None,
            "stale": bool(meta.get("head_sha") and head and meta["head_sha"] != head),
            "blocking_requirements": len(self.list_requirements(
                status="unresolved_blocking")),
            "confirmed_decisions": len(self.list_decisions(status="confirmed")),
            "latest_handoff": (self.latest_handoff() or {}).get("handoff_id"),
        }

    def write_context_files(self) -> dict:
        self.context_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        meta = self.get_meta()
        last = self.latest_handoff()
        decisions = self.list_decisions(limit=200)
        requirements = self.list_requirements(limit=200)
        observations = [dict(row) for row in self.conn.execute(
            "SELECT * FROM observations WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT 50", (self.project_id,))]
        for item in observations:
            item["files"] = json.loads(item.get("files") or "[]")
        summary = self.context_summary(limit=8000)
        files = {
            "project-summary.md": summary,
            "architecture-decisions.json": {
                "project_id": self.project_id,
                "decisions": decisions,
                "generated_at": time.time(),
            },
            "requirements.json": {
                "project_id": self.project_id,
                "requirements": requirements,
                "generated_at": time.time(),
            },
            "recent-changes.json": {
                "project_id": self.project_id,
                "meta": meta,
                "observations": observations,
            },
            "open-tasks.json": {
                "project_id": self.project_id,
                "blocking": self.list_requirements(status="unresolved_blocking"),
                "unresolved": self.list_requirements(status="unresolved_nonblocking"),
            },
            "current-handoff.json": last or {},
        }
        paths = {}
        for name, value in files.items():
            path = self.context_dir / name
            if isinstance(value, str):
                temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
                temporary.write_text(value + "\n", encoding="utf-8")
                os.replace(temporary, path)
            else:
                atomic_write_json(path, value)
            paths[name] = str(path)
        return paths

    # -- portable export / import ---------------------------------------
    def export_bundle(self, path) -> dict:
        target = Path(path)
        if not target.is_absolute():
            raise MemoryError("export path must be absolute")
        if target.exists() and target.is_dir():
            raise MemoryError("export path must be a file")
        payload = {
            "schema_version": 1,
            "exported_at": time.time(),
            "identity": export_identity(self.root),
            "meta": self.get_meta(),
            "decisions": self.list_decisions(limit=500),
            "requirements": self.list_requirements(limit=500),
            "handoffs": self.list_handoffs(limit=100),
            "observations": [
                {k: row[k] for k in row.keys() if k != "project_id"}
                for row in self.conn.execute(
                    "SELECT * FROM observations WHERE project_id=? "
                    "ORDER BY created_at DESC LIMIT 100", (self.project_id,))
            ],
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
        return {"path": str(target), "bytes": len(text.encode()),
                "decisions": len(payload["decisions"]),
                "requirements": len(payload["requirements"])}

    def import_bundle(self, path, *, apply=False) -> dict:
        source = Path(path)
        if not source.is_absolute() or not source.is_file():
            raise MemoryError("import path must be an absolute existing file")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MemoryError("import bundle is invalid") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise MemoryError("unsupported import bundle")
        identity = payload.get("identity") or {}
        if identity.get("project_uuid") != self.identity["project_uuid"]:
            raise MemoryError("import bundle belongs to a different project")
        conflicts = []
        for item in payload.get("decisions", []):
            if not isinstance(item, dict):
                continue
            existing = self.conn.execute(
                "SELECT decision,status FROM decisions WHERE project_id=? AND decision_id=?",
                (self.project_id, item.get("decision_id"))).fetchone()
            if existing and (existing["decision"] != item.get("decision")
                             or existing["status"] != item.get("status")):
                conflicts.append({"kind": "decision",
                                  "id": item.get("decision_id")})
        for item in payload.get("requirements", []):
            if not isinstance(item, dict):
                continue
            existing = self.conn.execute(
                "SELECT text,status FROM requirements WHERE project_id=? AND requirement_id=?",
                (self.project_id, item.get("requirement_id"))).fetchone()
            if existing and (existing["text"] != item.get("text")
                             or existing["status"] != item.get("status")):
                conflicts.append({"kind": "requirement",
                                  "id": item.get("requirement_id")})
        if conflicts or not apply:
            return {"applied": False, "conflicts": conflicts,
                    "decisions": len(payload.get("decisions", [])),
                    "requirements": len(payload.get("requirements", []))}
        imported = 0
        for item in payload.get("decisions", []):
            try:
                self.add_decision(
                    topic=item["topic"], decision=item["decision"],
                    rationale=item.get("rationale", ""),
                    alternatives=item.get("alternatives", []),
                    source=item.get("source", "import"),
                    source_ref=item.get("source_ref"),
                    status=item.get("status", "unresolved"),
                    confirmed_by=item.get("confirmed_by"),
                    affected_tasks=item.get("affected_tasks", []),
                    supersedes=item.get("supersedes"),
                    decision_id=item.get("decision_id"))
                imported += 1
            except (MemoryError, KeyError, sqlite3.Error):
                continue
        for item in payload.get("requirements", []):
            try:
                self.add_requirement(
                    item["text"], category=item.get("category", "confirmed"),
                    source=item.get("source", "import"),
                    source_ref=item.get("source_ref"),
                    status=item.get("status", "confirmed"),
                    requirement_id=item.get("requirement_id"))
                imported += 1
            except (MemoryError, KeyError, sqlite3.Error):
                continue
        self.update_meta(last_platform="import")
        self.write_context_files()
        return {"applied": True, "imported": imported, "conflicts": []}
