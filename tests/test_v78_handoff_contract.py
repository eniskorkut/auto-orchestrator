from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import ide_handoff as handoff
from tools.project_identity import ensure_project_identity
from tools.project_memory import ProjectMemory
from tools.requirements_contract import contract_digest
from tools.run_owner import atomic_write_json, project_lock
from tools.teamwork import TeamworkEngine


def _git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        check=check, timeout=30)


def _make_repository(root: Path):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "handoff@example.invalid")
    _git(root, "config", "user.name", "Handoff Test")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-q", "-m", "initial")


def _commit_all(root: Path, message: str):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def _minimal_plan():
    return {
        "run_id": "run-1", "max_parallel": 1, "work_mode": "fast",
        "pattern": "iterative_coding",
        "milestones": [{
            "id": "M1", "objective": "Ship the change", "depends_on": [],
            "acceptance": ["done"],
            "verification_policy": {"require_roles": [], "max_repair": 1},
        }],
        "tasks": [{
            "id": "build", "role": "implementer", "milestone": "M1",
            "phase": "build", "objective": "Build it", "acceptance": ["done"],
            "read_scope": [], "write_scope": ["src/"], "depends_on": [],
            "timeout_seconds": 60, "max_retries": 1, "verify_commands": [],
            "department": "backend", "provider": "opencode",
        }],
    }


class HandoffContractTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        _make_repository(self.root)

    def identity(self):
        return ensure_project_identity(self.root)

    def activate(self):
        result = handoff.activate(
            self.root, "opencode", session_id="source")
        self.assertTrue(result["ok"], result)
        return result

    def prepare(self, **kwargs):
        active = self.activate()
        result = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            next_action="continue", idle_confirmation="user_confirmed", **kwargs)
        self.assertTrue(result["ok"], result)
        return active, result

    def release(self, active, prepared, **kwargs):
        result = handoff.release(
            self.root, active["owner_token"], prepared["fence"],
            prepared["handoff"]["handoff_id"],
            idle_confirmation="user_confirmed", **kwargs)
        self.assertTrue(result["ok"], result)
        return result

    def snapshot(self, prepared):
        return json.loads(
            (self.root / prepared["handoff"]["path"]).read_text(
                encoding="utf-8"))

    def ownership_bytes(self):
        return handoff._ownership_path(self.root).read_bytes()

    def install_interview(self):
        identity = self.identity()
        interview_id = "iv_" + hashlib.sha256(b"interview").hexdigest()[:16]
        question_id = "q_scope"
        decision_id = "d_" + hashlib.sha256(b"decision").hexdigest()[:16]
        requirement_id = "req_blocking"
        now = __import__("time").time()
        with ProjectMemory(self.root, identity=identity) as memory:
            memory.conn.execute(
                "INSERT INTO interviews(interview_id,project_id,"
                "origin_platform,request_hash,request_summary,mode,status,"
                "approved_at,created_at,updated_at,schema_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (interview_id, identity["project_id"], "opencode",
                 hashlib.sha256(b"request").hexdigest(), "Ship it", "auto",
                 "approved", now, now, now, 2))
            memory.conn.execute(
                "INSERT INTO interview_questions(interview_id,question_id,"
                "ordinal,question_key,question,options,recommended_option,"
                "selected_option,answer_text,answer_source,status,depends_on,"
                "created_at,answered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (interview_id, question_id, 1, "scope", "Scope?",
                 "[]", None, None, None, None, "open", "[]", now, None))
            memory.conn.execute(
                "INSERT INTO decisions(decision_id,project_id,topic,decision,"
                "rationale,alternatives,source,source_ref,confirmed_by,"
                "confirmed_at,status,supersedes,affected_tasks,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, identity["project_id"], "scope", "small",
                 "bounded", "[]", "user", None, "user", now, "confirmed",
                 None, "[]", now))
            memory.conn.execute(
                "INSERT INTO requirements(requirement_id,project_id,text,"
                "category,status,source,source_ref,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (requirement_id, identity["project_id"], "Needs approval",
                 "scope", "unresolved_blocking", "user", None, now, now))
            memory.conn.commit()
        return interview_id, question_id, decision_id, requirement_id

    def install_contract(self, interview_id=None):
        identity = self.identity()
        contract_id = "rc_" + hashlib.sha256(b"contract").hexdigest()[:16]
        contract = {
            "contract_id": contract_id,
            "contract_version": 1,
            "status": "confirmed",
            "project_id": identity["project_id"],
            "interview_id": interview_id,
            "goal": "Ship the change",
            "constraints": [],
            "acceptance_criteria": [],
        }
        sha = contract_digest(contract)
        contract["sha256"] = sha
        directory = self.root / ".auto-orchestrator" / "contracts"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{contract_id}-v1.json"
        path.write_text(json.dumps(contract), encoding="utf-8")
        atomic_write_json(directory / "current.json", {
            "contract_id": contract_id,
            "contract_version": 1,
            "sha256": sha,
            "path": path.name,
        })
        return contract_id, sha

    def install_run_state(self):
        engine = TeamworkEngine(self.root)
        try:
            engine.init(_minimal_plan())
        finally:
            engine.close()
        atomic_write_json(
            self.root / ".auto-orchestrator" / "teamwork" / "active.json",
            {"run_id": "run-1", "mode": "fast"})


class SnapshotTests(HandoffContractTestBase):
    def test_snapshot_contains_bounded_contract_interview_and_run_fields(self):
        interview_id, question_id, decision_id, requirement_id = (
            self.install_interview())
        contract_id, contract_sha = self.install_contract(interview_id)
        self.install_run_state()
        context_dir = self.root / ".auto-orchestrator" / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "project-summary.md").write_text(
            "bounded context\n", encoding="utf-8")

        active, prepared = self.prepare()
        snapshot = self.snapshot(prepared)

        self.assertEqual(set(snapshot), set(handoff.SNAPSHOT_FIELDS))
        self.assertEqual(snapshot["project_id"], self.identity()["project_id"])
        self.assertEqual(snapshot["source_ide"], "opencode")
        self.assertEqual(snapshot["target_ide"], "codex")
        self.assertEqual(snapshot["source_session_id"], "source")
        self.assertIsNone(snapshot["target_session_id"])
        self.assertEqual(snapshot["run_id"], "run-1")
        self.assertEqual(snapshot["milestone_id"], "M1")
        self.assertEqual(snapshot["task_id"], "build")
        self.assertEqual(snapshot["run_status"], "planned")
        self.assertIn(snapshot["milestone_status"], ("ready", "planned"))
        self.assertEqual(snapshot["task_status"], "pending")
        self.assertEqual(snapshot["mode"], "fast")
        self.assertFalse(snapshot["dirty"])
        self.assertEqual(snapshot["changed_files"], [])
        self.assertEqual(snapshot["head_sha"], snapshot["head"])
        self.assertEqual(snapshot["schema_version"], snapshot["version"])
        self.assertEqual(snapshot["sha256"], contract_sha)
        self.assertEqual(snapshot["open_question_id"], question_id)
        self.assertEqual(snapshot["completed_tasks"], [])
        self.assertEqual(snapshot["pending_tasks"], ["build"])
        self.assertEqual(snapshot["contract_id"], contract_id)
        self.assertEqual(snapshot["contract_version"], 1)
        self.assertEqual(snapshot["contract_sha256"], contract_sha)
        self.assertEqual(snapshot["interview_id"], interview_id)
        self.assertEqual(snapshot["open_question"], question_id)
        self.assertEqual(snapshot["blocking_questions"], [question_id])
        self.assertTrue(snapshot["request_hash"])
        self.assertEqual(snapshot["confirmed_decision_refs"], [decision_id])
        self.assertIn("requirement:" + requirement_id, snapshot["blockers"])
        self.assertIn(".auto-orchestrator/context/project-summary.md",
                      snapshot["context_refs"])
        self.assertEqual(
            snapshot["context_provenance"], "project_memory_context")
        self.assertEqual(snapshot["next_action"], "continue")
        body = {key: value for key, value in snapshot.items()
                if key != "digest"}
        self.assertEqual(snapshot["digest"], handoff._canonical_digest(body))

        self.release(active, prepared)
        accepted = handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "codex", session_id="target")
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(active["state"], "active")

    def test_status_after_prepare_is_read_only(self):
        self.prepare()
        state_root = self.root / ".auto-orchestrator"
        before = {
            path.relative_to(self.root).as_posix(): (
                path.stat().st_mtime_ns, path.read_bytes())
            for path in state_root.rglob("*") if path.is_file()
        }
        status = handoff.status(self.root)
        self.assertTrue(status["ok"], status)
        after = {
            path.relative_to(self.root).as_posix(): (
                path.stat().st_mtime_ns, path.read_bytes())
            for path in state_root.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertFalse((state_root / "memory.sqlite3").exists())
        self.assertFalse((state_root / "supervisor.sqlite3").exists())
        self.assertTrue(status["read_only"])
        self.assertEqual(status["state"], "handoff_prepared")
        self.assertNotIn("owner_token", status)
        self.assertNotIn("token", status["owner"])
        self.assertNotIn("token", status["ownership"]["owner"])

    def test_status_on_empty_project_creates_nothing(self):
        empty = self.root / "empty"
        empty.mkdir()
        status = handoff.status(empty)
        self.assertEqual(status["state"], "inactive")
        self.assertFalse((empty / ".auto-orchestrator").exists())


class DirtyPolicyTests(HandoffContractTestBase):
    def test_dirty_worktree_requires_explicit_allow(self):
        active = self.activate()
        (self.root / "notes.txt").write_text("dirty\n", encoding="utf-8")
        blocked = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            idle_confirmation="user_confirmed")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reasons"][0]["code"], "dirty_worktree")
        self.assertEqual(self.snapshot_files(), [])

        allowed = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            allow_dirty=True, idle_confirmation="user_confirmed")
        self.assertTrue(allowed["ok"], allowed)
        snapshot = self.snapshot(allowed)
        self.assertTrue(snapshot["dirty"])
        self.assertIn("notes.txt", [
            item["path"] for item in snapshot["changed_files"]])
        self.release(active, allowed)
        accepted = handoff.accept(
            self.root, allowed["handoff"]["handoff_id"],
            allowed["handoff"]["digest"], "codex")
        self.assertTrue(accepted["ok"], accepted)

    def test_auto_orchestrator_changes_are_excluded_from_dirty_digest(self):
        active = self.activate()
        (self.root / ".auto-orchestrator" / "scratch.txt").write_text(
            "ignored\n", encoding="utf-8")
        prepared = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            idle_confirmation="user_confirmed")
        self.assertTrue(prepared["ok"], prepared)
        snapshot = self.snapshot(prepared)
        self.assertFalse(snapshot["dirty"])
        self.assertEqual(snapshot["changed_files"], [])

    def test_utf8_paths_and_crlf_snapshot_are_preserved(self):
        active = self.activate()
        name = "ünïcode-测试.txt"
        (self.root / name).write_bytes("değer\r\n".encode("utf-8"))
        prepared = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            allow_dirty=True, idle_confirmation="user_confirmed")
        self.assertTrue(prepared["ok"], prepared)
        snapshot = self.snapshot(prepared)
        paths = [item["path"] for item in snapshot["changed_files"]]
        self.assertIn(name, paths)

        path = self.root / prepared["handoff"]["path"]
        text = json.dumps(snapshot, indent=2, ensure_ascii=False)
        self.release(active, prepared)
        path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        accepted = handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "codex")
        self.assertTrue(accepted["ok"], accepted)

    def snapshot_files(self):
        directory = handoff._snapshots_dir(self.root)
        if not directory.is_dir():
            return []
        return sorted(path.name for path in directory.glob("*.json"))


class SnapshotSafetyTests(HandoffContractTestBase):
    def test_tampered_oversized_and_symlinked_snapshots_are_rejected(self):
        active, prepared = self.prepare()
        self.release(active, prepared)
        handoff_id = prepared["handoff"]["handoff_id"]
        digest = prepared["handoff"]["digest"]
        path = self.root / prepared["handoff"]["path"]
        original = path.read_bytes()
        ownership = self.ownership_bytes()

        tampered = json.loads(original)
        tampered["next_action"] = "tampered"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        result = handoff.accept(self.root, handoff_id, digest, "codex")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reasons"][0]["code"], "snapshot_rejected")
        self.assertEqual(self.ownership_bytes(), ownership)
        path.write_bytes(original)

        path.write_bytes(b"{\"padding\":\"" + b"x" * (70 * 1024) + b"\"}")
        result = handoff.accept(self.root, handoff_id, digest, "codex")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reasons"][0]["code"], "snapshot_rejected")
        self.assertEqual(self.ownership_bytes(), ownership)
        path.write_bytes(original)

        backup = self.root / "snapshot-backup.json"
        backup.write_bytes(original)
        path.unlink()
        path.symlink_to(backup)
        result = handoff.accept(self.root, handoff_id, digest, "codex")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reasons"][0]["code"], "snapshot_rejected")
        self.assertEqual(self.ownership_bytes(), ownership)
        path.unlink()
        path.write_bytes(original)

    def test_wrong_project_snapshot_and_traversal_are_rejected(self):
        active, prepared = self.prepare()
        self.release(active, prepared)
        handoff_id = prepared["handoff"]["handoff_id"]
        path = self.root / prepared["handoff"]["path"]
        snapshot = self.snapshot(prepared)
        snapshot["project_id"] = "p_" + "0" * 16
        snapshot["project_uuid"] = "00000000-0000-4000-8000-000000000000"
        body = {key: value for key, value in snapshot.items()
                if key != "digest"}
        snapshot["digest"] = handoff._canonical_digest(body)
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        record = json.loads(self.ownership_bytes())
        record["handoff"]["digest"] = snapshot["digest"]
        with project_lock(self.root):
            atomic_write_json(handoff._ownership_path(self.root), record)
        result = handoff.accept(
            self.root, handoff_id, snapshot["digest"], "codex")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reasons"][0]["code"], "wrong_project")

        with self.assertRaises(handoff.HandoffError):
            handoff.accept(self.root, "../ownership", "a" * 64, "codex")


class MismatchTests(HandoffContractTestBase):
    def _assert_blocked_without_mutation(self, prepared, code, active=None):
        if active is not None:
            self.release(active, prepared)
        before = self.ownership_bytes()
        result = handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "codex")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn(code, [item["code"] for item in result["reasons"]])
        self.assertEqual(self.ownership_bytes(), before)

    def test_contract_pointer_change_blocks_accept(self):
        interview_id, *_ = self.install_interview()
        self.install_contract(interview_id)
        active, prepared = self.prepare()
        pointer = (self.root / ".auto-orchestrator" / "contracts"
                   / "current.json")
        value = json.loads(pointer.read_text(encoding="utf-8"))
        value["sha256"] = "a" * 64
        atomic_write_json(pointer, value)
        self._assert_blocked_without_mutation(
            prepared, "contract_mismatch", active)

    def test_head_change_blocks_accept(self):
        active, prepared = self.prepare()
        (self.root / "app.py").write_text("value = 2\n", encoding="utf-8")
        _commit_all(self.root, "second")
        self._assert_blocked_without_mutation(
            prepared, "head_mismatch", active)

    def test_interview_question_change_blocks_accept(self):
        interview_id, question_id, *_ = self.install_interview()
        self.install_contract(interview_id)
        active, prepared = self.prepare()
        memory_path = self.root / ".auto-orchestrator" / "memory.sqlite3"
        import sqlite3
        connection = sqlite3.connect(str(memory_path))
        try:
            connection.execute(
                "UPDATE interview_questions SET status='answered' "
                "WHERE question_id=?", (question_id,))
            connection.commit()
        finally:
            connection.close()
        self._assert_blocked_without_mutation(
            prepared, "interview_mismatch", active)

    def test_dirty_change_after_prepare_blocks_accept(self):
        active, prepared = self.prepare()
        (self.root / "late.txt").write_text("late\n", encoding="utf-8")
        self._assert_blocked_without_mutation(
            prepared, "dirty_digest_mismatch", active)

    def test_content_change_with_same_status_blocks_accept(self):
        active = self.activate()
        path = self.root / "notes.txt"
        path.write_text("first\n", encoding="utf-8")
        prepared = handoff.prepare_handoff(
            self.root, active["owner_token"], active["fence"], "codex",
            allow_dirty=True, idle_confirmation="user_confirmed")
        self.assertTrue(prepared["ok"], prepared)
        path.write_text("second\n", encoding="utf-8")
        self._assert_blocked_without_mutation(
            prepared, "dirty_digest_mismatch", active)

    def test_project_manifest_change_blocks_accept(self):
        active, prepared = self.prepare()
        self.release(active, prepared)
        path = self.root / ".auto-orchestrator" / "project.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["project_id"] = "p_" + "1" * 16
        value["id_scheme"] = "legacy"
        atomic_write_json(path, value)
        self._assert_blocked_without_mutation(prepared, "wrong_project")

    def test_ownership_generation_change_blocks_accept(self):
        active, prepared = self.prepare()
        self.release(active, prepared)
        record = json.loads(self.ownership_bytes())
        record["fence"] += 1
        with project_lock(self.root):
            atomic_write_json(handoff._ownership_path(self.root), record)
        self._assert_blocked_without_mutation(
            prepared, "release_required")


class ModuleSafetyTests(HandoffContractTestBase):
    def test_module_does_not_launch_processes_or_instantiate_engines(self):
        source = Path(handoff.__file__).read_text(encoding="utf-8")
        self.assertNotIn("Popen", source)
        status_source = __import__("inspect").getsource(handoff.status)
        self.assertNotIn("TeamworkEngine", status_source)
        self.assertNotIn("ProjectMemory", status_source)
        self.assertNotIn("mkdir", status_source)


if __name__ == "__main__":
    unittest.main()
