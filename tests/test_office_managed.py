from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.office_model import build_office
from tools.teamwork import TeamworkEngine


def plan() -> dict:
    return {
        "run_id": "managed-office",
        "max_parallel": 1,
        "pattern": "distributed_coding",
        "milestones": [{
            "id": "M1", "objective": "Build", "depends_on": [],
            "acceptance": ["evidence"],
            "verification_policy": {"require_roles": [], "max_repair": 0},
        }],
        "tasks": [{
            "id": "backend", "role": "implementer", "milestone": "M1",
            "phase": "build", "objective": "Create backend",
            "acceptance": ["file"],
            "read_scope": [], "write_scope": ["backend/"], "depends_on": [],
            "timeout_seconds": 60, "max_retries": 1, "verify_commands": [],
            "department": "backend", "provider": "opencode",
            "model": "demo-model",
        }],
    }


class ManagedOfficeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = TeamworkEngine(self.root)
        self.addCleanup(self.engine.close)
        self.engine.init(plan())

    def test_latest_managed_session_is_exposed_without_raw_identity(self):
        self.engine.record_managed_session(
            "managed-office", "backend", attempt=1, provider="opencode",
            session_id="ses_raw_should_not_leak", status="running",
            model="demo-model", cost=0.01, tokens={"input": 3, "output": 2},
            tool_count=4,
        )
        project = {
            "project_id": "p_" + "1" * 16, "name": "Managed Lab",
            "root": str(self.root), "theme": "ai", "agents": [],
        }
        payload = build_office(project, {"runs": []}, bindings=[],
                               detail_provider=lambda run_id: {})
        self.assertEqual(len(payload["managed_sessions"]), 1)
        entry = payload["managed_sessions"][0]
        self.assertEqual(entry["role"], "implementer")
        self.assertEqual(entry["room"], "backend")
        self.assertEqual(entry["status"], "working")
        self.assertTrue(entry["spawnable"])
        self.assertEqual(entry["managed"]["tool_count"], 4)
        self.assertEqual(entry["managed"]["session_hash"],
                         hashlib.sha256(b"ses_raw_should_not_leak").hexdigest()[:12])
        self.assertNotIn("ses_raw_should_not_leak", str(payload))

    def test_latest_attempt_only_and_completed_status(self):
        for attempt, status in ((1, "failed"), (2, "completed")):
            self.engine.record_managed_session(
                "managed-office", "backend", attempt=attempt, provider="opencode",
                session_id=f"ses_{attempt}", status=status, model="demo-model",
            )
        project = {
            "project_id": "p_" + "1" * 16, "name": "Managed Lab",
            "root": str(self.root), "theme": "ai", "agents": [],
        }
        payload = build_office(project, {"runs": []}, bindings=[],
                               detail_provider=lambda run_id: {})
        self.assertEqual(len(payload["managed_sessions"]), 1)
        entry = payload["managed_sessions"][0]
        self.assertEqual(entry["managed"]["attempt"], 2)
        self.assertEqual(entry["status"], "completed")
        self.assertFalse(entry["evidence"]["open_span"])


if __name__ == "__main__":
    unittest.main()
