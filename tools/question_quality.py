"""Deterministic question-quality signals for Grill-Me 5.0.

The scorer is advisory metadata, not an approval mechanism. Critical questions
are never dropped; optional low-gain questions can be filtered by adapters.
"""
from __future__ import annotations

import json

RISK_TOPICS = frozenset({
    "auth_session", "payments", "data_retention", "database", "file_access",
    "api_compat", "cryptography", "concurrency",
})


def _depends(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return [item for item in parsed if isinstance(item, str)]
    return []


def score_question(question, *, answered_keys=(), decision_topics=(),
                   repo_facts=None, risk_topics=None) -> dict:
    if not isinstance(question, dict):
        raise ValueError("question must be an object")
    key = question.get("question_key")
    if not isinstance(key, str) or not key:
        raise ValueError("question_key is required")
    critical = bool(question.get("critical", False))
    dependencies = _depends(question.get("depends_on"))
    answered = {str(item) for item in answered_keys}
    topics = {str(item) for item in decision_topics}
    facts = repo_facts if isinstance(repo_facts, dict) else {}
    risk = set(risk_topics or RISK_TOPICS)
    unmet = [item for item in dependencies if item not in answered]
    relevance = 1.0 if not unmet else 0.35
    information_gain = 0.15 if key in topics else 1.0
    redundancy = 0.0 if key in answered else 1.0
    topic = str(question.get("topic") or key)
    if critical and topic in risk:
        risk_coverage = 1.0
    elif critical:
        risk_coverage = 0.7
    elif topic in risk:
        risk_coverage = 0.6
    else:
        risk_coverage = 0.25
    options = question.get("options")
    option_count = len(options) if isinstance(options, list) else 0
    burden = 0.2 if critical else (0.4 if option_count else 0.8)
    fact_suppressed = bool(str(facts.get(topic) or "").strip()) and not critical
    if fact_suppressed:
        information_gain *= 0.3
    score = round(
        0.30 * relevance + 0.25 * information_gain + 0.20 * redundancy
        + 0.15 * risk_coverage + 0.10 * (1.0 - burden), 3)
    return {
        "question_key": key,
        "relevance": relevance,
        "information_gain": round(information_gain, 3),
        "redundancy": redundancy,
        "risk_coverage": risk_coverage,
        "user_burden": burden,
        "score": score,
        "critical": critical,
        "eligible": not unmet and redundancy > 0,
    }


def rank_questions(questions, *, answered_keys=(), decision_topics=(),
                   repo_facts=None, threshold=0.45) -> list[dict]:
    scored = []
    for question in questions:
        value = score_question(
            question, answered_keys=answered_keys,
            decision_topics=decision_topics, repo_facts=repo_facts)
        value["question"] = question
        scored.append(value)
    scored.sort(key=lambda item: (-item["score"], item["question_key"]))
    return [item for item in scored
            if item["critical"] or item["score"] >= threshold]
