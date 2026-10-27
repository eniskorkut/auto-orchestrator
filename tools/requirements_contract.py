#!/usr/bin/env python3
"""Versioned Verified Requirements Contract.

A contract turns approved decisions into an immutable, digest-bound artifact
that the planner, runner and Quality Gate can verify. Files live under
``.auto-orchestrator/contracts`` and contain no raw prompts, secrets or machine
paths. The contract is a projection over the durable Decision/Requirements
Ledger; the ledger remains the source of truth.

Only executable code/test evidence can mark an acceptance criterion as passed.
Anything else is reported as ``not_verified`` rather than silently accepted.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import time
from pathlib import Path

try:
    from tools.project_identity import ensure_project_identity
    from tools.project_memory import ProjectMemory
    from tools.repo_facts import scan_repository
    from tools.run_owner import atomic_write_json, read_json
except ModuleNotFoundError:  # flat installed layout
    from project_identity import ensure_project_identity  # type: ignore
    from project_memory import ProjectMemory  # type: ignore
    from repo_facts import scan_repository  # type: ignore
    from run_owner import atomic_write_json, read_json  # type: ignore

CONTRACT_ID = re.compile(r"^rc_[0-9a-f]{16}$")
PROJECT_ID = re.compile(r"^p_[0-9a-f]{16}$")
INTERVIEW_ID = re.compile(r"^iv_[0-9a-f]{16}$")
DECISION_ID = re.compile(r"^d_[0-9a-f]{16}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CONTRACT_VERSION = 1
MAX_CONTRACT_VERSION = 10000
MAX_CONTRACT_BYTES = 131072
MAX_POINTER_BYTES = 8192
MAX_ITEMS = 80
MAX_TEXT = 1200
PHASES = ("build", "verify", "review", "audit")
STATUSES = ("confirmed", "invalidated", "superseded")
SOURCE_KINDS = ("grill-me", "user", "plan", "import")
# Metadata changes do not alter the contract body or its digest.
METADATA_FIELDS = frozenset({
    "sha256", "created_at", "approved_at", "status", "supersedes",
    "superseded_by", "path",
})
CODE_TOPICS = {
    "auth_session": {
        "JWT": "jwt", "jwt": "jwt",
        "session cookie": "session", "cookie": "session",
    },
    "database": {
        "PostgreSQL": "postgres", "postgres": "postgres",
        "SQLite": "sqlite", "sqlite": "sqlite",
        "MySQL": "mysql", "mysql": "mysql",
    },
    "file_storage": {"S3": "s3", "s3": "s3"},
}


class ContractError(ValueError):
    pass


def _text(value, field, *, required=True, limit=MAX_TEXT):
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ContractError(f"{field} is required")
    if len(value) > limit or "\x00" in value:
        raise ContractError(f"{field} is invalid")
    return value


def _timestamp(value, field, default):
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ContractError(f"{field} is invalid")
    return number


def _items(value, field, *, limit=MAX_ITEMS, text_limit=MAX_TEXT):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise ContractError(f"{field} must be a bounded array")
    result = []
    for item in value:
        text = _text(item, field, limit=text_limit)
        if text not in result:
            result.append(text)
    return result


def _phase_list(value):
    if not isinstance(value, list) or not value:
        return list(PHASES)
    result = []
    for item in value:
        if item not in PHASES:
            raise ContractError("criterion applies_to contains an unknown phase")
        if item not in result:
            result.append(item)
    return result


def canonical_digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def contract_body(contract: dict) -> dict:
    return {key: value for key, value in contract.items()
            if key not in METADATA_FIELDS}


def contract_digest(contract: dict) -> str:
    return canonical_digest(contract_body(contract))


def compact_contract_ref(contract: dict) -> dict:
    return {
        "contract_id": contract["contract_id"],
        "contract_version": contract["contract_version"],
        "sha256": contract["sha256"],
    }


def _validate_criterion(item, decisions: set[str]) -> dict:
    if not isinstance(item, dict):
        raise ContractError("acceptance criterion must be an object")
    criterion_id = item.get("id")
    if not isinstance(criterion_id, str) or not re.fullmatch(
            r"^ac_[a-z0-9_]{1,40}$", criterion_id):
        raise ContractError("criterion id is invalid")
    text = _text(item.get("text"), "criterion text")
    applies_to = _phase_list(item.get("applies_to", []))
    decision_ref = item.get("decision_ref")
    if decision_ref is not None and decision_ref not in decisions:
        raise ContractError("criterion references an unknown decision")
    evidence = item.get("evidence") or {"kind": "not_automated"}
    if not isinstance(evidence, dict):
        raise ContractError("criterion evidence must be an object")
    kind = evidence.get("kind")
    if kind not in ("code_evidence", "command", "not_automated"):
        raise ContractError("unsupported criterion evidence kind")
    normalized = {"id": criterion_id, "text": text,
                  "applies_to": applies_to,
                  "decision_ref": decision_ref,
                  "evidence": {"kind": kind}}
    if kind == "code_evidence":
        topic = evidence.get("topic")
        expected = evidence.get("expected")
        if topic not in CODE_TOPICS:
            raise ContractError("unsupported code evidence topic")
        if (expected not in CODE_TOPICS[topic]
                and expected not in CODE_TOPICS[topic].values()):
            raise ContractError("unsupported code evidence expectation")
        normalized["evidence"].update({"topic": topic, "expected": expected})
    elif kind == "command":
        command = _text(evidence.get("command"), "criterion command", limit=500)
        if any(char in command for char in (";", "|", "&", "$", "`", "\n", "\r")):
            raise ContractError("criterion command contains shell metacharacters")
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ContractError("criterion command is not parseable") from exc
        if not _allowed_command(argv):
            raise ContractError("criterion command is not an allowed test command")
        normalized["evidence"]["command"] = command
    return normalized


def _allowed_command(argv: list[str]) -> bool:
    """Only test runners, never arbitrary interpreters or package fetch."""
    if not argv:
        return False
    executable = argv[0]
    if executable in ("python", "python3", "python3.11", "python3.12"):
        return (len(argv) >= 3 and argv[1] == "-m"
                and argv[2] in ("unittest", "pytest"))
    if executable == "pytest":
        return True
    if executable == "node":
        return len(argv) >= 2 and argv[1] == "--test"
    if executable == "npm":
        return (len(argv) >= 2 and argv[1] == "test") or (
            len(argv) >= 3 and argv[1] == "run" and argv[2] in ("test", "check"))
    if executable == "go":
        return len(argv) >= 2 and argv[1] == "test"
    if executable == "cargo":
        return len(argv) >= 2 and argv[1] == "test"
    if executable == "make":
        return len(argv) >= 2 and argv[1] in ("test", "check")
    return False


def _validate_version_ref(value, field):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ContractError(f"{field} is invalid")
    contract_id = value.get("contract_id")
    version = value.get("contract_version")
    if not isinstance(contract_id, str) or not CONTRACT_ID.fullmatch(contract_id):
        raise ContractError(f"{field} is invalid")
    if type(version) is not int or not 1 <= version <= MAX_CONTRACT_VERSION:
        raise ContractError(f"{field} is invalid")
    return {"contract_id": contract_id, "contract_version": version}


def validate_contract(contract: dict) -> dict:
    if not isinstance(contract, dict):
        raise ContractError("contract must be an object")
    contract_id = contract.get("contract_id")
    if not isinstance(contract_id, str) or not CONTRACT_ID.fullmatch(contract_id):
        raise ContractError("contract id is invalid")
    version = contract.get("contract_version")
    if type(version) is not int or not 1 <= version <= MAX_CONTRACT_VERSION:
        raise ContractError("contract_version is invalid")
    status = contract.get("status", "confirmed")
    if status not in STATUSES:
        raise ContractError("contract status is invalid")
    project_id = contract.get("project_id")
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
        raise ContractError("project id is invalid")
    interview_id = contract.get("interview_id")
    if interview_id is not None and (not isinstance(interview_id, str)
                                     or not INTERVIEW_ID.fullmatch(interview_id)):
        raise ContractError("interview id is invalid")
    goal = _text(contract.get("goal"), "goal")
    source = contract.get("source")
    if not isinstance(source, dict) or source.get("kind") not in SOURCE_KINDS:
        raise ContractError("contract source is invalid")
    source_ref = _text(source.get("ref"), "contract source ref", limit=200)
    raw_decisions = contract.get("confirmed_decisions", [])
    if not isinstance(raw_decisions, list) or len(raw_decisions) > MAX_ITEMS:
        raise ContractError("confirmed_decisions must be a bounded array")
    decisions = []
    decision_ids: set[str] = set()
    for item in raw_decisions:
        if not isinstance(item, dict):
            raise ContractError("decision entry must be an object")
        decision_id = item.get("decision_ref")
        if not isinstance(decision_id, str) or not DECISION_ID.fullmatch(decision_id):
            raise ContractError("decision_ref is invalid")
        if decision_id in decision_ids:
            continue
        decision_ids.add(decision_id)
        decisions.append({
            "decision_ref": decision_id,
            "topic": _text(item.get("topic"), "decision topic", limit=300),
            "decision": _text(item.get("decision"), "decision"),
            "rationale": _text(item.get("rationale", ""), "rationale",
                               required=False),
            "confirmed_by": _text(item.get("confirmed_by", "user"),
                                  "confirmed_by", required=False, limit=80),
        })
    raw_criteria = contract.get("acceptance_criteria", [])
    if not isinstance(raw_criteria, list) or len(raw_criteria) > MAX_ITEMS:
        raise ContractError("acceptance_criteria must be a bounded array")
    criteria = [_validate_criterion(item, decision_ids) for item in raw_criteria]
    if not criteria:
        raise ContractError("contract requires acceptance criteria")
    raw_blockers = contract.get("blockers", [])
    if not isinstance(raw_blockers, list) or len(raw_blockers) > MAX_ITEMS:
        raise ContractError("blockers must be a bounded array")
    blockers = []
    for item in raw_blockers:
        if not isinstance(item, dict):
            raise ContractError("blocker must be an object")
        blocking = item.get("blocking", True)
        if not isinstance(blocking, bool):
            raise ContractError("blocker blocking flag must be a boolean")
        blockers.append({
            "id": _text(item.get("id"), "blocker id", limit=80),
            "text": _text(item.get("text"), "blocker text"),
            "blocking": blocking,
        })
    strategy = contract.get("verification_strategy") or {}
    if not isinstance(strategy, dict):
        raise ContractError("verification_strategy must be an object")
    normalized = {
        "contract_id": contract_id,
        "contract_version": version,
        "status": status,
        "project_id": project_id,
        "interview_id": interview_id,
        "goal": goal,
        "source": {"kind": source["kind"], "ref": source_ref},
        "confirmed_decisions": decisions,
        "constraints": _items(contract.get("constraints"), "constraints"),
        "out_of_scope": _items(contract.get("out_of_scope"), "out_of_scope"),
        "acceptance_criteria": criteria,
        "verification_strategy": {
            "commands": _items(strategy.get("commands"), "verification commands"),
            "required_roles": _items(strategy.get("required_roles"),
                                     "required roles", limit=10, text_limit=40),
        },
        "blockers": blockers,
        "tasks": _items(contract.get("tasks"), "tasks", limit=100, text_limit=100),
        "supersedes": _validate_version_ref(contract.get("supersedes"), "supersedes"),
        "superseded_by": _validate_version_ref(contract.get("superseded_by"),
                                               "superseded_by"),
        "created_at": _timestamp(contract.get("created_at"), "created_at", 0.0),
        "approved_at": _timestamp(contract.get("approved_at"), "approved_at", 0.0),
    }
    normalized["sha256"] = contract_digest(normalized)
    expected = contract.get("sha256")
    if expected is not None and expected != normalized["sha256"]:
        raise ContractError("contract digest does not match its content")
    for field in ("supersedes", "superseded_by"):
        reference = normalized[field]
        if reference is not None and reference["contract_id"] != contract_id:
            raise ContractError(f"{field} must reference the same contract id")
    encoded = json.dumps(normalized, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_CONTRACT_BYTES:
        raise ContractError("contract is too large")
    return normalized


def validate_for_plan(contract: dict, *, project_id: str | None = None) -> dict:
    """Validate a contract that is about to constrain a plan.

    A plan may only consume a confirmed contract without unresolved blockers,
    and (when a plan project is known) the contract must belong to that exact
    project.
    """
    normalized = validate_contract(contract)
    if normalized["status"] != "confirmed":
        raise ContractError(
            f"requirements contract is {normalized['status']}, not confirmed")
    if normalized["blockers"]:
        raise ContractError("requirements contract has unresolved blockers")
    if project_id is not None and normalized["project_id"] != project_id:
        raise ContractError("requirements contract belongs to another project")
    return normalized


def contracts_dir(root) -> Path:
    return Path(root).resolve() / ".auto-orchestrator" / "contracts"


def _contract_versions(root, contract_id: str) -> list[int]:
    if not isinstance(contract_id, str) or not CONTRACT_ID.fullmatch(contract_id):
        raise ContractError("contract id is invalid")
    directory = contracts_dir(root)
    if directory.is_symlink():
        raise ContractError("contract directory must not be a symlink")
    if not directory.is_dir():
        return []
    pattern = re.compile(rf"^{re.escape(contract_id)}-v([0-9]+)\.json$")
    versions = set()
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        version = int(match.group(1))
        if 1 <= version <= MAX_CONTRACT_VERSION:
            versions.add(version)
    return sorted(versions)


def next_contract_version(root, contract_id: str) -> int:
    """Return the next version for ``contract_id`` by scanning stored files."""
    versions = _contract_versions(root, contract_id)
    return versions[-1] + 1 if versions else 1


def latest_contract(root, contract_id: str) -> dict | None:
    """Return the highest stored version for ``contract_id``."""
    versions = _contract_versions(root, contract_id)
    if not versions:
        return None
    return load_contract(root, contract_id, versions[-1])


def _contract_path(root, contract: dict) -> Path:
    return contracts_dir(root) / (
        f"{contract['contract_id']}-v{contract['contract_version']}.json")


def _safe_write(root, contract: dict) -> Path:
    directory = contracts_dir(root)
    if directory.is_symlink():
        raise ContractError("contract directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ContractError("contract directory must not be a symlink")
    path = _contract_path(root, contract)
    if path.is_symlink():
        raise ContractError("contract file must not be a symlink")
    text = json.dumps(contract, indent=2, ensure_ascii=False) + "\n"
    if len(text.encode("utf-8")) > MAX_CONTRACT_BYTES:
        raise ContractError("contract is too large")
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    if temporary.is_symlink():
        raise ContractError("contract temporary file must not be a symlink")
    try:
        temporary.write_text(text, encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def _write_pointer(root, contract: dict, path: Path) -> None:
    pointer_path = contracts_dir(root) / "current.json"
    if pointer_path.is_symlink():
        raise ContractError("contract pointer must not be a symlink")
    atomic_write_json(pointer_path, {
        "contract_id": contract["contract_id"],
        "contract_version": contract["contract_version"],
        "sha256": contract["sha256"],
        "path": path.name,
        "updated_at": time.time(),
    })


def save_contract(root, contract: dict) -> dict:
    """Persist a contract and supersede only its own previous version.

    Versions are allocated per ``contract_id`` by scanning stored files. A
    contract from another interview/project never supersedes the previous one.
    """
    if isinstance(contract, dict):
        raw = dict(contract)
        now = time.time()
        if raw.get("created_at") is None:
            raw["created_at"] = now
        if raw.get("approved_at") is None:
            raw["approved_at"] = raw["created_at"]
        contract = raw
    normalized = validate_contract(contract)
    if normalized["status"] != "confirmed":
        raise ContractError("only confirmed contracts can be published")
    if normalized["blockers"]:
        raise ContractError("contract has unresolved blockers")
    contract_id = normalized["contract_id"]
    versions = _contract_versions(root, contract_id)
    highest = versions[-1] if versions else 0
    version = normalized["contract_version"]
    if version <= highest:
        existing = load_contract(root, contract_id, version)
        if (existing["sha256"] == normalized["sha256"]
                and existing["status"] == "confirmed"):
            path = _contract_path(root, normalized)
            _write_pointer(root, normalized, path)
            return {**existing, "path": str(path)}
        raise ContractError("contract version must increase")
    expected = highest + 1
    if version != expected:
        raise ContractError(f"contract version must be {expected}")
    if highest:
        previous = load_contract(root, contract_id, highest)
        if previous["status"] != "superseded":
            previous["status"] = "superseded"
            previous["superseded_by"] = {
                "contract_id": contract_id,
                "contract_version": version,
            }
            _safe_write(root, previous)
    path = _safe_write(root, normalized)
    _write_pointer(root, normalized, path)
    return {**normalized, "path": str(path)}


def _read_contract_file(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ContractError("contract file not found")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ContractError("contract file not found") from exc
    if size > MAX_CONTRACT_BYTES:
        raise ContractError("contract file is too large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError("contract file is invalid") from exc
    return validate_contract(value)


def load_contract(root, contract_id: str, version: int) -> dict:
    if not isinstance(contract_id, str) or not CONTRACT_ID.fullmatch(contract_id):
        raise ContractError("contract id is invalid")
    if type(version) is not int or not 1 <= version <= MAX_CONTRACT_VERSION:
        raise ContractError("contract version is invalid")
    directory = contracts_dir(root)
    if directory.is_symlink():
        raise ContractError("contract directory must not be a symlink")
    return _read_contract_file(directory / f"{contract_id}-v{version}.json")


def load_current(root) -> dict | None:
    pointer_path = contracts_dir(root) / "current.json"
    if pointer_path.is_symlink():
        raise ContractError("contract pointer must not be a symlink")
    if not pointer_path.is_file():
        return None
    try:
        if pointer_path.stat().st_size > MAX_POINTER_BYTES:
            raise ContractError("contract pointer is invalid")
    except OSError as exc:
        raise ContractError("contract pointer is invalid") from exc
    pointer = read_json(pointer_path, None)
    if not isinstance(pointer, dict):
        raise ContractError("contract pointer is invalid")
    contract_id = pointer.get("contract_id")
    version = pointer.get("contract_version")
    sha256 = pointer.get("sha256")
    name = pointer.get("path")
    if not isinstance(contract_id, str) or not CONTRACT_ID.fullmatch(contract_id):
        raise ContractError("contract pointer is invalid")
    if type(version) is not int or not 1 <= version <= MAX_CONTRACT_VERSION:
        raise ContractError("contract pointer is invalid")
    if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
        raise ContractError("contract pointer is invalid")
    expected_name = f"{contract_id}-v{version}.json"
    if name != expected_name:
        raise ContractError("contract pointer is invalid")
    contract = load_contract(root, contract_id, version)
    if contract["sha256"] != sha256:
        raise ContractError("contract pointer digest does not match")
    return contract


def _decision_rows(memory: ProjectMemory, interview_id: str) -> list[dict]:
    rows = []
    for item in memory.list_decisions(limit=500):
        ref = item.get("source_ref")
        if isinstance(ref, str) and ref.startswith("question:"):
            question_id = ref.split(":", 1)[1]
            row = memory.conn.execute(
                "SELECT interview_id FROM interview_questions WHERE question_id=?",
                (question_id,)).fetchone()
            if row is not None and row["interview_id"] == interview_id:
                value = dict(item)
                value.setdefault("decision_ref", item["decision_id"])
                rows.append(value)
    return rows


def _expected_code(topic: str, decision: str) -> str | None:
    if topic not in CODE_TOPICS:
        return None
    lower = decision.casefold()
    for label, key in CODE_TOPICS[topic].items():
        if label.casefold() in lower:
            return key
    return None


def _code_criterion(decision: dict, index: int) -> dict:
    topic = decision["topic"]
    expected = _expected_code(topic, decision["decision"])
    criterion_id = f"ac_{index:02d}_{topic}"[:44]
    text = f"Decision '{topic}' is implemented as '{decision['decision']}'"
    decision_ref = (decision.get("decision_ref")
                    or decision.get("decision_id"))
    if expected is None:
        return {
            "id": criterion_id, "text": text,
            "applies_to": ["verify", "review"],
            "decision_ref": decision_ref,
            "evidence": {"kind": "not_automated"},
        }
    return {
        "id": criterion_id, "text": text,
        "applies_to": ["build", "verify", "review"],
        "decision_ref": decision_ref,
        "evidence": {"kind": "code_evidence", "topic": topic,
                     "expected": expected},
    }


def build_from_interview(root, interview_id: str, *, version: int | None = None) -> dict:
    """Build a confirmed contract from an approved persistent interview."""
    root = Path(root).resolve()
    if not isinstance(interview_id, str) or not INTERVIEW_ID.fullmatch(interview_id):
        raise ContractError("interview id is invalid")
    identity = ensure_project_identity(root)
    with ProjectMemory(root, identity=identity) as memory:
        interview = memory.conn.execute(
            "SELECT * FROM interviews WHERE project_id=? AND interview_id=?",
            (identity["project_id"], interview_id)).fetchone()
        if interview is None:
            raise ContractError("interview does not exist")
        if interview["status"] != "approved":
            raise ContractError("interview is not approved")
        questions = [dict(row) for row in memory.conn.execute(
            "SELECT * FROM interview_questions WHERE interview_id=? "
            "ORDER BY ordinal", (interview_id,))]
        open_blockers = []
        for question in questions:
            if question["status"] in ("open", "skipped", "invalidated"):
                open_blockers.append({
                    "id": str(question["question_id"]),
                    "text": str(question["question"]),
                    "blocking": True,
                })
        requirements = memory.list_requirements(limit=500)
        for item in requirements:
            if item["status"] == "unresolved_blocking":
                open_blockers.append({
                    "id": str(item["requirement_id"]),
                    "text": str(item["text"]), "blocking": True,
                })
        decisions = _decision_rows(memory, interview_id)
        confirmed = [item for item in decisions if item["status"] == "confirmed"]
        if open_blockers:
            raise ContractError("interview still has blocking questions")
        constraints = [
            f"{item['topic']}: {item['decision']}" for item in confirmed
        ]
        question_refs = {f"question:{item['question_id']}" for item in questions}
        for item in requirements:
            if item["status"] not in ("confirmed", "doc_constraint"):
                continue
            ref = item.get("source_ref")
            # Only requirements owned by this interview (or global doc
            # constraints) belong to this contract.
            if (ref not in question_refs
                    and ref != f"interview:{interview_id}"
                    and item["status"] != "doc_constraint"):
                continue
            if item["text"] not in constraints:
                constraints.append(item["text"])
        out_of_scope = []
        for question in questions:
            if question["question_key"] == "scope" and question["status"] == "answered":
                answer = question.get("answer_text") or ""
                if answer and answer.strip().lower() not in (
                        "none", "ek kapsam dışı madde yok"):
                    out_of_scope.append(answer)
        criteria = [_code_criterion(item, index + 1)
                    for index, item in enumerate(confirmed)][:MAX_ITEMS - 1]
        criteria.append({
            "id": "ac_99_coverage",
            "text": "All approved decisions are covered by test or review evidence",
            "applies_to": ["verify", "review", "audit"],
            "decision_ref": None,
            "evidence": {"kind": "not_automated"},
        })
        contract_id = "rc_" + hashlib.sha256(
            str(interview_id).encode()).hexdigest()[:16]
        latest = latest_contract(root, contract_id)
        if latest is not None and latest.get("interview_id") != interview_id:
            raise ContractError("contract id collides with another interview")
        next_version = (version if version is not None
                        else next_contract_version(root, contract_id))
        contract = {
            "contract_id": contract_id,
            "contract_version": next_version,
            "status": "confirmed",
            "project_id": identity["project_id"],
            "interview_id": interview_id,
            "goal": str(interview["request_summary"])[:MAX_TEXT],
            "source": {"kind": "grill-me", "ref": interview_id},
            "confirmed_decisions": [{
                "decision_ref": item["decision_id"],
                "topic": item["topic"], "decision": item["decision"],
                "rationale": item.get("rationale") or "",
                "confirmed_by": item.get("confirmed_by") or "user",
            } for item in confirmed],
            "constraints": constraints,
            "out_of_scope": out_of_scope,
            "acceptance_criteria": criteria,
            "verification_strategy": {
                "commands": [],
                "required_roles": ["tester", "reviewer"],
            },
            "blockers": [],
            "tasks": [],
            "supersedes": (
                {"contract_id": latest["contract_id"],
                 "contract_version": latest["contract_version"]}
                if latest is not None else None),
            "created_at": time.time(),
            "approved_at": time.time(),
        }
        return save_contract(root, contract)


def validate_contract_state(root, contract: dict) -> dict:
    """Validate a contract against the live ledger before planning/execution."""
    root = Path(root).resolve()
    normalized = validate_contract(contract)
    if normalized["status"] != "confirmed":
        raise ContractError("requirements contract is not confirmed")
    if normalized["blockers"]:
        raise ContractError("requirements contract has unresolved blockers")
    identity = ensure_project_identity(root)
    if normalized["project_id"] != identity["project_id"]:
        raise ContractError("requirements contract belongs to another project")
    with ProjectMemory(root, identity=identity) as memory:
        if normalized.get("interview_id"):
            interview = memory.conn.execute(
                "SELECT status FROM interviews WHERE project_id=? AND "
                "interview_id=?",
                (memory.project_id, normalized["interview_id"])).fetchone()
            if interview is None or interview["status"] != "approved":
                raise ContractError(
                    "requirements contract interview is not approved")
        decisions = {item["decision_id"]: item for item in
                     memory.list_decisions(limit=500)}
        for item in normalized["confirmed_decisions"]:
            live = decisions.get(item["decision_ref"])
            if live is None or live["status"] != "confirmed":
                raise ContractError(
                    "requirements contract references a superseded decision")
            if live["decision"] != item["decision"]:
                raise ContractError("requirements contract decision changed")
        if memory.list_requirements(status="unresolved_blocking", limit=1):
            raise ContractError(
                "requirements contract has unresolved blocking requirements")
    return normalized


def verify_contract(root, contract: dict, *, test_results=None) -> dict:
    """Evaluate executable criteria against code and real test exit codes."""
    root = Path(root).resolve()
    normalized = validate_contract(contract)
    if normalized["status"] != "confirmed":
        return {"ok": False, "status": "invalidated",
                "checks": [], "reasons": ["contract is not confirmed"]}
    if normalized["blockers"]:
        return {"ok": False, "status": "invalidated", "checks": [],
                "reasons": ["contract has unresolved blockers"]}
    identity = ensure_project_identity(root)
    if normalized["project_id"] != identity["project_id"]:
        return {"ok": False, "status": "invalidated",
                "checks": [], "reasons": ["contract belongs to another project"]}
    with ProjectMemory(root, identity=identity) as memory:
        current = {item["decision_id"]: item for item in memory.list_decisions(limit=500)}
        blocking = memory.list_requirements(status="unresolved_blocking")
    reasons = []
    for decision in normalized["confirmed_decisions"]:
        live = current.get(decision["decision_ref"])
        if live is None:
            reasons.append(f"decision {decision['decision_ref']} is missing")
        elif live["status"] != "confirmed":
            reasons.append(
                f"decision {decision['decision_ref']} is {live['status']}")
        elif live["decision"] != decision["decision"]:
            reasons.append(
                f"decision {decision['decision_ref']} changed")
    if blocking:
        reasons.append(f"{len(blocking)} unresolved blocking requirement(s)")
    facts = scan_repository(root)
    code = facts.get("detected") or {}
    results = list(test_results or [])

    def command_status(command):
        matches = [item for item in results
                   if isinstance(item, dict)
                   and (item.get("command") == command
                        or item.get("name") == command[:200])]
        if not matches:
            return "not_verified", "no matching test result"
        if any(item.get("exit_code") == 0 for item in matches):
            return "passed", "matching test exited 0"
        return "failed", "matching test did not exit 0"

    checks = []
    for criterion in normalized["acceptance_criteria"]:
        evidence = criterion["evidence"]
        kind = evidence["kind"]
        if kind == "code_evidence":
            present = bool(code.get(evidence["expected"]))
            checks.append({
                "criterion_id": criterion["id"], "kind": kind,
                "status": "passed" if present else "failed",
                "evidence": f"code evidence: {evidence['expected']}={present}",
            })
        elif kind == "command":
            status, detail = command_status(evidence["command"])
            checks.append({"criterion_id": criterion["id"], "kind": kind,
                           "status": status, "evidence": detail})
        else:
            checks.append({"criterion_id": criterion["id"], "kind": kind,
                           "status": "not_verified",
                           "evidence": "no automated verification available"})
    failed = any(item["status"] == "failed" for item in checks)
    not_verified = sum(1 for item in checks if item["status"] == "not_verified")
    passed = sum(1 for item in checks if item["status"] == "passed")
    if reasons or failed:
        status = "failed"
    elif not_verified:
        status = "partially_verified"
    else:
        status = "verified"
    return {
        "ok": not reasons and not failed,
        "status": status,
        "contract_id": normalized["contract_id"],
        "contract_version": normalized["contract_version"],
        "sha256": normalized["sha256"],
        "checks": checks,
        "passed": passed,
        "not_verified": not_verified,
        "reasons": reasons,
    }


def current_contract_ref(root) -> dict | None:
    current = load_current(root)
    if current is None:
        return None
    return {**compact_contract_ref(current), "status": current["status"]}
