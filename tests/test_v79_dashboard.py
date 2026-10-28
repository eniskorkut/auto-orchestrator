"""V7.9 dashboard evidence: verification and integrity from real artifacts."""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import tools.dashboard as dashboard


class DashboardEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "p"
        self.root.mkdir()
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "a@b.c"),
                     ("config", "user.name", "t")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True,
                           capture_output=True)
        (self.root / ".gitignore").write_text(".auto-orchestrator/\n")
        (self.root / "README.md").write_text("x\n")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "i"],
                       check=True, capture_output=True)

    def write(self, relative, value):
        path = self.root / ".auto-orchestrator" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_missing_artifacts_are_unknown(self):
        view = dashboard._ide_ownership_snapshot(self.root)
        self.assertIsNone(view["verification"])
        self.assertIsNone(view["integrity"])

    def test_real_verification_and_plan_artifacts_are_exposed(self):
        self.write("verification/run-1-build-1.json", {
            "version": 1,
            "kind": "native-verification",
            "commit": "a" * 40,
            "exit_code": 0,
            "merged_tree_clean": True,
            "recorded_at": time.time(),
            "verified": True,
            "result": {"aggregate_status": "verified"},
        })
        self.write("plans/run-1.json", {
            "version": 1,
            "plan_version": 2,
            "run_id": "run-1",
            "integrity_policy": "demo",
            "adaptive_decision": {
                "chosen_mode": "balanced", "score": 42,
                "reason": "risk floor",
            },
            "scope_contract": {"objective": "Ship it"},
        })
        view = dashboard._ide_ownership_snapshot(self.root)
        self.assertTrue(view["verification"]["verified"])
        self.assertEqual(view["verification"]["aggregate_status"], "verified")
        self.assertEqual(view["verification"]["commit"], "a" * 40)
        self.assertEqual(view["integrity"]["integrity_policy"], "demo")
        self.assertEqual(view["integrity"]["chosen_mode"], "balanced")
        self.assertEqual(view["integrity"]["risk_score"], 42)
        self.assertEqual(view["integrity"]["objective"], "Ship it")

    def test_dispatch_summary_is_read_from_dispatch_records(self):
        from tools import native_dispatch
        native_dispatch.create(
            self.root, run_id="run-d", task_id="build", role="implementer",
            ide="codex", owner_session_id="s", write_scope=["src/"])
        view = dashboard._ide_ownership_snapshot(self.root)
        self.assertEqual(view["dispatches"]["total"], 1)
        self.assertEqual(view["dispatches"]["counts"]["queued"], 1)
        self.assertEqual(view["dispatches"]["active"], 1)
        self.assertEqual(view["dispatches"]["verified"], 0)

    def test_oversized_or_invalid_artifacts_stay_unknown(self):
        path = self.root / ".auto-orchestrator" / "verification" / "big.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(b" " * 300_000)
        self.assertIsNone(dashboard._verification_evidence(self.root))
        bad = self.root / ".auto-orchestrator" / "plans" / "bad.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{", encoding="utf-8")
        self.assertIsNone(dashboard._plan_integrity(self.root))


if __name__ == "__main__":
    unittest.main()
