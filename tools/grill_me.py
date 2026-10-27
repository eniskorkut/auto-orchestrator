#!/usr/bin/env python3
"""Persistent Grill-Me interview engine.

Questions are one-at-a-time, a recommendation is never accepted as a user
answer, and blocking decisions keep the interview/plan gated. State lives in the
project memory database so another IDE can resume it.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from pathlib import Path

try:
    from tools import assumption_guard
    from tools.project_memory import MemoryError, ProjectMemory
    from tools.repo_facts import scan_repository
    from tools.requirements_contract import (
        ContractError,
        build_from_interview,
        load_current,
    )
    try:
        from tools import grill_llm
    except ModuleNotFoundError:  # older flat installation
        grill_llm = None  # type: ignore
except ModuleNotFoundError:
    import assumption_guard  # type: ignore
    from project_memory import MemoryError, ProjectMemory  # type: ignore
    from repo_facts import scan_repository  # type: ignore
    from requirements_contract import (  # type: ignore
        ContractError,
        build_from_interview,
        load_current,
    )
    try:
        import grill_llm  # type: ignore
    except ModuleNotFoundError:  # older flat installation
        grill_llm = None  # type: ignore

MODES = ("off", "auto", "required", "plan-only")
INTERVIEW_SCHEMA_VERSION = 2
INTERVIEW_ID = re.compile(r"^iv_[0-9a-f]{16}$")
QUESTION_ID = re.compile(r"^q_[0-9a-f]{16}$")


class GrillError(ValueError):
    pass


def _text(value, field, limit=4000):
    if not isinstance(value, str) or not value.strip():
        raise GrillError(f"{field} is required")
    value = value.strip()
    if len(value) > limit or "\x00" in value:
        raise GrillError(f"{field} is invalid")
    return value


class GrillMeEngine:
    def __init__(self, root, *, identity=None, llm=None):
        self.root = Path(root).resolve()
        self.memory = ProjectMemory(self.root, identity=identity)
        self.project_id = self.memory.project_id
        self.conn = self.memory.conn
        # Optional proposal seam. None (the default) is fully deterministic and
        # performs no provider call. A raw callable/command is wrapped so the
        # strict schema validator is always applied.
        if llm is not None and not hasattr(llm, "propose"):
            if grill_llm is None:
                raise GrillError(
                    "optional LLM support is not installed; pass a configured "
                    "provider object or omit llm")
            llm = grill_llm.GrillLLM(llm, enabled=True)
        self.llm = llm

    def close(self):
        self.memory.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # -- start/resume ----------------------------------------------------
    def start(self, request, *, origin_platform="unknown", mode="auto",
              allow_new=False) -> dict:
        request = _text(request, "request", limit=20000)
        if mode not in MODES:
            raise GrillError("mode must be off, auto, required or plan-only")
        digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
        existing = None if allow_new else self.open_interview()
        if existing and existing["status"] not in ("approved", "cancelled"):
            if not self.verify_request(existing["interview_id"], request):
                raise GrillError(
                    "a different Grill-Me interview is already open; "
                    "resume or cancel it before starting a new one")
            return {"interview": existing, "resumed": True,
                    "question": self.next_question(existing["interview_id"])}
        facts = scan_repository(self.root)
        summary = request.splitlines()[0].strip()[:200]
        interview_id = "iv_" + secrets.token_hex(8)
        now = time.time()
        questions = self._build_questions(request, facts, mode)
        status = "open" if questions else "ready"
        if mode == "off" and questions:
            status = "blocked"
        self.conn.execute("""
          INSERT INTO interviews(interview_id,project_id,origin_platform,
            request_hash,request_summary,mode,status,created_at,updated_at,
            schema_version)
          VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (interview_id, self.project_id, _text(origin_platform, "platform",
              limit=40), digest, summary, mode, status, now, now,
              INTERVIEW_SCHEMA_VERSION))
        self._insert_questions(interview_id, questions, now)
        self.conn.commit()
        self._record_code_facts(facts, request)
        self.memory.add_requirement(
            summary, category="confirmed", status="confirmed", source="user",
            source_ref=f"interview:{interview_id}")
        self.conn.execute(
            "UPDATE interviews SET updated_at=? WHERE interview_id=?",
            (now, interview_id))
        self.conn.commit()
        return {"interview": self.get_interview(interview_id),
                "resumed": False,
                "question": self.next_question(interview_id)}

    def verify_request(self, interview_id, request) -> bool:
        interview = self.get_interview(interview_id)
        request = _text(request, "request", limit=20000)
        digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
        row = self.conn.execute(
            "SELECT request_hash FROM interviews WHERE project_id=? AND interview_id=?",
            (self.project_id, interview["interview_id"])).fetchone()
        return bool(row and row["request_hash"] == digest)

    def open_interview(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM interviews WHERE project_id=? AND status IN "
            "('open','blocked','cancelled','ready') ORDER BY updated_at DESC LIMIT 1",
            (self.project_id,)).fetchone()
        return self._interview_dict(row) if row else None

    def get_interview(self, interview_id) -> dict:
        if not INTERVIEW_ID.fullmatch(str(interview_id)):
            raise GrillError("invalid interview id")
        row = self.conn.execute(
            "SELECT * FROM interviews WHERE project_id=? AND interview_id=?",
            (self.project_id, interview_id)).fetchone()
        if row is None:
            raise GrillError("interview does not exist")
        return self._interview_dict(row)

    @staticmethod
    def _interview_dict(row):
        value = dict(row)
        value.pop("request_hash", None)
        return value

    def resume(self, interview_id=None) -> dict:
        interview = (self.get_interview(interview_id) if interview_id
                     else self.open_interview())
        if interview is None:
            raise GrillError("no open interview")
        if interview["status"] == "cancelled":
            self.conn.execute(
                "UPDATE interviews SET status='open',updated_at=? WHERE interview_id=?",
                (time.time(), interview["interview_id"]))
            self.conn.commit()
            interview = self.get_interview(interview["interview_id"])
        # A skipped question stays unresolved_blocking, but it must remain
        # answerable: resume reopens it instead of losing it forever.
        reopened = self.conn.execute(
            "UPDATE interview_questions SET status='open',answered_at=NULL "
            "WHERE interview_id=? AND status='skipped'",
            (interview["interview_id"],)).rowcount
        if reopened:
            self.conn.execute(
                "UPDATE interviews SET status='open',updated_at=? WHERE interview_id=?",
                (time.time(), interview["interview_id"]))
            self.conn.commit()
            interview = self.get_interview(interview["interview_id"])
        return {"interview": interview,
                "question": self.next_question(interview["interview_id"])}

    # -- question generation --------------------------------------------
    def _active_decisions(self) -> list[dict]:
        return [item for item in self.memory.list_decisions(limit=500)
                if item["status"] in ("confirmed", "code_derived")]

    def _build_questions(self, request: str, facts: dict, mode: str = "auto") -> list[dict]:
        questions = assumption_guard.critical_questions(
            request, self._active_decisions(), facts, mode=mode)
        if self.llm is None or grill_llm is None or not questions:
            return questions
        try:
            result = self.llm.propose(
                request, decisions=self._active_decisions(), facts=facts,
                topics=[item["key"] for item in questions])
        except Exception as exc:  # fail closed to deterministic questions
            result = {"status": "error", "topics": [],
                      "rejected": [{"topic": None,
                                    "reason": str(exc)[:300]}],
                      "error": "provider_exception"}
        if not isinstance(result, dict):
            result = {"status": "error", "topics": [],
                      "rejected": [{"topic": None,
                                    "reason": "provider result is invalid"}],
                      "error": "provider_result_invalid"}
        if result.get("status") == "ok":
            questions = grill_llm.enrich_questions(
                questions, result.get("topics") or [])
        if result.get("status") not in ("disabled", "ok"):
            grill_llm.record_rejected(self.memory, result)
        elif result.get("rejected"):
            grill_llm.record_rejected(self.memory, result)
        return questions

    @staticmethod
    def _make_question(key, text, options, *, critical, depends_on=None):
        return assumption_guard.make_question(
            key, text, options, critical=critical, depends_on=depends_on)

    def _insert_questions(self, interview_id, questions, now,
                          *, after_ordinal=None):
        existing_rows = {
            row["question_key"]: row for row in self.conn.execute(
                "SELECT question_key,question_id,status,ordinal "
                "FROM interview_questions WHERE interview_id=?",
                (interview_id,))}
        new_questions = [question for question in questions
                         if question["key"] not in existing_rows]
        if after_ordinal is not None and new_questions:
            self.conn.execute(
                "UPDATE interview_questions SET ordinal=ordinal+? "
                "WHERE interview_id=? AND ordinal>?",
                (len(new_questions), interview_id, int(after_ordinal)))
            ordinal = int(after_ordinal)
        else:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(ordinal),0) FROM interview_questions "
                "WHERE interview_id=?", (interview_id,)).fetchone()
            ordinal = int(row[0] or 0)
        for question in questions:
            exists = existing_rows.get(question["key"])
            metadata = (
                question.get("why"), question.get("rationale"),
                json.dumps(question.get("alternatives") or [],
                           ensure_ascii=False),
                json.dumps(question.get("affected_requirements") or [],
                           ensure_ascii=False),
                question.get("dependency"),
                1 if question.get("critical", True) else 0)
            if exists is not None:
                if exists["status"] in ("invalidated", "superseded"):
                    self.conn.execute("""
                      UPDATE interview_questions SET status='open',
                        selected_option=NULL,answer_text=NULL,answer_source=NULL,
                        answered_at=NULL,question=?,options=?,recommended_option=?,
                        depends_on=?,why=?,rationale=?,alternatives=?,
                        affected_requirements=?,dependency=?,critical=?
                      WHERE interview_id=? AND question_id=?
                    """, (question["text"],
                          json.dumps(question["options"], ensure_ascii=False),
                          question["recommended_option"],
                          json.dumps(question["depends_on"], ensure_ascii=False),
                          *metadata, interview_id, exists["question_id"]))
                continue
            ordinal += 1
            self.conn.execute("""
              INSERT INTO interview_questions(interview_id,question_id,ordinal,
                question_key,question,options,recommended_option,status,
                depends_on,created_at,why,rationale,alternatives,
                affected_requirements,dependency,critical)
              VALUES (?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?)
            """, (interview_id, "q_" + secrets.token_hex(8), ordinal,
                  question["key"], question["text"],
                  json.dumps(question["options"], ensure_ascii=False),
                  question["recommended_option"],
                  json.dumps(question["depends_on"], ensure_ascii=False), now,
                  *metadata))

    def _record_code_facts(self, facts, request):
        detected = facts.get("code_detected") or facts.get("detected") or {}
        active_topics = {item["topic"] for item in self._active_decisions()}
        if "auth_session" not in active_topics:
            if bool(detected.get("jwt")) != bool(detected.get("session")):
                if detected.get("jwt"):
                    self.memory.add_decision(
                        topic="auth_session", decision="JWT",
                        rationale="Detected JWT usage in repository code/config",
                        source="code", source_ref="repository scan",
                        status="code_derived")
                else:
                    self.memory.add_decision(
                        topic="auth_session", decision="session cookie",
                        rationale="Detected session/cookie usage in repository code/config",
                        source="code", source_ref="repository scan",
                        status="code_derived")
        if "database" not in active_topics:
            found = [name for name, key in (
                ("PostgreSQL", "postgres"), ("SQLite", "sqlite"),
                ("MySQL", "mysql")) if detected.get(key)]
            if len(found) == 1:
                self.memory.add_decision(
                    topic="database", decision=found[0],
                    rationale=f"Detected {found[0]} usage in repository code/config",
                    source="code", source_ref="repository scan",
                    status="code_derived")

    # -- conversation ----------------------------------------------------
    def _eligible(self, interview_id, row, rows_by_key) -> bool:
        raw = row["depends_on"]
        dependencies = json.loads(raw) if isinstance(raw, str) else (raw or [])
        for dependency in dependencies:
            parent = rows_by_key.get(dependency)
            if parent is None or parent["status"] != "answered":
                return False
        return True

    def next_question(self, interview_id) -> dict | None:
        self.get_interview(interview_id)
        rows = list(self.conn.execute(
            "SELECT * FROM interview_questions WHERE interview_id=? AND status='open' "
            "ORDER BY ordinal", (interview_id,)))
        if not rows:
            self._settle(interview_id)
            return None
        rows_by_key = {row["question_key"]: row for row in self.conn.execute(
            "SELECT question_key,status FROM interview_questions "
            "WHERE interview_id=?", (interview_id,))}
        for row in rows:
            if self._eligible(interview_id, row, rows_by_key):
                return self._question_dict(row)
        self._settle(interview_id)
        return None

    @staticmethod
    def _question_dict(row):
        value = dict(row)
        value["options"] = json.loads(value.get("options") or "[]")
        value["depends_on"] = json.loads(value.get("depends_on") or "[]")
        value["alternatives"] = json.loads(value.get("alternatives") or "[]")
        value["affected_requirements"] = json.loads(
            value.get("affected_requirements") or "[]")
        value["why"] = value.get("why") or None
        value["rationale"] = value.get("rationale") or None
        value["dependency"] = value.get("dependency") or None
        value["critical"] = bool(value.get("critical", True))
        return value

    def answer(self, question_id, *, option=None, text=None) -> dict:
        question = self._question(question_id)
        if question["status"] not in ("open", "skipped"):
            raise GrillError("question is not open")
        rows_by_key = {row["question_key"]: row for row in self.conn.execute(
            "SELECT question_key,status FROM interview_questions "
            "WHERE interview_id=?", (question["interview_id"],))}
        if question["status"] == "open" and not self._eligible(
                question["interview_id"], question, rows_by_key):
            raise GrillError("a dependency question must be answered first")
        options = {item["id"]: item for item in question["options"]}
        if option is not None:
            if not isinstance(option, str) or option not in options:
                raise GrillError("selected option is invalid")
            label = options[option]["label"]
            answer_source = "user"
        elif text is not None:
            label = _text(text, "answer", limit=2000)
            option = "other"
            answer_source = "user"
        else:
            raise GrillError("provide --option or --text")
        now = time.time()
        self.conn.execute("""
          UPDATE interview_questions SET selected_option=?,answer_text=?,
            answer_source=?,status='answered',answered_at=?
          WHERE interview_id=? AND question_id=?
        """, (option, label, answer_source, now,
              question["interview_id"], question_id))
        self.conn.commit()
        existing = self.conn.execute(
            "SELECT * FROM decisions WHERE project_id=? AND topic=? AND decision=? "
            "AND status='code_derived'",
            (self.project_id, question["question_key"], label)).fetchone()
        if existing is not None:
            self.conn.execute("""
              UPDATE decisions SET status='confirmed',source='user',
                source_ref=?,confirmed_by='user',confirmed_at=?,updated_at=?
              WHERE project_id=? AND decision_id=?
            """, (f"question:{question_id}", now, now, self.project_id,
                  existing["decision_id"]))
            self.conn.commit()
        else:
            self.memory.add_decision(
                topic=question["question_key"], decision=label,
                rationale=f"User answered: {question['question']}",
                alternatives=[item["label"] for item in question["options"]
                              if item["id"] != option],
                source="user", source_ref=f"question:{question_id}",
                status="confirmed", confirmed_by="user")
        self.memory.resolve_requirements_by_ref(f"question:{question_id}")
        self._expand(question["interview_id"], question["question_key"], option, now,
                     after_ordinal=question.get("ordinal"))
        self.conn.execute("UPDATE interviews SET updated_at=? WHERE interview_id=?",
                          (now, question["interview_id"]))
        self.conn.commit()
        self._settle(question["interview_id"])
        return {"question": self._question(question_id),
                "next": self.next_question(question["interview_id"])}

    def _expand(self, interview_id, key, option, now, *, after_ordinal=None):
        if key == "database":
            row = self.conn.execute(
                "SELECT request_summary FROM interviews WHERE interview_id=?",
                (interview_id,)).fetchone()
            summary = row["request_summary"] if row is not None else ""
            if not assumption_guard.mentions_any(
                    summary, ("migration", "migrations", "migrasyon",
                              "schema", "şema", "sema", "migrate")):
                return
        questions = assumption_guard.followup_questions(key, option)
        if questions:
            self._insert_questions(interview_id, questions, now,
                                   after_ordinal=after_ordinal)

    def skip(self, question_id) -> dict:
        question = self._question(question_id)
        if question["status"] != "open":
            raise GrillError("question is not open")
        now = time.time()
        self.conn.execute(
            "UPDATE interview_questions SET status='skipped',answered_at=? "
            "WHERE interview_id=? AND question_id=?",
            (now, question["interview_id"], question_id))
        self.conn.commit()
        self.memory.add_requirement(
            question["question"], category="unresolved_blocking",
            status="unresolved_blocking", source="user",
            source_ref=f"question:{question_id}")
        self._settle(question["interview_id"])
        self.conn.commit()
        return {"question": self._question(question_id),
                "next": self.next_question(question["interview_id"])}

    def back(self, question_id=None) -> dict:
        interview = self.open_interview()
        if interview is None:
            raise GrillError("no open interview")
        interview_id = interview["interview_id"]
        if question_id is None:
            row = self.conn.execute(
                "SELECT * FROM interview_questions WHERE interview_id=? "
                "AND status='answered' ORDER BY answered_at DESC LIMIT 1",
                (interview_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM interview_questions WHERE interview_id=? "
                "AND question_id=?", (interview_id, question_id)).fetchone()
        if row is None:
            raise GrillError("no answered question to go back to")
        question = self._question_dict(row)
        now = time.time()
        self.conn.execute("""
          UPDATE interview_questions SET status='open',selected_option=NULL,
            answer_text=NULL,answer_source=NULL,answered_at=NULL
          WHERE interview_id=? AND question_id=?
        """, (interview_id, row["question_id"]))
        questions = [self._question_dict(item) for item in self.conn.execute(
            "SELECT * FROM interview_questions WHERE interview_id=?",
            (interview_id,))]
        invalidated = {question["question_key"]}
        changed = True
        while changed:
            changed = False
            for item in questions:
                if item["question_id"] == row["question_id"]:
                    continue
                if item["status"] not in ("open", "answered", "skipped"):
                    continue
                if set(item["depends_on"]) & invalidated:
                    self.conn.execute("""
                      UPDATE interview_questions SET status='invalidated',
                        selected_option=NULL,answer_text=NULL,answer_source=NULL,
                        answered_at=NULL
                      WHERE interview_id=? AND question_id=?
                    """, (interview_id, item["question_id"]))
                    self.memory.mark_decisions_superseded(
                        source_ref=f"question:{item['question_id']}",
                        reason="dependent answer invalidated by an earlier change")
                    invalidated.add(item["question_key"])
                    item["status"] = "invalidated"
                    changed = True
        self.conn.commit()
        self.memory.mark_decisions_superseded(
            source_ref=f"question:{row['question_id']}",
            reason="user went back and changed the answer")
        self.conn.execute("UPDATE interviews SET status='open',updated_at=? "
                          "WHERE interview_id=?", (now, interview_id))
        self.conn.commit()
        return {"question": self._question(row["question_id"]),
                "next": self.next_question(interview_id)}

    def cancel(self) -> dict:
        interview = self.open_interview()
        if interview is None:
            raise GrillError("no open interview")
        self.conn.execute("UPDATE interviews SET status='cancelled',updated_at=? "
                          "WHERE interview_id=?",
                          (time.time(), interview["interview_id"]))
        self.conn.commit()
        return self.get_interview(interview["interview_id"])

    def _settle(self, interview_id):
        open_rows = list(self.conn.execute(
            "SELECT * FROM interview_questions WHERE interview_id=? "
            "AND status='open' ORDER BY ordinal", (interview_id,)))
        skipped = self.conn.execute(
            "SELECT COUNT(*) FROM interview_questions WHERE interview_id=? "
            "AND status='skipped'", (interview_id,)).fetchone()[0]
        rows_by_key = {row["question_key"]: row for row in self.conn.execute(
            "SELECT question_key,status FROM interview_questions "
            "WHERE interview_id=?", (interview_id,))}
        eligible = [row for row in open_rows
                    if self._eligible(interview_id, row, rows_by_key)]
        if skipped or (open_rows and not eligible):
            status = "blocked"
        elif open_rows:
            status = "open"
        else:
            status = "ready"
        self.conn.execute("UPDATE interviews SET status=?,updated_at=? "
                          "WHERE interview_id=?", (status, time.time(), interview_id))
        self.conn.commit()

    def summary(self, interview_id=None) -> dict:
        interview = (self.get_interview(interview_id) if interview_id
                     else self.open_interview())
        if interview is None:
            raise GrillError("no open interview")
        questions = [self._question_dict(row) for row in self.conn.execute(
            "SELECT * FROM interview_questions WHERE interview_id=? ORDER BY ordinal",
            (interview["interview_id"],))]
        blocking = [item for item in questions
                    if item["status"] in ("open", "skipped")]
        source_refs = {f"question:{item['question_id']}" for item in questions}
        decisions = [item for item in self.memory.list_decisions(limit=500)
                     if item.get("source_ref") in source_refs]
        summary = {
            "interview_id": interview["interview_id"],
            "status": interview["status"],
            "ready": interview["status"] == "ready" and not blocking,
            "question_count": len(questions),
            "answered_count": sum(1 for item in questions
                                  if item["status"] == "answered"),
            "blocking_questions": blocking,
            "decisions": decisions,
            "goal": interview["request_summary"],
            "milestones": self._suggest_milestones(interview["request_summary"]),
            "risks": [
                "Do not start implementation while blocking questions remain.",
                "Recommendations are not user confirmations.",
            ],
        }
        if interview["status"] == "approved":
            try:
                current = load_current(self.root)
            except ContractError:
                current = None
            if (current is not None
                    and current.get("interview_id") == interview["interview_id"]):
                summary["requirements_contract"] = self._contract_ref(current)
        return summary

    @staticmethod
    def _contract_ref(contract):
        return {
            "contract_id": contract["contract_id"],
            "contract_version": contract["contract_version"],
            "sha256": contract["sha256"],
            "interview_id": contract.get("interview_id"),
            "source": contract.get("source"),
            "status": contract.get("status"),
        }

    @staticmethod
    def _suggest_milestones(request):
        lower = request.lower()
        milestones = ["M1 — Research current code and constraints"]
        if any(word in lower for word in ("auth", "login", "password")):
            milestones.append("M2 — Implement authentication flow")
            milestones.append("M3 — Add authorization and regression tests")
        elif any(word in lower for word in ("share", "file", "upload")):
            milestones.append("M2 — Implement sharing and storage")
            milestones.append("M3 — Add access-control and deletion tests")
        else:
            milestones.append("M2 — Implement the requested change")
            milestones.append("M3 — Verify and review")
        return milestones

    def approve(self, interview_id=None) -> dict:
        summary = self.summary(interview_id)
        if not summary["ready"]:
            raise GrillError("cannot approve while blocking questions remain")
        interview = summary["interview_id"]
        now = time.time()
        self.conn.execute("UPDATE interviews SET status='approved',approved_at=?,"
                          "updated_at=? WHERE interview_id=?",
                          (now, now, interview))
        # Invalidated dependents are obsolete, not unresolved. The contract
        # builder treats them as blockers, so retire them before building.
        self.conn.execute(
            "UPDATE interview_questions SET status='superseded' "
            "WHERE interview_id=? AND status='invalidated'", (interview,))
        self.conn.commit()
        try:
            try:
                contract = build_from_interview(self.root, interview)
            except ContractError:
                contract = assumption_guard.build_requirements_contract(
                    self.root, interview)
        except (ContractError, KeyError, MemoryError, ValueError) as exc:
            self.conn.execute(
                "UPDATE interviews SET status='ready',approved_at=NULL,"
                "updated_at=? WHERE interview_id=?",
                (time.time(), interview))
            self.conn.commit()
            raise GrillError(f"requirements contract could not be created: {exc}") from exc
        contract_ref = self._contract_ref(contract)
        summary["requirements_contract"] = contract_ref
        # Approval must not rewrite the active platform. The next real run
        # owns that field, not the interview.
        self.memory.write_context_files()
        return {"interview_id": interview, "approved": True,
                "requirements_contract": contract_ref,
                "summary": summary}

    def _question(self, question_id) -> dict:
        if not QUESTION_ID.fullmatch(str(question_id)):
            raise GrillError("invalid question id")
        row = self.conn.execute(
            "SELECT * FROM interview_questions WHERE interview_id IN "
            "(SELECT interview_id FROM interviews WHERE project_id=?) "
            "AND question_id=?", (self.project_id, question_id)).fetchone()
        if row is None:
            raise GrillError("question does not exist")
        return self._question_dict(row)

    # -- CLI helpers -----------------------------------------------------
    def status(self) -> dict:
        interview = self.open_interview()
        if interview is None:
            return {"project_id": self.project_id, "interview": None}
        return {"project_id": self.project_id,
                "interview": interview,
                "question": self.next_question(interview["interview_id"])}
