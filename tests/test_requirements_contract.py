from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from tools.project_identity import ensure_project_identity
from tools.project_memory import ProjectMemory
from tools.requirements_contract import (
    DEFAULT_VERIFICATION_COMMAND,
    ContractError,
    build_from_interview,
    contract_digest,
    contract_schema_version,
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

    def test_republishing_an_old_version_does_not_roll_back_current_pointer(self):
        save_contract(self.root, contract(1))
        save_contract(self.root, contract(2))
        save_contract(self.root, contract(1))
        current = load_current(self.root)
        self.assertEqual(current["contract_version"], 2)

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

    def test_v1_normalization_and_digest_are_byte_identical(self):
        normalized = validate_contract(contract())
        # This is the pre-V7.7 normalized digest. Adding schema v2 must never
        # change an old contract body or its stored digest.
        self.assertEqual(
            normalized["sha256"],
            "5c8a94fddfc43b078ea3831696dcd18e7107abaab9ba3bf333d60b73eb3aa6e1")
        self.assertNotIn("schema_version", normalized)
        self.assertNotIn("verification_command", normalized)
        self.assertEqual(contract_schema_version(contract()), 1)
        saved = save_contract(self.root, contract())
        self.assertEqual(saved["sha256"], normalized["sha256"])
        self.assertEqual(load_contract(self.root, CONTRACT_ID, 1)["sha256"],
                         normalized["sha256"])

    def test_v2_adds_bound_behavior_fields_without_storing_status(self):
        value = contract()
        value["schema_version"] = 2
        value["acceptance_criteria"][0].update({
            "requirement_id": "req_01_database",
            "critical": True,
            "review_required": True,
            "implementation_files": ["db/schema.sql"],
            "test_cases": [{
                "test_id": "test_migration_up",
                "expected_behavior": "The forward migration applies.",
                "negative": False,
                "required": True,
                "status": "verified",
            }],
            "verification_status": "verified",
        })
        normalized = validate_contract(value)
        self.assertEqual(normalized["schema_version"], 2)
        self.assertEqual(normalized["verification_command"],
                         DEFAULT_VERIFICATION_COMMAND)
        criterion = normalized["acceptance_criteria"][0]
        self.assertEqual(criterion["requirement_id"], "req_01_database")
        self.assertTrue(criterion["critical"])
        self.assertTrue(criterion["review_required"])
        self.assertEqual(criterion["implementation_files"], ["db/schema.sql"])
        self.assertEqual(criterion["test_cases"][0]["test_id"],
                         "test_migration_up")
        self.assertNotIn("status", criterion["test_cases"][0])
        self.assertNotIn("verification_status", criterion)
        self.assertNotIn("status", criterion)
        self.assertEqual(normalized["sha256"], contract_digest(normalized))
        saved = save_contract(self.root, value)
        self.assertEqual(saved["sha256"], normalized["sha256"])
        self.assertEqual(contract_schema_version(saved), 2)

    def test_v2_test_cases_are_bounded_and_unique(self):
        value = contract()
        value["schema_version"] = 2
        value["acceptance_criteria"][0]["test_cases"] = [{
            "test_id": f"test_case_{index}",
            "expected_behavior": "bounded",
            "negative": False,
            "required": True,
        } for index in range(13)]
        with self.assertRaises(ContractError):
            validate_contract(value)
        value["acceptance_criteria"][0]["test_cases"] = [{
            "test_id": "test_duplicate",
            "expected_behavior": "bounded",
            "negative": False,
            "required": True,
        }] * 2
        with self.assertRaises(ContractError):
            validate_contract(value)


class RequirementsContractBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.identity = ensure_project_identity(self.root)
        self.counter = 0

    def _insert_interview(self, summary="Add a feature", decisions=None):
        self.counter += 1
        interview_id = "iv_" + hashlib.sha256(
            f"interview-{self.counter}".encode()).hexdigest()[:16]
        question_id = "q_" + hashlib.sha256(
            f"question-{self.counter}".encode()).hexdigest()[:16]
        now = time.time()
        with ProjectMemory(self.root, identity=self.identity) as memory:
            memory.conn.execute(
                "INSERT INTO interviews(interview_id,project_id,origin_platform,"
                "request_hash,request_summary,mode,status,approved_at,created_at,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (interview_id, self.identity["project_id"], "opencode",
                 hashlib.sha256(summary.encode()).hexdigest(), summary,
                 "auto", "approved", now, now, now))
            memory.conn.execute(
                "INSERT INTO interview_questions(interview_id,question_id,"
                "ordinal,question_key,question,options,recommended_option,"
                "status,depends_on,created_at,answered_at) VALUES(?,?,?,?,?,?,?,"
                "?,?,?,?)",
                (interview_id, question_id, 1, "scope", "Scope?", "[]", None,
                 "answered", "[]", now, now))
            memory.conn.commit()
            for topic, decision in decisions or []:
                memory.add_decision(
                    topic=topic, decision=decision, source="user",
                    source_ref=f"question:{question_id}", status="confirmed",
                    confirmed_by="tester")
        return interview_id

    def _contract(self, decisions):
        interview_id = self._insert_interview(decisions=decisions)
        return build_from_interview(self.root, interview_id)

    def test_generated_contract_is_v2_with_default_command(self):
        value = self._contract([("database", "Use SQLite")])
        self.assertEqual(value["schema_version"], 2)
        self.assertEqual(value["verification_command"],
                         DEFAULT_VERIFICATION_COMMAND)
        self.assertEqual(value["sha256"], contract_digest(value))
        self.assertEqual(contract_schema_version(value), 2)

    def test_jwt_decision_generates_access_refresh_rejection_and_expiry(self):
        value = self._contract([("auth_session", "JWT")])
        criterion = next(item for item in value["acceptance_criteria"]
                         if item["decision_ref"])
        ids = {case["test_id"] for case in criterion["test_cases"]}
        self.assertEqual(ids, {
            "test_jwt_access_issuance",
            "test_jwt_refresh_issuance",
            "test_invalid_refresh_rejected",
            "test_jwt_expiry_enforced",
        })
        negative = {case["test_id"] for case in criterion["test_cases"]
                    if case["negative"]}
        self.assertEqual(negative, {"test_invalid_refresh_rejected"})
        self.assertTrue(criterion["critical"])
        self.assertTrue(criterion["review_required"])

    def test_cookie_decision_generates_issue_rejection_expiry_and_logout(self):
        value = self._contract([("auth_session", "session cookie")])
        criterion = next(item for item in value["acceptance_criteria"]
                         if item["decision_ref"])
        ids = {case["test_id"] for case in criterion["test_cases"]}
        self.assertEqual(ids, {
            "test_session_issued",
            "test_invalid_session_rejected",
            "test_session_expiry_enforced",
            "test_logout_invalidates_session",
        })

    def test_migration_and_file_access_decisions_generate_behavior(self):
        migration = self._contract([("database", "Use SQLite")])
        migration_criterion = next(
            item for item in migration["acceptance_criteria"]
            if item["decision_ref"])
        self.assertEqual(
            {case["test_id"] for case in migration_criterion["test_cases"]},
            {"test_migration_up", "test_migration_down_or_rollback"})

        file_contract = self._contract([("file_access", "owner only")])
        file_criterion = next(
            item for item in file_contract["acceptance_criteria"]
            if item["decision_ref"])
        self.assertEqual(
            {case["test_id"] for case in file_criterion["test_cases"]},
            {"test_file_access_owner_allowed",
             "test_file_access_unauthorized_denied",
             "test_file_delete_policy"})

    def test_no_decision_does_not_invent_behavioral_tests(self):
        value = self._contract([])
        self.assertEqual(value["schema_version"], 2)
        for criterion in value["acceptance_criteria"]:
            self.assertEqual(criterion["test_cases"], [])


if __name__ == "__main__":
    unittest.main()
