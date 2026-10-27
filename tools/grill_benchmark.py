#!/usr/bin/env python3
"""Deterministic V7.7 Grill-Me benchmark with an honest optional-provider slot.

The fixture has 40 bilingual cases (20 Turkish / 20 English). The deterministic
Assumption Guard is always the baseline; an optional injected provider can be
measured but never becomes authoritative and never writes decisions.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

try:
    from tools import assumption_guard, grill_llm
    from tools.project_memory import ProjectMemory
    from tools.repo_facts import scan_repository
except ModuleNotFoundError:  # flat installed layout
    import assumption_guard  # type: ignore
    import grill_llm  # type: ignore
    from project_memory import ProjectMemory  # type: ignore
    from repo_facts import scan_repository  # type: ignore

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "assets" / "v77-grill-benchmark.json"
TECH_IDS = {"jwt", "session", "postgres", "sqlite", "mysql", "mongodb", "s3"}
MAX_FIXTURE_ITEMS = 40


def _case(case_id, lang, request, topics, *, should_interview=None,
          forbidden=None, seeded=None, contradiction=False, floor=None,
          repo_files=None):
    topics = list(topics)
    seeded_decisions = [dict(item, status=item.get("status", "confirmed"),
                             source=item.get("source", "user"),
                             confirmed_by=item.get("confirmed_by", "user"))
                        for item in (seeded or [])]
    return {
        "id": case_id,
        "lang": lang,
        "request": request,
        "expected_topics": topics,
        "expected_question_keys": list(topics),
        "should_interview": (bool(topics) if should_interview is None
                             else bool(should_interview)),
        "forbidden_technologies": list(forbidden or []),
        "seeded_decisions": seeded_decisions,
        "contradiction": bool(contradiction),
        "expected_floor": (len(topics) if floor is None else int(floor)),
        "repo_files": dict(repo_files or {}),
    }


README_TECH = {"README.md": "This project uses JWT, PostgreSQL and S3.\n"}
JWT_CODE = {"backend/auth.py": "import jwt\n"}

FIXTURE = [
    # Turkish (20)
    _case("tr-01", "tr", "Kullanıcı giriş sistemini geliştir",
          ["auth_session"]),
    _case("tr-02", "tr", "Kredi kartı ödemesi ekle", ["payments"]),
    _case("tr-03", "tr", "Veritabanı migrasyonu ekle", ["database"]),
    _case("tr-04", "tr", "Dosya paylaşımı ve depolama ekle",
          ["file_access", "file_storage"]),
    _case("tr-05", "tr", "Kalıcı silme özelliği ekle", ["data_retention"]),
    _case("tr-06", "tr", "Veriyi üçüncü tarafa aktar",
          ["external_transfer"]),
    _case("tr-07", "tr", "Breaking API değişikliği yap", ["api_compat"]),
    _case("tr-08", "tr", "Rol ve yetki modeli ekle", ["roles"]),
    _case("tr-09", "tr", "Dosya yükleme depolaması ekle", ["file_storage"],
          forbidden=["s3"]),
    _case("tr-10", "tr", "README dosyasındaki yazım hatasını düzelt", [],
          repo_files=README_TECH,
          forbidden=["jwt", "postgres", "s3"]),
    _case("tr-11", "tr", "JSON doğrulama aracı ekle", [],
          forbidden=["sqlite", "postgres", "mysql", "jwt"]),
    _case("tr-12", "tr", "Kullanıcı giriş sistemini geliştir", [],
          seeded=[{"topic": "auth_session", "decision": "JWT",
                   "rationale": "User confirmed JWT"}]),
    _case("tr-13", "tr", "Veritabanı migrasyonu ekle", [],
          seeded=[{"topic": "database", "decision": "PostgreSQL",
                   "rationale": "User confirmed PostgreSQL"}]),
    _case("tr-14", "tr", "Oturum çerezi ile kimlik doğrulama yap",
          ["auth_session"], contradiction=True, repo_files=JWT_CODE),
    _case("tr-15", "tr", "Kimlik doğrulama için JWT kullan",
          ["auth_session"], contradiction=True,
          seeded=[{"topic": "auth_session", "decision": "session cookie",
                   "rationale": "User confirmed session cookies"}]),
    _case("tr-16", "tr", "Ödeme, giriş ve veri silme ekle",
          ["payments", "data_retention", "auth_session"]),
    _case("tr-17", "tr", "Kullanıcı verilerini sil ve dışa aktar",
          ["data_retention", "external_transfer"]),
    _case("tr-18", "tr", "Kullanıcı giriş sistemini geliştir",
          ["auth_session"], repo_files=README_TECH,
          forbidden=["jwt", "postgres"]),
    _case("tr-19", "tr", "Yazar biyografisini güncelle", [],
          forbidden=["jwt"]),
    _case("tr-20", "tr", "Çok kullanıcılı dosya paylaşım sistemi ekle",
          ["file_access"]),
    # English (20)
    _case("en-01", "en", "Add login to the app", ["auth_session"]),
    _case("en-02", "en", "Add Stripe checkout", ["payments"]),
    _case("en-03", "en", "Add PostgreSQL migration", ["database"]),
    _case("en-04", "en", "Implement multi-user file sharing and uploads",
          ["file_access", "file_storage"]),
    _case("en-05", "en", "Add data deletion", ["data_retention"]),
    _case("en-06", "en", "Send user data to a third-party analytics service",
          ["external_transfer"]),
    _case("en-07", "en",
          "Make a breaking change to the API response shape", ["api_compat"]),
    _case("en-08", "en", "Add a role model for administrators", ["roles"]),
    _case("en-09", "en", "Add file uploads to object storage",
          ["file_storage"], forbidden=["s3"]),
    _case("en-10", "en", "Fix a typo in the README", [],
          repo_files=README_TECH,
          forbidden=["jwt", "postgres", "s3"]),
    _case("en-11", "en", "Add a JSON schema validator", [],
          forbidden=["sqlite", "postgres", "mysql", "jwt"]),
    _case("en-12", "en", "Add login", [],
          seeded=[{"topic": "auth_session", "decision": "JWT",
                   "rationale": "User confirmed JWT"}]),
    _case("en-13", "en", "Add checkout", [],
          seeded=[{"topic": "payments", "decision": "Stripe",
                   "rationale": "User confirmed Stripe"}]),
    _case("en-14", "en", "Use session cookies for authentication",
          ["auth_session"], contradiction=True, repo_files=JWT_CODE),
    _case("en-15", "en", "Use JWT for authentication", ["auth_session"],
          contradiction=True,
          seeded=[{"topic": "auth_session", "decision": "session cookie",
                   "rationale": "User confirmed session cookies"}]),
    _case("en-16", "en", "Add payments, login and user deletion",
          ["payments", "data_retention", "auth_session"]),
    _case("en-17", "en", "Export user data to an external service",
          ["external_transfer"]),
    _case("en-18", "en", "Add authentication", ["auth_session"],
          repo_files=README_TECH, forbidden=["jwt", "postgres"]),
    _case("en-19", "en", "Update the author biography", [],
          forbidden=["jwt"]),
    _case("en-20", "en", "Add session management", ["auth_session"]),
]

# Compatibility aliases for readers that call the fixture by another name.
CASES = FIXTURE
BENCHMARK_CASES = FIXTURE


def fixture_cases():
    """Return a copy-safe fixture list (the fixture itself is immutable)."""
    return [dict(case) for case in FIXTURE]


def _write_repo(root: Path, files) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in (files or {}).items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("fixture repository path is invalid")
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(text), encoding="utf-8")


def _seed_decisions(memory, case) -> None:
    for index, decision in enumerate(case.get("seeded_decisions") or []):
        memory.add_decision(
            topic=decision["topic"], decision=decision["decision"],
            rationale=decision.get("rationale") or "User confirmed decision",
            source="user", source_ref=f"seed:{case['id']}:{index}",
            status="confirmed", confirmed_by="user")


def _inferred_technologies(facts, questions) -> set[str]:
    code = (facts or {}).get("code_detected") or {}
    inferred = {key for key in TECH_IDS if code.get(key)}
    for question in questions:
        recommended = question.get("recommended_option")
        if recommended in TECH_IDS:
            inferred.add(recommended)
    return inferred


def _evaluate_case(case, provider=None, *, timeout=2.0) -> dict:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _write_repo(root, case.get("repo_files"))
        with ProjectMemory(root) as memory:
            _seed_decisions(memory, case)
            facts = scan_repository(root)
            decisions = memory.list_decisions(limit=500)
            questions = assumption_guard.critical_questions(
                case["request"], decisions, facts, mode="auto")
            detected = assumption_guard.detect_topics(case["request"])
            contradictions = assumption_guard.find_contradictions(
                case["request"], decisions, facts)
            question_keys = [question["key"] for question in questions]
            expected_keys = set(case["expected_question_keys"])
            extraneous = [key for key in question_keys if key not in expected_keys]
            duplicates = sorted({key for key in question_keys
                                 if question_keys.count(key) > 1})
            inferred = _inferred_technologies(facts, questions)
            forbidden = set(case.get("forbidden_technologies") or [])
            false_hits = sorted(inferred & forbidden)
            unconfirmed = 0
            for decision in memory.list_decisions(status="confirmed", limit=500):
                source_ref = decision.get("source_ref") or ""
                if decision.get("source") != "user" or not source_ref.startswith(
                        f"seed:{case['id']}"):
                    unconfirmed += 1
            provider_result = {"status": "not_run"}
            if provider is not None:
                allowed = [question["key"] for question in questions]
                if not allowed:
                    provider_result = {"status": "skipped",
                                       "reason": "no deterministic question"}
                else:
                    before = len(memory.list_decisions(limit=500))
                    provider_result = provider.propose(
                        case["request"], decisions=decisions, facts=facts,
                        topics=allowed)
                    after = len(memory.list_decisions(limit=500))
                    provider_result = {
                        "status": provider_result.get("status"),
                        "topic_count": len(provider_result.get("topics") or []),
                        "topics": [item.get("topic") for item in
                                   provider_result.get("topics") or []],
                        "rejected": len(provider_result.get("rejected") or []),
                        "error": provider_result.get("error"),
                        "decisions_written": max(0, after - before),
                    }
            matched = sorted(set(case["expected_topics"]) & set(question_keys))
            contradiction_found = bool(contradictions)
            interview_match = bool(questions) == bool(case["should_interview"])
            floor_met = len(question_keys) >= int(case["expected_floor"])
            return {
                "id": case["id"],
                "lang": case["lang"],
                "request": case["request"],
                "should_interview": bool(questions),
                "expected_should_interview": bool(case["should_interview"]),
                "interview_match": interview_match,
                "detected_topics": detected,
                "questions": question_keys,
                "expected_topics": list(case["expected_topics"]),
                "expected_question_keys": list(case["expected_question_keys"]),
                "expected_floor": int(case["expected_floor"]),
                "forbidden_technologies": list(
                    case.get("forbidden_technologies") or []),
                "seeded_decision_count": len(
                    case.get("seeded_decisions") or []),
                "matched_topics": matched,
                "unnecessary_question_keys": extraneous,
                "duplicate_question_keys": duplicates,
                "contradiction_expected": bool(case["contradiction"]),
                "contradiction_found": contradiction_found,
                "contradictions": [item["topic"] for item in contradictions],
                "false_tech_inference": false_hits,
                "unconfirmed_assumptions": unconfirmed,
                "floor_met": floor_met,
                "completion": bool(interview_match and floor_met),
                "provider": provider_result,
            }


def _metrics(cases) -> dict:
    expected_topics = sum(len(case["expected_topics"]) for case in cases)
    matched_topics = sum(len(case["matched_topics"]) for case in cases)
    asked = sum(len(case["questions"]) for case in cases)
    unnecessary = sum(len(case["unnecessary_question_keys"]) for case in cases)
    duplicates = sum(len(case["duplicate_question_keys"]) for case in cases)
    false_cases = sum(1 for case in cases if case["false_tech_inference"])
    contradiction_cases = [case for case in cases
                           if case["contradiction_expected"]]
    contradiction_found = sum(1 for case in contradiction_cases
                              if case["contradiction_found"])
    unconfirmed = sum(case["unconfirmed_assumptions"] for case in cases)
    completed = sum(1 for case in cases if case["completion"])
    return {
        "cases": len(cases),
        "tr_cases": sum(1 for case in cases if case["lang"] == "tr"),
        "en_cases": sum(1 for case in cases if case["lang"] == "en"),
        "expected_topics": expected_topics,
        "matched_topics": matched_topics,
        "ambiguity_recall": (round(matched_topics / expected_topics, 4)
                             if expected_topics else 1.0),
        "asked_questions": asked,
        "unnecessary_questions": unnecessary,
        "unnecessary_question_rate": (round(unnecessary / asked, 4)
                                      if asked else 0.0),
        "unnecessary_rate": (round(unnecessary / asked, 4)
                             if asked else 0.0),
        "duplicate_questions": duplicates,
        "duplicate_question_rate": (round(duplicates / asked, 4)
                                    if asked else 0.0),
        "duplicate_rate": (round(duplicates / asked, 4)
                           if asked else 0.0),
        "false_tech_inference_cases": false_cases,
        "false_tech_inference": false_cases,
        "false_tech_inference_rate": (round(false_cases / len(cases), 4)
                                      if cases else 0.0),
        "contradiction_cases": len(contradiction_cases),
        "contradictions_found": contradiction_found,
        "contradiction_recall": (round(contradiction_found /
                                       len(contradiction_cases), 4)
                                 if contradiction_cases else 1.0),
        "unconfirmed_assumptions": unconfirmed,
        "completed_cases": completed,
        "completion": round(completed / len(cases), 4) if cases else 0.0,
        "completion_rate": round(completed / len(cases), 4) if cases else 0.0,
    }


def _provider_metrics(cases, provider) -> dict:
    if provider is None:
        return {
            "status": "not_run",
            "configured": False,
            "ran": False,
            "reason": ("No optional LLM provider was configured; only the "
                       "deterministic baseline ran."),
        }
    if isinstance(provider, grill_llm.GrillLLM) and not provider.enabled:
        return {
            "status": "not_run",
            "configured": False,
            "ran": False,
            "reason": "The supplied optional LLM provider is disabled.",
        }
    case_results = [case.get("provider") or {} for case in cases]
    ran = [item for item in case_results if item.get("status") not in
           ("not_run", "skipped")]
    valid = sum(int(item.get("topic_count") or 0) for item in ran)
    rejected = sum(int(item.get("rejected") or 0) for item in ran
                   if item.get("status") != "ok")
    errors = sum(1 for item in ran if item.get("status") in
                 ("timeout", "error", "invalid"))
    proposed_topics = set()
    for item in ran:
        if item.get("status") == "ok":
            proposed_topics |= set(item.get("topics") or [])
    expected = {topic for case in cases
                for topic in case["expected_topics"]}
    written = sum(int(item.get("decisions_written") or 0) for item in ran)
    return {
        "status": "run",
        "configured": True,
        "ran": True,
        "cases_called": len(ran),
        "cases_skipped": len(case_results) - len(ran),
        "valid_topics": valid,
        "rejected_outputs": rejected,
        "error_outputs": errors,
        "proposal_topic_recall": (round(len(proposed_topics & expected) /
                                       len(expected), 4)
                                  if expected else 1.0),
        "decisions_written": written,
        "unconfirmed_assumptions": 0,
        "note": ("Provider proposals are diagnostic only; the deterministic "
                 "guard questions and recommendations remain authoritative."),
    }


def run_benchmark(provider=None, *, timeout=2.0, include_cases=True) -> dict:
    if len(FIXTURE) != MAX_FIXTURE_ITEMS:
        raise ValueError("fixture must contain exactly 40 cases")
    languages = {case["lang"] for case in FIXTURE}
    if languages != {"tr", "en"}:
        raise ValueError("fixture must contain Turkish and English cases")
    if sum(1 for case in FIXTURE if case["lang"] == "tr") != 20:
        raise ValueError("fixture must contain 20 Turkish cases")
    if sum(1 for case in FIXTURE if case["lang"] == "en") != 20:
        raise ValueError("fixture must contain 20 English cases")
    provider_llm = None
    if provider is not None:
        provider_llm = (provider if isinstance(provider, grill_llm.GrillLLM)
                        else grill_llm.GrillLLM(provider, enabled=True,
                                                timeout=timeout))
    cases = [_evaluate_case(case, provider_llm, timeout=timeout)
             for case in FIXTURE]
    baseline = _metrics(cases)
    report = {
        "schema_version": 1,
        "kind": "v77-grill-benchmark",
        "generated_at": time.time(),
        "fixture": {"total": len(FIXTURE), "tr": 20, "en": 20,
                    "labels": ["expected_topics", "expected_question_keys",
                               "should_interview", "forbidden_technologies",
                               "seeded_decisions", "contradiction",
                               "expected_floor"]},
        "baseline": baseline,
        "provider": _provider_metrics(cases, provider_llm),
        "comparison": {
            "baseline_source": "deterministic_assumption_guard",
            "provider_authoritative": False,
            "provider_changes_guard_questions": False,
        },
    }
    if include_cases:
        report["cases"] = cases
    return report


def save_report(path, report) -> dict:
    target = Path(path)
    if not target.is_absolute():
        target = (REPO_ROOT / target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    target.write_text(text, encoding="utf-8")
    return {"path": str(target), "bytes": len(text.encode("utf-8")),
            "baseline": report.get("baseline"),
            "provider": report.get("provider")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--provider-command",
                        help="optional local command; default is not run")
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args(argv)
    report = run_benchmark(provider=args.provider_command,
                           timeout=args.timeout)
    saved = save_report(args.output, report)
    print(json.dumps(saved, indent=2, ensure_ascii=False))
    return 0


# Compatibility aliases for programmatic callers.
run = run_benchmark
benchmark = run_benchmark


if __name__ == "__main__":
    raise SystemExit(main())
