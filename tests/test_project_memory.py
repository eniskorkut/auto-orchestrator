from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.project_memory import MemoryError, ProjectMemory


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ProjectMemory(self.root)
        self.addCleanup(self.memory.close)

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


if __name__ == "__main__":
    unittest.main()
