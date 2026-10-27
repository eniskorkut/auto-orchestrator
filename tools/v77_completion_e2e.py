#!/usr/bin/env python3
"""V7.7 Verified Completion E2E.

Builds an approved JWT+refresh contract, demonstrates that access-token-only
code cannot satisfy the behavioral criteria, repairs the implementation to emit
the required test IDs, runs the real unittest command, and optionally asks a
read-only Codex reviewer to attest the final result.

The report contains no prompts, raw source or tested values.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from tools.completion_engine import (
        evaluate_completion,
        parse_passed_test_signals,
        parse_test_signals,
    )
    from tools.grill_me import GrillMeEngine
    from tools.project_identity import ensure_project_identity
    from tools.requirements_contract import load_current
except ModuleNotFoundError:
    from completion_engine import (  # type: ignore
        evaluate_completion,
        parse_passed_test_signals,
        parse_test_signals,
    )
    from grill_me import GrillMeEngine  # type: ignore
    from project_identity import ensure_project_identity  # type: ignore
    from requirements_contract import load_current  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
BAD_CODE = """\
import hashlib

def create_token(subject):
    return hashlib.sha256(subject.encode()).hexdigest()

def verify_token(token):
    return token
"""
BAD_TEST = """\
import unittest
from auth_jwt import create_token, verify_token

class AuthTests(unittest.TestCase):
    def test_jwt_access_issuance(self):
        token = create_token("user-1")
        self.assertEqual(verify_token(token), token)
"""
GOOD_CODE = """\
import base64
import hashlib
import hmac
import json
import time

SECRET = b"test-only-secret"

def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

def _sign(part):
    return _b64(hmac.new(SECRET, part.encode(), hashlib.sha256).digest())

def create_token(subject, token_type="access", ttl=None):
    if ttl is None:
        ttl = 604800 if token_type == "refresh" else 900
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"},
                             separators=(",", ":")).encode())
    payload = _b64(json.dumps(
        {"sub": subject, "type": token_type, "exp": time.time() + ttl},
        separators=(",", ":")).encode())
    body = header + "." + payload
    return body + "." + _sign(body)

def verify_token(token, expected_type=None):
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid token format")
    header, payload, sig = parts
    body = header + "." + payload
    if not hmac.compare_digest(sig, _sign(body)):
        raise ValueError("invalid signature")
    claims = json.loads(_unb64(payload))
    if claims["exp"] <= time.time():
        raise ValueError("expired")
    if expected_type is not None and claims["type"] != expected_type:
        raise ValueError("invalid token type")
    return claims["sub"]

def refresh_access_token(refresh_token):
    subject = verify_token(refresh_token, expected_type="refresh")
    return create_token(subject, "access")
"""
GOOD_TEST = """\
import unittest
from auth_jwt import create_token, verify_token, refresh_access_token

class AuthTests(unittest.TestCase):
    def test_jwt_access_issuance(self):
        self.assertEqual(verify_token(create_token("user-1")), "user-1")

    def test_jwt_refresh_issuance(self):
        refresh = create_token("user-1", "refresh", ttl=600)
        self.assertEqual(verify_token(refresh_access_token(refresh)), "user-1")

    def test_invalid_refresh_rejected(self):
        with self.assertRaises(ValueError):
            verify_token("not-a-token")
        with self.assertRaises(ValueError):
            refresh_access_token(create_token("user-1", "access"))

    def test_jwt_expiry_enforced(self):
        with self.assertRaises(ValueError):
            verify_token(create_token("user-1", ttl=-1))
"""


def _run_tests(root):
    command = "python3 -m unittest -v test_auth_jwt"
    process = subprocess.run(
        command.split(), cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60)
    output = (process.stdout or "") + (process.stderr or "")
    return {
        "command": command,
        "exit_code": process.returncode,
        "signals": parse_passed_test_signals(output),
        "output": output[-4000:],
        "source": "runner",
    }


def _codex_review(root, contract):
    try:
        from tools.managed_exec import CodexAdapter
    except ModuleNotFoundError:
        from managed_exec import CodexAdapter  # type: ignore
    prompt = (
        "Review auth_jwt.py and test_auth_jwt.py against these criteria: "
        + "; ".join(item["text"] for item in contract["acceptance_criteria"])
        + '. Return only JSON {"verdict":"pass|fail","findings":[],"summary":"..."}. '
          "Do not modify files.")
    adapter = CodexAdapter(root, model="gpt-6.1-sol", sandbox="read-only",
                           approval_policy="never")
    handle = None
    try:
        handle = adapter.start(task_id="completion-review", prompt=prompt,
                               cwd=root, title="completion review",
                               model="gpt-6.1-sol")
        state = adapter.poll(handle, timeout=0)
        deadline = time.monotonic() + 240
        while state.get("status") == "running" and time.monotonic() < deadline:
            time.sleep(0.5)
            state = adapter.poll(handle, timeout=0)
        text = state.get("text") or ""
        value = {}
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
                if isinstance(value, dict):
                    break
            except ValueError:
                value = {}
                continue
        return {
            "role": "reviewer", "verdict": value.get("verdict"),
            "independent": True, "summary": str(value.get("summary") or "")[:300],
            "findings": [str(x)[:200] for x in value.get("findings", [])][:10],
            "status": state.get("status"), "tokens": state.get("tokens"),
            "parsed": bool(value),
        }
    finally:
        if handle is not None:
            adapter.close(handle)


def _sanitize(value, roots):
    text = json.dumps(value, ensure_ascii=False)
    for root in roots:
        text = text.replace(str(root), "$PROJECT")
    text = text.replace(str(Path.home()), "$HOME")
    for temp in (tempfile.gettempdir(), "/private" + tempfile.gettempdir()):
        text = text.replace(str(temp), "$TMPDIR")
    return json.loads(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--codex-review", action="store_true")
    args = parser.parse_args(argv)
    base = Path(tempfile.mkdtemp(prefix="v77-completion-"))
    report = {"schema_version": 1, "kind": "v77-completion-e2e"}
    roots = [base]
    try:
        subprocess.run(["git", "-C", str(base), "init"], check=True,
                       capture_output=True)
        ensure_project_identity(base)
        with GrillMeEngine(base) as engine:
            started = engine.start(
                "Kullanıcı giriş sistemini JWT ve refresh token ile geliştir",
                origin_platform="codex", mode="auto")
            first = started["question"]
            nxt = engine.answer(first["question_id"], option="jwt")["next"]
            engine.answer(nxt["question_id"], option="refresh")
            engine.approve()
        contract = load_current(base)
        auth = next(item for item in contract["acceptance_criteria"]
                    if item.get("critical"))
        required = [case["test_id"] for case in auth.get("test_cases", [])]
        report["contract"] = {
            "id": contract["contract_id"], "version": contract["contract_version"],
            "schema_version": contract.get("schema_version"),
            "critical_test_ids": required,
        }

        (base / "auth_jwt.py").write_text(BAD_CODE, encoding="utf-8")
        (base / "test_auth_jwt.py").write_text(BAD_TEST, encoding="utf-8")
        bad_tests = _run_tests(base)
        missing = evaluate_completion(
            base, contract, changed_files=["auth_jwt.py", "test_auth_jwt.py"],
            commits=[], test_results=[bad_tests], reviews=[],
            check_commit=False)
        report["wrong_implementation"] = {
            "exit_code": bad_tests["exit_code"],
            "signals": bad_tests["signals"],
            "status": missing["status"],
            "verified_completion": missing["verified_completion"],
            "critical_failures": len(missing["critical_failures"]),
            "missing_test_ids": sorted({
                item for criterion in missing["criteria"]
                for item in criterion["missing_test_ids"]}),
        }

        (base / "auth_jwt.py").write_text(GOOD_CODE, encoding="utf-8")
        (base / "test_auth_jwt.py").write_text(GOOD_TEST, encoding="utf-8")
        good_tests = _run_tests(base)
        review = None
        if args.codex_review:
            review = _codex_review(base, contract)
        reviews = ([review] if review and review.get("verdict") == "pass" else [])
        repaired = evaluate_completion(
            base, contract, changed_files=["auth_jwt.py", "test_auth_jwt.py"],
            commits=[], test_results=[good_tests], reviews=reviews,
            check_commit=False)
        report["repair"] = {
            "exit_code": good_tests["exit_code"],
            "signals": good_tests["signals"],
            "review": review,
            "status": repaired["status"],
            "verified_completion": repaired["verified_completion"],
            "fully_verified": repaired["fully_verified"],
            "criteria": [{
                "id": item["criterion_id"], "state": item["state"],
                "behavioral": item["behavioral_status"],
                "conformance": item["conformance_status"],
                "missing": item["missing_test_ids"],
            } for item in repaired["criteria"]],
            "levels": repaired["levels"],
        }
    finally:
        pass
    text = _sanitize(report, roots)
    text = json.dumps(text, indent=2, ensure_ascii=False) + "\n"
    print(text, end="")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
