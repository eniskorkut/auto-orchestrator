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
# Interviews written by V7.7+ carry question metadata columns. Older bundle
# schema_version 1 imports remain readable and are kept at version 1.
INTERVIEW_SCHEMA_VERSION = 2
QUESTION_METADATA_COLUMNS = (
    ("why", "TEXT"),
    ("rationale", "TEXT"),
    ("alternatives", "TEXT"),
    ("affected_requirements", "TEXT"),
    ("dependency", "TEXT"),
    ("critical", "INTEGER"),
)
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
HANDOFF_ID = re.compile(r"^h_[0-9a-f]{16}$")
OBSERVATION_ID = re.compile(r"^o_[0-9a-f]{16}$")
INTERVIEW_ID = re.compile(r"^iv_[0-9a-f]{16}$")
QUESTION_ID = re.compile(r"^q_[0-9a-f]{16}$")
MAX_TEXT = 4000
MAX_ITEMS = 200
EXPORT_LIMITS = {
    "decisions": 500,
    "requirements": 500,
    "handoffs": 100,
    "observations": 100,
    "interviews": 100,
    "questions": 500,
}
INTERVIEW_STATUSES = {"open", "blocked", "ready", "approved", "cancelled"}
QUESTION_STATUSES = {"open", "answered", "skipped", "invalidated"}
INTERVIEW_MODES = {"off", "auto", "required", "plan-only"}


class MemoryError(ValueError):
    pass


class _ImportConflict(Exception):
    """Raised inside an import transaction when a fresh preview conflicts."""

    def __init__(self, preview):
        super().__init__("import conflicts")
        self.preview = preview


def bundle_digest(payload) -> str:
    """Stable digest over a bundle body, excluding the digest field itself."""
    body = {key: value for key, value in payload.items() if key != "digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def _timestamp(value, field):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MemoryError(f"{field} is invalid")
    number = float(value)
    if number < 0 or number > 10_000_000_000:
        raise MemoryError(f"{field} is invalid")
    return number


def _json_list(value, field, *, limit=MAX_ITEMS, text_limit=1000):
    """Validate a bounded list of JSON-safe primitives (objects allowed)."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise MemoryError(f"{field} must be a bounded array")
    result = []
    for item in value:
        if isinstance(item, str):
            result.append(_text(item, field, limit=text_limit))
        elif isinstance(item, dict):
            encoded = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if len(encoded) > text_limit:
                raise MemoryError(f"{field} item is too long")
            result.append(item)
        else:
            raise MemoryError(f"{field} items must be strings or objects")
    return result


def _json_array(value):
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _reason(field, code, message):
    return {"field": field, "code": code, "reason": message}


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
          why TEXT,
          rationale TEXT,
          alternatives TEXT,
          affected_requirements TEXT,
          dependency TEXT,
          critical INTEGER,
          PRIMARY KEY(interview_id,question_id),
          UNIQUE(interview_id,question_key)
        );
        CREATE INDEX IF NOT EXISTS questions_open
          ON interview_questions(interview_id,status,ordinal);
        """)
        try:
            columns = {row[1] for row in self.conn.execute(
                "PRAGMA table_info(project_handoffs)")}
            if "imported" not in columns:
                self.conn.execute(
                    "ALTER TABLE project_handoffs ADD COLUMN imported "
                    "INTEGER NOT NULL DEFAULT 0")
        except sqlite3.Error:
            pass
        # V7.7 question metadata. Columns are nullable so a database created
        # by V7.6 (or an old bundle) keeps working without a data rewrite.
        try:
            columns = {row[1] for row in self.conn.execute(
                "PRAGMA table_info(interview_questions)")}
            for name, sql_type in QUESTION_METADATA_COLUMNS:
                if name not in columns:
                    try:
                        self.conn.execute(
                            f"ALTER TABLE interview_questions ADD COLUMN "
                            f"{name} {sql_type}")
                    except sqlite3.Error:
                        # Another process may have migrated concurrently; a
                        # failed column must not stop the remaining columns.
                        pass
        except sqlite3.Error:
            pass
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
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        existing = self.conn.execute(
            "SELECT * FROM project_handoffs WHERE project_id=? AND run_id=? "
            "AND milestone_id=? AND sha256=? ORDER BY version LIMIT 1",
            (self.project_id, run_id, milestone_id, digest)).fetchone()
        if existing is not None:
            return self.get_handoff(existing["handoff_id"])
        handoff_id = handoff_id or _id("h_")
        now = time.time()

        def op():
            row = self.conn.execute(
                "SELECT MAX(version) FROM project_handoffs WHERE project_id=? "
                "AND run_id=? AND milestone_id=?",
                (self.project_id, run_id, milestone_id)).fetchone()
            version = int(row[0] or 0) + 1
            self.conn.execute("""
              INSERT INTO project_handoffs(handoff_id,project_id,run_id,
                milestone_id,version,platform,payload,sha256,created_at)
              VALUES (?,?,?,?,?,?,?,?,?)
            """, (handoff_id, self.project_id, run_id, milestone_id, version,
                  platform, canonical, digest, now))

        self._transaction(op)
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
        self.update_meta(
            head_sha=head.stdout.strip(), branch=branch.stdout.strip(),
            last_platform=(platform if platform not in (None, "", "unknown")
                           else None))
        return observation

    # -- context ---------------------------------------------------------
    def _git_state(self) -> dict:
        """Read HEAD, branch and source-tree dirtiness without mutating Git."""
        state = {"head": None, "branch": None, "dirty": False,
                 "available": False}
        try:
            head = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10)
            if head.returncode != 0:
                return state
            state["available"] = True
            state["head"] = head.stdout.strip() or None
            branch = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=10)
            if branch.returncode == 0:
                state["branch"] = branch.stdout.strip() or None
            status = subprocess.run(
                ["git", "-C", str(self.root), "status", "--porcelain"],
                capture_output=True, text=True, timeout=10)
            if status.returncode == 0:
                for line in status.stdout.splitlines():
                    if not line.strip():
                        continue
                    paths = line[3:].split(" -> ") if len(line) > 3 else [line]
                    # The durable state directory is generated, not source.
                    if all(path.strip().startswith(".auto-orchestrator/")
                           or path.strip() == ".auto-orchestrator" for path in paths):
                        continue
                    state["dirty"] = True
                    break
        except (OSError, subprocess.SubprocessError):
            return {"head": None, "branch": None, "dirty": False,
                    "available": False}
        return state

    def _latest_verification(self) -> dict | None:
        """Return the newest recorded verification observation, if any."""
        row = self.conn.execute(
            "SELECT * FROM observations WHERE project_id=? AND "
            "(lower(kind) LIKE '%verif%' OR "
            "(test_evidence IS NOT NULL AND test_evidence<>'')) "
            "ORDER BY created_at DESC LIMIT 1", (self.project_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["files"] = _json_array(value.get("files"))
        return value

    def contract_ref(self) -> dict | None:
        """Read the safe contract pointer without loading or trusting contract data."""
        directory = self.directory / "contracts"
        path = directory / "current.json"
        if directory.is_symlink() or path.is_symlink() or not path.is_file():
            return None
        try:
            if path.stat().st_size > 32_768:
                return None
            pointer = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(pointer, dict):
            return None
        contract_id = pointer.get("contract_id")
        version = pointer.get("contract_version")
        sha = pointer.get("sha256")
        name = pointer.get("path")
        if (not isinstance(contract_id, str)
                or not re.fullmatch(r"^rc_[0-9a-f]{16}$", contract_id)
                or type(version) is not int or not 1 <= version <= 10000):
            return None
        sha = sha if (isinstance(sha, str)
                      and re.fullmatch(r"[0-9a-f]{64}", sha)) else None
        name = name if (isinstance(name, str) and "/" not in name
                        and "\\" not in name and ".." not in name) else None
        try:
            updated_at = _timestamp(pointer.get("updated_at"), "updated_at")
        except MemoryError:
            updated_at = None
        return {"contract_id": contract_id, "contract_version": version,
                "sha256": sha, "path": name, "updated_at": updated_at}

    @staticmethod
    def question_view(row):
        """Public, JSON-ready interview question including V7.7 metadata."""
        value = dict(row)
        value["options"] = _json_array(value.get("options"))
        value["depends_on"] = _json_array(value.get("depends_on"))
        value["alternatives"] = _json_array(value.get("alternatives"))
        value["affected_requirements"] = _json_array(
            value.get("affected_requirements"))
        value["why"] = value.get("why") or None
        value["rationale"] = value.get("rationale") or None
        value["dependency"] = value.get("dependency") or None
        value["critical"] = bool(
            True if value.get("critical") is None else value.get("critical"))
        return value

    def list_questions(self, interview_id, *, status=None, limit=200) -> list[dict]:
        """Select persisted questions (with metadata) for one interview."""
        if not INTERVIEW_ID.fullmatch(str(interview_id)):
            raise MemoryError("interview id is invalid")
        values = [self.project_id, interview_id]
        sql = ("SELECT q.* FROM interview_questions q JOIN interviews i "
               "ON i.interview_id=q.interview_id WHERE i.project_id=? "
               "AND q.interview_id=?")
        if status is not None:
            if status not in QUESTION_STATUSES:
                raise MemoryError("unsupported question status")
            sql += " AND q.status=?"
            values.append(status)
        values.append(min(max(int(limit), 1), 500))
        return [self.question_view(row) for row in self.conn.execute(
            sql + " ORDER BY q.ordinal LIMIT ?", values)]

    def active_interview(self) -> dict | None:
        """Return the newest unresolved Grill-Me interview and its open question."""
        row = self.conn.execute(
            "SELECT * FROM interviews WHERE project_id=? AND status IN "
            "('open','blocked','ready') ORDER BY updated_at DESC LIMIT 1",
            (self.project_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        question = self.conn.execute(
            "SELECT * FROM interview_questions WHERE interview_id=? AND "
            "status='open' ORDER BY ordinal LIMIT 1",
            (value["interview_id"],)).fetchone()
        if question is not None:
            value["question"] = self.question_view(question)
        else:
            value["question"] = None
        counts = {row["status"]: row["count"] for row in self.conn.execute(
            "SELECT status, COUNT(*) AS count FROM interview_questions "
            "WHERE interview_id=? GROUP BY status",
            (value["interview_id"],))}
        value["question_count"] = sum(counts.values())
        value["answered_count"] = int(counts.get("answered", 0))
        value["blocking_question_count"] = int(
            counts.get("open", 0) + counts.get("skipped", 0)
            + counts.get("invalidated", 0))
        value["blocking_questions"] = []
        for row in self.conn.execute(
                "SELECT * FROM interview_questions WHERE interview_id=? AND "
                "status IN ('open','skipped','invalidated') "
                "ORDER BY ordinal LIMIT 20", (value["interview_id"],)):
            value["blocking_questions"].append(self.question_view(row))
        value["goal"] = value["request_summary"]
        value["summary"] = (
            f"{value['status']}: {value['request_summary']} "
            f"({value['answered_count']}/{value['question_count']} answered)")
        return value

    @staticmethod
    def handoff_view(handoff, *, bounded=False):
        """Add an evidence-derived verified flag; never trust a claimed flag."""
        if not isinstance(handoff, dict):
            return None
        value = dict(handoff)
        payload = value.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        value["payload"] = payload
        verified_tests = payload.get("verified_tests")
        value["verified"] = bool(
            isinstance(verified_tests, list) and verified_tests
            and not value.get("imported"))
        if bounded:
            keep = ("milestone_id", "next_milestone", "context_summary")
            compact = {key: payload[key] for key in keep if key in payload}
            if "context_summary" in compact:
                compact["context_summary"] = str(
                    compact["context_summary"])[:700]
            for key in ("completed_tasks", "decisions", "changed_files",
                        "commits", "verified_tests", "review_findings",
                        "open_issues", "next_tasks", "evidence_refs"):
                items = payload.get(key)
                if isinstance(items, list):
                    compact[key] = items[:12]
            value["payload"] = compact
        return value

    def context_bundle(self, *, limit=8) -> dict:
        """Bounded read-only context for dashboard and universal CLI clients."""
        limit = min(max(int(limit), 1), 20)
        meta = self.get_meta()
        state = self.context_state()
        confirmed = self.list_decisions(status="confirmed", limit=limit)
        derived = self.list_decisions(status="code_derived", limit=limit)
        blockers = self.list_requirements(status="unresolved_blocking", limit=limit)
        active = self.active_interview()
        handoff = self.handoff_view(self.latest_handoff(), bounded=True)
        contract = self.contract_ref()
        verification = self._latest_verification()
        last_milestone = meta.get("last_milestone")
        if last_milestone is None and handoff is not None:
            last_milestone = (handoff.get("payload") or {}).get("milestone_id")
        return {
            "project_id": self.project_id,
            "project_uuid": self.identity["project_uuid"],
            "last_ide": meta.get("last_platform"),
            "last_run_id": meta.get("last_run_id"),
            "last_milestone": last_milestone,
            "confirmed_decisions": confirmed,
            "derived_decisions": derived,
            "blocking_requirements": blockers,
            "blockers": blockers,
            "active_interview": active,
            "handoff": handoff,
            "stale": {"is_stale": bool(state["stale"]),
                      "reasons": list(state["stale_reasons"])},
            "is_stale": bool(state["stale"]),
            "stale_reasons": list(state["stale_reasons"]),
            "contract": contract,
            "contract_ref": contract,
            "verification": verification,
            "limit": limit,
            "generated_at": time.time(),
        }

    def context_summary(self, *, limit=2400, request=None) -> str:
        meta = self.get_meta()
        state = self.context_state()
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
        if state["stale_reasons"]:
            lines.append("stale_warning: project memory may be stale ("
                         + ", ".join(state["stale_reasons"]) + ")")
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
        git = self._git_state()
        reasons = []
        if not git["head"]:
            reasons.append("missing_head")
        else:
            if meta.get("head_sha") and meta["head_sha"] != git["head"]:
                reasons.append("head_mismatch")
            if (meta.get("branch") and git["branch"]
                    and meta["branch"] != git["branch"]):
                reasons.append("branch_mismatch")
        if git["dirty"]:
            reasons.append("dirty_worktree")
        return {
            "project_id": self.project_id,
            "project_uuid": self.identity["project_uuid"],
            "instance_id": self.identity.get("instance_id"),
            "last_platform": meta.get("last_platform"),
            "last_run_id": meta.get("last_run_id"),
            "last_milestone": meta.get("last_milestone"),
            "head_sha": meta.get("head_sha"),
            "current_head": git["head"],
            "current_branch": git["branch"],
            "worktree_dirty": git["dirty"],
            "git_available": git["available"],
            "stale": bool(reasons),
            "stale_reasons": reasons,
            "blocking_requirements": len(self.list_requirements(
                status="unresolved_blocking")),
            "confirmed_decisions": len(self.list_decisions(status="confirmed")),
            "latest_handoff": (self.latest_handoff() or {}).get("handoff_id"),
        }

    def write_context_files(self) -> dict:
        self.context_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        meta = self.get_meta()
        last = self.latest_handoff()
        last_payload = (last or {}).get("payload") or {}
        active = self.active_interview()
        decisions = self.list_decisions(limit=200)
        requirements = self.list_requirements(limit=200)
        observations = [dict(row) for row in self.conn.execute(
            "SELECT * FROM observations WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT 50", (self.project_id,))]
        for item in observations:
            item["files"] = _json_array(item.get("files"))
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
                "next_tasks": (last_payload.get("next_tasks")
                               if isinstance(last_payload.get("next_tasks"), list)
                               else [])[:32],
                "next_milestone": last_payload.get("next_milestone"),
                "active_interview": active,
                "active_interview_summary": (active or {}).get("summary"),
                "contract": self.contract_ref(),
                "latest_verification": self._latest_verification(),
                "verification": self._latest_verification(),
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
    @staticmethod
    def _redact_export_text(value):
        if not isinstance(value, str):
            return value
        import re as _re
        patterns = (
            (r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [redacted]"),
            (r"(?i)\b(api[_-]?key|api-key|client[_-]?secret|db[_-]?password|"
             r"secret|token|password|passwd|authorization|credential|"
             r"credentials)\b[\"']?\s*[:=]\s*[\"']?"
             r"([^\"'\s,;]{4,})", r"\1=[redacted]"),
            (r"\b(?:sk|ghp|gho|github_pat)[-_][A-Za-z0-9_-]{10,}",
             "[redacted]"),
        )
        for pattern, replacement in patterns:
            value = _re.sub(pattern, replacement, value)
        return value

    @classmethod
    def _redact_export_value(cls, value):
        if isinstance(value, str):
            return cls._redact_export_text(value)
        if isinstance(value, list):
            return [cls._redact_export_value(item) for item in value]
        if isinstance(value, dict):
            sensitive = {"token", "access_token", "refresh_token", "password",
                         "passwd", "secret", "api_key", "api-key", "apikey",
                         "client_secret", "db_password", "authorization",
                         "credential", "credentials"}
            result = {}
            for key, item in value.items():
                if (isinstance(item, str) and item
                        and str(key).lower() in sensitive):
                    result[key] = "[redacted]"
                else:
                    result[key] = cls._redact_export_value(item)
            return result
        return value

    def _export_interviews(self, limit) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM interviews WHERE project_id=? "
            "ORDER BY updated_at DESC LIMIT ?",
            (self.project_id, limit)).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            questions = []
            for question in self.conn.execute(
                    "SELECT * FROM interview_questions WHERE interview_id=? "
                    "ORDER BY ordinal LIMIT ?",
                    (value["interview_id"], EXPORT_LIMITS["questions"])):
                questions.append(self.question_view(question))
            value["questions"] = questions
            result.append(value)
        return result

    def _export_observations(self, limit) -> list[dict]:
        result = []
        for row in self.conn.execute(
                "SELECT * FROM observations WHERE project_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (self.project_id, limit)):
            value = {key: row[key] for key in row.keys() if key != "project_id"}
            value["files"] = _json_array(value.get("files"))
            result.append(value)
        return result

    def export_bundle(self, path) -> dict:
        target = Path(path)
        if not target.is_absolute():
            raise MemoryError("export path must be absolute")
        if target.is_symlink() or (target.exists() and target.is_dir()):
            raise MemoryError("export path must be a regular file")
        meta = self.get_meta()
        safe_meta = {key: meta.get(key) for key in (
            "project_id", "project_uuid", "last_platform", "last_run_id",
            "last_milestone", "head_sha", "branch", "updated_at")}
        decisions = [self._redact_export_value(item) for item in
                     self.list_decisions(limit=EXPORT_LIMITS["decisions"])]
        requirements = [self._redact_export_value(item) for item in
                        self.list_requirements(limit=EXPORT_LIMITS["requirements"])]
        handoffs = []
        for raw in self.list_handoffs(limit=EXPORT_LIMITS["handoffs"]):
            item = self._redact_export_value(raw)
            payload = item.get("payload")
            if isinstance(payload, dict):
                canonical = json.dumps(payload, sort_keys=True,
                                       separators=(",", ":"),
                                       ensure_ascii=False)
                digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                if digest != raw.get("sha256"):
                    item["sha256"] = digest
                    item["redacted"] = True
            handoffs.append(item)
        observations = [self._redact_export_value(item) for item in
                        self._export_observations(EXPORT_LIMITS["observations"])]
        interviews = [self._redact_export_value(item) for item in
                      self._export_interviews(EXPORT_LIMITS["interviews"])]
        totals = {
            name: int(self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                (self.project_id,)).fetchone()[0])
            for name, table in (
                ("decisions", "decisions"), ("requirements", "requirements"),
                ("handoffs", "project_handoffs"),
                ("observations", "observations"), ("interviews", "interviews"))
        }
        totals["questions"] = int(self.conn.execute(
            "SELECT COUNT(*) FROM interview_questions q JOIN interviews i "
            "ON i.interview_id=q.interview_id WHERE i.project_id=?",
            (self.project_id,)).fetchone()[0])
        exported_questions = sum(len(item.get("questions", []))
                                for item in interviews)
        truncated = bool(
            len(decisions) < totals["decisions"]
            or len(requirements) < totals["requirements"]
            or len(handoffs) < totals["handoffs"]
            or len(observations) < totals["observations"]
            or len(interviews) < totals["interviews"]
            or exported_questions < totals["questions"])
        payload = {
            "schema_version": 1,
            "kind": "auto-orchestrator-project-memory",
            "exported_at": time.time(),
            "identity": export_identity(self.root),
            "source": {
                "instance_id": self.identity.get("instance_id"),
                "platform": meta.get("last_platform"),
            },
            "source_instance": self.identity.get("instance_id"),
            "meta": safe_meta,
            "limits": dict(EXPORT_LIMITS),
            "totals": totals,
            "truncated": truncated,
            "decisions": decisions,
            "requirements": requirements,
            "handoffs": handoffs,
            "observations": observations,
            "interviews": interviews,
        }
        payload["digest"] = bundle_digest(payload)
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
        try:
            temporary.write_text(text, encoding="utf-8")
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return {"path": str(target), "bytes": len(text.encode()),
                "decisions": len(decisions),
                "requirements": len(requirements),
                "handoffs": len(handoffs),
                "observations": len(observations),
                "interviews": len(interviews),
                "digest": payload["digest"],
                "truncated": truncated,
                "source_instance": self.identity.get("instance_id")}

    # -- import normalization -------------------------------------------
    @staticmethod
    def _normalize_import_decision(item: dict) -> dict:
        decision_id = item.get("decision_id") or _id("d_")
        if not DECISION_ID.fullmatch(str(decision_id)):
            raise MemoryError("decision id is invalid")
        status = item.get("status", "unresolved")
        if status not in DECISION_STATUSES:
            raise MemoryError("unsupported decision status")
        source = item.get("source", "import")
        if source not in SOURCES:
            raise MemoryError("unsupported decision source")
        confirmed_by = item.get("confirmed_by")
        if status == "confirmed":
            if not isinstance(confirmed_by, str) or not confirmed_by.strip():
                raise MemoryError("confirmed decisions require a user confirmation")
            if confirmed_by.strip().lower() in ("agent", "recommended", "auto", "system"):
                raise MemoryError("recommendations cannot be recorded as confirmed")
            confirmed_by = confirmed_by.strip()
        else:
            confirmed_by = None
        return {
            "decision_id": decision_id,
            "topic": _text(item.get("topic"), "topic", limit=300),
            "decision": _text(item.get("decision"), "decision", limit=2000),
            "rationale": _text(item.get("rationale", ""), "rationale",
                               required=False, limit=2000),
            "alternatives": _list(item.get("alternatives"), "alternatives"),
            "source": source,
            "source_ref": (_text(item.get("source_ref"), "source_ref",
                                required=False, limit=500) or None),
            "confirmed_by": confirmed_by,
            "status": status,
            "supersedes": _optional_id(item.get("supersedes"),
                                       DECISION_ID, "supersedes"),
            "affected_tasks": _list(item.get("affected_tasks"),
                                    "affected_tasks"),
        }

    @staticmethod
    def _normalize_import_requirement(item: dict) -> dict:
        requirement_id = item.get("requirement_id") or _id("r_")
        if not REQUIREMENT_ID.fullmatch(str(requirement_id)):
            raise MemoryError("requirement id is invalid")
        status = item.get("status", "confirmed")
        if status not in REQUIREMENT_STATUSES:
            raise MemoryError("unsupported requirement status")
        source = item.get("source", "import")
        if source not in SOURCES:
            raise MemoryError("unsupported requirement source")
        return {
            "requirement_id": requirement_id,
            "text": _text(item.get("text"), "requirement"),
            "category": _text(item.get("category", status), "category",
                              limit=80),
            "status": status,
            "source": source,
            "source_ref": (_text(item.get("source_ref"), "source_ref",
                                required=False, limit=500) or None),
        }

    @staticmethod
    def _normalize_import_handoff(item: dict) -> dict:
        handoff_id = item.get("handoff_id") or _id("h_")
        if not HANDOFF_ID.fullmatch(str(handoff_id)):
            raise MemoryError("handoff id is invalid")
        payload = item.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise MemoryError("handoff payload must be an object") from exc
        if not isinstance(payload, dict):
            raise MemoryError("handoff payload must be an object")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        supplied = item.get("sha256")
        if supplied is not None and supplied != digest:
            raise MemoryError("handoff payload digest does not match")
        version = item.get("version")
        if type(version) is not int or not 1 <= version <= 1_000_000:
            raise MemoryError("handoff version is invalid")
        return {
            "handoff_id": handoff_id,
            "run_id": _text(item.get("run_id"), "run_id", limit=100),
            "milestone_id": _text(item.get("milestone_id"), "milestone_id",
                                 limit=100),
            "version": version,
            "platform": _text(item.get("platform", "unknown"), "platform",
                              limit=40),
            "payload": payload,
            "sha256": digest,
            "canonical": canonical,
            "created_at": (_timestamp(item.get("created_at"), "created_at")
                           or time.time()),
        }

    @staticmethod
    def _normalize_import_observation(item: dict) -> dict:
        observation_id = item.get("observation_id") or _id("o_")
        if not OBSERVATION_ID.fullmatch(str(observation_id)):
            raise MemoryError("observation id is invalid")
        files = item.get("files")
        if isinstance(files, str):
            try:
                files = json.loads(files)
            except ValueError as exc:
                raise MemoryError("observation files are invalid") from exc
        return {
            "observation_id": observation_id,
            "kind": _text(item.get("kind"), "kind", limit=80),
            "commit_sha": (_text(item.get("commit_sha"), "commit_sha",
                                 required=False, limit=80) or None),
            "branch": (_text(item.get("branch"), "branch", required=False,
                             limit=200) or None),
            "files": _list(files, "files"),
            "test_evidence": (_text(item.get("test_evidence"),
                                    "test_evidence", required=False,
                                    limit=2000) or None),
            "platform": (_text(item.get("platform"), "platform",
                               required=False, limit=40) or None),
            "created_at": (_timestamp(item.get("created_at"), "created_at")
                           or time.time()),
        }

    @staticmethod
    def _normalize_import_interview(item: dict) -> dict:
        interview_id = item.get("interview_id") or _id("iv_")
        if not INTERVIEW_ID.fullmatch(str(interview_id)):
            raise MemoryError("interview id is invalid")
        status = item.get("status", "open")
        if status not in INTERVIEW_STATUSES:
            raise MemoryError("unsupported interview status")
        mode = item.get("mode", "auto")
        if mode not in INTERVIEW_MODES:
            raise MemoryError("unsupported interview mode")
        request_hash = item.get("request_hash")
        if (not isinstance(request_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", request_hash)):
            raise MemoryError("interview request hash is invalid")
        schema_version = item.get("schema_version", 1)
        if type(schema_version) is not int or schema_version not in (
                SCHEMA_VERSION, INTERVIEW_SCHEMA_VERSION):
            raise MemoryError("unsupported interview schema version")
        return {
            "interview_id": interview_id,
            "origin_platform": _text(item.get("origin_platform", "unknown"),
                                     "origin_platform", limit=40),
            "request_hash": request_hash,
            "request_summary": _text(item.get("request_summary"),
                                     "request_summary", limit=300),
            "mode": mode,
            "status": status,
            "approved_at": _timestamp(item.get("approved_at"), "approved_at"),
            "created_at": (_timestamp(item.get("created_at"), "created_at")
                           or time.time()),
            "updated_at": (_timestamp(item.get("updated_at"), "updated_at")
                           or time.time()),
            "schema_version": schema_version,
        }

    @staticmethod
    def _normalize_import_question(item: dict, interview_id) -> dict:
        question_id = item.get("question_id") or _id("q_")
        if not QUESTION_ID.fullmatch(str(question_id)):
            raise MemoryError("question id is invalid")
        owner = item.get("interview_id") or interview_id
        if not isinstance(owner, str) or not INTERVIEW_ID.fullmatch(owner):
            raise MemoryError("question interview id is invalid")
        ordinal = item.get("ordinal")
        if type(ordinal) is not int or ordinal < 0 or ordinal > 1_000_000:
            raise MemoryError("question ordinal is invalid")
        status = item.get("status", "open")
        if status not in QUESTION_STATUSES:
            raise MemoryError("unsupported question status")
        return {
            "interview_id": owner,
            "question_id": question_id,
            "ordinal": ordinal,
            "question_key": _text(item.get("question_key"), "question_key",
                                  limit=80),
            "question": _text(item.get("question"), "question", limit=1000),
            "options": _json_list(item.get("options"), "options", limit=20,
                                  text_limit=2000),
            "recommended_option": (_text(item.get("recommended_option"),
                                         "recommended_option", required=False,
                                         limit=100) or None),
            "selected_option": (_text(item.get("selected_option"),
                                      "selected_option", required=False,
                                      limit=100) or None),
            "answer_text": (_text(item.get("answer_text"), "answer_text",
                                  required=False, limit=2000) or None),
            "answer_source": (_text(item.get("answer_source"), "answer_source",
                                    required=False, limit=40) or None),
            "status": status,
            "depends_on": _list(item.get("depends_on"), "depends_on"),
            "why": (_text(item.get("why"), "why", required=False, limit=2000)
                    or None),
            "rationale": (_text(item.get("rationale"), "rationale",
                                required=False, limit=2000) or None),
            "alternatives": _list(item.get("alternatives"), "alternatives"),
            "affected_requirements": _list(
                item.get("affected_requirements"), "affected_requirements"),
            "dependency": (_text(item.get("dependency"), "dependency",
                                 required=False, limit=200) or None),
            "created_at": (_timestamp(item.get("created_at"), "created_at")
                           or time.time()),
            "answered_at": _timestamp(item.get("answered_at"), "answered_at"),
        }

    @staticmethod
    def _field_reasons(existing, incoming, fields):
        reasons = []
        for field in fields:
            if existing.get(field) != incoming.get(field):
                reasons.append(_reason(
                    field, "field_mismatch",
                    f"{field} differs from the existing record"))
        return reasons

    def _bundle_list(self, payload, name, unsupported):
        value = payload.get(name)
        if value is None:
            return []
        if not isinstance(value, list):
            raise MemoryError(f"{name} must be an array")
        if len(value) > 5000:
            raise MemoryError(f"{name} is too large")
        result = []
        for index, item in enumerate(value):
            if isinstance(item, dict):
                result.append(item)
            else:
                unsupported.append(f"{name}[{index}]")
        return result

    def _classify_bundle(self, payload) -> dict:
        """Classify every bundle item as add, unchanged or conflict."""
        unsupported = []
        items = []
        counts = {}
        work = []

        def record(kind, identifier, action, *, natural_key=None, match=None,
                   reasons=None):
            reasons = reasons or []
            value = {"kind": kind, "id": identifier, "action": action,
                     "status": action, "match": match,
                     "natural_key": natural_key, "reasons": reasons}
            items.append(value)
            bucket = counts.setdefault(
                kind, {"add": 0, "unchanged": 0, "conflict": 0})
            bucket[action] += 1
            return value

        decisions = []
        seen_decisions = set()
        for raw in self._bundle_list(payload, "decisions", unsupported):
            try:
                value = self._normalize_import_decision(raw)
            except (MemoryError, TypeError, ValueError) as exc:
                record("decision", raw.get("decision_id"), "conflict",
                       reasons=[_reason("decision", "invalid_item", str(exc))])
                continue
            if value["decision_id"] in seen_decisions:
                record("decision", value["decision_id"], "conflict",
                       reasons=[_reason(
                           "decision_id", "duplicate_id",
                           "decision id appears more than once in the bundle")])
                continue
            seen_decisions.add(value["decision_id"])
            decisions.append(value)

        existing_ids = {row["decision_id"] for row in self.conn.execute(
            "SELECT decision_id FROM decisions WHERE project_id=?",
            (self.project_id,))}
        all_decision_ids = existing_ids | seen_decisions
        bundle_decisions = {}
        for value in decisions:
            key = (value["topic"], value["decision"])
            previous = bundle_decisions.get(key)
            if previous is not None:
                reasons = self._field_reasons(
                    previous, value,
                    ("rationale", "alternatives", "source", "source_ref",
                     "status", "supersedes", "affected_tasks",
                     "confirmed_by"))
                if reasons:
                    reasons.insert(0, _reason(
                        "topic", "duplicate_natural_key",
                        "the same topic and decision appear more than once "
                        "in the bundle with different fields"))
                record("decision", value["decision_id"],
                       "conflict" if reasons else "unchanged",
                       natural_key=f"{value['topic']}\u0000{value['decision']}",
                       match="natural_key", reasons=reasons)
                continue
            bundle_decisions[key] = value
            reasons = []
            row = self.conn.execute(
                "SELECT * FROM decisions WHERE project_id=? AND decision_id=?",
                (self.project_id, value["decision_id"])).fetchone()
            action = "add"
            match = None
            natural_key = f"{value['topic']}\u0000{value['decision']}"
            if row is not None:
                existing = self._decision_dict(row)
                reasons.extend(self._field_reasons(
                    existing, value,
                    ("topic", "decision", "rationale", "alternatives",
                     "source", "source_ref", "status", "supersedes",
                     "affected_tasks", "confirmed_by")))
                if reasons:
                    reasons.insert(0, _reason(
                        "decision_id", "id_conflict",
                        "decision id exists with different content"))
                action = "conflict" if reasons else "unchanged"
                match = "id"
            else:
                row = self.conn.execute(
                    "SELECT * FROM decisions WHERE project_id=? AND topic=? "
                    "AND decision=?", (self.project_id, value["topic"],
                                      value["decision"])).fetchone()
                if row is not None:
                    existing = self._decision_dict(row)
                    reasons.extend(self._field_reasons(
                        existing, value,
                        ("rationale", "alternatives", "source", "source_ref",
                         "status", "supersedes", "affected_tasks",
                         "confirmed_by")))
                    if reasons:
                        reasons.insert(0, _reason(
                            "topic", "natural_key_conflict",
                            "the same topic and decision already exist with "
                            "different fields"))
                    action = "conflict" if reasons else "unchanged"
                    match = "natural_key"
                else:
                    if value["supersedes"] is not None:
                        if value["supersedes"] == value["decision_id"]:
                            reasons.append(_reason(
                                "supersedes", "invalid_item",
                                "a decision cannot supersede itself"))
                        elif value["supersedes"] not in all_decision_ids:
                            reasons.append(_reason(
                                "supersedes", "missing_supersedes",
                                "supersedes target is absent from the "
                                "bundle and the local ledger"))
                    same_topic = self.conn.execute(
                        "SELECT decision FROM decisions WHERE project_id=? "
                        "AND topic=? AND status<>'superseded' AND decision<>? "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (self.project_id, value["topic"],
                         value["decision"])).fetchone()
                    if same_topic is not None and value["supersedes"] is None:
                        reasons.append(_reason(
                            "topic", "same_topic_different_decision",
                            f"topic already has a different decision: "
                            f"{same_topic['decision'][:120]}"))
                    action = "conflict" if reasons else "add"
            record("decision", value["decision_id"], action,
                   natural_key=natural_key, match=match, reasons=reasons)
            if action == "add":
                work.append(("decision", value))

        seen_requirements = set()
        requirements = []
        for raw in self._bundle_list(payload, "requirements", unsupported):
            try:
                value = self._normalize_import_requirement(raw)
            except (MemoryError, TypeError, ValueError) as exc:
                record("requirement", raw.get("requirement_id"), "conflict",
                       reasons=[_reason("requirement", "invalid_item", str(exc))])
                continue
            if value["requirement_id"] in seen_requirements:
                record("requirement", value["requirement_id"], "conflict",
                       reasons=[_reason(
                           "requirement_id", "duplicate_id",
                           "requirement id appears more than once in the bundle")])
                continue
            seen_requirements.add(value["requirement_id"])
            requirements.append(value)
        bundle_requirements = {}
        for value in requirements:
            reasons = []
            natural_key = f"{value['text']}\u0000{value['source_ref'] or ''}"
            previous = bundle_requirements.get(natural_key)
            if previous is not None:
                reasons = self._field_reasons(
                    previous, value, ("category", "status", "source"))
                if reasons:
                    reasons.insert(0, _reason(
                        "text", "duplicate_natural_key",
                        "the same requirement appears more than once in the "
                        "bundle with different fields"))
                record("requirement", value["requirement_id"],
                       "conflict" if reasons else "unchanged",
                       natural_key=natural_key, match="natural_key",
                       reasons=reasons)
                continue
            bundle_requirements[natural_key] = value
            row = self.conn.execute(
                "SELECT * FROM requirements WHERE project_id=? AND "
                "requirement_id=?",
                (self.project_id, value["requirement_id"])).fetchone()
            action = "add"
            match = None
            if row is not None:
                existing = dict(row)
                reasons.extend(self._field_reasons(
                    existing, value,
                    ("text", "category", "status", "source", "source_ref")))
                if reasons:
                    reasons.insert(0, _reason(
                        "requirement_id", "id_conflict",
                        "requirement id exists with different content"))
                action = "conflict" if reasons else "unchanged"
                match = "id"
            else:
                row = self.conn.execute(
                    "SELECT * FROM requirements WHERE project_id=? AND "
                    "text=? AND COALESCE(source_ref,'')=?",
                    (self.project_id, value["text"],
                     value["source_ref"] or "")).fetchone()
                if row is not None:
                    existing = dict(row)
                    reasons.extend(self._field_reasons(
                        existing, value,
                        ("category", "status", "source")))
                    if reasons:
                        reasons.insert(0, _reason(
                            "text", "natural_key_conflict",
                            "the same requirement already exists with "
                            "different fields"))
                    action = "conflict" if reasons else "unchanged"
                    match = "natural_key"
            record("requirement", value["requirement_id"], action,
                   natural_key=natural_key, match=match, reasons=reasons)
            if action == "add":
                work.append(("requirement", value))

        seen_handoffs = set()
        handoffs = []
        for raw in self._bundle_list(payload, "handoffs", unsupported):
            try:
                value = self._normalize_import_handoff(raw)
            except (MemoryError, TypeError, ValueError) as exc:
                record("handoff", raw.get("handoff_id"), "conflict",
                       reasons=[_reason("handoff", "invalid_item", str(exc))])
                continue
            if value["handoff_id"] in seen_handoffs:
                record("handoff", value["handoff_id"], "conflict",
                       reasons=[_reason(
                           "handoff_id", "duplicate_id",
                           "handoff id appears more than once in the bundle")])
                continue
            seen_handoffs.add(value["handoff_id"])
            handoffs.append(value)
        for value in handoffs:
            reasons = []
            natural_key = (f"{value['run_id']}\u0000{value['milestone_id']}"
                           f"\u0000{value['sha256']}")
            row = self.conn.execute(
                "SELECT * FROM project_handoffs WHERE project_id=? AND "
                "handoff_id=?",
                (self.project_id, value["handoff_id"])).fetchone()
            action = "add"
            match = None
            if row is not None:
                existing = self.get_handoff(row["handoff_id"])
                reasons.extend(self._field_reasons(
                    existing, value,
                    ("run_id", "milestone_id", "version", "platform",
                     "payload", "sha256")))
                if reasons:
                    reasons.insert(0, _reason(
                        "handoff_id", "id_conflict",
                        "handoff id exists with different content"))
                action = "conflict" if reasons else "unchanged"
                match = "id"
            else:
                row = self.conn.execute(
                    "SELECT * FROM project_handoffs WHERE project_id=? AND "
                    "run_id=? AND milestone_id=? AND sha256=? "
                    "ORDER BY version LIMIT 1",
                    (self.project_id, value["run_id"], value["milestone_id"],
                     value["sha256"])).fetchone()
                if row is not None:
                    action = "unchanged"
                    match = "natural_key"
                else:
                    row = self.conn.execute(
                        "SELECT * FROM project_handoffs WHERE project_id=? AND "
                        "run_id=? AND milestone_id=? AND version=?",
                        (self.project_id, value["run_id"],
                         value["milestone_id"], value["version"])).fetchone()
                    if row is not None:
                        reasons.append(_reason(
                            "version", "unique_key_conflict",
                            "a different handoff already uses this run, "
                            "milestone and version"))
            record("handoff", value["handoff_id"],
                   "conflict" if reasons else action,
                   natural_key=natural_key, match=match, reasons=reasons)
            if not reasons and action == "add":
                work.append(("handoff", value))

        seen_observations = set()
        observations = []
        for raw in self._bundle_list(payload, "observations", unsupported):
            try:
                value = self._normalize_import_observation(raw)
            except (MemoryError, TypeError, ValueError) as exc:
                record("observation", raw.get("observation_id"), "conflict",
                       reasons=[_reason("observation", "invalid_item", str(exc))])
                continue
            if value["observation_id"] in seen_observations:
                record("observation", value["observation_id"], "conflict",
                       reasons=[_reason(
                           "observation_id", "duplicate_id",
                           "observation id appears more than once in the bundle")])
                continue
            seen_observations.add(value["observation_id"])
            observations.append(value)
        for value in observations:
            reasons = []
            row = self.conn.execute(
                "SELECT * FROM observations WHERE project_id=? AND "
                "observation_id=?",
                (self.project_id, value["observation_id"])).fetchone()
            action = "add"
            match = None
            if row is not None:
                existing = dict(row)
                existing["files"] = _json_array(existing.get("files"))
                reasons.extend(self._field_reasons(
                    existing, value,
                    ("kind", "commit_sha", "branch", "files",
                     "test_evidence", "platform", "created_at")))
                if reasons:
                    reasons.insert(0, _reason(
                        "observation_id", "id_conflict",
                        "observation id exists with different content"))
                action = "conflict" if reasons else "unchanged"
                match = "id"
            record("observation", value["observation_id"], action,
                   natural_key=value["observation_id"], match=match,
                   reasons=reasons)
            if action == "add":
                work.append(("observation", value))

        interviews = []
        question_raw = []
        seen_interviews = set()
        for raw in self._bundle_list(payload, "interviews", unsupported):
            try:
                value = self._normalize_import_interview(raw)
            except (MemoryError, TypeError, ValueError) as exc:
                record("interview", raw.get("interview_id"), "conflict",
                       reasons=[_reason("interview", "invalid_item", str(exc))])
                continue
            if value["interview_id"] in seen_interviews:
                record("interview", value["interview_id"], "conflict",
                       reasons=[_reason(
                           "interview_id", "duplicate_id",
                           "interview id appears more than once in the bundle")])
                continue
            seen_interviews.add(value["interview_id"])
            interviews.append(value)
            nested = raw.get("questions")
            if nested is not None:
                if not isinstance(nested, list) or len(nested) > 5000:
                    unsupported.append(f"interview:{value['interview_id']}.questions")
                else:
                    for index, item in enumerate(nested):
                        if isinstance(item, dict):
                            question_raw.append((item, value["interview_id"]))
                        else:
                            unsupported.append(
                                f"interview:{value['interview_id']}.questions[{index}]")
        for raw in self._bundle_list(payload, "questions", unsupported):
            question_raw.append((raw, raw.get("interview_id")))

        interview_ids = seen_interviews | {
            row["interview_id"] for row in self.conn.execute(
                "SELECT interview_id FROM interviews WHERE project_id=?",
                (self.project_id,))}
        for value in interviews:
            reasons = []
            natural_key = (f"{value['request_hash']}\u0000"
                           f"{value['origin_platform']}")
            row = self.conn.execute(
                "SELECT * FROM interviews WHERE project_id=? AND interview_id=?",
                (self.project_id, value["interview_id"])).fetchone()
            action = "add"
            match = None
            if row is not None:
                existing = dict(row)
                reasons.extend(self._field_reasons(
                    existing, value,
                    ("origin_platform", "request_hash", "request_summary",
                     "mode", "status", "approved_at", "created_at",
                     "updated_at", "schema_version")))
                if reasons:
                    reasons.insert(0, _reason(
                        "interview_id", "id_conflict",
                        "interview id exists with different content"))
                action = "conflict" if reasons else "unchanged"
                match = "id"
            else:
                row = self.conn.execute(
                    "SELECT * FROM interviews WHERE project_id=? AND "
                    "request_hash=? AND origin_platform=?",
                    (self.project_id, value["request_hash"],
                     value["origin_platform"])).fetchone()
                if row is not None:
                    existing = dict(row)
                    reasons.extend(self._field_reasons(
                        existing, value,
                        ("request_summary", "mode", "status", "approved_at",
                         "created_at", "updated_at", "schema_version")))
                    if reasons:
                        reasons.insert(0, _reason(
                            "request_hash", "natural_key_conflict",
                            "the same interview request already exists with "
                            "different fields"))
                    action = "conflict" if reasons else "unchanged"
                    match = "natural_key"
            record("interview", value["interview_id"], action,
                   natural_key=natural_key, match=match, reasons=reasons)
            if action == "add":
                work.append(("interview", value))

        seen_questions = set()
        seen_question_keys = set()
        questions = []
        for raw, owner in question_raw:
            try:
                value = self._normalize_import_question(raw, owner)
            except (MemoryError, TypeError, ValueError) as exc:
                record("question", raw.get("question_id"), "conflict",
                       reasons=[_reason("question", "invalid_item", str(exc))])
                continue
            if value["question_id"] in seen_questions:
                record("question", value["question_id"], "conflict",
                       reasons=[_reason(
                           "question_id", "duplicate_id",
                           "question id appears more than once in the bundle")])
                continue
            seen_questions.add(value["question_id"])
            key = (value["interview_id"], value["question_key"])
            if key in seen_question_keys:
                record("question", value["question_id"], "conflict",
                       reasons=[_reason(
                           "question_key", "duplicate_natural_key",
                           "question key appears more than once for this "
                           "interview in the bundle")])
                continue
            seen_question_keys.add(key)
            questions.append(value)
        for value in questions:
            reasons = []
            natural_key = (f"{value['interview_id']}\u0000"
                           f"{value['question_key']}")
            if value["interview_id"] not in interview_ids:
                reasons.append(_reason(
                    "interview_id", "missing_interview",
                    "question references an interview absent from the bundle "
                    "and the local ledger"))
            row = self.conn.execute(
                "SELECT * FROM interview_questions WHERE interview_id=? AND "
                "question_id=?",
                (value["interview_id"], value["question_id"])).fetchone()
            action = "add"
            match = None
            if row is not None:
                existing = self.question_view(row)
                reasons.extend(self._field_reasons(
                    existing, value,
                    ("ordinal", "question_key", "question", "options",
                     "recommended_option", "selected_option", "answer_text",
                     "answer_source", "status", "depends_on", "why",
                     "rationale", "alternatives", "affected_requirements",
                     "dependency")))
                if reasons:
                    reasons.insert(0, _reason(
                        "question_id", "id_conflict",
                        "question id exists with different content"))
                action = "conflict" if reasons else "unchanged"
                match = "id"
            else:
                row = self.conn.execute(
                    "SELECT * FROM interview_questions WHERE interview_id=? "
                    "AND question_key=?",
                    (value["interview_id"], value["question_key"])).fetchone()
                if row is not None:
                    existing = self.question_view(row)
                    reasons.extend(self._field_reasons(
                        existing, value,
                        ("ordinal", "question", "options",
                         "recommended_option", "selected_option",
                         "answer_text", "answer_source", "status",
                         "depends_on", "why", "rationale", "alternatives",
                         "affected_requirements", "dependency")))
                    if reasons:
                        reasons.insert(0, _reason(
                            "question_key", "natural_key_conflict",
                            "the same question key already exists with "
                            "different fields"))
                    action = "conflict" if reasons else "unchanged"
                    match = "natural_key"
            record("question", value["question_id"], action,
                   natural_key=natural_key, match=match, reasons=reasons)
            if action == "add" and not reasons:
                work.append(("question", value))

        known = {"schema_version", "kind", "exported_at", "identity", "source",
                 "meta", "limits", "totals", "truncated", "digest",
                 "decisions", "requirements", "handoffs", "observations",
                 "interviews", "questions", "source_instance"}
        for name, value in payload.items():
            if name not in known and value not in (None, [], {}, ""):
                unsupported.append(name)

        # A duplicate id is ambiguous for every occurrence, not only the
        # second one. Mark all of them as conflicts before any apply.
        duplicate_keys = {
            (item["kind"], item["id"]) for item in items
            if any(reason["code"] == "duplicate_id"
                   for reason in item.get("reasons", []))}
        id_fields = {
            "decision": "decision_id", "requirement": "requirement_id",
            "handoff": "handoff_id", "observation": "observation_id",
            "interview": "interview_id", "question": "question_id",
        }
        if duplicate_keys:
            for item in items:
                key = (item["kind"], item["id"])
                if key not in duplicate_keys or item["action"] == "conflict":
                    continue
                bucket = counts[item["kind"]]
                bucket[item["action"]] -= 1
                bucket["conflict"] += 1
                item["action"] = "conflict"
                item["status"] = "conflict"
                item["reasons"].append(_reason(
                    "id", "duplicate_id",
                    "this id appears more than once in the bundle"))
            work = [
                (kind, value) for kind, value in work
                if (kind, value.get(id_fields[kind], "")) not in duplicate_keys
            ]

        conflicts = [item for item in items if item["action"] == "conflict"]
        reasons = [reason for item in conflicts
                   for reason in item.get("reasons", [])]
        total = {"add": 0, "unchanged": 0, "conflict": 0}
        for bucket in counts.values():
            for action in total:
                total[action] += bucket[action]
        return {
            "items": items,
            "counts": counts,
            "totals": total,
            "conflicts": conflicts,
            "reasons": reasons,
            "unsupported": unsupported,
            "_work": work,
        }

    def _insert_import_item(self, kind, value):
        if kind == "decision":
            self.conn.execute("""
              INSERT INTO decisions(decision_id,project_id,topic,decision,
                rationale,alternatives,source,source_ref,confirmed_by,
                confirmed_at,status,supersedes,affected_tasks,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (value["decision_id"], self.project_id, value["topic"],
                  value["decision"], value["rationale"],
                  json.dumps(value["alternatives"], ensure_ascii=False),
                  value["source"], value["source_ref"],
                  value["confirmed_by"],
                  time.time() if value["status"] == "confirmed" else None,
                  value["status"], value["supersedes"],
                  json.dumps(value["affected_tasks"], ensure_ascii=False),
                  time.time()))
        elif kind == "requirement":
            now = time.time()
            self.conn.execute("""
              INSERT INTO requirements(requirement_id,project_id,text,category,
                status,source,source_ref,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?)
            """, (value["requirement_id"], self.project_id, value["text"],
                  value["category"], value["status"], value["source"],
                  value["source_ref"], now, now))
        elif kind == "handoff":
            self.conn.execute("""
              INSERT INTO project_handoffs(handoff_id,project_id,run_id,
                milestone_id,version,platform,payload,sha256,created_at,imported)
              VALUES (?,?,?,?,?,?,?,?,?,1)
            """, (value["handoff_id"], self.project_id, value["run_id"],
                  value["milestone_id"], value["version"], value["platform"],
                  value["canonical"], value["sha256"], value["created_at"]))
        elif kind == "observation":
            self.conn.execute("""
              INSERT INTO observations(observation_id,project_id,kind,commit_sha,
                branch,files,test_evidence,platform,created_at)
              VALUES (?,?,?,?,?,?,?,?,?)
            """, (value["observation_id"], self.project_id, value["kind"],
                  value["commit_sha"], value["branch"],
                  json.dumps(value["files"], ensure_ascii=False),
                  value["test_evidence"], value["platform"],
                  value["created_at"]))
        elif kind == "interview":
            self.conn.execute("""
              INSERT INTO interviews(interview_id,project_id,origin_platform,
                request_hash,request_summary,mode,status,approved_at,
                created_at,updated_at,schema_version)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (value["interview_id"], self.project_id,
                  value["origin_platform"], value["request_hash"],
                  value["request_summary"], value["mode"], value["status"],
                  value["approved_at"], value["created_at"],
                  value["updated_at"], value["schema_version"]))
        elif kind == "question":
            self.conn.execute("""
              INSERT INTO interview_questions(interview_id,question_id,ordinal,
                question_key,question,options,recommended_option,
                selected_option,answer_text,answer_source,status,depends_on,
                created_at,answered_at,why,rationale,alternatives,
                affected_requirements,dependency)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (value["interview_id"], value["question_id"],
                  value["ordinal"], value["question_key"], value["question"],
                  json.dumps(value["options"], ensure_ascii=False),
                  value["recommended_option"], value["selected_option"],
                  value["answer_text"], value["answer_source"],
                  value["status"],
                  json.dumps(value["depends_on"], ensure_ascii=False),
                  value["created_at"], value["answered_at"],
                  value["why"], value["rationale"],
                  json.dumps(value["alternatives"], ensure_ascii=False),
                  json.dumps(value["affected_requirements"], ensure_ascii=False),
                  value["dependency"]))
        else:  # pragma: no cover - classification only emits known kinds
            raise MemoryError("unsupported import item")

    def _apply_import_meta(self, meta):
        """Fill continuity fields without changing local platform or Git state."""
        if not isinstance(meta, dict):
            return
        run_id = meta.get("last_run_id")
        milestone = meta.get("last_milestone")
        run_id = (_text(run_id, "last_run_id", required=False, limit=100)
                  or None) if run_id is not None else None
        milestone = (_text(milestone, "last_milestone", required=False,
                           limit=100) or None) if milestone is not None else None
        if not run_id and not milestone:
            return
        now = time.time()
        row = self.conn.execute(
            "SELECT project_id FROM memory_meta WHERE project_id=?",
            (self.project_id,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO memory_meta(project_id,project_uuid,last_run_id,"
                "last_milestone,updated_at) VALUES (?,?,?,?,?)",
                (self.project_id, self.identity["project_uuid"], run_id,
                 milestone, now))
        else:
            self.conn.execute(
                "UPDATE memory_meta SET last_run_id=COALESCE(last_run_id,?),"
                "last_milestone=COALESCE(last_milestone,?),updated_at=? "
                "WHERE project_id=?",
                (run_id, milestone, now, self.project_id))

    def _apply_bundle(self, payload):
        def op():
            fresh = self._classify_bundle(payload)
            if fresh["conflicts"]:
                raise _ImportConflict(fresh)
            for kind, value in fresh["_work"]:
                self._insert_import_item(kind, value)
            for kind, value in fresh["_work"]:
                if kind == "decision" and value.get("supersedes"):
                    self.conn.execute(
                        "UPDATE decisions SET status='superseded',updated_at=? "
                        "WHERE project_id=? AND decision_id=? AND "
                        "status<>'superseded'",
                        (time.time(), self.project_id,
                         value["supersedes"]))
            self._apply_import_meta(payload.get("meta"))
        self._transaction(op)

    def _import_result(self, payload, preview, *, applied, aborted=False,
                       imported=0):
        public = {key: value for key, value in preview.items()
                  if not key.startswith("_")}
        public.update({
            "applied": applied,
            "aborted": aborted,
            "preview": not applied and not aborted,
            "imported": imported,
            "add": public["totals"]["add"],
            "additions": public["totals"]["add"],
            "unchanged": public["totals"]["unchanged"],
            "conflict_count": public["totals"]["conflict"],
            "conflicts_count": public["totals"]["conflict"],
            "skipped": (public["totals"]["unchanged"]
                        + len(public["unsupported"])
                        + (public["totals"]["conflict"] if aborted else 0)),
            "not_applied": public["totals"]["conflict"],
            "partial": bool(aborted or public["unsupported"]),
            "reason_codes": [reason.get("code")
                             for reason in public.get("reasons", [])],
            "reason_messages": [reason.get("reason")
                                for reason in public.get("reasons", [])],
            "decisions": len(payload.get("decisions") or []),
            "requirements": len(payload.get("requirements") or []),
            "source": payload.get("source") if isinstance(
                payload.get("source"), dict) else {},
            "source_instance": payload.get("source_instance") or (
                payload.get("source", {}).get("instance_id")
                if isinstance(payload.get("source"), dict) else None),
            "truncated": bool(payload.get("truncated")),
        })
        return public

    def import_bundle(self, path, *, apply=False) -> dict:
        source = Path(path)
        if (not source.is_absolute() or source.is_symlink()
                or not source.is_file()):
            raise MemoryError("import path must be an absolute regular file")
        try:
            if source.stat().st_size > 16 * 1024 * 1024:
                raise MemoryError("import bundle is too large")
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MemoryError("import bundle is invalid") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise MemoryError("unsupported import bundle")
        identity = payload.get("identity")
        if (not isinstance(identity, dict)
                or identity.get("project_uuid") != self.identity["project_uuid"]):
            raise MemoryError("import bundle belongs to a different project")
        expected = payload.get("digest")
        if expected is not None:
            if (not isinstance(expected, str)
                    or bundle_digest(payload) != expected):
                raise MemoryError("bundle digest does not match")
        preview = self._classify_bundle(payload)
        if preview["conflicts"]:
            return self._import_result(payload, preview, applied=False,
                                       aborted=bool(apply))
        if not apply:
            return self._import_result(payload, preview, applied=False)
        try:
            self._apply_bundle(payload)
        except _ImportConflict as exc:
            return self._import_result(payload, exc.preview, applied=False,
                                       aborted=True)
        self.write_context_files()
        imported = preview["totals"]["add"]
        return self._import_result(payload, preview, applied=True,
                                   imported=imported)
