#!/usr/bin/env python3
"""Optional structured task-result review gate (metadata only; no tests run).

A review artifact is an attestation from the caller, not proof that an
independent agent or test process executed. Native evidence is checked by
Supervisor independently. Never turn an unsupported review into a pass.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


class ReviewError(ValueError):
    pass


ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
SEVERITIES = {"blocker", "high", "medium", "low", "info"}
STATUSES = {"open", "resolved"}


def result_digest(result: dict) -> str:
    """Stable fingerprint of the exact submitted result structure."""
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def code_digest(root, changed_files) -> str:
    """Stable digest of the reviewed files' current contents.

    The review is still caller-attested metadata. This digest prevents a stale
    review from being reused after the reviewed files change.
    """
    if not isinstance(changed_files, list) or any(
            not isinstance(x, str) or not x or x.startswith("/")
            or ".." in Path(x).parts for x in changed_files):
        raise ReviewError("Invalid changed_files for code digest")
    entries = []
    base = Path(root)
    for relative in sorted(set(changed_files)):
        path = base / relative
        try:
            data = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        except OSError:
            data = b""
        entries.append(relative + "\0" + hashlib.sha256(data).hexdigest())
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def validate_review(review: dict, *, task_id: str, attempt: int,
                    result: dict, code_sha256: str) -> dict:
    """Reject stale, unsupported, unreviewed, or failed evidence."""
    if not isinstance(review, dict) or review.get("version") != 1:
        raise ReviewError("A version 1 review artifact is required")
    if review.get("task_id") != task_id or type(review.get("attempt")) is not int or review["attempt"] != attempt:
        raise ReviewError("Review does not match the current task attempt")
    digest = review.get("result_sha256")
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest) or digest != result_digest(result):
        raise ReviewError("Review digest does not match the submitted result")
    reviewed_code = review.get("code_sha256")
    if not isinstance(reviewed_code, str) or not DIGEST.fullmatch(reviewed_code) or reviewed_code != code_sha256:
        raise ReviewError("Review does not match the current reviewed files")
    if review.get("verdict") != "approved":
        raise ReviewError("Review verdict is not approved")
    tests = result.get("tests")
    if not isinstance(tests, list) or not tests or any(
        not isinstance(x, dict) or x.get("status") != "passed"
        or not isinstance(x.get("name"), str) or not x["name"]
        for x in tests
    ):
        raise ReviewError("Review requires nonempty, reported passing tests")
    expected = [x["name"] for x in tests]
    if len(set(expected)) != len(expected):
        raise ReviewError("Duplicate test name in result")
    checks = review.get("checks")
    if not isinstance(checks, list) or len(checks) != len(expected) or any(
        not isinstance(x, dict) or not isinstance(x.get("name"), str)
        or x.get("status") != "passed" for x in checks
    ):
        raise ReviewError("Review checks must confirm every passing test")
    names = [x["name"] for x in checks]
    if sorted(names) != sorted(expected):
        raise ReviewError("Review checks differ from result tests")
    findings = review.get("findings")
    if not isinstance(findings, list) or len(findings) > 100:
        raise ReviewError("Review findings must be a bounded list")
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("severity") not in SEVERITIES or (
            finding.get("status") not in STATUSES
        ) or not isinstance(finding.get("summary"), str) or not 1 <= len(finding["summary"]) <= 1000:
            raise ReviewError("Invalid review finding")
        if finding["status"] == "open" and finding["severity"] in {"blocker", "high"}:
            raise ReviewError("Unresolved blocker/high review finding")
    return {"approved": True, "result_sha256": digest, "check_count": len(checks),
            "findings": len(findings)}
