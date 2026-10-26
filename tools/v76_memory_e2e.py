#!/usr/bin/env python3
"""Deterministic V7.6 cross-IDE memory and Grill-Me E2E harness.

This harness exercises the same CLI in separate processes for Codex and
OpenCode. It does not claim that Codex/Cursor/Antigravity code execution was
used for the memory scenarios; those are real CLI state tests. A separate V7.5
harness records real Codex app-server execution.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from tools import teamwork_chat
    from tools.project_identity import ensure_project_identity
    from tools.project_memory import ProjectMemory
    from tools.run_owner import project_writer_lease
except ModuleNotFoundError:
    import teamwork_chat  # type: ignore
    from project_identity import ensure_project_identity  # type: ignore
    from project_memory import ProjectMemory  # type: ignore
    from run_owner import project_writer_lease  # type: ignore

ROOT = Path(__file__).resolve().parents[1]


def _run(root, *args, platform="codex", expect=0):
    command = [sys.executable, str(ROOT / "tools" / "universal_teamwork.py"),
               "--root", str(root), "--platform", platform, *args]
    process = subprocess.run(command, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=180,
                             cwd=str(root))
    if process.returncode != expect:
        raise RuntimeError(process.stderr or process.stdout)
    return json.loads(process.stdout) if process.stdout.strip() else {}


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True)


def _request_file(root, text):
    requests = root / ".auto-orchestrator" / "teamwork" / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    path = requests / "v76-e2e.md"
    path.write_text(text, encoding="utf-8")
    return str(path.relative_to(root))


def _interview(root, text, platform="codex", mode="auto"):
    relative = _request_file(root, text)
    return _run(root, "grill", "start", "--grill-mode", mode,
                "--request-file", relative, platform=platform)


def _answer(root, question, option, platform="codex"):
    return _run(root, "grill", "answer", "--question-id",
                question["question_id"], "--option", option,
                platform=platform)


def _summary(root, interview_id, platform="codex"):
    return _run(root, "grill", "summary", "--interview-id", interview_id,
                platform=platform)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)
    base = Path(tempfile.mkdtemp(prefix="v76-e2e-"))
    report = {"schema_version": 1, "kind": "v76-memory-grill-e2e"}
    try:
        # A/B: Codex starts an interview, OpenCode resumes it.
        project = base / "auth-project"
        project.mkdir()
        _git(project, "init")
        _git(project, "config", "user.email", "e2e@example.invalid")
        _git(project, "config", "user.name", "E2E")
        (project / "README.md").write_text("# sample project\n")
        _git(project, "add", ".")
        _git(project, "commit", "-m", "initial")
        started = _interview(project, "Kullanıcı giriş sistemini geliştir")
        first = started["question"]
        answered = _answer(project, first, "jwt")
        second = answered["next"]
        codex_summary = _summary(project, started["interview"]["interview_id"])
        resumed = _run(project, "grill", "resume", platform="opencode")
        resumed_question = resumed.get("question")
        _answer(project, resumed_question, "refresh", platform="opencode")
        final_summary = _summary(project, started["interview"]["interview_id"],
                                 platform="opencode")
        approved = _run(project, "grill", "approve", "--interview-id",
                        started["interview"]["interview_id"], platform="opencode")
        context = _run(project, "context", platform="opencode")
        report["cross_ide_grill"] = {
            "codex_first_question": first["question_key"],
            "codex_second_question": second["question_key"],
            "codex_ready": codex_summary["ready"],
            "opencode_resumed_question": resumed_question["question_key"]
            if resumed_question else None,
            "opencode_ready_after_resume": final_summary["ready"],
            "opencode_approved": approved.get("approved", False),
            "opencode_confirmed_decisions": len(context["decisions"]),
            "project_id_same": context["identity"]["project_id"]
            == ensure_project_identity(project)["project_id"],
        }

        # C: assumption guard for an ambiguous multi-user sharing request.
        guard = base / "guard-project"
        guard.mkdir()
        (guard / "README.md").write_text("# empty\n")
        _git(guard, "init")
        _git(guard, "config", "user.email", "e2e@example.invalid")
        _git(guard, "config", "user.name", "E2E")
        _git(guard, "add", ".")
        _git(guard, "commit", "-m", "initial")
        guarded = _interview(guard, "Çok kullanıcılı bir dosya paylaşım sistemi geliştir")
        with ProjectMemory(guard) as memory:
            decisions = memory.list_decisions()
            plans = list((guard / ".auto-orchestrator" / "plans").glob("*.json"))
        report["assumption_guard"] = {
            "question": guarded["question"]["question_key"],
            "confirmed_decisions_before_answer": sum(
                1 for item in decisions if item["status"] == "confirmed"),
            "teamwork_plan_created": bool(plans),
        }

        # D: code-derived facts suppress unnecessary questions.
        facts_project = base / "facts-project"
        (facts_project / "backend").mkdir(parents=True)
        (facts_project / "backend" / "auth.py").write_text(
            "import jwt\n# postgresql\n", encoding="utf-8")
        facts = _interview(facts_project, "Kullanıcı giriş sistemini geliştir")
        with ProjectMemory(facts_project) as memory:
            derived = [item for item in memory.list_decisions()
                       if item["status"] == "code_derived"]
        report["code_derived"] = {
            "question": facts["question"],
            "status": facts["interview"]["status"],
            "code_derived_decisions": len(derived),
        }

        # E: normal git observation becomes memory without intent invention.
        _git(project, "status")
        (project / "README.md").write_text("# sample project\n\nsmall change\n")
        _git(project, "add", ".")
        _git(project, "commit", "-m", "small normal change")
        captured = _run(project, "memory", "capture", platform="opencode")
        observed_context = _run(project, "context", platform="opencode")
        with ProjectMemory(project) as memory:
            observations = memory.conn.execute(
                "SELECT COUNT(*) FROM observations WHERE project_id=?",
                (memory.project_id,)).fetchone()[0]
        report["normal_coding"] = {
            "captured": captured.get("observation") is not None,
            "observation_rows": observations,
            "context_has_project_id": observed_context["identity"]["project_id"]
            == ensure_project_identity(project)["project_id"],
        }

        # G: project writer lease rejects a second writer.
        relative = _request_file(project, "Küçük bir değişiklik yap")
        with project_writer_lease(project):
            rejected = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "universal_teamwork.py"),
                 "--root", str(project), "--platform", "cursor", "start",
                 "--request-file", relative],
                capture_output=True, text=True, encoding="utf-8",
                cwd=str(project))
        report["concurrent_writer"] = {
            "exit_code": rejected.returncode,
            "rejected": rejected.returncode != 0,
            "message": (rejected.stderr or rejected.stdout)[-300:],
        }

        # H: copy/relocation keeps portable identity; local instance changes.
        relocated = base / "relocated-project"
        shutil.copytree(project, relocated)
        original_identity = ensure_project_identity(project)
        moved_identity = ensure_project_identity(relocated)
        export_path = base / "memory-export.json"
        _run(project, "memory", "export", "--path", str(export_path))
        imported = _run(relocated, "memory", "import", "--path", str(export_path))
        report["relocation"] = {
            "project_id_same": original_identity["project_id"]
            == moved_identity["project_id"],
            "instance_id_changed": original_identity["instance_id"]
            != moved_identity["instance_id"],
            "import_preview_conflicts": len(imported.get("conflicts", [])),
        }
    finally:
        pass
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    print(text, end="")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
