#!/usr/bin/env python3
"""Deterministic V7.6.1 assumption-guard and contract E2E.

A: an ambiguous multi-user sharing request opens an interview and writes no plan.
B: an approved JWT contract rejects cookie-only code and accepts code evidence.
D: a Codex-started interview is resumed and approved from OpenCode.

The harness uses the real local CLI and SQLite ledger; it does not call an LLM.
Evidence is sanitized (no prompts, absolute paths or session ids).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from tools.project_identity import ensure_project_identity
    from tools.project_memory import ProjectMemory
    from tools.requirements_contract import load_current, verify_contract
except ModuleNotFoundError:
    from project_identity import ensure_project_identity  # type: ignore
    from project_memory import ProjectMemory  # type: ignore
    from requirements_contract import load_current, verify_contract  # type: ignore

ROOT = Path(__file__).resolve().parents[1]


def _run(root, platform, *args):
    process = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "universal_teamwork.py"),
         "--root", str(root), "--platform", platform, *args],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)
    data = json.loads(process.stdout) if process.stdout.strip() else {}
    return process.returncode, data, process.stderr


def _init(root):
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init"], check=True,
                   capture_output=True)
    (root / "README.md").write_text("# sample\n", encoding="utf-8")
    ensure_project_identity(root)


def _sanitize(value, roots):
    text = json.dumps(value, ensure_ascii=False)
    for root in roots:
        text = text.replace(str(root), "$PROJECT")
        text = text.replace(str(Path(root).resolve()), "$PROJECT")
    text = text.replace(str(Path.home()), "$HOME")
    for temp in (tempfile.gettempdir(), "/private" + tempfile.gettempdir()):
        text = text.replace(str(temp), "$TMPDIR")
    return json.loads(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)
    base = Path(tempfile.mkdtemp(prefix="v761-guard-e2e-"))
    report = {"schema_version": 1, "kind": "v761-guard-contract-e2e"}
    roots = [base]
    try:
        # A: ambiguous sharing must not start code.
        guard = base / "guard"
        _init(guard)
        rc, payload, err = _run(
            guard, "opencode", "start", "--request",
            "Çok kullanıcılı bir dosya paylaşım sistemi geliştir",
            "--max-seconds", "5")
        report["assumption_guard"] = {
            "exit_code": rc,
            "status": payload.get("status"),
            "question": (payload.get("question") or {}).get("question_key"),
            "reasons": payload.get("reasons", []),
            "plan_created": (guard / ".auto-orchestrator" / "plans").exists(),
            "active_created": (guard / ".auto-orchestrator" / "teamwork"
                               / "active.json").exists(),
        }

        # B: approved JWT contract; cookie-only code must fail.
        auth = base / "auth"
        _init(auth)
        rc, started, err = _run(
            auth, "codex", "grill", "start", "--request",
            "Kullanıcı giriş sistemini JWT ile geliştir", "--grill-mode", "auto")
        first = started["question"]
        rc, answered, err = _run(
            auth, "codex", "grill", "answer", "--question-id",
            first["question_id"], "--option", "jwt")
        second = answered["next"]
        _run(auth, "codex", "grill", "answer", "--question-id",
             second["question_id"], "--option", "refresh")
        rc, approved, err = _run(
            auth, "codex", "grill", "approve", "--interview-id",
            started["interview"]["interview_id"])
        contract = load_current(auth)
        (auth / "auth.py").write_text(
            "SESSION_COOKIE = 'sid'\n# session cookie authentication\n",
            encoding="utf-8")
        rejected = verify_contract(auth, contract)
        (auth / "auth_jwt.py").write_text(
            "JWT_ALGORITHM = 'HS256'\n# JWT access token\n", encoding="utf-8")
        accepted = verify_contract(auth, contract)
        report["contract_verification"] = {
            "approved": bool(approved.get("approved")),
            "contract_id": contract["contract_id"],
            "version": contract["contract_version"],
            "cookie_code": {
                "status": rejected["status"], "ok": rejected["ok"],
                "failed_checks": [item["criterion_id"] for item in rejected["checks"]
                                  if item["status"] == "failed"],
            },
            "jwt_code": {
                "status": accepted["status"], "ok": accepted["ok"],
                "passed_checks": [item["criterion_id"] for item in accepted["checks"]
                                  if item["status"] == "passed"],
                "not_verified": accepted["not_verified"],
            },
        }

        # D: Codex starts, OpenCode resumes the same persistent interview.
        resume = base / "resume"
        _init(resume)
        rc, started, err = _run(
            resume, "codex", "grill", "start", "--request",
            "Kullanıcı giriş sistemini geliştir", "--grill-mode", "auto")
        _run(resume, "codex", "grill", "answer", "--question-id",
             started["question"]["question_id"], "--option", "jwt")
        rc, resumed, err = _run(resume, "opencode", "grill", "resume")
        rc, answered, err = _run(
            resume, "opencode", "grill", "answer", "--question-id",
            resumed["question"]["question_id"], "--option", "refresh")
        rc, approved, err = _run(
            resume, "opencode", "grill", "approve", "--interview-id",
            started["interview"]["interview_id"])
        with ProjectMemory(resume) as memory:
            decisions = memory.list_decisions(status="confirmed")
        report["cross_ide_resume"] = {
            "origin_platform": resumed["interview"]["origin_platform"],
            "resumed_question": resumed["question"]["question_key"],
            "approved": bool(approved.get("approved")),
            "confirmed_topics": [item["topic"] for item in decisions],
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
