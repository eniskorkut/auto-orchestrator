#!/usr/bin/env python3
"""Optional, fail-closed proposal seam for Grill-Me.

The deterministic Assumption Guard remains authoritative: this module can only
enrich the ``why``/``rationale`` metadata of an already-selected question, or
record a rejected provider observation. It never writes decisions, never accepts
a recommendation as a user answer, and never changes a question's options,
recommended option, dependency or critical flag.

Providers are injected as a callable taking one prompt string, or as a local
command (string/list) reading the prompt from stdin. The module never supplies
network credentials; command providers receive a deliberately small environment.
It is disabled by default.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time

try:
    from tools import assumption_guard
except ModuleNotFoundError:  # flat installed layout
    import assumption_guard  # type: ignore

MAX_TOPICS = 6
MAX_OUTPUT_BYTES = 64 * 1024
MAX_EVIDENCE = 8
MAX_QUOTE = 500
MAX_WHY = 1200
MAX_REF = 500
MAX_OPTIONS = 20
DEFAULT_TIMEOUT = 5.0
ALLOWED_EVIDENCE_KINDS = ("request", "decision", "code")
TOPIC_FIELDS = frozenset({"topic", "why", "evidence", "options",
                          "recommended_option"})
EVIDENCE_FIELDS = frozenset({"kind", "ref", "quote"})
OPTION_FIELDS = frozenset({"id", "label", "recommended"})
VALID_ENV_FLAGS = {"1", "true", "yes", "on", "enabled"}

PROMPT_INSTRUCTION = """You are an optional evidence proposer for a deterministic
Grill-Me interview. Return ONLY strict JSON with this exact shape:
{"topics":[{"topic":"<guard topic>","why":"<short reason>","evidence":[{"kind":"request|decision|code","ref":"<request|decision id/topic|code path>","quote":"<exact substring of the referenced normalized evidence>"}],"options":[{"id":"<existing option id>","label":"<label>","recommended":true|false}],"recommended_option":"<existing option id>"}]}
Rules: at most 6 topics; only topics listed in allowed_topics; every option id
must already exist for that topic; exactly one recommended option; evidence
quotes must be exact normalized substrings of the request, a supplied decision,
or a supplied code evidence path. Do not invent decisions, options or evidence.
A recommendation is only a proposal; the user must confirm it. Never include
credentials, secrets or markdown fences.
"""


class GrillLLMError(ValueError):
    pass


def normalize(value) -> str:
    return assumption_guard.normalize(value)


def _bounded_text(value, field, *, limit, required=True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise GrillLLMError(f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise GrillLLMError(f"{field} is required")
    if len(value) > limit or "\x00" in value:
        raise GrillLLMError(f"{field} is invalid")
    return value


def _rule_options(topic: str) -> dict[str, dict]:
    rule = assumption_guard.RULES.get(topic)
    if rule is None:
        raise GrillLLMError("topic is not an Assumption Guard rule")
    return {option_id: {"label": label, "recommended": bool(recommended)}
            for option_id, label, recommended in rule["options"]}


def _decision_sources(decisions) -> dict[str, str]:
    sources: dict[str, list[str]] = {}
    for item in decisions or []:
        if not isinstance(item, dict):
            continue
        decision_id = item.get("decision_id")
        topic = item.get("topic")
        text = " ".join(str(item.get(key) or "") for key in
                        ("decision", "rationale", "source_ref", "topic"))
        if isinstance(decision_id, str) and decision_id:
            sources[decision_id] = sources.get(decision_id, []) + [text]
        if isinstance(topic, str) and topic:
            sources[topic] = sources.get(topic, []) + [text]
    return {key: " ".join(values) for key, values in sources.items()}


def _code_paths(facts) -> list[str]:
    facts = facts if isinstance(facts, dict) else {}
    found = facts.get("code_evidence") or facts.get("evidence") or {}
    paths = []
    if isinstance(found, dict):
        for values in found.values():
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, str) and value and value not in paths:
                    paths.append(value)
    return paths[:50]


def _strict_object(value, fields, label):
    if not isinstance(value, dict):
        raise GrillLLMError(f"{label} must be an object")
    unknown = set(value) - set(fields)
    if unknown:
        raise GrillLLMError(f"{label} has unknown fields")
    missing = set(fields) - set(value)
    if missing:
        raise GrillLLMError(f"{label} is missing required fields")


def _load_strict_json(text: str):
    if not isinstance(text, str):
        raise GrillLLMError("provider output must be text")
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise GrillLLMError("provider output contains duplicate keys")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=no_duplicates,
                          parse_constant=lambda value: (_ for _ in ()).throw(
                              GrillLLMError("provider output contains a "
                                            "non-finite number")))
    except GrillLLMError:
        raise
    except (TypeError, ValueError) as exc:
        raise GrillLLMError("provider output is not valid JSON") from exc


def validate_proposals(payload, *, request: str, decisions=None, facts=None,
                       allowed_topics=None) -> list[dict]:
    """Validate and normalize provider proposals; raise on any violation.

    Validation is intentionally all-or-nothing: one malformed topic rejects the
    whole provider output so a partial proposal can never be treated as
    authoritative.
    """
    _strict_object(payload, {"topics"}, "output")
    topics = payload.get("topics")
    if not isinstance(topics, list) or len(topics) > MAX_TOPICS:
        raise GrillLLMError("topics must be a bounded array")
    rules = assumption_guard.RULES
    if allowed_topics is None:
        allowed = set(rules)
    elif isinstance(allowed_topics, str):
        allowed = {allowed_topics}
    else:
        allowed = set(allowed_topics)
    request_text = normalize(request)
    decision_text = _decision_sources(decisions)
    code_paths = _code_paths(facts)
    seen_topics = set()
    result = []
    for index, item in enumerate(topics):
        label = f"topics[{index}]"
        _strict_object(item, TOPIC_FIELDS, label)
        topic = item.get("topic")
        if not isinstance(topic, str) or topic not in rules:
            raise GrillLLMError(f"{label}.topic is not a guard rule")
        if topic not in allowed:
            raise GrillLLMError(f"{label}.topic was not requested by the guard")
        if topic in seen_topics:
            raise GrillLLMError(f"{label}.topic is duplicated")
        seen_topics.add(topic)
        why = _bounded_text(item.get("why"), f"{label}.why", limit=MAX_WHY)
        evidence_raw = item.get("evidence")
        if (not isinstance(evidence_raw, list) or not evidence_raw
                or len(evidence_raw) > MAX_EVIDENCE):
            raise GrillLLMError(f"{label}.evidence must be a bounded non-empty array")
        evidence = []
        for evidence_index, evidence_item in enumerate(evidence_raw):
            evidence_label = f"{label}.evidence[{evidence_index}]"
            _strict_object(evidence_item, EVIDENCE_FIELDS, evidence_label)
            kind = evidence_item.get("kind")
            if kind not in ALLOWED_EVIDENCE_KINDS:
                raise GrillLLMError(f"{evidence_label}.kind is invalid")
            ref = _bounded_text(evidence_item.get("ref"), f"{evidence_label}.ref",
                                limit=MAX_REF)
            quote = _bounded_text(evidence_item.get("quote"),
                                  f"{evidence_label}.quote", limit=MAX_QUOTE)
            folded_quote = normalize(quote)
            if not folded_quote:
                raise GrillLLMError(f"{evidence_label}.quote is empty")
            if kind == "request":
                if folded_quote not in request_text:
                    raise GrillLLMError(
                        f"{evidence_label}.quote is not in the request")
            elif kind == "decision":
                source = decision_text.get(ref)
                if source is None:
                    raise GrillLLMError(
                        f"{evidence_label}.ref is not a supplied decision")
                if folded_quote not in normalize(source):
                    raise GrillLLMError(
                        f"{evidence_label}.quote is not in the decision")
            else:
                if ref == "repository scan":
                    if not code_paths or not any(
                            folded_quote in normalize(path)
                            for path in code_paths):
                        raise GrillLLMError(
                            f"{evidence_label}.quote is not in supplied "
                            f"code evidence")
                elif ref not in code_paths:
                    raise GrillLLMError(
                        f"{evidence_label}.ref is not supplied code evidence")
                elif folded_quote not in normalize(ref):
                    raise GrillLLMError(
                        f"{evidence_label}.quote is not in the code evidence path")
            evidence.append({"kind": kind, "ref": ref, "quote": quote})
        valid_options = _rule_options(topic)
        options_raw = item.get("options")
        if (not isinstance(options_raw, list) or not options_raw
                or len(options_raw) > MAX_OPTIONS):
            raise GrillLLMError(f"{label}.options must be a bounded non-empty array")
        options = []
        seen_options = set()
        for option_index, option in enumerate(options_raw):
            option_label = f"{label}.options[{option_index}]"
            _strict_object(option, OPTION_FIELDS, option_label)
            option_id = option.get("id")
            if option_id not in valid_options:
                raise GrillLLMError(f"{option_label}.id is not a valid option")
            if option_id in seen_options:
                raise GrillLLMError(f"{option_label}.id is duplicated")
            seen_options.add(option_id)
            text = _bounded_text(option.get("label"), f"{option_label}.label",
                                 limit=300)
            recommended = option.get("recommended")
            if not isinstance(recommended, bool):
                raise GrillLLMError(f"{option_label}.recommended must be boolean")
            options.append({"id": option_id, "label": text,
                            "recommended": recommended})
        recommended_option = item.get("recommended_option")
        if (not isinstance(recommended_option, str)
                or recommended_option not in seen_options
                or recommended_option not in valid_options):
            raise GrillLLMError(f"{label}.recommended_option is invalid")
        marked = [option["id"] for option in options if option["recommended"]]
        if marked != [recommended_option]:
            raise GrillLLMError(
                f"{label}.recommended_option must be the only recommendation")
        result.append({
            "topic": topic,
            "why": why,
            "evidence": evidence,
            "options": options,
            "recommended_option": recommended_option,
        })
    return result


class GrillLLM:
    """Fail-closed optional provider wrapper (disabled unless enabled)."""

    def __init__(self, provider=None, *, enabled: bool = False,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_output_bytes: int = MAX_OUTPUT_BYTES):
        if provider is not None and not callable(provider) and not isinstance(
                provider, (str, list, tuple)):
            raise GrillLLMError("provider must be a callable or local command")
        if timeout <= 0 or timeout > 120:
            raise GrillLLMError("timeout must be between 0 and 120 seconds")
        if max_output_bytes < 1024 or max_output_bytes > 1024 * 1024:
            raise GrillLLMError("max_output_bytes is invalid")
        self.provider = provider
        self.enabled = bool(enabled and provider is not None)
        self.timeout = float(timeout)
        self.max_output_bytes = int(max_output_bytes)

    @classmethod
    def from_env(cls, provider=None):
        """Build from AO_GRILL_LLM=1 plus an explicit injected command/callable."""
        command = provider
        if command is None:
            command = os.environ.get("AO_GRILL_LLM_COMMAND")
        enabled = os.environ.get("AO_GRILL_LLM", "").strip().lower() in VALID_ENV_FLAGS
        return cls(command, enabled=enabled)

    def _prompt(self, request, decisions, facts, allowed) -> str:
        payload = {
            "request": request[:8000],
            "allowed_topics": allowed,
            "decision_evidence": [
                {"decision_id": item.get("decision_id"),
                 "topic": item.get("topic"),
                 "decision": str(item.get("decision") or "")[:600],
                 "rationale": str(item.get("rationale") or "")[:600],
                 "status": item.get("status")}
                for item in (decisions or [])[:50] if isinstance(item, dict)
            ],
            "code_evidence": _code_paths(facts),
        }
        return PROMPT_INSTRUCTION + "\nInput:\n" + json.dumps(
            payload, ensure_ascii=False, sort_keys=True)

    def _invoke(self, prompt: str):
        provider = self.provider
        if callable(provider):
            outcome = {}

            def target():
                try:
                    outcome["value"] = provider(prompt)
                except BaseException as exc:  # noqa: BLE001 - fail closed
                    outcome["error"] = exc

            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            thread.join(self.timeout)
            if thread.is_alive():
                return None, "timeout", "provider timed out"
            if "error" in outcome:
                return None, "error", str(outcome["error"])[:300]
            return outcome.get("value"), None, None
        if isinstance(provider, str):
            command = shlex.split(provider)
        else:
            command = [str(part) for part in provider]
        if not command:
            return None, "error", "provider command is empty"
        env = {key: os.environ[key] for key in
               ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "SYSTEMROOT")
               if key in os.environ}
        try:
            completed = subprocess.run(
                command, input=prompt, capture_output=True, text=True,
                timeout=self.timeout, env=env)
        except subprocess.TimeoutExpired:
            return None, "timeout", "provider timed out"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return None, "error", str(exc)[:300]
        if completed.returncode != 0:
            return None, "error", (
                f"provider exit {completed.returncode}: "
                f"{completed.stderr.strip()[:300]}")
        return completed.stdout, None, None

    def propose(self, request, *, decisions=None, facts=None, topics=None) -> dict:
        started = time.monotonic()
        if not self.enabled:
            return {"status": "disabled", "topics": [], "rejected": [],
                    "provider": "none",
                    "reason": "optional LLM provider is not enabled",
                    "elapsed_ms": 0}
        if not isinstance(request, str) or not request.strip():
            return {"status": "error", "topics": [], "provider": "injected",
                    "rejected": [{"topic": None, "reason": "request is invalid"}],
                    "error": "invalid_request"}
        allowed = (list(assumption_guard.RULES) if topics is None
                   else ([topics] if isinstance(topics, str) else list(topics)))
        if (not allowed
                or any(topic not in assumption_guard.RULES for topic in allowed)):
            return {"status": "error", "topics": [], "provider": "injected",
                    "rejected": [{"topic": None,
                                  "reason": "allowed topics are invalid"}],
                    "error": "invalid_topics"}
        prompt = self._prompt(request, decisions, facts, allowed)
        raw, failure, detail = self._invoke(prompt)
        elapsed = int((time.monotonic() - started) * 1000)
        if failure is not None:
            return {"status": failure, "topics": [], "provider": "injected",
                    "rejected": [{"topic": None, "reason": detail or failure}],
                    "error": failure, "elapsed_ms": elapsed}
        if isinstance(raw, dict):
            try:
                encoded = json.dumps(raw, ensure_ascii=False)
            except (TypeError, ValueError):
                return {"status": "invalid", "topics": [],
                        "provider": "injected",
                        "rejected": [{"topic": None,
                                      "reason": "provider output is not JSON"}],
                        "error": "invalid_output", "elapsed_ms": elapsed}
            if len(encoded.encode("utf-8")) > self.max_output_bytes:
                return {"status": "invalid", "topics": [], "provider": "injected",
                        "rejected": [{"topic": None,
                                      "reason": "provider output is oversized"}],
                        "error": "oversized_output", "elapsed_ms": elapsed}
            output = raw
        elif not isinstance(raw, str):
            return {"status": "invalid", "topics": [], "provider": "injected",
                    "rejected": [{"topic": None,
                                  "reason": "provider output is not text"}],
                    "error": "invalid_output", "elapsed_ms": elapsed}
        else:
            if len(raw.encode("utf-8")) > self.max_output_bytes:
                return {"status": "invalid", "topics": [], "provider": "injected",
                        "rejected": [{"topic": None,
                                      "reason": "provider output is oversized"}],
                        "error": "oversized_output", "elapsed_ms": elapsed}
            try:
                output = _load_strict_json(raw)
            except GrillLLMError as exc:
                return {"status": "invalid", "topics": [], "provider": "injected",
                        "rejected": [{"topic": None, "reason": str(exc)[:300]}],
                        "error": "invalid_json", "elapsed_ms": elapsed}
        try:
            proposals = validate_proposals(
                output, request=request, decisions=decisions, facts=facts,
                allowed_topics=allowed)
        except GrillLLMError as exc:
            return {"status": "invalid", "topics": [], "provider": "injected",
                    "rejected": [{"topic": None, "reason": str(exc)[:300]}],
                    "error": "schema_rejected", "elapsed_ms": elapsed}
        return {"status": "ok", "topics": proposals, "rejected": [],
                "provider": "injected", "elapsed_ms": elapsed}


def enrich_questions(questions, proposals) -> list[dict]:
    """Return questions with only why/rationale enriched; never authority."""
    by_topic = {}
    for proposal in proposals or []:
        if isinstance(proposal, dict) and isinstance(proposal.get("topic"), str):
            by_topic[proposal["topic"]] = proposal
    enriched = []
    for question in questions:
        value = dict(question)
        proposal = by_topic.get(value.get("key"))
        if proposal is not None:
            for field in ("why", "rationale"):
                proposed = proposal.get(field)
                if not isinstance(proposed, str) or not proposed.strip():
                    continue
                existing = str(value.get(field) or "").strip()
                if proposed.strip() in existing:
                    continue
                value[field] = ((existing + " Proposal context: " +
                                 proposed.strip()) if existing
                                else proposed.strip())
        enriched.append(value)
    return enriched


def record_rejected(memory, result) -> dict | None:
    """Record bounded rejection provenance as an observation, never a decision."""
    rejected = result.get("rejected") if isinstance(result, dict) else None
    if not rejected:
        rejected = [{"topic": None, "reason": str(
            (result or {}).get("error") or (result or {}).get("status")
            or "provider rejected")}]
    refs = []
    details = []
    for item in rejected[:MAX_EVIDENCE]:
        if not isinstance(item, dict):
            continue
        topic = item.get("topic")
        reason = str(item.get("reason") or "rejected")[:300]
        if isinstance(topic, str) and topic:
            refs.append(f"topic:{topic[:80]}")
        details.append({"topic": topic if isinstance(topic, str) else None,
                        "reason": reason})
    if not details:
        details = [{"topic": None, "reason": "provider rejected"}]
    return memory.record_observation(
        kind="grill_llm_rejected", files=refs[:MAX_EVIDENCE],
        test_evidence=json.dumps(details, ensure_ascii=False,
                                 sort_keys=True)[:2000])


# Compatibility aliases for callers that describe validation as parsing.
validate_output = validate_proposals
parse_proposals = validate_proposals
