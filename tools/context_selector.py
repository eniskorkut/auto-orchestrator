#!/usr/bin/env python3
"""Bounded, scored context selection for runner prompts and IDE context.

The guard receipt digest is deliberately independent of this module: selection
is a read-only projection over project memory and never feeds the digest inputs.
Every selected item carries a stable provenance ref so a prompt can cite why the
context was included.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path

try:
    from tools import assumption_guard
    from tools.project_memory import ProjectMemory
except ModuleNotFoundError:  # flat installed layout
    import assumption_guard  # type: ignore
    from project_memory import ProjectMemory  # type: ignore

DEFAULT_BUDGET_TOKENS = 900
DEFAULT_MAX_ITEMS = 16
MAX_ITEM_CHARS = 320
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "into", "is", "it", "of", "on", "or", "that", "the", "to", "with",
    "add", "update", "fix", "make", "use", "using", "new", "this", "what",
    "bir", "bu", "da", "de", "için", "icin", "ile", "olan", "olarak",
    "ekle", "ekleyin", "yap", "yapın", "yapin", "güncelle", "guncelle",
    "düzelt", "duzelt", "ve", "veya", "the", "and",
}
FAILURE_WORDS = (
    "fail", "failed", "failure", "failing", "error", "errors", "broken",
    "regression", "red", "hata", "hatalı", "hatali", "başarısız",
    "basarisiz", "kırık", "kirik", "çöktü", "coktu",
)


def _is_failure_text(value) -> bool:
    folded = assumption_guard.normalize(str(value or ""))
    # "0 errors" / "no failures" is passing evidence, not a failed test.
    folded = re.sub(
        r"(?<![a-z0-9])(?:0|no|zero)\s+(?:errors?|failures?|failed)"
        r"(?![a-z0-9])", " ", folded)
    return any(re.search(r"(?<![a-z0-9])" + re.escape(
        assumption_guard.normalize(word)) + r"(?![a-z0-9])", folded)
        for word in FAILURE_WORDS)


def estimate_tokens(text) -> int:
    """Conservative character-based estimate; no tokenizer dependency."""
    if not isinstance(text, str) or not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def _tokens(value) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        result = set()
        for item in value:
            result |= _tokens(item)
        return result
    if value is None:
        return set()
    folded = assumption_guard.normalize(str(value))
    folded = re.sub(r"[_/\\.:-]+", " ", folded)
    return {word for word in re.findall(r"[a-z0-9]{3,}", folded)
            if word not in STOPWORDS}


def _recency(value, now) -> float:
    try:
        age = max(0.0, float(now) - float(value))
    except (TypeError, ValueError):
        return 0.0
    if age <= 86_400:
        return 1.0
    if age <= 7 * 86_400:
        return 0.7
    if age <= 30 * 86_400:
        return 0.4
    return 0.0


def _bounded(value, limit=MAX_ITEM_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _item(kind, ref, text, provenance, *, score, reason, mandatory=False):
    text = _bounded(text)
    return {
        "kind": kind,
        "ref": ref,
        "text": text,
        "provenance": provenance,
        "score": round(float(score), 3),
        "reason": reason,
        "mandatory": bool(mandatory),
        "tokens": estimate_tokens(text + " " + ref),
    }


def _observations(memory, *, now):
    rows = list(memory.conn.execute(
        "SELECT * FROM observations WHERE project_id=? "
        "ORDER BY created_at DESC LIMIT 30", (memory.project_id,)))
    result = []
    for row in rows:
        value = dict(row)
        try:
            files = json.loads(value.get("files") or "[]")
        except (TypeError, ValueError):
            files = []
        value["files"] = files if isinstance(files, list) else []
        result.append(value)
    return result


def _handoff_payload(handoff):
    payload = (handoff or {}).get("payload")
    return payload if isinstance(payload, dict) else {}


def _selection(source, request, *, budget_tokens, max_items, modules, now):
    if isinstance(source, (str, Path)):
        with ProjectMemory(Path(source)) as memory:
            return _selection(memory, request, budget_tokens=budget_tokens,
                              max_items=max_items, modules=modules, now=now)
    memory = source
    if not hasattr(memory, "conn"):
        raise ValueError("source must be a project root or ProjectMemory")
    request = request if isinstance(request, str) else ""
    request = request.strip()[:20000]
    module_values = modules or []
    if isinstance(module_values, str):
        module_values = [module_values]
    module_values = [str(value) for value in module_values if value]
    query_tokens = _tokens(request) | _tokens(module_values)
    detected_topics = set(assumption_guard.detect_topics(request)) \
        if request else set()
    now = float(now)

    items = []
    considered = {"decisions": 0, "blockers": 0, "failed_tests": 0,
                  "handoffs": 1, "recent_changes": 0, "contracts": 0}

    decisions = memory.list_decisions(status="confirmed", limit=200)
    for decision in decisions:
        considered["decisions"] += 1
        text = (f"{decision.get('topic')}: {decision.get('decision')}. "
                f"{decision.get('rationale') or ''}")
        item_tokens = _tokens(text) | _tokens(decision.get("affected_tasks"))
        topic = decision.get("topic")
        overlap = len(query_tokens & item_tokens)
        anchored = bool(topic and topic in detected_topics)
        if query_tokens and not overlap and not anchored:
            continue
        score = (2.6 * overlap + (2.0 if anchored else 0.0)
                 + 1.0 + _recency(decision.get("updated_at"), now))
        provenance = {
            "source": "decisions",
            "decision_id": decision.get("decision_id"),
            "topic": topic,
            "status": decision.get("status"),
            "source_ref": decision.get("source_ref"),
            "updated_at": decision.get("updated_at"),
        }
        items.append(_item(
            "decision", f"decision:{decision.get('decision_id')}", text,
            provenance, score=score,
            reason=("topic anchor" if anchored else "token overlap")
            + " + confirmed/recency"))

    blockers = memory.list_requirements(status="unresolved_blocking", limit=50)
    for blocker in blockers:
        considered["blockers"] += 1
        text = blocker.get("text") or ""
        item_tokens = _tokens(text)
        overlap = len(query_tokens & item_tokens)
        score = 8.0 + 2.0 * overlap + _recency(blocker.get("updated_at"), now)
        provenance = {
            "source": "requirements",
            "requirement_id": blocker.get("requirement_id"),
            "category": blocker.get("category"),
            "status": blocker.get("status"),
            "source_ref": blocker.get("source_ref"),
            "updated_at": blocker.get("updated_at"),
        }
        items.append(_item(
            "blocker", f"requirement:{blocker.get('requirement_id')}", text,
            provenance, score=score,
            reason="unresolved blocking requirement (always included)",
            mandatory=True))

    handoff = memory.latest_handoff()
    payload = _handoff_payload(handoff)
    if handoff is not None:
        changed = payload.get("changed_files") or []
        if not isinstance(changed, list):
            changed = []
        summary = " ".join(str(payload.get(key) or "") for key in (
            "context_summary", "next_milestone", "open_issues"))
        next_tasks = payload.get("next_tasks") or []
        task_text = " ".join(
            str(item.get("objective") or item.get("id") or "")
            for item in next_tasks if isinstance(item, dict))
        handoff_text = " ".join(part for part in (
            str(payload.get("context_summary") or ""),
            ("changed: " + ", ".join(str(path) for path in changed[:12])
             if changed else ""),
            ("next: " + task_text[:300] if task_text else "")) if part)
        item_tokens = (_tokens(summary) | _tokens(task_text)
                       | _tokens(changed))
        overlap = len(query_tokens & item_tokens)
        relevant = bool(overlap) or not query_tokens
        if relevant:
            score = 1.2 + 2.0 * overlap + _recency(
                handoff.get("created_at"), now)
            provenance = {
                "source": "project_handoffs",
                "handoff_id": handoff.get("handoff_id"),
                "run_id": handoff.get("run_id"),
                "milestone_id": handoff.get("milestone_id"),
                "version": handoff.get("version"),
                "verified": bool(memory.handoff_view(handoff).get("verified")),
                "created_at": handoff.get("created_at"),
            }
            items.append(_item(
                "handoff", f"handoff:{handoff.get('handoff_id')}",
                handoff_text, provenance, score=score,
                reason="request/module overlap with the latest handoff"))
            if changed:
                considered["recent_changes"] += 1
                change_text = "changed: " + ", ".join(
                    str(path) for path in changed[:16])
                if query_tokens:
                    change_relevant = (relevant or bool(
                        _tokens(changed) & query_tokens))
                else:
                    change_relevant = True
                if change_relevant:
                    items.append(_item(
                        "recent_change",
                        f"handoff:{handoff.get('handoff_id')}#changed_files",
                        change_text, dict(provenance, paths=changed[:16]),
                        score=1.8 + 2.0 * len(_tokens(changed) & query_tokens)
                        + _recency(handoff.get("created_at"), now),
                        reason="recent changed files overlap the request"))

    for observation in _observations(memory, now=now):
        text = (f"{observation.get('kind')}: "
                f"{observation.get('test_evidence') or ''}")
        is_failure = _is_failure_text(text)
        if is_failure:
            considered["failed_tests"] += 1
            item_tokens = _tokens(text) | _tokens(observation.get("files"))
            overlap = len(query_tokens & item_tokens)
            provenance = {
                "source": "observations",
                "observation_id": observation.get("observation_id"),
                "kind": observation.get("kind"),
                "commit_sha": observation.get("commit_sha"),
                "branch": observation.get("branch"),
                "created_at": observation.get("created_at"),
            }
            items.append(_item(
                "failed_test", f"observation:{observation.get('observation_id')}",
                text, provenance,
                score=6.0 + 2.0 * overlap
                + _recency(observation.get("created_at"), now),
                reason="failed test observation (safety evidence; always included)",
                mandatory=True))
        files = observation.get("files") or []
        if observation.get("kind") == "git_commit" and files:
            considered["recent_changes"] += 1
            if query_tokens:
                relevant = bool(_tokens(files) & query_tokens)
            else:
                relevant = True
            if relevant:
                provenance = {
                    "source": "observations",
                    "observation_id": observation.get("observation_id"),
                    "kind": observation.get("kind"),
                    "commit_sha": observation.get("commit_sha"),
                    "branch": observation.get("branch"),
                    "created_at": observation.get("created_at"),
                    "paths": files[:16],
                }
                items.append(_item(
                    "recent_change",
                    f"observation:{observation.get('observation_id')}#files",
                    "changed: " + ", ".join(str(path) for path in files[:16]),
                    provenance,
                    score=1.5 + 2.0 * len(_tokens(files) & query_tokens)
                    + _recency(observation.get("created_at"), now),
                    reason="recent commit files overlap the request"))

    contract = memory.contract_ref()
    if contract:
        considered["contracts"] += 1
        text = (f"requirements contract {contract.get('contract_id')} "
                f"v{contract.get('contract_version')} "
                f"sha256={contract.get('sha256')}")
        provenance = {
            "source": "contract",
            "contract_id": contract.get("contract_id"),
            "contract_version": contract.get("contract_version"),
            "sha256": contract.get("sha256"),
            "path": contract.get("path"),
            "updated_at": contract.get("updated_at"),
        }
        items.append(_item(
            "contract", f"contract:{contract.get('contract_id')}",
            text, provenance, score=3.0,
            reason="approved contract reference (always included)",
            mandatory=True))

    # Stable tie-breakers keep repeated calls deterministic.
    items.sort(key=lambda item: (not item["mandatory"], -item["score"],
                                 item["kind"], item["ref"]))
    budget = max(1, int(budget_tokens))
    limit = min(max(1, int(max_items)), 64)
    selected = []
    used = 0
    skipped = 0
    truncated = False
    for item in items:
        if len(selected) >= limit:
            skipped += 1
            continue
        cost = item["tokens"]
        if used + cost <= budget:
            selected.append(item)
            used += cost
            continue
        remaining = budget - used
        if item["mandatory"] and remaining > 0:
            base = estimate_tokens(" " + item["ref"])
            text_chars = max(0, (remaining - base) * 4)
            value = dict(item)
            value["text"] = item["text"][:text_chars]
            value["tokens"] = estimate_tokens(value["text"] + " " + value["ref"])
            if used + value["tokens"] <= budget:
                value["truncated"] = True
                selected.append(value)
                used += value["tokens"]
                truncated = True
                continue
        if item["mandatory"] and not selected:
            # A safety item is never silently dropped by a tiny custom budget;
            # its text is elided and the ref alone is retained.
            value = dict(item)
            value["text"] = ""
            value["tokens"] = estimate_tokens(" " + value["ref"])
            value["truncated"] = True
            selected.append(value)
            used += value["tokens"]
            truncated = True
            continue
        skipped += 1
        truncated = True

    counts = {}
    for item in selected:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    return {
        "request_hash": hashlib.sha256(request.encode("utf-8")).hexdigest(),
        "budget_tokens": budget,
        "token_estimate": used,
        "estimated_tokens": used,
        "max_items": limit,
        "items": selected,
        "refs": [item["ref"] for item in selected],
        "provenance": [{"ref": item["ref"], "kind": item["kind"],
                        **({"source": item["provenance"].get("source")}
                           if isinstance(item["provenance"], dict) else {})}
                       for item in selected],
        "counts": counts,
        "considered": considered,
        "skipped": skipped,
        "truncated": truncated,
        "module_scope": module_values[:20],
    }


def select_context(source, request=None, *, budget_tokens=DEFAULT_BUDGET_TOKENS,
                   max_items=DEFAULT_MAX_ITEMS, modules=None,
                   now=None) -> dict:
    """Select a bounded, scored context block with refs and provenance.

    ``source`` may be a repository root or an already-open ProjectMemory.
    """
    return _selection(source, request, budget_tokens=budget_tokens,
                      max_items=max_items, modules=modules,
                      now=now if now is not None else time.time())


def format_for_prompt(selection, *, max_chars=4000) -> str:
    """Render a selection as a compact runner prompt block."""
    if not isinstance(selection, dict):
        return ""
    lines = [
        "## Selected project context (bounded, provenance refs)",
        (f"token_estimate: {selection.get('token_estimate', 0)}/"
         f"{selection.get('budget_tokens', 0)}"),
    ]
    items = selection.get("items") or []
    if not items:
        lines.append("- none selected")
    for item in items:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"- [{item.get('kind')}] {item.get('text')} "
            f"(ref={item.get('ref')}; score={item.get('score')})")
    text = "\n".join(lines)
    return text[:int(max_chars)]


def runner_context(source, request, *, budget_tokens=600, modules=None,
                   max_chars=4000) -> str:
    """Prompt-ready helper for runners (runner code is intentionally untouched)."""
    selection = select_context(source, request, budget_tokens=budget_tokens,
                               modules=modules)
    return format_for_prompt(selection, max_chars=max_chars)


# Short aliases for callers that prefer an action-oriented name.
select = select_context
build_selection = select_context
format_selection = format_for_prompt
select_for_prompt = runner_context
select_for_runner = runner_context
build_runner_context = runner_context
