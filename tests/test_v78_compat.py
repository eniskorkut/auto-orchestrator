"""V7.8 backward-compatibility tests.

V7.8 adds sequential IDE ownership and handoff records on top of the V7.6/V7.7
memory, contract and handoff formats. Nothing in this module imports V7.8
modules: it proves the *old* formats keep working after the upgrade.

Run with:
    python3.11 -m unittest tests.test_v78_compat -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools import requirements_contract
from tools.project_identity import ensure_project_identity
from tools.project_memory import ProjectMemory, bundle_digest


V76_BUNDLE = {
    "schema_version": 1,
    "kind": "auto-orchestrator-project-memory",
    "exported_at": 1_690_000_000.0,
    "source": {"platform": "codex"},
    "meta": {
        "last_platform": "codex",
        "last_run_id": "run-legacy",
        "last_milestone": "M1",
        "head_sha": "0" * 40,
        "branch": "main",
    },
    "limits": {"decisions": 200, "requirements": 200, "handoffs": 20},
    "totals": {
        "decisions": 1, "requirements": 1, "handoffs": 1,
        "observations": 0, "interviews": 0, "questions": 0,
    },
    "truncated": False,
    "decisions": [{
        "decision_id": "d_00000000000000a1",
        "topic": "auth_session",
        "decision": "jwt",
        "status": "confirmed",
        "source": "user",
        "confirmed_by": "user",
        "created_at": 1_690_000_000.0,
    }],
    "requirements": [{
        "requirement_id": "r_00000000000000b2",
        "text": "Kullanıcı oturumu JWT ile doğrulanmalı",
        "category": "confirmed",
        "status": "confirmed",
        "source": "user",
        "confirmed_by": "user",
        "created_at": 1_690_000_000.0,
    }],
    "handoffs": [{
        "handoff_id": "h_00000000000000c3",
        "run_id": "run-legacy",
        "milestone_id": "M1",
        "version": 1,
        "platform": "codex",
        "payload": {
            "milestone_id": "M1",
            "completed_tasks": ["T1"],
            "decisions": [],
            "changed_files": ["src/auth.py"],
            "commits": [],
            "verified_tests": ["tests/test_auth.py"],
            "review_findings": [],
            "open_issues": [],
            "next_milestone": None,
            "context_summary": "legacy handoff without next_tasks",
            "evidence_refs": [],
        },
        "created_at": 1_690_000_000.0,
    }],
    "observations": [],
    "interviews": [],
}


class V78CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "proje çalışma alanı"
        self.root.mkdir()

    # -- helpers ---------------------------------------------------------
    def _identity(self) -> dict:
        return ensure_project_identity(self.root)

    def _write_legacy_bundle(self, *, newline="\n") -> Path:
        identity = self._identity()
        payload = json.loads(json.dumps(V76_BUNDLE))
        payload["identity"] = {
            "version": 1,
            "project_uuid": identity["project_uuid"],
            "project_id": identity["project_id"],
            "name": identity["name"],
        }
        payload["source_instance"] = None
        payload["digest"] = bundle_digest(payload)
        target = Path(self.temp.name) / "legacy export.json"
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        with open(target, "w", encoding="utf-8", newline=newline) as handle:
            handle.write(text + "\n")
        return target

    # -- legacy bundle import -------------------------------------------
    def test_v76_bundle_imports_with_counts_and_utf8_intact(self):
        bundle = self._write_legacy_bundle()
        with ProjectMemory(self.root) as memory:
            preview = memory.import_bundle(bundle)
            self.assertFalse(preview["applied"])
            self.assertEqual(preview["conflicts"], [])
            applied = memory.import_bundle(bundle, apply=True)
            self.assertTrue(applied["applied"])
            requirements = memory.list_requirements()
            self.assertEqual(len(requirements), 1)
            self.assertIn("doğrulanmalı", requirements[0]["text"])
            self.assertEqual(requirements[0]["status"], "confirmed")
            handoff = memory.latest_handoff()
            self.assertEqual(handoff["handoff_id"], "h_00000000000000c3")
            self.assertNotIn("next_tasks", handoff["payload"])

    def test_v76_bundle_with_crlf_line_endings_is_accepted(self):
        bundle = self._write_legacy_bundle(newline="\r\n")
        self.assertIn(b"\r\n", bundle.read_bytes())
        with ProjectMemory(self.root) as memory:
            applied = memory.import_bundle(bundle, apply=True)
        self.assertTrue(applied["applied"])
        self.assertEqual(len(applied["conflicts"]), 0)

    def test_bundle_import_is_idempotent(self):
        bundle = self._write_legacy_bundle()
        with ProjectMemory(self.root) as memory:
            first = memory.import_bundle(bundle, apply=True)
            second = memory.import_bundle(bundle, apply=True)
            self.assertEqual(len(memory.list_decisions()), 1)
        self.assertTrue(first["applied"])
        self.assertEqual(second["imported"], 0)

    def test_bundle_for_another_project_is_rejected(self):
        bundle = self._write_legacy_bundle()
        other = Path(self.temp.name) / "başka proje"
        other.mkdir()
        with ProjectMemory(other) as memory:
            with self.assertRaises(Exception) as caught:
                memory.import_bundle(bundle, apply=True)
        self.assertIn("different project", str(caught.exception))

    # -- export/import round trip ---------------------------------------
    def test_exported_bundle_reimports_after_memory_reset(self):
        with ProjectMemory(self.root) as memory:
            memory.add_requirement("Ödeme akışı idempotent olmalı",
                                   category="confirmed", status="confirmed")
            memory.record_observation(
                kind="test_run", commit_sha="a" * 40, branch="main",
                files=["tests/test_pay.py"],
                test_evidence="pytest: 3 passed, 0 failed",
                platform="codex")
            target = Path(self.temp.name) / "yedek çıktı.json"
            exported = memory.export_bundle(target)
        self.assertEqual(exported["requirements"], 1)

        reset = self.root / ".auto-orchestrator" / "memory.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(reset) + suffix)
            if candidate.exists():
                candidate.unlink()
        with ProjectMemory(self.root) as memory:
            result = memory.import_bundle(target, apply=True)
            self.assertTrue(result["applied"], result)
            texts = [item["text"] for item in memory.list_requirements()]
            self.assertIn("Ödeme akışı idempotent olmalı", texts)

    # -- contract compatibility -----------------------------------------
    def test_v71_contract_without_schema_version_stays_v1(self):
        contract = {
            "contract_id": "rc_00000000000000d4",
            "contract_version": 1,
            "project_id": self._identity()["project_id"],
            "interview_id": "iv_00000000000000f6",
            "request_hash": "f" * 64,
            "goal": "Oturum güvenliğini tamamla",
            "source": {"kind": "grill-me", "ref": "interview:iv_00000000000000f6"},
            "acceptance_criteria": [{
                "id": "ac_session_refresh",
                "text": "Oturum 15 dakikada bir yenilenir",
                "applies_to": ["build"],
                "evidence": {"kind": "not_automated"},
            }],
        }
        normalized = requirements_contract.validate_contract(contract)
        self.assertEqual(requirements_contract.contract_schema_version(contract), 1)
        self.assertNotIn("schema_version", normalized)
        self.assertEqual(normalized["sha256"],
                         requirements_contract.contract_digest(normalized))

    def test_explicit_v2_contract_keeps_its_version_and_digest(self):
        contract = {
            "schema_version": 2,
            "contract_id": "rc_00000000000000e5",
            "contract_version": 3,
            "project_id": self._identity()["project_id"],
            "interview_id": "iv_00000000000000a7",
            "request_hash": "e" * 64,
            "goal": "JWT yenileme akışını doğrula",
            "source": {"kind": "grill-me", "ref": "interview:iv_00000000000000a7"},
            "verification_command": "python3.11 -m unittest",
            "acceptance_criteria": [{
                "id": "ac_jwt_refresh",
                "text": "JWT yenileme testi geçer",
                "applies_to": ["build", "verify"],
                "evidence": {"kind": "not_automated"},
            }],
        }
        normalized = requirements_contract.validate_contract(contract)
        self.assertEqual(requirements_contract.contract_schema_version(contract), 2)
        self.assertEqual(normalized["schema_version"], 2)
        again = requirements_contract.validate_contract(normalized)
        self.assertEqual(again["sha256"], normalized["sha256"])

    # -- memory reopen ---------------------------------------------------
    def test_v77_memory_survives_reopen(self):
        with ProjectMemory(self.root) as memory:
            record = memory.record_handoff(
                run_id="run-1", milestone_id="M1", platform="opencode",
                payload={"milestone_id": "M1", "completed_tasks": ["T1"]})
        with ProjectMemory(self.root) as reopened:
            stored = reopened.get_handoff(record["handoff_id"])
        self.assertEqual(stored["sha256"], record["sha256"])
        self.assertEqual(stored["payload"]["completed_tasks"], ["T1"])


if __name__ == "__main__":
    unittest.main()
