"""V8.0 verification receipt key and tamper regressions."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tools import verification_receipt


class ReceiptSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "proje"
        self.root.mkdir()
        (self.root / ".auto-orchestrator").mkdir()
        self.payload = {
            "verification_run_id": "vr_0123456789abcdef",
            "project_id": "p_" + "1" * 16,
            "run_id": "run-1",
            "task_id": "build",
            "dispatch_id": "nd_" + "2" * 16,
            "commit": "a" * 40,
            "verified": True,
            "exit_code": 0,
            "test_results_sha256": "b" * 64,
            "test_output_sha256": "c" * 64,
            "dispatch_artifact_sha256": "d" * 64,
            "dispatch_artifact_size": 4,
            "verification_record_sha256": "e" * 64,
            "record_ref": ".auto-orchestrator/verification/record.json",
        }
        self.path = verification_receipt.issue(self.root, self.payload)

    def test_key_permissions_and_no_symlink(self):
        key = self.root / ".auto-orchestrator" / "ide" / "verification.key"
        self.assertTrue(key.is_file())
        if os.name == "posix":
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        backup = self.root / "key.backup"
        key.rename(backup)
        key.symlink_to(backup)
        with self.assertRaises(verification_receipt.ReceiptError):
            verification_receipt.read(self.root, "vr_0123456789abcdef")

    def test_tampered_receipt_payload_is_rejected(self):
        value = json.loads(self.path.read_text(encoding="utf-8"))
        value["payload"]["run_id"] = "other"
        self.path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(verification_receipt.ReceiptError):
            verification_receipt.read(self.root, "vr_0123456789abcdef")

    def test_receipt_is_single_use_per_dispatch(self):
        receipt_id = self.payload["verification_run_id"]
        dispatch = self.payload["dispatch_id"]
        self.assertTrue(verification_receipt.consume(
            self.root, receipt_id, dispatch))
        self.assertFalse(verification_receipt.consume(
            self.root, receipt_id, dispatch))
        with self.assertRaises(verification_receipt.ReceiptError):
            verification_receipt.consume(
                self.root, receipt_id, "nd_" + "3" * 16)

    def test_wrong_project_or_artifact_binding_is_rejected(self):
        receipt_id = self.payload["verification_run_id"]
        cases = [
            {"project_id": "p_" + "9" * 16},
            {"dispatch_id": "nd_" + "8" * 16},
            {"dispatch_artifact_sha256": "9" * 64},
            {"dispatch_artifact_size": 5},
            {"commit": "f" * 40},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                kwargs = dict(
                    run_id="run-1", task_id="build", commit="a" * 40,
                    project_id="p_" + "1" * 16,
                    dispatch_id="nd_" + "2" * 16,
                    dispatch_artifact_sha256="d" * 64,
                    dispatch_artifact_size=4,
                )
                kwargs.update(overrides)
                with self.assertRaises(verification_receipt.ReceiptError):
                    verification_receipt.require(
                        self.root, receipt_id, **kwargs)


if __name__ == "__main__":
    unittest.main()
