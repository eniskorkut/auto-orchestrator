from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.project_memory import MemoryError, ProjectMemory, bundle_digest


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ProjectMemory(self.root)
        self.addCleanup(self.memory.close)

    def _insert_interview(self, request="secret raw prompt: build login",
                          summary="Login feature") -> tuple[str, str]:
        interview_id = "iv_" + "a" * 16
        question_id = "q_" + "b" * 16
        now = time.time()
        self.memory.conn.execute(
            "INSERT INTO interviews(interview_id,project_id,origin_platform,"
            "request_hash,request_summary,mode,status,created_at,updated_at,"
            "schema_version) VALUES (?,?,?,?,?,?,?,?,?,1)",
            (interview_id, self.memory.project_id, "codex",
             hashlib.sha256(request.encode()).hexdigest(), summary, "auto",
             "open", now, now))
        self.memory.conn.execute(
            "INSERT INTO interview_questions(interview_id,question_id,ordinal,"
            "question_key,question,options,recommended_option,status,"
            "depends_on,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (interview_id, question_id, 0, "auth_session",
             "How should sessions work?",
             json.dumps([{"id": "jwt", "label": "JWT",
                          "recommended": True}]), "jwt", "open", "[]", now))
        self.memory.conn.commit()
        return interview_id, question_id

    def _write_bundle(self, decisions=None, requirements=None, **extra) -> Path:
        payload = {
            "schema_version": 1,
            "identity": {"project_uuid": self.memory.identity["project_uuid"]},
            "decisions": decisions or [],
            "requirements": requirements or [],
            **extra,
        }
        payload["digest"] = bundle_digest(payload)
        path = Path(self.temp.name) / "import.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_symlinked_memory_database_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            outside = Path(temp) / "outside.sqlite3"
            outside.write_bytes(b"not a database")
            root = Path(temp) / "project"
            state = root / ".auto-orchestrator"
            state.mkdir(parents=True)
            (state / "memory.sqlite3").symlink_to(outside)
            with self.assertRaises(MemoryError):
                ProjectMemory(root)

    def test_confirmed_decision_requires_user_confirmation(self):
        with self.assertRaises(MemoryError):
            self.memory.add_decision(topic="auth", decision="JWT", status="confirmed")
        with self.assertRaises(MemoryError):
            self.memory.add_decision(
                topic="auth", decision="JWT", status="confirmed",
                confirmed_by="recommended")
        item = self.memory.add_decision(
            topic="auth", decision="JWT", status="confirmed",
            confirmed_by="user", source="user")
        self.assertEqual(item["status"], "confirmed")
        self.assertEqual(self.memory.list_decisions(status="confirmed")[0]["decision"], "JWT")

    def test_decision_versioning_and_context_projection(self):
        old = self.memory.add_decision(topic="db", decision="SQLite", status="code_derived",
                                      source="code", source_ref="scan")
        new = self.memory.add_decision(
            topic="db", decision="PostgreSQL", status="confirmed", confirmed_by="user",
            source="user", supersedes=old["decision_id"])
        self.assertEqual(self.memory.get_decision(old["decision_id"])["status"], "superseded")
        self.assertEqual(new["supersedes"], old["decision_id"])
        paths = self.memory.write_context_files()
        self.assertTrue(Path(paths["architecture-decisions.json"]).is_file())
        payload = json.loads(Path(paths["architecture-decisions.json"]).read_text())
        self.assertTrue(any(item["status"] == "superseded" for item in payload["decisions"]))

    def test_requirements_and_handoff_versions(self):
        req = self.memory.add_requirement("Use secure cookies", category="unresolved_blocking")
        self.assertEqual(self.memory.list_requirements(status="unresolved_blocking")[0]["text"],
                         "Use secure cookies")
        resolved = self.memory.resolve_requirement(req["requirement_id"], status="confirmed")
        self.assertEqual(resolved["status"], "confirmed")
        first = self.memory.record_handoff(run_id="r1", milestone_id="M1",
                                          platform="codex", payload={"summary": "one"})
        second = self.memory.record_handoff(run_id="r1", milestone_id="M1",
                                           platform="opencode", payload={"summary": "two"})
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(self.memory.latest_handoff()["payload"]["summary"], "two")
        repeated = self.memory.record_handoff(
            run_id="r1", milestone_id="M1", platform="opencode",
            payload={"summary": "two"})
        self.assertEqual(repeated["handoff_id"], second["handoff_id"])
        self.assertEqual(repeated["version"], 2)
        self.assertEqual(len(self.memory.list_handoffs()), 2)

    def test_repository_facts_use_token_boundaries(self):
        from tools.repo_facts import scan_repository
        (self.root / "README.md").write_text("Every author owns their chapter.\n")
        facts = scan_repository(self.root)
        self.assertFalse(facts["detected"]["auth"])
        self.assertFalse(facts["detected"]["session"])
        (self.root / "auth.py").write_text("import jwt\n", encoding="utf-8")
        detected = scan_repository(self.root)
        self.assertTrue(detected["detected"]["jwt"])
        self.assertTrue(detected["detected"]["auth"])
        (self.root / "cache.py").write_text("session = load()\n", encoding="utf-8")
        self.assertFalse(scan_repository(self.root)["code_detected"]["session"])

    def test_export_redacts_obvious_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with ProjectMemory(root) as memory:
                memory.add_decision(
                    topic="auth", decision="JWT",
                    rationale=("api_key=supersecretvalue and Bearer abcdefghijklmnop "
                               "client_secret=clientvalue sk-proj-abcdefghijklmnop"),
                    status="code_derived", source="code")
                memory.record_handoff(
                    run_id="r1", milestone_id="M1", platform="codex",
                    payload={"context_summary": "ok", "token": "supersecretvalue"})
                target = Path(temp) / "bundle.json"
                memory.export_bundle(target)
            text = target.read_text(encoding="utf-8")
            self.assertNotIn("supersecretvalue", text)
            self.assertNotIn("abcdefghijklmnop", text)
            self.assertNotIn("clientvalue", text)
            self.assertIn("[redacted]", text)
            payload = json.loads(text)
            handoff = payload["handoffs"][0]
            canonical = json.dumps(handoff["payload"], sort_keys=True,
                                   separators=(",", ":"), ensure_ascii=False)
            import hashlib
            self.assertEqual(handoff["sha256"],
                             hashlib.sha256(canonical.encode()).hexdigest())

    def test_unknown_git_capture_does_not_overwrite_last_platform(self):
        root = Path(self.temp.name)
        subprocess.run(["git", "-C", str(root), "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "a@b.c"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "A"], check=True)
        (root / "a.txt").write_text("one\n")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "one"],
                       capture_output=True, check=True)
        memory = ProjectMemory(root)
        try:
            memory.update_meta(last_platform="codex")
            memory.capture_git_observation(platform="unknown")
            self.assertEqual(memory.get_meta()["last_platform"], "codex")
        finally:
            memory.close()

    def test_export_import_conflict_requires_review(self):
        decision = self.memory.add_decision(topic="auth", decision="JWT",
                                           status="code_derived", source="code")
        target = Path(self.temp.name) / "bundle.json"
        exported = self.memory.export_bundle(target)
        self.assertGreater(exported["bytes"], 0)
        # Same bundle, unchanged: preview should be conflict-free.
        preview = self.memory.import_bundle(target, apply=False)
        self.assertFalse(preview["applied"])
        self.assertEqual(preview["conflicts"], [])
        # A conflicting local record is surfaced, not silently merged.
        self.memory.conn.execute(
            "UPDATE decisions SET decision='Session cookie' WHERE decision_id=?",
            (decision["decision_id"],))
        self.memory.conn.commit()
        conflict = self.memory.import_bundle(target, apply=False)
        self.assertTrue(conflict["conflicts"])

    def test_context_stale_detection_and_git_observation(self):
        subprocess.run(["git", "-C", str(self.root), "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "a@b.c"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "A"], check=True)
        (self.root / "a.txt").write_text("one\n")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "one"],
                       capture_output=True, check=True)
        observed = self.memory.capture_git_observation(platform="codex")
        self.assertEqual(observed["kind"], "git_commit")
        self.assertFalse(self.memory.context_state()["stale"])
        (self.root / "a.txt").write_text("two\n")
        subprocess.run(["git", "-C", str(self.root), "commit", "-am", "two"],
                       check=True, capture_output=True)
        self.assertTrue(self.memory.context_state()["stale"])

    def test_export_roundtrip_carries_safe_structured_items(self):
        self.memory.add_decision(topic="auth", decision="JWT", status="confirmed",
                                 confirmed_by="user", source="user")
        self.memory.add_requirement("Use secure cookies")
        self.memory.record_handoff(
            run_id="run-1", milestone_id="M1", platform="codex",
            payload={"milestone_id": "M1", "context_summary": "M1 passed",
                     "verified_tests": ["test_login"], "next_tasks": [],
                     "next_milestone": "M2"})
        self.memory.record_observation(kind="verification", files=["a.py"],
                                       test_evidence="11 tests passed")
        interview_id, question_id = self._insert_interview()
        self.memory.update_meta(last_platform="codex", last_milestone="M1")
        target = Path(self.temp.name) / "bundle.json"
        exported = self.memory.export_bundle(target)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["digest"], bundle_digest(payload))
        self.assertEqual(payload["source"]["instance_id"],
                         self.memory.identity["instance_id"])
        self.assertFalse(payload["truncated"])
        self.assertNotIn("secret raw prompt", target.read_text(encoding="utf-8"))
        self.assertEqual(payload["handoffs"][0]["payload"]["verified_tests"],
                         ["test_login"])
        self.assertEqual(payload["observations"][0]["files"], ["a.py"])
        interview = payload["interviews"][0]
        self.assertEqual(interview["interview_id"], interview_id)
        self.assertIn("request_hash", interview)
        self.assertNotIn("request", interview)
        self.assertEqual(interview["questions"][0]["question_id"], question_id)
        self.assertEqual(interview["questions"][0]["options"][0]["id"], "jwt")
        self.assertEqual(exported["handoffs"], 1)
        self.assertEqual(exported["interviews"], 1)

    def test_export_import_apply_roundtrip_brings_all_supported_items(self):
        self.memory.add_decision(topic="auth", decision="JWT",
                                 status="confirmed", confirmed_by="user",
                                 source="user")
        self.memory.add_requirement("Use secure cookies")
        self.memory.record_handoff(
            run_id="run-1", milestone_id="M1", platform="codex",
            payload={"milestone_id": "M1", "context_summary": "M1 passed",
                     "verified_tests": ["test_login"]})
        self.memory.record_observation(kind="verification", files=["a.py"],
                                       test_evidence="11 tests passed")
        interview_id, question_id = self._insert_interview()
        self.memory.update_meta(last_platform="codex", last_milestone="M1")
        target = Path(self.temp.name) / "bundle.json"
        self.memory.export_bundle(target)
        target_root = Path(self.temp.name) / "target"
        state = target_root / ".auto-orchestrator"
        state.mkdir(parents=True)
        shutil.copy2(self.root / ".auto-orchestrator" / "project.json",
                     state / "project.json")
        with ProjectMemory(target_root) as memory:
            result = memory.import_bundle(target, apply=True)
            self.assertTrue(result["applied"])
            self.assertEqual(result["conflicts"], [])
            self.assertEqual(result["imported"], 6)
            self.assertEqual(memory.get_meta()["last_milestone"], "M1")
            self.assertEqual(memory.get_meta()["last_platform"], None)
            self.assertEqual(memory.active_interview()["interview_id"],
                             interview_id)
            self.assertEqual(memory.active_interview()["question"]["question_id"],
                             question_id)
            self.assertTrue(memory.latest_handoff()["payload"]["verified_tests"])
            self.assertEqual(memory._latest_verification()["test_evidence"],
                             "11 tests passed")

    def test_preview_classifies_conflicts_with_field_level_reasons(self):
        existing_id = "d_" + "1" * 16
        self.memory.add_decision(topic="auth", decision="JWT",
                                 status="code_derived", source="code",
                                 decision_id=existing_id)
        duplicate_id = "d_" + "4" * 16
        path = self._write_bundle(decisions=[
            {"decision_id": existing_id, "topic": "auth",
             "decision": "Session cookie", "status": "code_derived",
             "source": "code"},
            {"decision_id": "d_" + "2" * 16, "topic": "auth",
             "decision": "SAML", "status": "unresolved",
             "source": "import"},
            {"decision_id": "d_" + "3" * 16, "topic": "db",
             "decision": "PostgreSQL", "status": "unresolved",
             "source": "import", "supersedes": "d_" + "9" * 16},
            {"decision_id": duplicate_id, "topic": "cache",
             "decision": "Redis", "status": "unresolved",
             "source": "import"},
            {"decision_id": duplicate_id, "topic": "cache",
             "decision": "Redis", "status": "unresolved",
             "source": "import"},
            {"decision_id": "d_" + "5" * 16, "topic": "auth",
             "decision": "JWT", "status": "code_derived", "source": "code"},
        ], mystery={"ignored": True})
        preview = self.memory.import_bundle(path, apply=False)
        self.assertFalse(preview["applied"])
        self.assertIn("mystery", preview["unsupported"])
        actions = {(item["kind"], item["id"]): item for item in preview["items"]}
        self.assertEqual(actions[("decision", existing_id)]["action"], "conflict")
        self.assertIn("id_conflict", [
            reason["code"] for reason in actions[("decision", existing_id)]["reasons"]])
        self.assertIn("decision", [
            reason["field"] for reason in actions[("decision", existing_id)]["reasons"]])
        self.assertEqual(actions[("decision", "d_" + "2" * 16)]["action"],
                         "conflict")
        self.assertIn("same_topic_different_decision", [
            reason["code"] for reason in actions[
                ("decision", "d_" + "2" * 16)]["reasons"]])
        self.assertIn("missing_supersedes", [
            reason["code"] for reason in actions[
                ("decision", "d_" + "3" * 16)]["reasons"]])
        duplicate_items = [item for item in preview["items"]
                           if item["kind"] == "decision"
                           and item["id"] == duplicate_id]
        self.assertTrue(any(
            "duplicate_id" in [reason["code"] for reason in item["reasons"]]
            for item in duplicate_items))
        self.assertEqual(actions[("decision", "d_" + "5" * 16)]["action"],
                         "unchanged")
        self.assertEqual(actions[("decision", "d_" + "5" * 16)]["match"],
                         "natural_key")

    def test_apply_aborts_on_conflicts_without_silent_merge(self):
        self.memory.add_decision(topic="auth", decision="JWT",
                                 status="code_derived", source="code")
        before = len(self.memory.list_decisions())
        path = self._write_bundle(decisions=[
            {"decision_id": "d_" + "1" * 16, "topic": "new",
             "decision": "Cache", "status": "unresolved", "source": "import"},
            {"decision_id": "d_" + "2" * 16, "topic": "auth",
             "decision": "SAML", "status": "unresolved", "source": "import"},
        ])
        result = self.memory.import_bundle(path, apply=True)
        self.assertFalse(result["applied"])
        self.assertTrue(result["aborted"])
        self.assertFalse(result["preview"])
        self.assertGreaterEqual(result["not_applied"], 1)
        self.assertEqual(len(self.memory.list_decisions()), before)
        self.assertFalse(any(item["topic"] == "new"
                             for item in self.memory.list_decisions()))

    def test_transactional_apply_rolls_back_partial_inserts(self):
        ids = ["d_" + "a" * 16, "d_" + "b" * 16]
        path = self._write_bundle(decisions=[
            {"decision_id": ids[0], "topic": "one", "decision": "First",
             "status": "unresolved", "source": "import"},
            {"decision_id": ids[1], "topic": "two", "decision": "Second",
             "status": "unresolved", "source": "import"},
        ])
        original = self.memory._insert_import_item
        calls = {"decisions": 0}

        def broken(kind, value):
            if kind == "decision":
                calls["decisions"] += 1
                if calls["decisions"] == 2:
                    raise RuntimeError("simulated apply failure")
            return original(kind, value)

        with mock.patch.object(self.memory, "_insert_import_item",
                               side_effect=broken):
            with self.assertRaises(RuntimeError):
                self.memory.import_bundle(path, apply=True)
        stored = {item["decision_id"] for item in self.memory.list_decisions()}
        self.assertNotIn(ids[0], stored)
        self.assertNotIn(ids[1], stored)

    def test_digest_tampering_is_rejected_and_last_platform_is_preserved(self):
        target = Path(self.temp.name) / "export.json"
        self.memory.add_decision(topic="auth", decision="JWT",
                                 status="code_derived", source="code")
        self.memory.update_meta(last_platform="opencode")
        self.memory.export_bundle(target)
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["decisions"][0]["decision"] = "Tampered"
        target.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(MemoryError):
            self.memory.import_bundle(target, apply=False)

        bundle = self._write_bundle(
            decisions=[{"decision_id": "d_" + "c" * 16, "topic": "new",
                        "decision": "New", "status": "unresolved",
                        "source": "import"}],
            meta={"last_platform": "codex", "last_milestone": "M9"})
        result = self.memory.import_bundle(bundle, apply=True)
        self.assertTrue(result["applied"])
        meta = self.memory.get_meta()
        self.assertEqual(meta["last_platform"], "opencode")
        self.assertEqual(meta["last_milestone"], "M9")

    def test_context_state_reports_every_stale_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with ProjectMemory(root) as memory:
                state = memory.context_state()
                self.assertTrue(state["stale"])
                self.assertIn("missing_head", state["stale_reasons"])
                subprocess.run(["git", "-C", str(root), "init"],
                               capture_output=True, check=True)
                subprocess.run(["git", "-C", str(root), "config",
                                "user.email", "a@b.c"], check=True)
                subprocess.run(["git", "-C", str(root), "config",
                                "user.name", "A"], check=True)
                (root / "a.txt").write_text("one\n")
                subprocess.run(["git", "-C", str(root), "add", "."], check=True)
                subprocess.run(["git", "-C", str(root), "commit", "-m", "one"],
                               capture_output=True, check=True)
                memory.capture_git_observation(platform="codex")
                self.assertEqual(memory.context_state()["stale_reasons"], [])
                subprocess.run(["git", "-C", str(root), "checkout", "-b", "feature"],
                               capture_output=True, check=True)
                (root / "pending.txt").write_text("dirty\n")
                state = memory.context_state()
                self.assertIn("branch_mismatch", state["stale_reasons"])
                self.assertIn("dirty_worktree", state["stale_reasons"])

    def test_context_files_and_bundle_include_continuity_inputs(self):
        self.memory.add_decision(topic="auth", decision="JWT",
                                 status="confirmed", confirmed_by="user",
                                 source="user")
        self.memory.add_requirement("Use secure cookies")
        self.memory.record_handoff(
            run_id="run-1", milestone_id="M1", platform="codex",
            payload={"milestone_id": "M1", "context_summary": "M1 passed",
                     "verified_tests": ["test_login"],
                     "next_milestone": "M2",
                     "next_tasks": [{"id": "verify", "role": "tester"}]})
        self.memory.record_observation(kind="verification", files=["a.py"],
                                       test_evidence="11 tests passed")
        self.memory.update_meta(last_platform="codex", last_milestone="M1")
        self._insert_interview()
        contracts = self.root / ".auto-orchestrator" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / "current.json").write_text(json.dumps({
            "contract_id": "rc_" + "a" * 16,
            "contract_version": 2,
            "sha256": "b" * 64,
            "path": "rc_" + "a" * 16 + "-v2.json",
            "updated_at": time.time(),
        }), encoding="utf-8")
        paths = self.memory.write_context_files()
        open_tasks = json.loads(Path(paths["open-tasks.json"]).read_text())
        self.assertEqual(open_tasks["next_milestone"], "M2")
        self.assertEqual(open_tasks["next_tasks"][0]["id"], "verify")
        self.assertEqual(open_tasks["active_interview"]["question"]["question"],
                         "How should sessions work?")
        self.assertEqual(open_tasks["contract"]["contract_version"], 2)
        self.assertEqual(open_tasks["latest_verification"]["test_evidence"],
                         "11 tests passed")
        self.assertIn("stale_warning", self.memory.context_summary())
        bundle = self.memory.context_bundle()
        self.assertEqual(bundle["last_milestone"], "M1")
        self.assertEqual(len(bundle["confirmed_decisions"]), 1)
        self.assertEqual(len(bundle["blocking_requirements"]), 1)
        self.assertTrue(bundle["handoff"]["verified"])
        imported = ProjectMemory.handoff_view({
            "imported": 1,
            "payload": {"verified_tests": ["test_login"]},
        })
        self.assertFalse(imported["verified"])
        self.assertEqual(bundle["contract"]["contract_version"], 2)
        self.assertEqual(bundle["active_interview"]["interview_id"],
                         "iv_" + "a" * 16)
        self.assertIn("stale", bundle)


if __name__ == "__main__":
    unittest.main()
