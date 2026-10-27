#!/usr/bin/env python3
"""Central Assumption Guard preflight for Teamwork and Grill-Me.

The guard is deliberately deterministic: it reads the durable project memory,
confirmed/code-derived decisions, bounded repository facts and the current git
head, then decides whether a request contains a critical ambiguity that must be
interviewed before any plan or runner state is written.

A guard refusal only creates/reuses a persistent Grill-Me interview. It never
writes plans, active runner state or engine state.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
from pathlib import Path

try:
    from tools.project_identity import ensure_project_identity
    from tools.project_memory import MemoryError, ProjectMemory
    from tools.repo_facts import scan_repository
except ModuleNotFoundError:  # python tools/assumption_guard.py
    from project_identity import ensure_project_identity  # type: ignore
    from project_memory import MemoryError, ProjectMemory  # type: ignore
    from repo_facts import scan_repository  # type: ignore

MODES = ("off", "auto", "required", "plan-only")
MAX_REQUEST = 20000
GUARD_VERSION = 1


class AssumptionGuardError(ValueError):
    pass


# Compatibility aliases used by callers that prefer the shorter names.
GuardError = AssumptionGuardError
AssumptionError = AssumptionGuardError

_TRANSLATE = str.maketrans({
    "İ": "i", "I": "i", "ı": "i",
    "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g", "Ç": "c", "ç": "c",
    "Ö": "o", "ö": "o", "Ü": "u", "ü": "u", "Â": "a", "â": "a",
    "Î": "i", "î": "i", "Û": "u", "û": "u",
})

_RULE_PATTERN_CACHE: dict[str, re.Pattern] = {}


def normalize(value) -> str:
    """Fold Turkish dotted/dotless I and diacritics for deterministic matching."""
    if not isinstance(value, str):
        return ""
    return value.translate(_TRANSLATE).casefold()


def _term_pattern(term: str) -> re.Pattern:
    pattern = _RULE_PATTERN_CACHE.get(term)
    if pattern is not None:
        return pattern
    folded = normalize(term)
    is_word = bool(re.fullmatch(r"[a-z0-9_]+", folded))
    is_phrase = " " in folded and all(
        re.fullmatch(r"[a-z0-9_]+", part) for part in folded.split(" "))
    if is_word or is_phrase:
        # Singular/plural tolerance plus common Turkish suffixes. The token
        # boundary is preserved, so "auth" still never matches "author".
        suffix = (r"(?:s|es|i|u|e|a|si|su|de|da|den|dan|te|ta|ten|tan|"
                  r"ler|lar|leri|lari|nin|nun|in|un|na|ne|ya|ye|me|ma|"
                  r"mesi|masi|inmesi|inmasi|mek|mak)?"
                  if folded[-1:].isalpha() else "")
        words = folded.split(" ")
        body = " ".join(re.escape(word) for word in words[:-1])
        if body:
            body += " "
        body += re.escape(words[-1]) + suffix
    else:
        body = re.escape(folded)
    pattern = re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")
    _RULE_PATTERN_CACHE[term] = pattern
    return pattern


def mentions(text: str, term: str) -> bool:
    return bool(_term_pattern(term).search(normalize(text)))


def mentions_any(text: str, terms) -> bool:
    return any(mentions(text, term) for term in terms)


def _bounded_request(request) -> str:
    if not isinstance(request, str):
        raise AssumptionGuardError("request must be text")
    value = request.strip()
    if not value:
        raise AssumptionGuardError("request must not be empty")
    if len(value) > MAX_REQUEST or "\x00" in value:
        raise AssumptionGuardError("request is invalid")
    return value


def make_question(key, text, options, *, critical=True, depends_on=None) -> dict:
    return {
        "key": key,
        "text": text,
        "options": [{"id": option_id, "label": label,
                     "recommended": bool(recommended)}
                    for option_id, label, recommended in options],
        "recommended_option": next(
            (option_id for option_id, _, recommended in options if recommended),
            None),
        "depends_on": list(depends_on or []),
        "critical": bool(critical),
    }


TOPIC_ORDER = (
    "payments",
    "data_retention",
    "external_transfer",
    "api_compat",
    "auth_session",
    "file_access",
    "roles",
    "database",
    "file_storage",
)
CRITICAL_TOPICS = TOPIC_ORDER

RULES = {
    "payments": {
        "terms": (
            "payment", "payments", "checkout", "billing", "invoice",
            "subscription", "refund", "stripe", "iyzico", "paypal",
            "ödeme", "odeme", "fatura", "abonelik", "iade", "tahsilat",
        ),
        "question": "Ödemeler nasıl işlenecek? / How should payments be handled?",
        "options": (
            ("existing", "Mevcut ödeme sağlayıcısı / Existing payment provider", True),
            ("stripe", "Stripe", False),
            ("manual", "Manuel ödeme veya fatura / Manual payment or invoicing", False),
            ("other", "Diğer — kendim yazacağım / Other — I will specify", False),
        ),
        "depends_on": (),
    },
    "roles": {
        "terms": (
            "role", "roles", "rbac", "role-based", "role based",
            "authorization model", "permission", "permissions",
            "permission model", "rol", "roller", "rol tabanlı", "yetki",
            "yetkiler", "yetkileri", "yetki modeli",
            "yetkilendirme modeli", "izin", "izin modeli",
        ),
        "question": "Rol ve yetki modeli ne olsun? / What role and permission model should apply?",
        "options": (
            ("existing", "Mevcut rol modeli / Existing role model", True),
            ("rbac", "Rol tabanlı erişim (RBAC) / Role-based access control", False),
            ("single", "Tek kullanıcı / Single user", False),
            ("other", "Diğer — kendim yazacağım / Other — I will specify", False),
        ),
        "depends_on": (),
    },
    "data_retention": {
        "terms": (
            "delete", "deletion", "purge", "erase", "wipe", "drop table",
            "retention", "data retention", "gdpr", "kvkk", "soft delete",
            "hard delete", "sil", "silme", "kalıcı sil", "kalici sil",
            "saklama", "veri saklama", "geri dönüşü olmayan", "yok et",
            "temizle",
        ),
        "question": ("Silme ve veri saklama politikası ne olsun? / "
                     "What deletion and data retention policy should apply?"),
        "options": (
            ("soft", "Soft delete + geri alma / Soft delete with recovery", True),
            ("hard", "Onaylı kalıcı silme / Hard delete with approval", False),
            ("retention", "Saklama süresi sonunda sil / Delete after retention period", False),
            ("other", "Diğer — kendim yazacağım / Other — I will specify", False),
        ),
        "depends_on": (),
    },
    "external_transfer": {
        "terms": (
            "external api", "external transfer", "third-party", "third party",
            "third parties", "third-party api", "external service",
            "export data",
            "export user data", "user data export", "send data",
            "transfer data", "data transfer", "webhook", "analytics",
            "cloud sync",
            "dış servis", "dis servis", "dışa veri aktarımı",
            "disa veri aktarimi", "üçüncü taraf", "ucuncu taraf", "harici",
            "dışa aktar", "disa aktar", "veri aktarımı", "veri aktarimi",
            "veri transferi", "aktarım", "aktarim",
        ),
        "question": ("Dışa veri aktarımı olacak mı? / "
                     "Will data be transferred to an external service?"),
        "options": (
            ("none", "Dış aktarım yok / No external transfer", True),
            ("consent", "Yalnızca kullanıcı onayıyla / Only with user consent", False),
            ("vendor", "Onaylı sağlayıcı ile / With an approved vendor", False),
            ("other", "Diğer — kendim yazacağım / Other — I will specify", False),
        ),
        "depends_on": (),
    },
    "api_compat": {
        "terms": (
            "breaking", "breaking change", "backwards incompatible",
            "backward incompatible", "api version", "api versioning",
            "deprecate", "deprecation", "bozucu", "geriye dönük uyumsuz",
            "geriye donuk uyumsuz", "api sürümü", "api surumu", "kaldırma",
        ),
        "question": ("Breaking API değişikliği nasıl yönetilsin? / "
                     "How should a breaking API change be managed?"),
        "options": (
            ("versioned", "Yeni API versiyonu / New API version", True),
            ("deprecate", "Kademeli deprecation / Gradual deprecation", False),
            ("break", "Doğrudan breaking change / Direct breaking change", False),
        ),
        "depends_on": (),
    },
    "auth_session": {
        "terms": (
            "auth", "authentication", "authorization", "login", "log in",
            "sign-in", "signin", "sign in", "password", "session", "jwt",
            "oauth", "sso", "credential", "credentials", "cookie", "çerez",
            "cerez", "giriş", "giris", "oturum", "şifre", "sifre", "parola",
            "kimlik doğrulama", "yetkilendirme",
        ),
        "question": "Kullanıcı oturumları nasıl yönetilsin? / How should user sessions be managed?",
        "options": (
            ("cookie", "HttpOnly secure session cookie", True),
            ("jwt", "Access/refresh JWT", False),
            ("sso", "Mevcut kurum SSO sistemi / Existing organizational SSO", False),
            ("other", "Diğer — kendim yazacağım / Other — I will specify", False),
        ),
        "depends_on": (),
    },
    "file_access": {
        "terms": (
            "share", "sharing", "shared", "multi-user", "multiuser",
            "multi user", "collaboration", "collaborative", "team access",
            "workspace", "permission", "permissions", "public link",
            "access control", "access model", "paylaş", "paylas",
            "paylaşım", "paylasim", "çok kullanıcı", "cok kullanici",
            "ekip", "erişim", "erisim", "erişim kontrolü",
            "erisim kontrolu", "erişim modeli", "erisim modeli", "izin",
        ),
        "question": "Dosya paylaşımında erişim modeli ne olsun? / What access model should file sharing use?",
        "options": (
            ("owner", "Yalnızca dosya sahibi / Owner only", True),
            ("team", "Ekip üyeleri / Team members", False),
            ("role", "Rol tabanlı erişim / Role-based access", False),
            ("public", "Herkese açık bağlantı / Public link", False),
        ),
        "depends_on": (),
    },
    "database": {
        "terms": (
            "database", "databases", "db", "migration", "migrations",
            "schema", "postgres", "postgresql", "sqlite", "mysql", "mongo",
            "mongodb", "sql", "veritabanı", "veritabani", "migrasyon",
            "şema", "sema", "göç", "goc",
        ),
        "question": "Veritabanı yaklaşımı ne olsun? / What database approach should apply?",
        "options": (
            ("postgres", "PostgreSQL", True),
            ("sqlite", "SQLite", False),
            ("mysql", "MySQL", False),
            ("existing", "Mevcut kurum veritabanı / Existing organizational database", False),
            ("other", "Diğer — kendim yazacağım / Other — I will specify", False),
        ),
        "depends_on": (),
    },
    "file_storage": {
        "terms": (
            "storage", "file storage", "upload", "uploads", "file upload",
            "object storage", "s3", "bucket", "depolama", "dosya yükle",
            "dosya depola", "dosya sakla",
        ),
        "question": "Dosyalar nerede saklansın? / Where should files be stored?",
        "options": (
            ("local", "Yerel dosya sistemi / Local filesystem", True),
            ("s3", "S3 uyumlu object storage / S3-compatible object storage", False),
            ("db", "Veritabanı içinde / In the database", False),
            ("other", "Diğer — kendim yazacağım / Other — I will specify", False),
        ),
        "depends_on": ("file_access",),
    },
}

CHOICES = {
    "auth_session": {
        "jwt": ("jwt", "json web token", "jsonwebtoken", "access token",
                "refresh token", "bearer token"),
        "cookie": ("session cookie", "session-based", "session based",
                   "cookie", "çerez", "cerez", "oturum çerezi",
                   "oturum cerezi", "oturum tabanlı", "oturum tabanli"),
        "sso": ("sso", "single sign-on", "single sign on", "kurum sso",
                "kurumsal sso"),
    },
    "database": {
        "postgres": ("postgres", "postgresql"),
        "sqlite": ("sqlite",),
        "mysql": ("mysql", "mariadb"),
        "mongodb": ("mongo", "mongodb"),
        "existing": ("existing organizational database", "existing database",
                     "mevcut kurum veritabanı", "mevcut kurum veritabani",
                     "mevcut veritabanı", "mevcut veritabani"),
    },
    "file_storage": {
        "s3": ("s3", "object storage", "bucket"),
        "local": ("local filesystem", "local file system", "local storage",
                  "local disk", "yerel dosya sistemi", "yerel disk",
                  "yerel depolama"),
        "db": ("in the database", "in database", "veritabanında",
               "veritabaninda"),
    },
    "file_access": {
        "owner": ("owner only", "yalnızca dosya sahibi", "yalnizca dosya sahibi"),
        "team": ("team members", "team access", "ekip üyeleri",
                 "ekip uyeleri"),
        "role": ("role-based access", "role based access", "rol tabanlı erişim",
                 "rol tabanli erisim"),
        "public": ("public link", "public access", "herkese açık bağlantı",
                   "herkese acik baglanti"),
    },
    "api_compat": {
        "versioned": ("new api version", "yeni api versiyonu", "api version"),
        "deprecate": ("deprecation", "deprecate", "kademeli deprecation"),
        "break": ("breaking change", "doğrudan breaking change",
                  "dogrudan breaking change"),
    },
    "data_retention": {
        "soft": ("soft delete", "geri alma", "recoverable delete"),
        "hard": ("hard delete", "permanent delete", "delete permanently",
                 "permanently delete", "kalıcı sil", "kalici sil",
                 "permanent deletion"),
        "retention": ("retention", "saklama süresi", "saklama suresi"),
    },
    "payments": {
        "existing": ("existing payment", "mevcut ödeme", "mevcut odeme"),
        "stripe": ("stripe",),
        "manual": ("manual payment", "manuel ödeme", "manuel odeme", "invoice"),
    },
    "roles": {
        "existing": ("existing role", "mevcut rol"),
        "rbac": ("rbac", "role-based access", "rol tabanlı erişim",
                 "rol tabanli erisim"),
        "single": ("single user", "tek kullanıcı", "tek kullanici"),
    },
}

CHOICE_LABELS = {
    "auth_session": {"jwt": "JWT", "cookie": "session cookie", "sso": "SSO"},
    "database": {"postgres": "PostgreSQL", "sqlite": "SQLite",
                 "mysql": "MySQL", "mongodb": "MongoDB",
                 "existing": "existing organizational database"},
    "file_storage": {"s3": "S3 object storage", "local": "local filesystem",
                     "db": "database storage"},
    "file_access": {"owner": "owner only", "team": "team members",
                    "role": "role-based access", "public": "public link"},
    "api_compat": {"versioned": "new API version", "deprecate": "deprecation",
                   "break": "direct breaking change"},
    "data_retention": {"soft": "soft delete", "hard": "hard delete",
                       "retention": "retention window"},
    "payments": {"existing": "existing payment provider", "stripe": "Stripe",
                 "manual": "manual payment"},
    "roles": {"existing": "existing role model", "rbac": "RBAC",
              "single": "single user"},
}


def detect_topics(request: str) -> list[str]:
    return [topic for topic in TOPIC_ORDER
            if mentions_any(request, RULES[topic]["terms"])]


def active_decisions(decisions) -> list[dict]:
    """Confirmed/code-derived decisions only; superseded is never suppression."""
    return [item for item in decisions or []
            if isinstance(item, dict)
            and item.get("status") in ("confirmed", "code_derived")]


def effective_decisions(decisions) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for item in active_decisions(decisions):
        topic = item.get("topic")
        if not isinstance(topic, str):
            continue
        current = result.get(topic)
        if current is None or (current.get("status") == "code_derived"
                               and item.get("status") == "confirmed"):
            result[topic] = item
    return result


def choice_from_text(topic: str, value: str) -> str | None:
    for choice, terms in CHOICES.get(topic, {}).items():
        if mentions_any(value, terms):
            return choice
    return None


def explicit_choices(request: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for topic in TOPIC_ORDER:
        choices = [choice for choice, terms in CHOICES.get(topic, {}).items()
                   if mentions_any(request, terms)]
        if choices:
            result[topic] = choices
    return result


def code_choice(topic: str, facts: dict) -> str | None:
    code = (facts or {}).get("code_detected") or (facts or {}).get("detected") or {}
    if topic == "auth_session":
        found = []
        if code.get("jwt"):
            found.append("jwt")
        if code.get("session"):
            found.append("cookie")
        return found[0] if len(found) == 1 else None
    if topic == "database":
        found = [choice for choice, key in (
            ("postgres", "postgres"), ("sqlite", "sqlite"),
            ("mysql", "mysql")) if code.get(key)]
        return found[0] if len(found) == 1 else None
    if topic == "file_storage":
        return "s3" if code.get("s3") else None
    return None


def code_evidence(topic: str, facts: dict) -> list[str]:
    code = (facts or {}).get("code_detected") or (facts or {}).get("detected") or {}
    found = (facts or {}).get("code_evidence") or (facts or {}).get("evidence") or {}
    keys = {
        "auth_session": ("jwt", "session"),
        "database": ("postgres", "sqlite", "mysql"),
        "file_storage": ("s3",),
    }.get(topic, ())
    paths = []
    for key in keys:
        if code.get(key):
            for path in found.get(key) or []:
                if isinstance(path, str) and path not in paths:
                    paths.append(path)
    return paths[:5]


def find_contradictions(request: str, decisions, facts: dict) -> list[dict]:
    requested = explicit_choices(request)
    effective = effective_decisions(decisions)
    contradictions = []
    for topic in TOPIC_ORDER:
        choices = requested.get(topic)
        if not choices:
            continue
        decision = effective.get(topic)
        if decision is not None:
            evidence = choice_from_text(topic, str(decision.get("decision") or ""))
            source = ("confirmed decision" if decision.get("status") == "confirmed"
                      else "code-derived decision")
            detail = str(decision.get("decision") or "")
        else:
            evidence = code_choice(topic, facts)
            source = "repository code evidence"
            detail = ", ".join(code_evidence(topic, facts))
        if evidence is None:
            continue
        conflicting = [choice for choice in choices if choice != evidence]
        if not conflicting:
            continue
        contradictions.append({
            "topic": topic,
            "requested": conflicting,
            "evidence": evidence,
            "source": source,
            "detail": detail[:300],
        })
    return contradictions


def is_suppressed(topic: str, decisions, facts: dict) -> bool:
    if topic in effective_decisions(decisions):
        return True
    code = (facts or {}).get("code_detected") or (facts or {}).get("detected") or {}
    if topic == "auth_session":
        # Exactly one code family is evidence; both JWT and session code is
        # ambiguous and must be interviewed rather than assumed.
        return bool(code.get("jwt")) != bool(code.get("session"))
    if topic == "database":
        return sum(1 for key in ("postgres", "sqlite", "mysql")
                   if code.get(key)) == 1
    if topic == "file_storage":
        return bool(code.get("s3"))
    return False


def _choice_label(topic: str, choice: str | None) -> str:
    if not choice:
        return "unknown"
    return CHOICE_LABELS.get(topic, {}).get(choice, choice)


def _conflict_text(topic: str, contradiction: dict) -> str:
    requested = ", ".join(_choice_label(topic, choice)
                          for choice in contradiction["requested"])
    evidence = _choice_label(topic, contradiction["evidence"])
    source = contradiction["source"]
    return (f"Çelişki: İstek {requested} diyor ancak {source} {evidence} gösteriyor. "
            f"Hangisi geçerli? / Conflict: the request names {requested} but "
            f"{source} indicates {evidence}. Which applies?")


def critical_questions(request: str, decisions, facts: dict, *,
                       mode: str = "auto") -> list[dict]:
    """Return deterministic, risk-ordered critical questions.

    Shared by the Assumption Guard and the Grill-Me engine so the decision to
    interview and the question actually asked cannot drift apart.
    """
    detected = detect_topics(request)
    contradictions = {item["topic"]: item for item in
                      find_contradictions(request, decisions, facts)}
    selected = []
    for topic in TOPIC_ORDER:
        wanted = topic in detected or topic in contradictions
        if not wanted:
            continue
        if topic not in contradictions and is_suppressed(topic, decisions, facts):
            continue
        selected.append(topic)
    # Dependencies must be answered first; add a missing parent question.
    known = set(effective_decisions(decisions))
    changed = True
    while changed:
        changed = False
        for topic in list(selected):
            for dependency in RULES[topic].get("depends_on", ()):
                if topic == "file_storage" and dependency == "file_access" \
                        and dependency not in selected:
                    # A pure storage request has no sharing/access ambiguity
                    # to resolve; do not invent an extra interview question.
                    continue
                if dependency in selected or dependency in known:
                    continue
                if is_suppressed(dependency, decisions, facts):
                    known.add(dependency)
                    continue
                selected.append(dependency)
                changed = True
    selected = [topic for topic in TOPIC_ORDER if topic in selected]
    questions = []
    for topic in selected:
        rule = RULES[topic]
        depends_on = list(rule["depends_on"])
        if topic == "file_storage" and "file_access" not in selected:
            depends_on = []
        contradiction = contradictions.get(topic)
        if contradiction is not None:
            text = _conflict_text(topic, contradiction)
            recommended = contradiction["evidence"]
            options = list(rule["options"])
            if not any(option[0] == recommended for option in options):
                options.append((recommended, _choice_label(topic, recommended), False))
        else:
            text = rule["question"]
            recommended = next((option[0] for option in rule["options"]
                                if option[2]), None)
            options = list(rule["options"])
        questions.append(make_question(
            topic, text, options, critical=True,
            depends_on=depends_on))
        if recommended is not None:
            questions[-1]["recommended_option"] = recommended
            for option in questions[-1]["options"]:
                option["recommended"] = option["id"] == recommended
    if not questions and mode == "required":
        questions.append(make_question(
            "scope",
            "Bu görevin kapsam dışında bırakılacak maddeleri neler? / "
            "What should be explicitly out of scope for this task?",
            [
                ("none", "Ek kapsam dışı madde yok", True),
                ("other", "Diğer — kendim yazacağım", False),
            ], critical=True))
    return questions


FOLLOWUPS = {
    ("auth_session", "jwt"): {
        "key": "jwt_refresh",
        "text": "JWT yenileme ve logout davranışı ne olsun? / What JWT refresh and logout behavior should apply?",
        "options": (
            ("refresh", "Kısa access token + refresh token / Short access token + refresh token", True),
            ("short", "Yalnızca kısa ömürlü access token / Short-lived access token only", False),
            ("sliding", "Sliding session", False),
        ),
        "depends_on": ("auth_session",),
    },
    ("auth_session", "cookie"): {
        "key": "session_expiry",
        "text": "Oturum süresi ve yenileme politikası ne olsun? / What session expiry policy should apply?",
        "options": (
            ("idle30", "30 dk hareketsizlikte sona er / Expire after 30 minutes idle", True),
            ("idle8h", "8 saat hareketsizlik / 8 hours idle", False),
            ("remember", "Beni hatırla (30 gün) / Remember me (30 days)", False),
            ("other", "Diğer — kendim yazacağım / Other — I will specify", False),
        ),
        "depends_on": ("auth_session",),
    },
    ("database", "any"): {
        "key": "db_rollback",
        "text": "Veritabanı migrasyonu için geri alma stratejisi ne olsun? / What rollback strategy should migrations use?",
        "options": (
            ("forward", "İleri-only + yedek / Forward-only with backup", True),
            ("down", "Her migrasyon için down script / Down script per migration", False),
            ("recreate", "Snapshot'tan yeniden oluştur / Recreate from snapshot", False),
            ("other", "Diğer — kendim yazacağım / Other — I will specify", False),
        ),
        "depends_on": ("database",),
    },
    ("api_compat", "any"): {
        "key": "api_deprecation",
        "text": "Breaking API değişikliği için deprecation stratejisi ne olsun? / What deprecation strategy should apply?",
        "options": (
            ("notice", "En az 2 sürüm önceden duyur / Announce at least 2 versions ahead", True),
            ("shim", "Uyumluluk katmanı / Compatibility shim", False),
            ("none", "Duyuru penceresi yok / No deprecation window", False),
        ),
        "depends_on": ("api_compat",),
    },
    ("file_access", "any"): {
        "key": "file_delete",
        "text": "Dosya silme politikası ne olsun? / What file deletion policy should apply?",
        "options": (
            ("soft", "Soft delete + geri alma / Soft delete with recovery", True),
            ("hard", "Kalıcı silme / Hard delete", False),
            ("retain", "Saklama süresi sonunda sil / Delete after retention period", False),
        ),
        "depends_on": ("file_access",),
    },
}


def followup_questions(key: str, option: str | None) -> list[dict]:
    spec = FOLLOWUPS.get((key, option)) or FOLLOWUPS.get((key, "any"))
    if spec is None:
        return []
    return [make_question(
        spec["key"], spec["text"], spec["options"], critical=True,
        depends_on=spec["depends_on"])]


def _git_head(root: Path, context_state: dict) -> str | None:
    current = context_state.get("current_head")
    if isinstance(current, str) and current:
        return current
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _canonical_digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_context(root: Path, request: str) -> dict:
    identity = ensure_project_identity(root)
    with ProjectMemory(root, identity=identity) as memory:
        state = memory.context_state()
        decisions = [item for item in memory.list_decisions(limit=500)
                     if item.get("status") in ("confirmed", "code_derived")]
        requirements = [item for item in
                        memory.list_requirements(status="unresolved_blocking",
                                                 limit=50)]
        context_summary = memory.context_summary(limit=2400, request=request)
    return {
        "identity": identity,
        "state": state,
        "decisions": decisions,
        "requirements": requirements,
        "context_summary": context_summary,
        "head_sha": _git_head(root, state),
    }


def _assumptions(decisions, facts: dict, required_topics, mode: str) -> list[dict]:
    assumptions = []
    effective = effective_decisions(decisions)
    for topic in TOPIC_ORDER:
        decision = effective.get(topic)
        if decision is not None:
            assumptions.append({
                "topic": topic,
                "decision": str(decision.get("decision") or "")[:300],
                "source": decision.get("status"),
                "decision_ref": decision.get("decision_id"),
                "critical": True,
            })
            continue
        choice = code_choice(topic, facts)
        if choice is not None:
            assumptions.append({
                "topic": topic,
                "decision": _choice_label(topic, choice),
                "source": "code",
                "evidence": code_evidence(topic, facts),
                "critical": True,
            })
    for topic in required_topics:
        if any(item["topic"] == topic for item in assumptions):
            continue
        assumptions.append({
            "topic": topic,
            "decision": None,
            "source": "unresolved_blocking",
            "critical": True,
        })
    return assumptions[:20]


def _receipt(request_hash: str, head_sha, context_digest: str, status: str,
             interview_id, assumptions, platform: str, mode: str) -> dict:
    return {
        "version": GUARD_VERSION,
        "request_hash": request_hash,
        "head_sha": head_sha,
        "context_digest": context_digest,
        "status": status,
        "interview_id": interview_id,
        "assumptions": assumptions,
        "platform": platform,
        "mode": mode,
    }


def _approved_interview(root: Path, request: str, interview_id: str,
                        identity: dict) -> dict:
    try:
        from tools.grill_me import GrillError, GrillMeEngine
    except ModuleNotFoundError:  # flat installed layout
        from grill_me import GrillError, GrillMeEngine  # type: ignore
    try:
        with GrillMeEngine(root, identity=identity) as engine:
            interview = engine.get_interview(interview_id)
            if interview["status"] != "approved":
                raise AssumptionGuardError("interview is not approved")
            if not engine.verify_request(interview_id, request):
                raise AssumptionGuardError(
                    "request does not match the approved interview; "
                    "use the original request text")
            return interview
    except GrillError as exc:
        raise AssumptionGuardError(str(exc)) from exc


def _public_question(question):
    if not isinstance(question, dict):
        return question
    value = dict(question)
    value.setdefault("key", value.get("question_key"))
    value.setdefault("text", value.get("question"))
    return value


def build_requirements_contract(root, interview_id):
    """Build a contract through the one shared requirements-contract builder."""
    try:
        from tools import requirements_contract
    except ModuleNotFoundError:  # flat installed layout
        import requirements_contract  # type: ignore
    return requirements_contract.build_from_interview(root, interview_id)


def _ensure_interview(root: Path, request: str, *, platform: str, mode: str,
                      identity: dict) -> tuple[dict, dict | None, bool]:
    try:
        from tools.grill_me import GrillError, GrillMeEngine
    except ModuleNotFoundError:  # flat installed layout
        from grill_me import GrillError, GrillMeEngine  # type: ignore
    try:
        with GrillMeEngine(root, identity=identity) as engine:
            existing = engine.open_interview()
            if (existing is not None
                    and existing["status"] not in ("approved", "cancelled")):
                if engine.verify_request(existing["interview_id"], request):
                    interview_id = existing["interview_id"]
                    return (engine.get_interview(interview_id),
                            _public_question(engine.next_question(interview_id)),
                            True)
                # A different request must not lose its own persistent
                # interview. Create a separate one; the old one remains
                # resumable by id and is never silently overwritten.
                result = engine.start(request, origin_platform=platform,
                                      mode=mode, allow_new=True)
                return (result["interview"],
                        _public_question(result["question"]), False)
            result = engine.start(request, origin_platform=platform, mode=mode)
            return (result["interview"], _public_question(result["question"]), False)
    except GrillError as exc:
        raise AssumptionGuardError(str(exc)) from exc


def evaluate(root, request, *, platform="unknown", mode="auto",
             interview_id=None) -> dict:
    """Preflight a request before plan/runner/engine state is written.

    Returns ``status == "allowed"`` or ``status == "interview_required"`` and a
    deterministic guard receipt suitable for plan metadata.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise AssumptionGuardError("root directory does not exist")
    request = _bounded_request(request)
    if mode not in MODES:
        raise AssumptionGuardError("mode must be off, auto, required or plan-only")
    if not isinstance(platform, str) or not platform or len(platform) > 40:
        raise AssumptionGuardError("platform is invalid")
    request_hash = hashlib.sha256(request.encode("utf-8")).hexdigest()
    try:
        context = _load_context(root, request)
        facts = scan_repository(root)
    except (MemoryError, OSError, ValueError) as exc:
        raise AssumptionGuardError(f"project context could not be read: {exc}") from exc
    decisions = context["decisions"]
    contradictions = find_contradictions(request, decisions, facts)
    detected = detect_topics(request)
    unresolved = [topic for topic in detected
                  if topic not in {item["topic"] for item in contradictions}
                  and not is_suppressed(topic, decisions, facts)]
    if interview_id:
        approved = _approved_interview(root, request, str(interview_id),
                                       context["identity"])
        digest = _canonical_digest({
            "head": context["head_sha"],
            "decisions": context["decisions"],
            "requirements": context["requirements"],
            "code_detected": facts.get("code_detected"),
            "code_evidence": facts.get("code_evidence"),
        })
        assumptions = _assumptions(decisions, facts, [], mode)
        receipt = _receipt(request_hash, context["head_sha"], digest,
                           "allowed", approved["interview_id"], assumptions,
                           platform, mode)
        return {
            "status": "allowed",
            "allowed": True,
            "request_hash": request_hash,
            "head_sha": context["head_sha"],
            "context_digest": digest,
            "interview_id": approved["interview_id"],
            "interview": approved,
            "question": None,
            "reasons": [],
            "contradictions": contradictions,
            "topics": detected,
            "assumptions": assumptions,
            "mode": mode,
            "platform": platform,
            "receipt": receipt,
            "guard": receipt,
            "guard_receipt": receipt,
        }
    required_topics = list(unresolved)
    reasons = []
    for item in contradictions:
        required_topics.append(item["topic"])
        reasons.append(
            f"Contradiction in {item['topic']}: request names "
            f"{', '.join(item['requested'])} but {item['source']} indicates "
            f"{item['evidence']}.")
    for topic in unresolved:
        reasons.append(
            f"Critical ambiguity in {topic}: no confirmed decision or "
            "code/config evidence resolves it.")
    if mode == "required":
        reasons.append("Grill-Me mode 'required' always interviews.")
    if mode == "off" and (unresolved or contradictions):
        reasons.append("Grill-Me mode 'off' still blocks critical ambiguity.")
    need_interview = mode == "required" or bool(unresolved) or bool(contradictions)
    digest = _canonical_digest({
        "head": context["head_sha"],
        "decisions": context["decisions"],
        "requirements": context["requirements"],
        "code_detected": facts.get("code_detected"),
        "code_evidence": facts.get("code_evidence"),
    })
    if not need_interview:
        assumptions = _assumptions(decisions, facts, [], mode)
        receipt = _receipt(request_hash, context["head_sha"], digest,
                           "allowed", None, assumptions, platform, mode)
        return {
            "status": "allowed",
            "allowed": True,
            "request_hash": request_hash,
            "head_sha": context["head_sha"],
            "context_digest": digest,
            "interview_id": None,
            "interview": None,
            "question": None,
            "reasons": [],
            "contradictions": [],
            "topics": detected,
            "assumptions": assumptions,
            "mode": mode,
            "platform": platform,
            "receipt": receipt,
            "guard": receipt,
            "guard_receipt": receipt,
        }
    interview, question, resumed = _ensure_interview(
        root, request, platform=platform, mode=mode,
        identity=context["identity"])
    if question is None:
        reasons.append("The interview has no open question; approve it before starting.")
    assumptions = _assumptions(decisions, facts, required_topics, mode)
    receipt = _receipt(request_hash, context["head_sha"], digest,
                       "interview_required", interview["interview_id"],
                       assumptions, platform, mode)
    return {
        "status": "interview_required",
        "allowed": False,
        "request_hash": request_hash,
        "head_sha": context["head_sha"],
        "context_digest": digest,
        "interview_id": interview["interview_id"],
        "interview": interview,
        "question": question,
        "reasons": reasons,
        "contradictions": contradictions,
        "topics": detected,
        "assumptions": assumptions,
        "mode": mode,
        "platform": platform,
        "resumed": resumed,
        "receipt": receipt,
        "guard": receipt,
        "guard_receipt": receipt,
    }
