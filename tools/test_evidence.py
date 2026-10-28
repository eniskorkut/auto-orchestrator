"""Structured test result parsing for verified completion.

Free-form stdout is not enough to prove a specific test passed. This module
parses bounded JUnit XML and pytest JSON files into normalized records. It
never executes anything and never trusts a test's own claim without a status.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
MAX_TESTS = 5000
MAX_NAME = 300
DANGEROUS_XML = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


class TestEvidenceError(ValueError):
    pass


def _read(path) -> str:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise TestEvidenceError("test evidence must be a regular file")
    try:
        if source.stat().st_size > MAX_BYTES:
            raise TestEvidenceError("test evidence is too large")
        return source.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise TestEvidenceError("test evidence is unreadable") from exc


def _status(value) -> str | None:
    text = str(value or "").strip().lower()
    if text in ("passed", "pass", "ok", "success"):
        return "passed"
    if text in ("failed", "fail", "failure", "error", "errored"):
        return "failed"
    if text in ("skipped", "skip", "xfail", "pending", "disabled"):
        return "skipped"
    return None


def parse_junit_xml(text: str) -> dict:
    if DANGEROUS_XML.search(text):
        raise TestEvidenceError("JUnit XML must not contain DOCTYPE or ENTITY")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise TestEvidenceError("JUnit XML is invalid") from exc
    cases = []
    if root.tag.endswith("testsuite") or root.tag.endswith("testsuites"):
        elements = root.iter("testcase")
    else:
        raise TestEvidenceError("JUnit XML has no testcase elements")
    for element in elements:
        if len(cases) >= MAX_TESTS:
            raise TestEvidenceError("test evidence has too many cases")
        name = (element.get("classname") or element.get("name") or "").strip()
        case_name = (element.get("name") or "").strip()
        if name and case_name and name != case_name:
            name = f"{name}.{case_name}"
        elif case_name:
            name = case_name
        if not name:
            continue
        if not isinstance(name, str) or len(name) > MAX_NAME:
            raise TestEvidenceError("test case name is invalid")
        status = "passed"
        failure = element.find("failure")
        error = element.find("error")
        skipped = element.find("skipped")
        if failure is not None or error is not None:
            status = "failed"
        elif skipped is not None:
            status = "skipped"
        cases.append({"name": name, "status": status})
    return _summary("junit", cases)


def parse_pytest_json(text: str) -> dict:
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise TestEvidenceError("pytest JSON is invalid") from exc
    if isinstance(payload, dict):
        raw = payload.get("tests")
        if not isinstance(raw, list):
            raw = []
        if not raw and isinstance(payload.get("testcases"), list):
            raw = payload["testcases"]
    elif isinstance(payload, list):
        raw = payload
    else:
        raise TestEvidenceError("pytest JSON has no tests array")
    if len(raw) > MAX_TESTS:
        raise TestEvidenceError("test evidence has too many cases")
    cases = []
    for item in raw:
        if not isinstance(item, dict):
            raise TestEvidenceError("pytest test entry is invalid")
        name = (item.get("nodeid") or item.get("name")
                or item.get("test_id") or "").strip()
        if not isinstance(name, str) or not name or len(name) > MAX_NAME:
            raise TestEvidenceError("pytest test name is invalid")
        status = _status(item.get("outcome") or item.get("status")
                         or item.get("result"))
        if status is None:
            raise TestEvidenceError("pytest test status is invalid")
        cases.append({"name": name, "status": status})
    return _summary("pytest-json", cases)


def _summary(fmt: str, cases: list[dict]) -> dict:
    totals = {"passed": 0, "failed": 0, "skipped": 0}
    for item in cases:
        totals[item["status"]] += 1
    return {
        "format": fmt,
        "tests": cases,
        "totals": {**totals, "total": len(cases)},
        "passed_names": [item["name"] for item in cases
                         if item["status"] == "passed"],
        "failed_names": [item["name"] for item in cases
                         if item["status"] == "failed"],
        "skipped_names": [item["name"] for item in cases
                          if item["status"] == "skipped"],
    }


def parse_file(path, fmt: str | None = None) -> dict:
    text = _read(path)
    if fmt is None:
        stripped = text.lstrip()
        fmt = "junit" if stripped.startswith("<") else "pytest-json"
    if fmt not in ("junit", "pytest-json"):
        raise TestEvidenceError("unsupported test evidence format")
    return parse_junit_xml(text) if fmt == "junit" else parse_pytest_json(text)
