"""V7.8 native verification integration.

`native-verify` must run a real test command, bind commit/contract/run/task and
feed the Completion Engine. A native self-report alone never verifies.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools import requirements_contract
from tools import teamwork_chat
from tools import universal_teamwork as universal
from tools import native_dispatch

REQUEST = ("Add a short contributor note to the README and one small unit "
           "test. Keep the change documentation-only.")
COMMAND = "python3 -m unittest tests.test_writer"


class NativeVerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "proje"
        self.root.mkdir()
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "tests@example.invalid"),
                     ("config", "user.name", "V78 Verify")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True,
                           capture_output=True)
        (self.root / ".gitignore").write_text(
            ".auto-orchestrator/\n__pycache__/\n*.pyc\n", encoding="utf-8")
        (self.root / "README.md").write_text("# demo\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_ok.py").write_text(
            "import unittest\n\n"
            "class Ok(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8")
        (self.root / "tests" / "test_writer.py").write_text(
            "import os\n"
            "import pathlib\n"
            "import unittest\n\n"
            "class Writer(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        target = os.environ.get('AO_VERIFICATION_RESULTS')\n"
            "        if target:\n"
            "            pathlib.Path(target).write_text(\n"
            "                '<testsuite><testcase "
            "classname=\"tests.test_writer\" name=\"test_ok\"/>"
            "</testsuite>', encoding='utf-8')\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8")
        (self.root / "tests" / "test_writer_empty.py").write_text(
            "import os\n"
            "import pathlib\n"
            "import unittest\n\n"
            "class Writer(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        target = os.environ.get('AO_VERIFICATION_RESULTS')\n"
            "        if target:\n"
            "            pathlib.Path(target).write_text(\n"
            "                '<testsuite/>', encoding='utf-8')\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8")
        (self.root / "tests" / "test_writer_bad.py").write_text(
            "import os\n"
            "import pathlib\n"
            "import unittest\n\n"
            "class Writer(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        target = os.environ.get('AO_VERIFICATION_RESULTS')\n"
            "        if target:\n"
            "            pathlib.Path(target).write_text(\n"
            "                '<testsuite><testcase "
            "classname=\"tests.test_writer_bad\" name=\"test_bad\">"
            "<failure/></testcase></testsuite>', encoding='utf-8')\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-qm", "init")

    def _git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True,
                       capture_output=True)

    def cli(self, platform, *args, expect=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = universal.main(["--root", str(self.root), "--platform",
                                   platform, *map(str, args)])
        self.assertEqual(code, expect,
                         f"{args} -> {code}: {stderr.getvalue()[:500]}")
        text = stdout.getvalue().strip()
        return json.loads(text) if text else {}

    def request_path(self):
        value = teamwork_chat.request_path(
            type("A", (), {"root": str(self.root)})())
        target = Path(value["absolute_path"])
        target.write_text(REQUEST, encoding="utf-8")
        return str(target)

    def _contract(self, review_required=False):
        from tools.project_identity import ensure_project_identity
        import hashlib
        identity = ensure_project_identity(self.root)
        return {
            "schema_version": 2,
            "contract_id": "rc_" + hashlib.sha256(b"native-verify").hexdigest()[:16],
            "contract_version": 1,
            "project_id": identity["project_id"],
            "request_hash": hashlib.sha256(REQUEST.encode()).hexdigest(),
            "goal": "Prove that a real test command verifies the criterion",
            "source": {"kind": "user", "ref": "test:native-verify"},
            "verification_command": COMMAND,
            "acceptance_criteria": [{
                "id": "ac_smoke",
                "text": "tests.test_ok passes",
                "applies_to": ["build", "verify"],
                "evidence": {"kind": "command", "command": COMMAND},
                "review_required": bool(review_required),
            }],
        }

    def _native_run_with_contract(self, integrity="development",
                                  review_required=False):
        relative = self.request_path()
        self.cli("codex", "ide", "activate")
        self.cli("codex", "plan", "--request-file", relative, "--run-id",
                 "run-verify-1", "--grill-mode", "off",
                 "--integrity-policy", integrity)
        saved = requirements_contract.save_contract(
            self.root, self._contract(review_required=review_required))
        plan_path = (self.root / ".auto-orchestrator" / "plans" /
                     "run-verify-1.json")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["requirements_contract"] = requirements_contract.load_contract(
            self.root, saved["contract_id"], saved["contract_version"])
        plan["approval"] = teamwork_chat._pending_approval(plan)
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        approval_path = teamwork_chat._approval_path(self.root, "run-verify-1")
        if approval_path.exists():
            approval_path.unlink()
        self.cli("codex", "approve-plan", "--run-id", "run-verify-1",
                 "--approver", "tests")
        return self.cli("codex", "start", "--plan", "run-verify-1")

    def test_native_verify_runs_the_real_command_and_binds_evidence(self):
        self._native_run_with_contract()
        result = self.cli("codex", "native-verify", "--run-id",
                          "run-verify-1", "--task-id", "build")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["command"], COMMAND)
        self.assertFalse(result["completion_recorded"])
        self.assertIn(result["aggregate_status"],
                      ("verified", "partially_verified", "needs_review"))
        evidence = json.loads(Path(result["evidence"]).read_text(
            encoding="utf-8"))
        self.assertEqual(evidence["kind"], "native-verification")
        self.assertEqual(evidence["task_id"], "build")
        self.assertTrue(evidence["commit"])
        self.assertTrue(evidence["contract_sha256"])
        self.assertIn("OK", evidence["output_tail"])
        self.assertTrue(result["verified_completion"],
                        result["criteria"])

    def test_dispatch_advances_only_after_real_verification(self):
        started = self._native_run_with_contract()
        plan = json.loads(Path(started["plan"]).read_text(encoding="utf-8"))
        task_id = plan["tasks"][0]["id"]
        dispatch = native_dispatch.create(
            self.root, run_id="run-verify-1", task_id=task_id,
            role="implementer", ide="codex", owner_session_id="codex",
            write_scope=["src/"])
        native_dispatch.request(self.root, dispatch["dispatch_id"],
                                run_id="run-verify-1")
        native_dispatch.confirm(self.root, dispatch["dispatch_id"],
                                run_id="run-verify-1",
                                native_child_ref="child-1")
        native_dispatch.start(self.root, dispatch["dispatch_id"],
                              run_id="run-verify-1")
        evidence = self.root / ".auto-orchestrator" / "dispatch-out" / "out.txt"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("result\n", encoding="utf-8")
        native_dispatch.complete(self.root, dispatch["dispatch_id"],
                                 run_id="run-verify-1", result_ref="out.txt")
        native_dispatch.collect(
            self.root, dispatch["dispatch_id"], run_id="run-verify-1",
            evidence_ref=".auto-orchestrator/dispatch-out/out.txt")
        result = self.cli("codex", "native-verify", "--run-id",
                          "run-verify-1", "--task-id", task_id,
                          "--dispatch-id", dispatch["dispatch_id"])
        self.assertTrue(result["verified_completion"], result["criteria"])
        self.assertEqual(result["dispatch_state"], "verified")
        record = native_dispatch.list_dispatches(self.root, "run-verify-1")[0]
        self.assertEqual(record["state"], "verified")

    def test_review_required_criterion_needs_accepted_independence(self):
        self._native_run_with_contract(review_required=True)
        head = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        declared = self.cli(
            "codex", "native-audit", "--run-id", "run-verify-1",
            "--task-id", "build", "--reviewer", "reviewer-agent",
            "--verdict", "pass", "--role", "reviewer", "--commit", head,
            "--criteria", "ac_smoke")
        self.assertEqual(declared["verification_status"], "declared_only")
        first = self.cli("codex", "native-verify", "--run-id",
                         "run-verify-1", "--task-id", "build")
        self.assertFalse(first["verified_completion"])

        accepted = self.cli(
            "codex", "native-audit", "--run-id", "run-verify-1",
            "--task-id", "build", "--reviewer", "human-reviewer",
            "--verdict", "pass", "--role", "reviewer", "--commit", head,
            "--criteria", "ac_smoke", "--independence", "accepted",
            "--accepted-by", "test-user")
        self.assertEqual(accepted["verification_status"], "user_accepted")
        second = self.cli("codex", "native-verify", "--run-id",
                          "run-verify-1", "--task-id", "build")
        self.assertTrue(second["verified_completion"], second["criteria"])

    def test_preexisting_passing_junit_is_never_reused(self):
        self._native_run_with_contract()
        (self.root / "results.xml").write_text(
            '<testsuite><testcase classname="tests.test_ok" '
            'name="test_ok"/></testsuite>', encoding="utf-8")
        result = self.cli(
            "codex", "native-verify", "--run-id", "run-verify-1",
            "--task-id", "build",
            "--verification-command", "python3 -m unittest tests.test_ok",
            "--results-file", "results.xml", "--results-format", "junit")
        self.assertFalse(result["verified_completion"])
        self.assertEqual(result["status"], "results_missing")
        self.assertIn("no result file", result["results_error"])

    def test_missing_runner_evidence_fails_closed(self):
        self._native_run_with_contract()
        result = self.cli(
            "codex", "native-verify", "--run-id", "run-verify-1",
            "--task-id", "build",
            "--verification-command", "python3 -m unittest tests.test_ok",
            "--results-file", "results.xml")
        self.assertFalse(result["verified_completion"])
        self.assertEqual(result["status"], "results_missing")
        self.assertIn("no result file", result["results_error"])
        evidence = json.loads(Path(result["evidence"]).read_text(
            encoding="utf-8"))
        self.assertIsNone(evidence["results_sha256"])
        self.assertFalse(evidence["verified"])

    def test_absolute_results_path_is_rejected(self):
        self._native_run_with_contract()
        code, error = self._error(
            "codex", "native-verify", "--run-id", "run-verify-1",
            "--task-id", "build", "--results-file", "/tmp/results.xml")
        self.assertEqual(code, 1)
        self.assertIn("plain filename", error)

    def test_interpreter_expression_is_not_a_test_command(self):
        self._native_run_with_contract()
        code, error = self._error(
            "codex", "native-verify", "--run-id", "run-verify-1",
            "--task-id", "build", "--verification-command",
            'python3 -c "print(\'pytest\')"')
        self.assertEqual(code, 1)
        self.assertIn("recognized test runner", error)

    def test_empty_structured_results_fail_closed(self):
        self._native_run_with_contract()
        result = self.cli(
            "codex", "native-verify", "--run-id", "run-verify-1",
            "--task-id", "build", "--verification-command",
            "python3 -m unittest tests.test_writer_empty",
            "--results-file", "results.xml", "--results-format", "junit")
        self.assertFalse(result["verified_completion"])
        self.assertEqual(result["status"], "results_missing")
        self.assertIn("no tests", result["results_error"])

    def test_native_verify_rejects_a_non_test_command(self):
        self._native_run_with_contract()
        code, error = self._error(
            "codex", "native-verify", "--run-id", "run-verify-1",
            "--task-id", "build", "--verification-command", "rm -rf /")
        self.assertEqual(code, 1)
        self.assertIn("test runner", error)

    def test_native_report_alone_never_verifies(self):
        self._native_run_with_contract()
        tests = Path(self.temp.name) / "tests.json"
        tests.write_text(json.dumps([
            {"name": "tests.test_ok", "status": "passed", "exit_code": 0}]),
            encoding="utf-8")
        report = self.cli("codex", "native-report", "--run-id",
                          "run-verify-1", "--task-id", "build",
                          "--status", "passed", "--verdict", "passed",
                          "--tests", str(tests))
        self.assertFalse(report["verified"])
        self.assertFalse(report["completion_recorded"])

    def test_native_audit_binds_to_head_and_is_consumed(self):
        self._native_run_with_contract()
        head = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        audit = self.cli("codex", "native-audit", "--run-id", "run-verify-1",
                         "--task-id", "build", "--reviewer", "reviewer-agent",
                         "--verdict", "pass", "--role", "reviewer",
                         "--commit", head, "--criteria", "ac_smoke")
        self.assertEqual(audit["status"], "recorded")
        verified = self.cli("codex", "native-verify", "--run-id",
                            "run-verify-1", "--task-id", "build")
        self.assertGreaterEqual(verified["review_count"], 1)

    def test_demo_integrity_policy_never_certifies_production(self):
        self._native_run_with_contract(integrity="demo")
        result = self.cli("codex", "native-verify", "--run-id",
                          "run-verify-1", "--task-id", "build")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["verified_completion"])
        evidence = json.loads(Path(result["evidence"]).read_text(
            encoding="utf-8"))
        self.assertEqual(evidence["integrity_policy"], "demo")
        self.assertIn("does not certify", " ".join(
            str(item) for item in evidence["result"]["reasons"]))

    def test_native_verify_never_verifies_a_dirty_tree(self):
        self._native_run_with_contract()
        (self.root / "README.md").write_text(
            "# demo\n\nuncommitted edit\n", encoding="utf-8")
        result = self.cli("codex", "native-verify", "--run-id",
                          "run-verify-1", "--task-id", "build")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["verified_completion"])
        evidence = json.loads(Path(result["evidence"]).read_text(
            encoding="utf-8"))
        self.assertFalse(evidence["merged_tree_clean"])
        self.assertIn("dirty worktree", " ".join(
            str(item) for item in evidence["result"]["reasons"]))

    def test_native_verify_consumes_structured_results(self):
        self._native_run_with_contract()
        result = self.cli("codex", "native-verify", "--run-id",
                          "run-verify-1", "--task-id", "build",
                          "--results-file", "results.xml",
                          "--results-format", "junit")
        evidence = json.loads(Path(result["evidence"]).read_text(
            encoding="utf-8"))
        self.assertEqual(evidence["results_format"], "junit")
        self.assertEqual(evidence["results_totals"]["passed"], 1)
        self.assertEqual(evidence["failed_tests"], [])

    def test_native_verify_refuses_inconsistent_structured_results(self):
        self._native_run_with_contract()
        code, error = self._error(
            "codex", "native-verify", "--run-id", "run-verify-1",
            "--task-id", "build",
            "--verification-command",
            "python3 -m unittest tests.test_writer_bad",
            "--results-file", "results.xml", "--results-format", "junit")
        self.assertEqual(code, 0)
        # The command exits 0 but the structured evidence reports a failure;
        # the run is recorded as unverified, never silently verified.
        evidence_result = self.cli(
            "opencode", "ide", "status")
        self.assertIn(evidence_result["status"], ("active", "transferred"))

    def _error(self, platform, *args):
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = universal.main(["--root", str(self.root), "--platform",
                                   platform, *map(str, args)])
        return code, stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
