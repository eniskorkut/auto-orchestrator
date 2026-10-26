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
    from tools.project_memory import MemoryError, ProjectMemory
    from tools.repo_facts import scan_repository
except ModuleNotFoundError:
    from project_memory import MemoryError, ProjectMemory  # type: ignore
    from repo_facts import scan_repository  # type: ignore

MODES = ("off", "auto", "required", "plan-only")
CRITICAL_WORDS = (
    "auth", "login", "password", "payment", "migration", "permission", "role",
    "database", "share", "multi-user", "multiuser", "tenant", "breaking",
    "delete", "privacy", "credential", "secret",
)
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
    def __init__(self, root, *, identity=None):
        self.root = Path(root).resolve()
        self.memory = ProjectMemory(self.root, identity=identity)
        self.project_id = self.memory.project_id
        self.conn = self.memory.conn

    def close(self):
        self.memory.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # -- start/resume ----------------------------------------------------
    def start(self, request, *, origin_platform="unknown", mode="auto") -> dict:
        request = _text(request, "request", limit=20000)
        if mode not in MODES:
            raise GrillError("mode must be off, auto, required or plan-only")
        existing = self.open_interview()
        if existing and existing["status"] not in ("approved", "cancelled"):
            return {"interview": existing, "resumed": True,
                    "question": self.next_question(existing["interview_id"])}
        facts = scan_repository(self.root)
        summary = request.splitlines()[0].strip()[:200]
        digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
        interview_id = "iv_" + secrets.token_hex(8)
        now = time.time()
        questions = self._build_questions(request, facts, mode)
        status = "open" if questions else "ready"
        if mode == "off" and questions:
            status = "blocked"
        self.conn.execute("""
          INSERT INTO interviews(interview_id,project_id,origin_platform,
            request_hash,request_summary,mode,status,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?)
        """, (interview_id, self.project_id, _text(origin_platform, "platform",
              limit=40), digest, summary, mode, status, now, now))
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
    def _build_questions(self, request: str, facts: dict, mode: str = "auto") -> list[dict]:
        text = request.lower()
        decisions = {item["topic"] for item in self.memory.list_decisions(limit=200)}
        detected = facts.get("detected", {})
        questions = []
        if (any(word in text for word in ("auth", "login", "password", "sign-in",
                                          "signin", "giriş", "şifre", "oturum",
                                          "kullanıcı giriş", "kullanıcı girişi",
                                          "kullanıcı oturum"))
                and not detected.get("auth") and not detected.get("jwt")
                and not detected.get("session") and "auth_session" not in decisions):
            questions.append(self._make_question(
                "auth_session", "Kullanıcı oturumları nasıl yönetilsin?",
                [
                    ("cookie", "HttpOnly secure session cookie", True),
                    ("jwt", "Access/refresh JWT", False),
                    ("sso", "Mevcut kurum SSO sistemi", False),
                    ("other", "Diğer — kendim yazacağım", False),
                ], critical=True))
        if (any(word in text for word in ("share", "sharing", "multi-user",
                                          "multiuser", "file upload", "paylaş",
                                          "dosya", "çok kullanıcı"))
                and "file_access" not in decisions):
            questions.append(self._make_question(
                "file_access", "Dosya paylaşımında erişim modeli ne olsun?",
                [
                    ("owner", "Yalnızca dosya sahibi", True),
                    ("team", "Ekip üyeleri", False),
                    ("role", "Rol tabanlı erişim", False),
                    ("public", "Herkese açık bağlantı", False),
                ], critical=True))
        if (any(word in text for word in ("share", "sharing", "storage", "upload",
                                          "dosya", "depolama"))
                and not detected.get("s3") and "file_storage" not in decisions):
            questions.append(self._make_question(
                "file_storage", "Dosyalar nerede saklansın?",
                [
                    ("local", "Yerel dosya sistemi", True),
                    ("s3", "S3 uyumlu object storage", False),
                    ("db", "Veritabanı içinde", False),
                    ("other", "Diğer", False),
                ], critical=True, depends_on=["file_access"]))
        if (any(word in text for word in ("database", "migration", "schema",
                                          "veritabanı", "migrasyon"))
                and not (detected.get("postgres") or detected.get("sqlite")
                         or detected.get("mysql"))
                and "database" not in decisions):
            questions.append(self._make_question(
                "database", "Veritabanı yaklaşımı ne olsun?",
                [
                    ("postgres", "PostgreSQL", True),
                    ("sqlite", "SQLite", False),
                    ("existing", "Mevcut kurum veritabanı", False),
                    ("other", "Diğer", False),
                ], critical=True))
        if "breaking" in text and "api_compat" not in decisions:
            questions.append(self._make_question(
                "api_compat", "Breaking API değişikliği nasıl yönetilsin?",
                [
                    ("versioned", "Yeni API versiyonu", True),
                    ("deprecate", "Kademeli deprecation", False),
                    ("break", "Doğrudan breaking change", False),
                ], critical=True))
        if not questions and mode == "required":
            questions.append(self._make_question(
                "scope", "Bu görevin kapsam dışında bırakılacak maddeleri neler?",
                [
                    ("none", "Ek kapsam dışı madde yok", True),
                    ("other", "Diğer — kendim yazacağım", False),
                ], critical=True))
        return questions

    @staticmethod
    def _make_question(key, text, options, *, critical, depends_on=None):
        return {
            "key": key, "text": text,
            "options": [{"id": option_id, "label": label,
                         "recommended": recommended}
                        for option_id, label, recommended in options],
            "recommended_option": next(
                (option_id for option_id, _, recommended in options if recommended),
                None),
            "depends_on": list(depends_on or []),
            "critical": bool(critical),
        }

    def _insert_questions(self, interview_id, questions, now):
        row = self.conn.execute(
            "SELECT COALESCE(MAX(ordinal),0) FROM interview_questions "
            "WHERE interview_id=?", (interview_id,)).fetchone()
        ordinal = int(row[0] or 0)
        for question in questions:
            exists = self.conn.execute(
                "SELECT 1 FROM interview_questions WHERE interview_id=? "
                "AND question_key=?", (interview_id, question["key"])).fetchone()
            if exists:
                continue
            ordinal += 1
            self.conn.execute("""
              INSERT INTO interview_questions(interview_id,question_id,ordinal,
                question_key,question,options,recommended_option,status,
                depends_on,created_at)
              VALUES (?,?,?,?,?,?,?,'open',?,?)
            """, (interview_id, "q_" + secrets.token_hex(8), ordinal,
                  question["key"], question["text"],
                  json.dumps(question["options"], ensure_ascii=False),
                  question["recommended_option"],
                  json.dumps(question["depends_on"], ensure_ascii=False), now))

    def _record_code_facts(self, facts, request):
        detected = facts.get("detected", {})
        topic = "auth_session" if "auth" in request.lower() else None
        if detected.get("jwt"):
            self.memory.add_decision(
                topic="auth_session", decision="JWT",
                rationale="Detected JWT usage in repository code",
                source="code", source_ref="repository scan", status="code_derived")
        elif detected.get("session"):
            self.memory.add_decision(
                topic="auth_session", decision="session cookie",
                rationale="Detected session/cookie usage in repository code",
                source="code", source_ref="repository scan", status="code_derived")
        if detected.get("postgres"):
            self.memory.add_decision(
                topic="database", decision="PostgreSQL",
                rationale="Detected PostgreSQL usage in repository code",
                source="code", source_ref="repository scan", status="code_derived")
        elif detected.get("sqlite"):
            self.memory.add_decision(
                topic="database", decision="SQLite",
                rationale="Detected SQLite usage in repository code",
                source="code", source_ref="repository scan", status="code_derived")

    # -- conversation ----------------------------------------------------
    def next_question(self, interview_id) -> dict | None:
        self.get_interview(interview_id)
        row = self.conn.execute(
            "SELECT * FROM interview_questions WHERE interview_id=? AND status='open' "
            "ORDER BY ordinal LIMIT 1", (interview_id,)).fetchone()
        if row is None:
            self._settle(interview_id)
            return None
        return self._question_dict(row)

    @staticmethod
    def _question_dict(row):
        value = dict(row)
        value["options"] = json.loads(value.get("options") or "[]")
        value["depends_on"] = json.loads(value.get("depends_on") or "[]")
        return value

    def answer(self, question_id, *, option=None, text=None) -> dict:
        question = self._question(question_id)
        if question["status"] not in ("open", "skipped"):
            raise GrillError("question is not open")
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
        self.memory.add_decision(
            topic=question["question_key"], decision=label,
            rationale=f"User answered: {question['question']}",
            alternatives=[item["label"] for item in question["options"]
                          if item["id"] != option],
            source="user", source_ref=f"question:{question_id}",
            status="confirmed", confirmed_by="user")
        self.memory.resolve_requirements_by_ref(f"question:{question_id}")
        self._expand(question["interview_id"], question["question_key"], option, now)
        self.conn.execute("UPDATE interviews SET updated_at=? WHERE interview_id=?",
                          (now, question["interview_id"]))
        self.conn.commit()
        self._settle(question["interview_id"])
        return {"question": self._question(question_id),
                "next": self.next_question(question["interview_id"])}

    def _expand(self, interview_id, key, option, now):
        if key == "auth_session" and option == "jwt":
            self._insert_questions(interview_id, [self._make_question(
                "jwt_refresh", "JWT yenileme ve logout davranışı ne olsun?",
                [
                    ("refresh", "Kısa access token + refresh token", True),
                    ("short", "Yalnızca kısa ömürlü access token", False),
                    ("sliding", "Sliding session", False),
                ], critical=True, depends_on=["auth_session"])], now)
        if key == "file_access" and option in ("team", "role", "public"):
            self._insert_questions(interview_id, [self._make_question(
                "file_delete", "Dosya silme politikası ne olsun?",
                [
                    ("soft", "Soft delete + geri alma", True),
                    ("hard", "Kalıcı silme", False),
                    ("retain", "Saklama süresi sonunda sil", False),
                ], critical=True, depends_on=["file_access"])], now)

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
        self.conn.execute("""
          UPDATE interview_questions SET status='invalidated'
          WHERE interview_id=? AND status='open' AND depends_on LIKE ?
        """, (interview_id, f'%"{question["question_key"]}"%'))
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
        open_rows = self.conn.execute(
            "SELECT COUNT(*) FROM interview_questions WHERE interview_id=? "
            "AND status='open'", (interview_id,)).fetchone()[0]
        skipped = self.conn.execute(
            "SELECT COUNT(*) FROM interview_questions WHERE interview_id=? "
            "AND status='skipped'", (interview_id,)).fetchone()[0]
        if skipped:
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
        decisions = [self.memory.get_decision(item["decision_id"])
                     for item in []]  # decisions are queried by topic below
        rows = self.memory.list_decisions(limit=100)
        summary = {
            "interview_id": interview["interview_id"],
            "status": interview["status"],
            "ready": interview["status"] == "ready" and not blocking,
            "question_count": len(questions),
            "answered_count": sum(1 for item in questions
                                  if item["status"] == "answered"),
            "blocking_questions": blocking,
            "decisions": rows,
            "goal": interview["request_summary"],
            "milestones": self._suggest_milestones(interview["request_summary"]),
            "risks": [
                "Do not start implementation while blocking questions remain.",
                "Recommendations are not user confirmations.",
            ],
        }
        return summary

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
        self.conn.commit()
        self.memory.update_meta(last_platform="grill-me",
                                last_run_id=None, last_milestone=None)
        self.memory.write_context_files()
        return {"interview_id": interview, "approved": True,
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
