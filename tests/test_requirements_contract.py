from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.requirements_contract import (
    ContractError,
    contract_digest,
    contracts_dir,
    load_contract,
    load_current,
    next_contract_version,
    save_contract,
    validate_contract,
    validate_for_plan,
)

PROJECT_ID = "p_" + "a" * 16
OTHER_PROJECT_ID = "p_" + "b" * 16
CONTRACT_ID = "rc_" + "1" * 16
OTHER_CONTRACT_ID = "rc_" + "2" * 16
INTERVIEW_ID = "iv_" + "c" * 16
DECISION_ID = "d_" + "3" * 16
COMMAND = "python3.11 -m unittest tests.test_requirements_contract -q"


def contract(version=1, *, contract_id=CONTRACT_ID, project_id=PROJECT_ID,
             interview_id=INTERVIEW_ID, **overrides):
    value = {
        "contract_id": contract_id,
        "contract_version": version,
        "status": "confirmed",
        "project_id": project_id,
        "interview_id": interview_id,
        "goal": "Ship a verified requirements contract",
        "source": {"kind": "user", "ref": "test-request"},
        "confirmed_decisions": [{
            "decision_ref": DECISION_ID,
            "topic": "database",
            "decision": "Use SQLite",
            "rationale": "local single-user deployment",
            "confirmed_by": "user",
        }],
        "constraints": ["Keep the public API stable"],
        "out_of_scope": ["Mobile application"],
        "acceptance_criteria": [
            {
                "id": "ac_01_database",
                "text": "SQLite is used for persistence",
                "applies_to": ["build", "verify"],
                "decision_ref": DECISION_ID,
                "evidence": {"kind": "code_evidence", "topic": "database",
                             "expected": "sqlite"},
            },
            {
                "id": "ac_02_tests",
                "text": "The targeted test command passes",
                "applies_to": ["verify"],
                "decision_ref": None,
                "evidence": {"kind": "command", "command": COMMAND},
            },
            {
                "id": "ac_03_review",
                "text": "Reviewer signs off on the evidence",
                "applies_to": ["review"],
                "decision_ref": None,
                "evidence": {"kind": "not_automated"},
            },
            {
                "id": "ac_04_audit",
                "text": "Auditor reconciles every claim",
                "applies_to": ["audit"],
                "decision_ref": None,
                "evidence": {"kind": "not_automated"},
            },
        ],
        "verification_strategy": {
            "commands": [],
            "required_roles": ["tester", "reviewer"],
        },
        "blockers": [],
        "tasks": [],
        "created_at": 1000.0,
        "approved_at": 1001.0,
    }
    value.update(overrides)
    return value


class RequirementsContractStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_roundtrip_is_digest_bound(self):
        saved = save_contract(self.root, contract())
        loaded = load_contract(self.root, CONTRACT_ID, 1)
        self.assertEqual(loaded["sha256"], saved["sha256"])
        self.assertEqual(loaded["sha256"], contract_digest(loaded))
        self.assertEqual(loaded["status"], "confirmed")
        self.assertEqual(loaded["project_id"], PROJECT_ID)
        self.assertEqual(loaded["contract_version"], 1)
        self.assertNotIn("path", loaded)
        self.assertEqual(load_current(self.root), loaded)
        self.assertEqual(validate_contract(saved)["sha256"], saved["sha256"])
        metadata = {**loaded, "created_at": 7.0, "approved_at": 8.0,
                    "superseded_by": {"contract_id": OTHER_CONTRACT_ID,
                                      "contract_version": 1}}
        self.assertEqual(contract_digest(metadata), loaded["sha256"])

    def test_digest_mismatch_is_rejected(self):
        loaded = save_contract(self.root, contract())
        tampered = {**loaded, "goal": "Silently changed goal"}
        with self.assertRaises(ContractError):
            validate_contract(tampered)

    def test_tampered_file_is_rejected(self):
        save_contract(self.root, contract())
        path = contracts_dir(self.root) / f"{CONTRACT_ID}-v1.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["goal"] = "Tampered on disk"
        path.write_text(json.dumps(value, indent=2), encoding="utf-8")
        with self.assertRaises(ContractError):
            load_contract(self.root, CONTRACT_ID, 1)
        with self.assertRaises(ContractError):
            load_current(self.root)

    def test_v1_to_v2_supersedes_only_same_contract(self):
        first = save_contract(self.root, contract(1))
        second = save_contract(self.root, contract(
            2, supersedes={"contract_id": CONTRACT_ID, "contract_version": 1}))
        previous = load_contract(self.root, CONTRACT_ID, 1)
        current = load_contract(self.root, CONTRACT_ID, 2)
        self.assertEqual(previous["status"], "superseded")
        self.assertEqual(previous["superseded_by"],
                         {"contract_id": CONTRACT_ID, "contract_version": 2})
        self.assertEqual(current["status"], "confirmed")
        self.assertEqual(current["sha256"], second["sha256"])
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(load_current(self.root)["contract_version"], 2)
        self.assertEqual(next_contract_version(self.root, CONTRACT_ID), 3)

        other = save_contract(self.root, contract(
            1, contract_id=OTHER_CONTRACT_ID,
            interview_id="iv_" + "d" * 16))
        self.assertEqual(other["status"], "confirmed")
        self.assertEqual(load_contract(self.root, CONTRACT_ID, 2)["status"],
                         "confirmed")
        self.assertEqual(load_current(self.root)["contract_id"],
                         OTHER_CONTRACT_ID)
        self.assertEqual(next_contract_version(self.root, CONTRACT_ID), 3)
        self.assertEqual(next_contract_version(self.root, OTHER_CONTRACT_ID), 2)

    def test_version_is_allocated_per_contract_id(self):
        save_contract(self.root, contract(1))
        save_contract(self.root, contract(
            1, contract_id=OTHER_CONTRACT_ID,
            interview_id="iv_" + "d" * 16))
        self.assertEqual(next_contract_version(self.root, CONTRACT_ID), 2)
        self.assertEqual(next_contract_version(self.root, OTHER_CONTRACT_ID), 2)
        save_contract(self.root, contract(
            2, supersedes={"contract_id": CONTRACT_ID, "contract_version": 1}))
        self.assertEqual(next_contract_version(self.root, CONTRACT_ID), 3)
        self.assertEqual(next_contract_version(self.root, OTHER_CONTRACT_ID), 2)

    def test_duplicate_modified_version_and_skipped_version_are_rejected(self):
        save_contract(self.root, contract(1))
        with self.assertRaises(ContractError):
            save_contract(self.root, contract(1, goal="A different contract"))
        with self.assertRaises(ContractError):
            save_contract(self.root, contract(3))
        # Re-publishing the byte-identical contract is idempotent.
        identical = save_contract(self.root, contract(1))
        self.assertEqual(identical["contract_version"], 1)

    def test_live_ledger_change_invalidates_a_contract(self):
        from tools.project_memory import ProjectMemory
        from tools.requirements_contract import validate_contract_state
        saved = save_contract(self.root, contract())
        with ProjectMemory(self.root) as memory:
            memory.conn.execute(
                "UPDATE decisions SET status='superseded',updated_at=? "
                "WHERE decision_id=?", (1.0, DECISION_ID))
            memory.conn.commit()
        with self.assertRaises(ContractError):
            validate_contract_state(self.root, saved)

    def test_contract_command_cannot_run_arbitrary_code(self):
        value = contract()
        value["acceptance_criteria"][1]["evidence"] = {
            "kind": "command",
            "command": "python -c \"__import__('os').system('x')\"",
        }
        with self.assertRaises(ContractError):
            validate_contract(value)

    def test_stale_blocker_and_wrong_project_are_rejected_for_plans(self):
        for status in ("superseded", "invalidated"):
            with self.subTest(status=status):
                with self.assertRaises(ContractError):
                    validate_for_plan(contract(status=status),
                                      project_id=PROJECT_ID)
        blocked = contract(blockers=[{
            "id": "Q1", "text": "Deployment target is unknown",
            "blocking": True,
        }])
        with self.assertRaises(ContractError):
            validate_for_plan(blocked, project_id=PROJECT_ID)
        with self.assertRaises(ContractError):
            save_contract(self.root, blocked)
        with self.assertRaises(ContractError):
            validate_for_plan(contract(), project_id=OTHER_PROJECT_ID)
        normalized = validate_for_plan(contract(), project_id=PROJECT_ID)
        self.assertEqual(normalized["project_id"], PROJECT_ID)

    def test_oversized_contract_is_rejected(self):
        oversized = contract()
        oversized["constraints"] = [
            ("c" * 1190) + f"{index:04d}" for index in range(80)
        ]
        oversized["out_of_scope"] = [
            ("o" * 1190) + f"{index:04d}" for index in range(80)
        ]
        with self.assertRaises(ContractError) as raised:
            save_contract(self.root, oversized)
        self.assertIn("too large", str(raised.exception))
        self.assertFalse((contracts_dir(self.root) /
                          f"{CONTRACT_ID}-v1.json").exists())

    def test_missing_pointer_means_no_current_contract(self):
        self.assertIsNone(load_current(self.root))
        self.assertEqual(next_contract_version(self.root, CONTRACT_ID), 1)


if __name__ == "__main__":
    unittest.main()
