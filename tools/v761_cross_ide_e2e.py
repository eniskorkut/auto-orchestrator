#!/usr/bin/env python3
"""Real Codex -> OpenCode feature-development E2E.

This harness uses isolated temporary repositories, the locally installed Codex
app-server (workspace-write, approvalPolicy never) and the OpenCode managed
adapter. It records only sanitized metadata: no prompts, absolute paths, thread
ids, session ids or raw agent text.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from tools.grill_me import GrillMeEngine
    from tools.project_identity import ensure_project_identity
    from tools.project_memory import ProjectMemory
    from tools.requirements_contract import load_current, verify_contract
    from tools.teamwork_runner import TeamworkRunner
except ModuleNotFoundError:  # direct execution from tools/
    from grill_me import GrillMeEngine  # type: ignore
    from project_identity import ensure_project_identity  # type: ignore
    from project_memory import ProjectMemory  # type: ignore
    from requirements_contract import load_current, verify_contract  # type: ignore
    from teamwork_runner import TeamworkRunner  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
CODEX_MODEL = os.environ.get("AO_E2E_CODEX_MODEL", "gpt-6.1-sol")
OPENCODE_MODEL = os.environ.get(
    "AO_E2E_OPENCODE_MODEL", "opencode-go/deepseek-v4.1-flash")


def _run(root, *args, check=True):
    process = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "universal_teamwork.py"),
         "--root", str(root), *args],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)
    if check and process.returncode != 0:
        raise RuntimeError(process.stderr or process.stdout)
    return process, (json.loads(process.stdout)
                     if process.stdout.strip() else {})


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True)


def _init(root):
    _git(root, "init")
    _git(root, "config", "user.email", "e2e@example.invalid")
    _git(root, "config", "user.name", "E2E")
    (root / ".gitignore").write_text(
        ".auto-orchestrator/*\n!.auto-orchestrator/project.json\n",
        encoding="utf-8")
    (root / "README.md").write_text("# auth sample\n", encoding="utf-8")
    ensure_project_identity(root)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")


def _plan(run_id, contract, platform, provider, model, build_objective,
          build_scope, tester_objective):
    common = {
        "milestone": "M1", "read_scope": [], "timeout_seconds": 420,
        "max_retries": 0, "verify_commands": ["python3 -m unittest discover -v"],
        "department": "backend", "provider": provider, "model": model,
    }
    build = {
        **common, "id": "build", "role": "implementer", "phase": "build",
        "objective": build_objective, "acceptance": ["feature and tests pass"],
        "write_scope": list(build_scope), "depends_on": [],
    }
    if provider == "codex":
        build.update({"sandbox": "workspace-write",
                      "approval_policy": "never"})
    tester = {
        **common, "id": "verify", "role": "tester", "phase": "verify",
        "objective": tester_objective, "acceptance": ["tests pass"],
        "write_scope": [], "depends_on": ["build"],
    }
    reviewer = {
        **common, "id": "review", "role": "reviewer", "phase": "verify",
        "objective": ("Independently review the implementation against the approved "
                      "contract; run tests and return JSON verdict pass/fail."),
        "acceptance": ["independent review verdict recorded"],
        "write_scope": [], "depends_on": ["verify"],
    }
    if provider == "codex":
        tester.update({"sandbox": "read-only", "approval_policy": "never"})
        reviewer.update({"sandbox": "read-only", "approval_policy": "never"})
    return {
        "version": 1, "run_id": run_id, "max_parallel": 1,
        "pattern": "review_and_repair", "work_mode": "balanced",
        "origin_platform": platform,
        "requirements_contract": contract,
        "milestones": [{
            "id": "M1", "objective": "Deliver the verified feature",
            "depends_on": [], "acceptance": ["feature and tests pass"],
            "verification_policy": {"require_roles": ["tester", "reviewer"],
                                    "max_repair": 0},
        }],
        "tasks": [build, tester, reviewer],
    }


def _brief(summary):
    result = {}
    for milestone in summary.get("milestones", []):
        result[milestone["id"]] = {
            "status": milestone["status"],
            "tasks": [{
                "id": task["id"], "state": task["state"],
                "changed_files": (task.get("result") or {}).get("changed_files", []),
                "tests": [{
                    "name": item.get("name"), "status": item.get("status"),
                    "exit_code": item.get("exit_code"),
                } for item in (task.get("result") or {}).get("tests", [])],
            } for task in milestone.get("tasks", [])],
        }
    sessions = summary.get("managed_sessions") or []
    tokens = sessions[0].get("tokens") if sessions else None
    return {
        "status": summary.get("status"),
        "milestones": result,
        "tokens": tokens,
        "requirements_contract": summary.get("requirements_contract"),
        "requirements": summary.get("requirements"),
        "completion": summary.get("requirements"),
        "error": summary.get("error"),
    }


def _sanitize(value, roots):
    text = json.dumps(value, ensure_ascii=False)
    for root in roots:
        text = text.replace(str(root), "$PROJECT")
        try:
            text = text.replace(str(Path(root).resolve()), "$PROJECT")
        except OSError:
            pass
    text = text.replace(str(Path.home()), "$HOME")
    for temp in (tempfile.gettempdir(), "/private" + tempfile.gettempdir()):
        text = text.replace(str(temp), "$TMPDIR")
    return json.loads(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)
    base = Path(tempfile.mkdtemp(prefix="v761-cross-ide-"))
    project = base / "auth-project"
    project.mkdir()
    report = {"schema_version": 1, "kind": "v761-cross-ide-e2e",
              "codex_model": CODEX_MODEL,
              "opencode_model": OPENCODE_MODEL}
    roots = [base, project]
    try:
        _init(project)
        with GrillMeEngine(project) as engine:
            started = engine.start(
                "Kullanıcı giriş sistemi için JWT tabanlı oturum ekle",
                origin_platform="codex", mode="auto")
            first = started["question"]
            next_question = engine.answer(
                first["question_id"], option="jwt")["next"]
            engine.answer(next_question["question_id"], option="refresh")
            engine.approve()
            interview_id = started["interview"]["interview_id"]
        contract = load_current(project)
        if contract is None:
            raise RuntimeError("approved interview did not create a contract")
        required_tests = [
            case["test_id"]
            for criterion in contract["acceptance_criteria"]
            if criterion.get("critical")
            for case in criterion.get("test_cases", [])
        ]
        report["contract"] = {
            "contract_id": contract["contract_id"],
            "version": contract["contract_version"],
            "schema_version": contract.get("schema_version"),
            "decisions": [item["topic"] for item in contract["confirmed_decisions"]],
            "required_tests": required_tests,
        }

        codex_plan = _plan(
            "v761-codex-auth", contract, "codex", "codex", CODEX_MODEL,
            "Create auth_jwt.py implementing a standard 3-segment JWT-like token "
            "signed with hmac and base64: create_token(subject, token_type='access'), "
            "verify_token(token, expected_type=None), and refresh_access_token. "
            "Use a short access TTL (900s) and a longer refresh TTL (7 days); "
            "refresh must reject access tokens and invalid signatures. Add "
            "test_auth_jwt.py with exactly these unittest method names: "
            + ", ".join(required_tests) + ". Run python3 -m unittest discover -v. "
            "Modify only those two files.",
            ["auth_jwt.py", "test_auth_jwt.py"],
            "Run python3 -m unittest discover -v and inspect both files. End with JSON "
            '{"verdict":"pass|fail","findings":[],"summary":"..."}. Do not modify files.')
        runner = TeamworkRunner(project, codex_plan, model=CODEX_MODEL,
                                max_seconds=600)
        try:
            codex_summary = runner.run()
        finally:
            runner.close()
        report["codex"] = _sanitize(_brief(codex_summary), roots)
        report["codex"]["auth_file"] = (project / "auth_jwt.py").is_file()
        report["codex"]["handoff"] = bool(
            (project / ".auto-orchestrator" / "handoffs").glob("*.json"))

        _, context = _run(project, "--platform", "opencode", "context")
        with ProjectMemory(project) as memory:
            decisions = memory.list_decisions(status="confirmed")
            meta = memory.get_meta()
        report["opencode_context"] = _sanitize({
            "project_id": context["identity"]["project_id"],
            "same_project": context["identity"]["project_id"]
            == ensure_project_identity(project)["project_id"],
            "last_platform": meta.get("last_platform"),
            "confirmed_topics": [item["topic"] for item in decisions],
            "blocking": len(context.get("blocking") or []),
            "handoff": bool(context.get("handoff")),
        }, roots)

        opencode_plan = _plan(
            "v761-opencode-reset", contract, "opencode", "opencode",
            OPENCODE_MODEL,
            "Read auth_jwt.py and the project memory. Add password_reset.py "
            "with create_reset_token(subject) using auth_jwt.create_token with "
            "a reset scope, and verify_reset_token(token) returning the subject. "
            "Add test_password_reset.py. Run python3 -m unittest discover -v. "
            "Modify only those two files.",
            ["password_reset.py", "test_password_reset.py"],
            "Run python3 -m unittest discover -v and inspect the new files. End with JSON "
            '{"verdict":"pass|fail","findings":[],"summary":"..."}. Do not modify files.')
        runner = TeamworkRunner(project, opencode_plan, model=OPENCODE_MODEL,
                                max_seconds=600)
        try:
            opencode_summary = runner.run()
        finally:
            runner.close()
        report["opencode"] = _sanitize(_brief(opencode_summary), roots)
        report["opencode"]["reset_file"] = (project / "password_reset.py").is_file()
        with ProjectMemory(project) as memory:
            report["final"] = _sanitize({
                "project_id": memory.project_id,
                "last_platform": memory.get_meta().get("last_platform"),
                "confirmed_topics": [item["topic"] for item in
                                     memory.list_decisions(status="confirmed")],
                "handoffs": len(memory.list_handoffs()),
            }, roots)
            report["final_contract"] = verify_contract(project, contract)
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
