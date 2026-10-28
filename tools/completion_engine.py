#!/usr/bin/env python3
"""Verified Completion Engine 2.0.

The completion engine evaluates a requirements contract at three levels:

* implementation -- real changed files, commits and declared write scopes;
* behavioral -- a real test command exit code plus test IDs parsed from that
  command's stdout (never agent prose);
* conformance -- code evidence, an independent structured review and a live
  decision/contract check.

Schema-v1 contracts deliberately fall back to the existing
``requirements_contract.verify_contract`` semantics. That keeps old contracts
byte-compatible while new contracts can bind test cases, a commit and a
contract digest to every criterion.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path

try:
    from tools.requirements_contract import (
        DEFAULT_VERIFICATION_COMMAND,
        ContractError,
        contract_digest,
        contract_schema_version,
        load_current,
        validate_contract,
        validate_contract_state,
        verify_contract,
    )
    from tools.repo_facts import scan_repository
except ModuleNotFoundError:  # flat installed layout
    from requirements_contract import (  # type: ignore
        DEFAULT_VERIFICATION_COMMAND,
        ContractError,
        contract_digest,
        contract_schema_version,
        load_current,
        validate_contract,
        validate_contract_state,
        verify_contract,
    )
    from repo_facts import scan_repository  # type: ignore

CRITERION_STATES = (
    "not_started",
    "implemented_unverified",
    "partially_verified",
    "needs_review",
    "verified",
    "failed",
    "blocked",
)
LEVELS = ("implementation", "behavioral", "conformance")
AGGREGATE_STATUSES = CRITERION_STATES

REVIEW_ROLES = frozenset({"reviewer", "auditor", "challenger", "success-auditor"})
COMMIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")
MAX_TEST_SIGNALS = 500
MAX_TEST_RESULTS = 500
MAX_OUTPUT = 500000

_NORMALIZE_ID = re.compile(r"[^a-z0-9_]+")
_UNITTEST_METHOD = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"\((?P<module>[A-Za-z0-9_.:\[\]-]+)\)")
_UNITTEST_RESULT = re.compile(
    r"^(?:FAIL|ERROR|ok|OK|test):?\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"\((?P<module>[A-Za-z0-9_.:\[\]-]+)\)")
_UNITTEST_ELLIPSIS = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"\((?P<module>[A-Za-z0-9_.:\[\]-]+)\)\s*\.\.\.")
_PYTEST_LINE = re.compile(
    r"^(?P<node>[A-Za-z0-9_./\\-]+::[A-Za-z0-9_.\[\]:-]+)"
    r"\s+(?:PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b")
_GENERIC_TEST = re.compile(r"\b(test_[A-Za-z0-9_]+)\b")


class CompletionError(ValueError):
    pass


def _normalize_test_id(value: str) -> str:
    text = str(value or "").strip().lower().replace("::", ".")
    return _NORMALIZE_ID.sub("_", text).strip("_")


def parse_test_signals(output: str) -> list[str]:
    """Extract real unittest/pytest/``test_*`` names from command stdout.

    This is a parser over actual command output, not a trust boundary by
    itself. The caller must pair the signals with a real process exit code.
    """
    if not isinstance(output, str) or not output.strip():
        return []
    output = output[:MAX_OUTPUT]
    found: list[str] = []
    seen: set[str] = set()

    def add(value):
        if not isinstance(value, str):
            return
        value = value.strip()
        if not value or len(value) > 300 or value in seen:
            return
        if not (re.search(r"(^|[.:_])test_", value)
                or value.startswith("test_")):
            return
        seen.add(value)
        found.append(value)

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _PYTEST_LINE.match(line)
        if match:
            node = match.group("node").replace("\\", "/")
            add(node)
            add(node.split("::")[-1])
            continue
        for pattern in (_UNITTEST_RESULT, _UNITTEST_METHOD, _UNITTEST_ELLIPSIS):
            match = pattern.match(line)
            if not match:
                continue
            name = match.group("name")
            module = match.group("module")
            add(name)
            if module:
                add(f"{module}.{name}")
                add(f"{module.rsplit('.', 1)[-1]}.{name}")
            break
        for match in _GENERIC_TEST.finditer(line):
            add(match.group(1))
        if len(found) >= MAX_TEST_SIGNALS:
            break
    return found[:MAX_TEST_SIGNALS]


_UNITTEST_PASS = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"\((?P<module>[A-Za-z0-9_.:\\[\]-]+)\)\s*\.\.\.\s+ok\s*$")
_PYTEST_PASS = re.compile(
    r"^(?P<node>[A-Za-z0-9_./\\-]+::[A-Za-z0-9_.\[\]:-]+)\s+PASSED\b")


def parse_passed_test_signals(output: str) -> list[str]:
    """Extract only positively passing test names from real command output."""
    if not isinstance(output, str) or not output.strip():
        return []
    found: list[str] = []
    seen: set[str] = set()

    def add(value):
        if not isinstance(value, str):
            return
        value = value.strip()
        if not value or len(value) > 300 or value in seen:
            return
        if "test_" not in value:
            return
        seen.add(value)
        found.append(value)

    for line in output[:MAX_OUTPUT].splitlines():
        line = line.strip()
        if not line:
            continue
        match = _PYTEST_PASS.match(line)
        if match:
            node = match.group("node").replace("\\", "/")
            add(node)
            add(node.split("::")[-1])
            continue
        match = _UNITTEST_PASS.match(line)
        if match:
            name = match.group("name")
            module = match.group("module")
            add(name)
            add(f"{module}.{name}")
            add(f"{module.rsplit('.', 1)[-1]}.{name}")
    return found[:MAX_TEST_SIGNALS]


def _test_id_matches(required: str, available: list[str]) -> bool:
    target = _normalize_test_id(required)
    if not target:
        return False
    for value in available:
        candidate = _normalize_test_id(value)
        if not candidate:
            continue
        if candidate == target:
            return True
        if candidate.endswith("_" + target) or candidate.endswith("." + target):
            return True
        parts = re.split(r"[._]", candidate)
        if target in parts:
            return True
    return False


def _argv(command: str) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _command_kind(command: str) -> str | None:
    argv = _argv(command)
    if not argv:
        return None
    executable = Path(argv[0]).name.lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    if executable in ("python", "python3", "python3.11", "python3.12",
                      "py"):
        if len(argv) >= 3 and argv[1] == "-m":
            if argv[2] in ("unittest", "pytest"):
                return argv[2]
        return "python"
    if executable in ("pytest", "py.test"):
        return "pytest"
    if executable == "node" and len(argv) >= 2 and argv[1] == "--test":
        return "node-test"
    if executable == "npm":
        return "npm-test"
    if executable == "go" and len(argv) >= 2 and argv[1] == "test":
        return "go-test"
    if executable == "cargo" and len(argv) >= 2 and argv[1] == "test":
        return "cargo-test"
    if executable == "make" and len(argv) >= 2:
        return "make-test"
    return None


def _command_matches(command: str, accepted: list[str]) -> bool:
    if not accepted:
        return True
    if not command:
        return True
    if command in accepted:
        return True
    kind = _command_kind(command)
    if kind is None:
        return False
    return any(_command_kind(item) == kind for item in accepted)


def _trusted_result(item: dict) -> tuple[bool, str | None]:
    if not isinstance(item, dict):
        return False, "not_an_object"
    if item.get("claimed") is True or item.get("forged") is True:
        return False, "claimed_only"
    if item.get("trusted") is False or item.get("source") in (
            "agent", "model", "prose", "claim"):
        return False, "untrusted_source"
    exit_code = item.get("exit_code")
    if type(exit_code) is not int:
        return False, "missing_real_exit_code"
    return True, None


def _collect_tests(test_results) -> tuple[list[dict], list[dict]]:
    if isinstance(test_results, dict):
        test_results = test_results.get("results") or test_results.get("tests") or []
    if not isinstance(test_results, list):
        return [], [{"reason": "test_results_not_a_list"}]
    accepted: list[dict] = []
    rejected: list[dict] = []
    for raw in test_results[:MAX_TEST_RESULTS]:
        ok, reason = _trusted_result(raw)
        if not ok:
            rejected.append({
                "name": str(raw.get("name") or raw.get("command") or "")[:200]
                if isinstance(raw, dict) else "",
                "reason": reason,
            })
            continue
        command = raw.get("command") or raw.get("name") or ""
        output = (raw.get("stdout") or raw.get("output")
                  or raw.get("evidence") or "")
        signals = raw.get("signals")
        if isinstance(output, str) and output.strip():
            signals = parse_passed_test_signals(output)
        elif not isinstance(signals, list):
            signals = raw.get("test_ids")
        if not isinstance(signals, list):
            signals = []
        clean_signals = []
        for signal in signals:
            if isinstance(signal, str) and signal.strip():
                value = signal.strip()[:300]
                if value not in clean_signals:
                    clean_signals.append(value)
        exit_code = int(raw["exit_code"])
        accepted.append({
            "command": str(command)[:500],
            "exit_code": exit_code,
            "passed": exit_code == 0,
            "signals": clean_signals[:MAX_TEST_SIGNALS],
            "duration_ms": raw.get("duration_ms"),
            "source": raw.get("source") or "runner",
            "task_id": raw.get("task_id"),
            "attempt": raw.get("attempt"),
        })
    return accepted, rejected


def _extract_state(state: dict) -> dict:
    if not isinstance(state, dict):
        return {"tests": [], "changed_files": [], "commits": [], "reviews": []}
    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
        for milestone in state.get("milestones") or []:
            if isinstance(milestone, dict):
                tasks.extend(milestone.get("tasks") or [])
    tests = []
    changed_files = []
    commits = []
    reviews = []
    seen_changed = set()
    seen_commits = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        result = task.get("result")
        if not isinstance(result, dict):
            continue
        task_id = task.get("id") or task.get("task_id")
        role = task.get("role")
        for test in result.get("tests") or []:
            if isinstance(test, dict):
                item = dict(test)
                item.setdefault("task_id", task_id)
                item.setdefault("source", "runner")
                tests.append(item)
        for path in result.get("changed_files") or []:
            if isinstance(path, str) and path not in seen_changed:
                seen_changed.add(path)
                changed_files.append(path)
        for commit in result.get("commits") or []:
            if isinstance(commit, str) and commit not in seen_commits:
                seen_commits.add(commit)
                commits.append(commit)
        verdict = result.get("verdict")
        if role in REVIEW_ROLES and verdict in ("pass", "fail"):
            reviews.append({
                "task_id": task_id,
                "role": role,
                "verdict": verdict,
                "independent": bool(result.get("independent", True)),
                "attempt": task.get("attempts") or task.get("attempt"),
            })
    return {"tests": tests, "changed_files": changed_files,
            "commits": commits, "reviews": reviews}


def _normalize_accounted_files(value) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, str):
            continue
        path = item.strip().replace("\\", "/")
        if not path or path.startswith("/") or ".." in path.split("/"):
            continue
        if path not in result:
            result.append(path)
    return result


def _normalize_reviews(value) -> list[dict]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        verdict = item.get("verdict")
        if role not in REVIEW_ROLES or verdict not in ("pass", "fail"):
            continue
        if item.get("independent") is False:
            continue
        level = item.get("independence_level")
        if level not in ("declared", "observed", "accepted"):
            level = "declared"
        reviewer = (item.get("reviewer_agent_id")
                    or item.get("reviewer_session_id")
                    or item.get("reviewer"))
        criteria = item.get("criterion_ids") or item.get("criteria") or []
        if not isinstance(criteria, list):
            criteria = []
        criteria = [str(value)[:80] for value in criteria
                    if isinstance(value, str)][:50]
        contract = item.get("contract_sha256") or item.get("contract_sha")
        result.append({
            "reviewed_task_id": (item.get("reviewed_task_id")
                                 or item.get("task_id") or item.get("id")),
            "reviewer_task_id": item.get("reviewer_task_id"),
            "reviewer_agent_id": item.get("reviewer_agent_id"),
            "reviewer_session_id": item.get("reviewer_session_id"),
            "reviewer": reviewer,
            "criterion_ids": criteria,
            "contract_sha256": contract if isinstance(contract, str) else None,
            "role": role,
            "verdict": verdict,
            "independent": level in ("observed", "accepted"),
            "independence_level": level,
            "attempt": item.get("attempt"),
            "commit": item.get("commit") or item.get("commit_sha"),
        })
    return result


def _plan_scopes(plan: dict | None) -> list[list[str]]:
    if not isinstance(plan, dict):
        return []
    scopes = []
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        write = task.get("write_scope", task.get("write", []))
        if isinstance(write, list) and any(isinstance(item, str) and item
                                           for item in write):
            scopes.append([item for item in write if isinstance(item, str)])
    return scopes


def _scope_violations(changed_files: list[str], plan: dict | None) -> list[str]:
    scopes = _plan_scopes(plan)
    if not scopes:
        return []
    violations = []
    for path in changed_files:
        allowed = False
        for scope in scopes:
            for entry in scope:
                entry = entry.rstrip("/")
                if path == entry or path.startswith(entry + "/"):
                    allowed = True
                    break
            if allowed:
                break
        if not allowed:
            violations.append(path)
    return violations


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        if not (Path(root) / ".git").exists():
            return None
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _head_sha(root: Path) -> str | None:
    result = _git(root, "rev-parse", "HEAD")
    if result is None or result.returncode != 0:
        return None
    value = result.stdout.strip().lower()
    return value if COMMIT_SHA.fullmatch(value) else None


def _commit_is_ancestor(root: Path, commit: str) -> bool | None:
    exists = _git(root, "cat-file", "-e", f"{commit}^{{commit}}")
    if exists is None:
        return None
    if exists.returncode != 0:
        return False
    result = _git(root, "merge-base", "--is-ancestor", commit, "HEAD")
    if result is None:
        return None
    return result.returncode == 0


def _is_test_command(command: str) -> bool:
    kind = _command_kind(command)
    if kind in ("unittest", "pytest", "node-test", "npm-test",
                "go-test", "cargo-test", "make-test"):
        return True
    text = str(command or "").casefold()
    return any(token in text for token in (
        "unittest", "pytest", "run_tests", "test_runner", "test-runner",
        " test ", "tests/", "test_", "npm test", "make test", "make check"))


def _accepted_commands(contract: dict, plan: dict | None) -> list[str]:
    values = []
    command = contract.get("verification_command")
    if isinstance(command, str) and command:
        values.append(command)
    if isinstance(plan, dict):
        for key in ("verification_command", "test_command"):
            item = plan.get(key)
            if isinstance(item, str) and item:
                values.append(item)
        for item in plan.get("test_commands") or []:
            if isinstance(item, str) and item:
                values.append(item)
        for task in plan.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            for item in task.get("verify_commands") or []:
                if isinstance(item, str) and item and _is_test_command(item):
                    values.append(item)
    for criterion in contract.get("acceptance_criteria") or []:
        evidence = criterion.get("evidence") or {}
        if evidence.get("kind") == "command":
            item = evidence.get("command")
            if isinstance(item, str) and item:
                values.append(item)
    return list(dict.fromkeys(values))


def _review_result(reviews: list[dict],
                   commit_sha: str | None = None, *,
                   criterion_id: str | None = None,
                   implementer_identity: str | None = None) -> tuple[bool, bool]:
    """Return (passed, failed) for independent reviews only.

    A declared review is kept for audit but never satisfies a review-required
    criterion. Observed or explicitly user-accepted reviews are independent.
    """
    if not reviews:
        return False, False
    usable = []
    for item in reviews:
        if commit_sha is not None and item.get("commit") and (
                str(item["commit"]).lower() != commit_sha.lower()):
            continue
        if (criterion_id and item.get("criterion_ids")
                and criterion_id not in item["criterion_ids"]):
            continue
        reviewer = (item.get("reviewer_agent_id")
                    or item.get("reviewer_session_id")
                    or item.get("reviewer"))
        if (implementer_identity and reviewer
                and str(reviewer) == str(implementer_identity)):
            continue
        if item.get("independence_level") not in ("observed", "accepted"):
            continue
        usable.append(item)
    if not usable:
        return False, False
    passed = any(item["verdict"] == "pass" for item in usable)
    failed = any(item["verdict"] == "fail" for item in usable)
    return passed and not failed, failed


def _code_facts(root: Path) -> tuple[dict, str | None]:
    try:
        facts = scan_repository(Path(root))
        return facts, None
    except (OSError, ValueError) as exc:
        return {}, str(exc)[:300]


def _state_for_criterion(implemented: bool, behavioral: str,
                         conformance: str) -> str:
    if behavioral == "failed" or conformance == "failed":
        return "failed"
    if not implemented:
        return "not_started"
    if conformance == "blocked":
        return "blocked"
    if behavioral == "missing":
        return "needs_review" if conformance == "needs_review" else "implemented_unverified"
    if behavioral == "partial":
        return "partially_verified"
    if conformance == "needs_review":
        return "needs_review"
    if behavioral in ("pass", "not_required") and conformance == "pass":
        return "verified"
    if behavioral == "pass":
        return "needs_review"
    return "implemented_unverified"


def _aggregate(criteria: list[dict], *, stale: bool = False) -> str:
    if stale or any(item["state"] == "blocked" for item in criteria):
        return "blocked"
    if any(item["state"] == "failed" for item in criteria):
        return "failed"
    if criteria and all(item["state"] == "verified" for item in criteria):
        return "verified"
    if any(item["state"] == "needs_review" for item in criteria):
        return "needs_review"
    if any(item["state"] == "partially_verified" for item in criteria):
        return "partially_verified"
    if any(item["state"] == "implemented_unverified" for item in criteria):
        return "implemented_unverified"
    if criteria and all(item["state"] == "not_started" for item in criteria):
        return "not_started"
    return "partially_verified" if criteria else "not_started"


def _binding(contract: dict, *, run_id, task_id, attempt, commit_sha,
             test_ids) -> dict:
    return {
        "contract_id": contract.get("contract_id"),
        "contract_version": contract.get("contract_version"),
        "contract_schema_version": contract_schema_version(contract),
        "contract_sha256": contract.get("sha256"),
        "contract_digest": contract_digest(contract),
        "commit_sha": commit_sha,
        "run_id": run_id,
        "task_id": task_id,
        "attempt": attempt,
        "test_ids": list(dict.fromkeys(test_ids))[:MAX_TEST_SIGNALS],
        "evaluated_at": time.time(),
    }


def _legacy_evaluation(root, contract, *, test_results, run_id, task_id,
                       attempt, commit_sha) -> dict:
    tests, rejected = _collect_tests(test_results)
    legacy_tests = [{
        "command": item["command"], "name": item["command"],
        "exit_code": item["exit_code"], "status": "passed" if item["passed"]
        else "failed",
    } for item in tests]
    result = verify_contract(root, contract, test_results=legacy_tests)
    by_id = {item.get("id"): item for item in
             contract.get("acceptance_criteria") or []}
    criteria = []
    for check in result.get("checks") or []:
        criterion_id = check.get("criterion_id")
        criterion = by_id.get(criterion_id, {})
        status = check.get("status")
        if status == "passed":
            state = "verified"
        elif status == "failed":
            state = "failed"
        else:
            state = "needs_review"
        criteria.append({
            "criterion_id": criterion_id,
            "requirement_id": criterion_id,
            "text": criterion.get("text"),
            "critical": True,
            "review_required": False,
            "state": state,
            "level": check.get("kind"),
            "test_ids": [],
            "required_test_ids": [],
            "missing_test_ids": [],
            "reasons": [check.get("evidence") or ""][:5],
            "evidence": check,
        })
    status = result.get("status")
    if status == "verified":
        aggregate = "verified"
    elif status == "failed":
        aggregate = "failed"
    elif status == "invalidated":
        aggregate = "blocked"
    else:
        aggregate = "partially_verified"
    failures = [item for item in criteria if item["state"] == "failed"]
    blocked = [item for item in criteria if item["state"] == "blocked"]
    test_ids = [signal for item in tests if item["passed"]
                for signal in item["signals"]]
    return {
        "schema_version": 1,
        "legacy": True,
        "status": aggregate,
        "aggregate_status": aggregate,
        "requirements_status": aggregate,
        "fully_verified": bool(result.get("fully_verified")),
        "verified": bool(result.get("fully_verified")),
        "verified_completion": bool(result.get("fully_verified")),
        "run_passed": bool(result.get("ok")),
        "criterion_states": {item["criterion_id"]: item["state"]
                             for item in criteria},
        "contract_id": contract.get("contract_id"),
        "contract_version": contract.get("contract_version"),
        "sha256": contract.get("sha256"),
        "commit_sha": commit_sha,
        "run_id": run_id,
        "task_id": task_id,
        "attempt": attempt,
        "criteria": criteria,
        "critical_failures": failures,
        "blocking": blocked,
        "levels": {
            "implementation": {"status": "not_evaluated", "legacy": True,
                               "checks": []},
            "behavioral": {"status": status, "checks": result.get("checks", [])},
            "conformance": {"status": status, "checks": result.get("checks", [])},
        },
        "passed": result.get("passed"),
        "not_verified": result.get("not_verified"),
        "reasons": list(result.get("reasons") or []),
        "rejected_test_results": rejected,
        "binding": _binding(contract, run_id=run_id, task_id=task_id,
                            attempt=attempt, commit_sha=commit_sha,
                            test_ids=test_ids),
    }


def evaluate_completion(root, contract, *, plan=None, run_id=None,
                        state=None, changed_files=None, commits=None,
                        test_results=None, reviews=None, commit_sha=None,
                        head_sha=None, expected_commit=None,
                        verification_command=None, task_id=None,
                        attempt=None, scan_code=True, check_state=True,
                        check_commit=True, implementer_identity=None) -> dict:
    """Evaluate a contract and return criterion states plus an aggregate.

    All behavioral inputs must be real runner observations: a list of
    ``{command, exit_code, signals, ...}`` dictionaries. Agent prose is never
    inspected. A missing exit code is rejected as claimed-only evidence.
    """
    root = Path(root)
    if not isinstance(contract, dict):
        return {
            "status": "not_started", "aggregate_status": "not_started",
            "fully_verified": False, "verified_completion": False,
            "run_passed": False, "criteria": [], "reasons": [
                "no requirements contract"],
            "binding": {}, "legacy": False,
        }
    try:
        normalized = validate_contract(contract)
    except (ContractError, TypeError, ValueError) as exc:
        return {
            "status": "blocked", "aggregate_status": "blocked",
            "fully_verified": False, "verified_completion": False,
            "run_passed": False, "criteria": [], "reasons": [
                f"contract is invalid: {exc}"][:3],
            "blocking": [{"reason": "invalid_contract"}],
            "binding": {"contract_id": contract.get("contract_id")},
            "legacy": False,
        }
    schema_version = contract_schema_version(normalized)
    state_values = _extract_state(state)
    if changed_files is None:
        changed_files = state_values["changed_files"]
    if commits is None:
        commits = state_values["commits"]
    if test_results is None:
        test_results = state_values["tests"]
    if reviews is None:
        reviews = state_values["reviews"]
    changed_files = _normalize_accounted_files(list(changed_files or []))
    commits = [item for item in (commits or []) if isinstance(item, str)]
    if commit_sha is None and commits:
        commit_sha = commits[-1]
    if isinstance(commit_sha, str):
        commit_sha = commit_sha.strip().lower() or None
    if review_records := _normalize_reviews(reviews):
        reviews = review_records
    else:
        reviews = []
    if task_id is not None:
        # The reviewed task is the implementation task. Never exclude it; the
        # old filter removed the only valid audit. Reviews for another task are
        # rejected explicitly.
        reviews = [item for item in reviews
                   if item.get("reviewed_task_id") in (None, task_id)]
    contract_digest = normalized.get("sha256")
    if contract_digest:
        reviews = [item for item in reviews
                   if not item.get("contract_sha256")
                   or item["contract_sha256"] == contract_digest]

    if schema_version == 1:
        return _legacy_evaluation(
            root, normalized, test_results=test_results, run_id=run_id,
            task_id=task_id, attempt=attempt, commit_sha=commit_sha)

    reasons: list[str] = []
    stale_reason = None
    if normalized.get("status") != "confirmed":
        stale_reason = f"contract is {normalized.get('status')}"
    if normalized.get("blockers"):
        stale_reason = "contract has unresolved blockers"
    if check_state:
        try:
            validate_contract_state(root, normalized)
        except (ContractError, OSError, ValueError) as exc:
            message = str(exc)
            stale_reason = ("stale_decision: " if "decision" in message.lower()
                            or "interview" in message.lower()
                            else "stale_contract: ") + message[:400]
    if check_state:
        try:
            current = load_current(root)
        except (ContractError, OSError, ValueError) as exc:
            current = None
            stale_reason = stale_reason or f"contract pointer is invalid: {exc}"
        if current is not None and current.get("contract_id") == normalized.get(
                "contract_id"):
            if current.get("contract_version") != normalized.get("contract_version"):
                stale_reason = "stale_contract: a newer contract version exists"
            elif current.get("sha256") != normalized.get("sha256"):
                stale_reason = "stale_contract: current pointer digest differs"
    if isinstance(plan, dict):
        reference = plan.get("contract_ref")
        if isinstance(reference, dict):
            if (reference.get("contract_id")
                    and reference["contract_id"] != normalized.get("contract_id")):
                stale_reason = "stale_contract: plan references another contract"
            if (reference.get("contract_version") is not None
                    and reference["contract_version"]
                    != normalized.get("contract_version")):
                stale_reason = "stale_contract: plan contract version differs"
            if (reference.get("sha256")
                    and reference["sha256"] != normalized.get("sha256")):
                stale_reason = "stale_contract: plan contract digest differs"
    if check_commit:
        if expected_commit is not None:
            if not isinstance(expected_commit, str) or not COMMIT_SHA.fullmatch(
                    expected_commit.strip().lower()):
                stale_reason = stale_reason or "stale_commit: invalid expected commit"
            elif commit_sha is None:
                stale_reason = stale_reason or "stale_commit: no commit binding"
            elif commit_sha != expected_commit.strip().lower():
                stale_reason = stale_reason or "stale_commit: commit changed"
        if commit_sha is not None and not COMMIT_SHA.fullmatch(commit_sha):
            stale_reason = stale_reason or "stale_commit: invalid commit sha"
        elif commit_sha is not None:
            actual_head = _head_sha(root)
            if head_sha is not None and isinstance(head_sha, str):
                head_value = head_sha.strip().lower()
                if head_value != (actual_head or head_value):
                    stale_reason = stale_reason or "stale_commit: HEAD changed"
                elif commit_sha != head_value:
                    stale_reason = stale_reason or (
                        "stale_commit: verified commit is not HEAD")
            ancestor = _commit_is_ancestor(root, commit_sha)
            if ancestor is False:
                stale_reason = stale_reason or (
                    "stale_commit: commit is not an ancestor of HEAD")

    tests, rejected = _collect_tests(test_results)
    accepted_commands = _accepted_commands(normalized, plan)
    if isinstance(verification_command, str) and verification_command:
        accepted_commands = list(dict.fromkeys(
            [verification_command, *accepted_commands]))
    matching = [item for item in tests
                if _command_matches(item["command"], accepted_commands)]
    failed_commands = [item for item in matching if not item["passed"]]
    passing_signals = [signal for item in matching if item["passed"]
                       for signal in item["signals"]]
    facts, code_error = (_code_facts(root) if scan_code
                         else ({}, "code scan skipped"))
    detected = facts.get("detected") or {}
    scopes = _scope_violations(changed_files, plan)
    scope_violation = bool(scopes)
    declared_existing = any(
        (root / path).is_file()
        for criterion in normalized.get("acceptance_criteria") or []
        for path in criterion.get("implementation_files") or [])
    code_present = any(
        detected.get((criterion.get("evidence") or {}).get("expected"))
        for criterion in normalized.get("acceptance_criteria") or []
        if (criterion.get("evidence") or {}).get("kind") == "code_evidence")
    implementation_present = bool(
        changed_files or commits or commit_sha or declared_existing or code_present)

    criteria = []
    for criterion in normalized.get("acceptance_criteria") or []:
        criterion_id = criterion["id"]
        requirement_id = criterion.get("requirement_id") or criterion_id
        implementation_files = list(criterion.get("implementation_files") or [])
        test_cases = list(criterion.get("test_cases") or [])
        evidence = criterion.get("evidence") or {}
        required_cases = [case for case in test_cases
                          if case.get("required", True)]
        missing = [case["test_id"] for case in required_cases
                   if not _test_id_matches(case["test_id"], passing_signals)]
        found = [case["test_id"] for case in required_cases
                 if case["test_id"] not in missing]
        if scopes:
            implemented = False
            implementation_detail = "write-scope violation"
        elif implementation_files:
            changed_matches = [path for path in implementation_files
                               if path in changed_files]
            existing = [path for path in implementation_files
                        if (root / path).is_file()]
            implemented = bool(changed_matches or (existing and implementation_present))
            implementation_detail = (
                f"changed={len(changed_matches)}; existing={len(existing)}")
        else:
            implemented = implementation_present
            implementation_detail = ("changed files are present"
                                     if changed_files else
                                     "commit bound" if commit_sha else
                                     "no implementation evidence")

        if failed_commands:
            behavioral = "failed"
            behavioral_detail = (
                f"{failed_commands[0]['command']} exited "
                f"{failed_commands[0]['exit_code']}")
        elif missing:
            behavioral = "partial" if found else "missing"
            behavioral_detail = "missing test signals: " + ", ".join(missing[:6])
        elif required_cases:
            behavioral = "pass"
            behavioral_detail = "all required test signals observed"
        elif (not test_cases
              and evidence.get("kind") == "command"
              and any(item["passed"] and
                      item["command"] == evidence.get("command")
                      for item in matching)):
            behavioral = "pass"
            behavioral_detail = "explicit criterion command exited 0"
        elif not tests and not test_cases:
            behavioral = "missing"
            behavioral_detail = "no trusted test result"
        else:
            behavioral = "missing"
            behavioral_detail = "no matching trusted test result"

        review_passed, review_failed = _review_result(
            reviews, commit_sha, criterion_id=criterion_id,
            implementer_identity=implementer_identity)
        if evidence.get("kind") == "code_evidence":
            expected = evidence.get("expected")
            if not scan_code:
                conformance = "needs_review"
                conformance_detail = "code scan skipped"
            elif detected.get(expected):
                conformance = "pass"
                conformance_detail = f"code evidence: {expected}=True"
            else:
                conformance = "failed"
                conformance_detail = f"code evidence: {expected}=False"
        elif review_failed:
            conformance = "failed"
            conformance_detail = "independent review failed"
        elif criterion.get("review_required"):
            conformance = "pass" if review_passed else "needs_review"
            conformance_detail = ("independent review passed" if review_passed
                                  else "independent review required")
        else:
            conformance = "pass"
            conformance_detail = "no independent review required"

        if stale_reason:
            state = "blocked"
        else:
            state = _state_for_criterion(implemented, behavioral, conformance)
            if (criterion.get("review_required")
                    and state in ("verified", "implemented_unverified")
                    and not review_passed):
                state = "needs_review"
            if (criterion.get("review_required")
                    and state == "implemented_unverified"
                    and behavioral == "missing"):
                # A required review cannot turn a missing behavioral test into
                # a silent pass; expose the review gap instead.
                state = "needs_review"
            if scope_violation:
                state = "failed"
        reasons_for = []
        if stale_reason:
            reasons_for.append(stale_reason)
        if scope_violation:
            reasons_for.append("write-scope violation: "
                               + ", ".join(scopes[:5]))
        if not implemented:
            reasons_for.append(implementation_detail)
        if missing:
            reasons_for.append("missing test IDs: " + ", ".join(missing[:8]))
        if failed_commands:
            reasons_for.append(behavioral_detail)
        if conformance in ("failed", "needs_review"):
            reasons_for.append(conformance_detail)
        if criterion.get("review_required") and not review_passed:
            reasons_for.append("independent review is required")
        criteria.append({
            "criterion_id": criterion_id,
            "requirement_id": requirement_id,
            "text": criterion.get("text"),
            "critical": bool(criterion.get("critical")),
            "review_required": bool(criterion.get("review_required")),
            "decision_ref": criterion.get("decision_ref"),
            "implementation_files": implementation_files,
            "state": state,
            "level": ("conformance" if conformance in ("failed", "needs_review")
                      else "behavioral" if behavioral in ("failed", "partial", "missing")
                      else "implementation"),
            "test_ids": found,
            "required_test_ids": [case["test_id"] for case in required_cases],
            "missing_test_ids": missing,
            "behavioral_status": behavioral,
            "conformance_status": conformance,
            "implementation_detail": implementation_detail,
            "behavioral_detail": behavioral_detail,
            "conformance_detail": conformance_detail,
            "reasons": reasons_for[:10],
            "evidence": {
                "code": evidence.get("expected"),
                "review": "pass" if review_passed else (
                    "fail" if review_failed else "missing"),
                "command_passed": not failed_commands and bool(
                    matching and any(item["passed"] for item in matching)),
                "signals": found,
            },
        })

    aggregate = _aggregate(criteria, stale=bool(stale_reason))
    critical_failures = [item for item in criteria
                         if item["critical"] and item["state"] in ("failed", "blocked")]
    critical_unverified = [item for item in criteria
                           if item["critical"] and item["state"] != "verified"]
    blocking = [{"criterion_id": item["criterion_id"],
                 "reason": "; ".join(item["reasons"][:3])}
                for item in critical_failures]
    if stale_reason:
        blocking.insert(0, {"reason": stale_reason})
    fully_verified = bool(criteria) and all(
        item["state"] == "verified" for item in criteria) and not stale_reason
    implementation_status = (
        "failed" if scopes else
        "verified" if criteria and all(
            item["state"] != "not_started" for item in criteria) and
        implementation_present else
        "not_started" if not implementation_present else "implemented_unverified")
    behavioral_status = (
        "failed" if failed_commands else
        "verified" if criteria and not any(
            item["behavioral_status"] in ("missing", "partial", "failed")
            for item in criteria) else
        "partially_verified" if any(
            item["behavioral_status"] in ("pass", "partial") for item in criteria)
        else "implemented_unverified")
    conformance_status = (
        "failed" if any(item["conformance_status"] == "failed"
                        for item in criteria) else
        "needs_review" if any(item["conformance_status"] == "needs_review"
                              for item in criteria) else
        "verified" if criteria else "not_started")
    if stale_reason:
        implementation_status = "blocked"
        behavioral_status = "blocked"
        conformance_status = "blocked"
    levels = {
        "implementation": {
            "status": implementation_status,
            "checks": [{"criterion_id": item["criterion_id"],
                        "state": item["state"],
                        "detail": item["implementation_detail"]}
                       for item in criteria],
        },
        "behavioral": {
            "status": behavioral_status,
            "checks": [{"criterion_id": item["criterion_id"],
                        "state": item["state"],
                        "detail": item["behavioral_detail"],
                        "test_ids": item["test_ids"],
                        "missing": item["missing_test_ids"]}
                       for item in criteria],
        },
        "conformance": {
            "status": conformance_status,
            "checks": [{"criterion_id": item["criterion_id"],
                        "state": item["state"],
                        "detail": item["conformance_detail"]}
                       for item in criteria],
        },
    }
    if stale_reason:
        reasons.append(stale_reason)
    if scope_violation:
        reasons.append("write-scope violation: " + ", ".join(scopes[:5]))
    reasons.extend(f"{item['criterion_id']}: {reason}"
                   for item in critical_failures
                   for reason in item["reasons"][:2])
    if code_error and scan_code:
        reasons.append(f"code scan warning: {code_error}")
    return {
        "schema_version": 2,
        "legacy": False,
        "status": aggregate,
        "aggregate_status": aggregate,
        "requirements_status": aggregate,
        "fully_verified": fully_verified,
        "verified": fully_verified,
        "verified_completion": fully_verified,
        "run_passed": not bool(critical_failures) and not stale_reason,
        "criterion_states": {item["criterion_id"]: item["state"]
                             for item in criteria},
        "contract_id": normalized.get("contract_id"),
        "contract_version": normalized.get("contract_version"),
        "sha256": normalized.get("sha256"),
        "commit_sha": commit_sha,
        "run_id": run_id,
        "task_id": task_id,
        "attempt": attempt,
        "criteria": criteria,
        "levels": levels,
        "critical_failures": critical_failures,
        "critical_unverified": critical_unverified,
        "blocking": blocking,
        "stale": bool(stale_reason),
        "stale_reason": stale_reason,
        "scope_violation": scope_violation,
        "scopes": {"violations": scopes, "checked": bool(_plan_scopes(plan))},
        "test_results": {
            "accepted": len(tests),
            "rejected": rejected[:20],
            "matching": len(matching),
            "failed": len(failed_commands),
            "evidence": [{
                "command": item["command"][:200],
                "exit_code": item["exit_code"],
                "signals": item["signals"][:20],
            } for item in matching[:50]],
        },
        "rejected_test_results": rejected,
        "reviews": reviews,
        "reasons": reasons[:20],
        "binding": _binding(normalized, run_id=run_id, task_id=task_id,
                            attempt=attempt, commit_sha=commit_sha,
                            test_ids=passing_signals),
    }


def requirements_projection(contract, *, state=None, run_id=None) -> dict:
    """Fast status projection that never scans the repository.

    The full ``evaluate_completion`` result is written by the runner at the
    end of a run. Engine status needs an additive, cheap block for dashboards
    and chat; it must not change ``verified_progress`` or fail status.
    """
    if not isinstance(contract, dict):
        return None
    try:
        normalized = validate_contract(contract)
    except (ContractError, TypeError, ValueError) as exc:
        return {
            "status": "blocked", "aggregate_status": "blocked",
            "fully_verified": False, "verified_completion": False,
            "run_passed": False, "criteria": [],
            "reasons": [f"contract is invalid: {exc}"][:3],
            "legacy": False, "projection": True,
        }
    schema_version = contract_schema_version(normalized)
    if schema_version == 1:
        return {
            "status": "partially_verified",
            "aggregate_status": "partially_verified",
            "requirements_status": "partially_verified",
            "fully_verified": False,
            "verified": False,
            "verified_completion": False,
            "run_passed": None,
            "contract_id": normalized.get("contract_id"),
            "contract_version": normalized.get("contract_version"),
            "sha256": normalized.get("sha256"),
            "criteria": [],
            "criterion_states": {},
            "legacy": True,
            "projection": True,
            "reason": "schema v1 is evaluated at run finalization",
        }
    result = evaluate_completion(
        ".", normalized, state=state, run_id=run_id, scan_code=False,
        check_state=False, check_commit=False,
    )
    result["projection"] = True
    return result


class CompletionEngine:
    """Small stateful wrapper around :func:`evaluate_completion`."""

    def __init__(self, root, contract=None, *, plan=None, run_id=None):
        self.root = Path(root)
        self.contract = contract
        self.plan = plan
        self.run_id = run_id

    def evaluate(self, contract=None, **kwargs):
        kwargs.setdefault("plan", self.plan)
        kwargs.setdefault("run_id", self.run_id)
        return evaluate_completion(
            self.root, contract if contract is not None else self.contract,
            **kwargs)

    def projection(self, contract=None, *, state=None):
        return requirements_projection(
            contract if contract is not None else self.contract,
            state=state, run_id=self.run_id)


# Friendly aliases for callers that use a different verb or noun.
evaluate = evaluate_completion
evaluate_requirements = evaluate_completion
evaluate_contract = evaluate_completion
evaluate_contract_completion = evaluate_completion
verify_completion = evaluate_completion
