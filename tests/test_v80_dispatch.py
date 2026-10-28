"""V8.0 native dispatch lifecycle, evidence provenance and path safety."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools import native_dispatch
from tools import universal_teamwork as universal
from tools import verification_receipt
from tools.project_identity import ensure_project_identity

REPO = Path(__file__).resolve().parent.parent
RECEIPT_ID = "vr_0123456789abcdef"


class DispatchBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "proje"
        self.root.mkdir()
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "a@b.c"),
                     ("config", "user.name", "t")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True,
                           capture_output=True)
        (self.root / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "i"],
                       check=True, capture_output=True)
        self.commit = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()

    def create(self, task="build", scope=None, dependencies=None, ide="codex",
               **kwargs):
        return native_dispatch.create(
            self.root, run_id="run-v8", task_id=task, role="implementer",
            ide=ide, owner_session_id="session-a", write_scope=scope or ["src/"],
            read_scope=["src/"], dependencies=dependencies or [], **kwargs)

    def receipt(self, dispatch, *, verified=True, task="build",
                run_id="run-v8", verification_run_id=RECEIPT_ID,
                commit=None, contract=None, plan=None, exit_code=0,
                rewrite_record=False):
        commit = commit or self.commit
        record = {
            "kind": "native-verification",
            "verified": verified,
            "run_id": run_id,
            "task_id": task,
            "verification_run_id": verification_run_id,
            "commit": commit,
            "exit_code": exit_code,
        }
        record_path = (self.root / ".auto-orchestrator" / "verification" /
                       f"{run_id}-{task}-{verification_run_id}.json")
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record), encoding="utf-8")
        digest = hashlib.sha256(json.dumps(
            record, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()
        verification_receipt.issue(self.root, {
            "verification_run_id": verification_run_id,
            "project_id": ensure_project_identity(self.root)["project_id"],
            "run_id": run_id,
            "task_id": task,
            "commit": commit,
            "contract_sha256": contract,
            "plan_sha256": plan,
            "verified": verified,
            "exit_code": exit_code,
            "results_sha256": "a" * 64,
            "output_sha256": "b" * 64,
            "record_sha256": digest,
            "record_ref": record_path.relative_to(self.root).as_posix(),
            "issued_at": time.time(),
        })
        if rewrite_record:
            record_path.write_text(json.dumps({**record, "tampered": True}),
                                   encoding="utf-8")
        return verification_run_id

    def output(self, name="out.txt", content="result\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return name

    def collected(self, **kwargs):
        dispatch = self.create(**kwargs)
        native_dispatch.request(self.root, dispatch["dispatch_id"],
                                run_id="run-v8")
        native_dispatch.confirm(self.root, dispatch["dispatch_id"],
                                run_id="run-v8", native_child_ref="child-1")
        native_dispatch.start(self.root, dispatch["dispatch_id"],
                              run_id="run-v8")
        native_dispatch.complete(self.root, dispatch["dispatch_id"],
                                 run_id="run-v8", result_ref="out.txt")
        native_dispatch.collect(self.root, dispatch["dispatch_id"],
                                run_id="run-v8", evidence_ref=self.output())
        return dispatch


class LifecycleTests(DispatchBase):
    def test_happy_path_with_a_trusted_receipt(self):
        dispatch = self.collected()
        receipt_id = self.receipt(dispatch)
        verified = native_dispatch.verify(
            self.root, dispatch["dispatch_id"], run_id="run-v8",
            verification_run_id=receipt_id)
        self.assertEqual(verified["state"], "verified")
        self.assertEqual(verified["child_provenance"], "declared")
        self.assertFalse(verified["native_lifecycle_observed"])
        self.assertTrue(verified["output_digest"])
        self.assertEqual(verified["verification_receipt_id"], receipt_id)

    def test_handwritten_verified_json_is_not_accepted(self):
        dispatch = self.collected()
        path = (self.root / ".auto-orchestrator" / "verification" /
                "fake.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "kind": "native-verification", "verified": True,
            "run_id": "run-v8", "task_id": "build",
            "verification_run_id": RECEIPT_ID,
        }), encoding="utf-8")
        with self.assertRaises(verification_receipt.ReceiptError):
            native_dispatch.verify(
                self.root, dispatch["dispatch_id"], run_id="run-v8",
                verification_run_id=RECEIPT_ID)

    def test_wrong_commit_contract_plan_task_run_and_failed_receipt(self):
        dispatch = self.create(commit=self.commit,
                               contract_sha256="c" * 64,
                               plan_sha256="d" * 64)
        native_dispatch.request(self.root, dispatch["dispatch_id"],
                                run_id="run-v8")
        native_dispatch.confirm(self.root, dispatch["dispatch_id"],
                                run_id="run-v8", native_child_ref="child-1")
        native_dispatch.start(self.root, dispatch["dispatch_id"],
                              run_id="run-v8")
        native_dispatch.complete(self.root, dispatch["dispatch_id"],
                                 run_id="run-v8", result_ref="out.txt")
        native_dispatch.collect(self.root, dispatch["dispatch_id"],
                                run_id="run-v8", evidence_ref=self.output())
        cases = [
            ("wrong_commit", {"commit": "e" * 40}),
            ("wrong_task", {"task": "other"}),
            ("wrong_run", {"run_id": "other"}),
            ("wrong_contract", {"contract": "f" * 64}),
            ("wrong_plan", {"plan": "1" * 64}),
            ("not_verified", {"verified": False}),
        ]
        for index, (name, overrides) in enumerate(cases):
            with self.subTest(name=name):
                vr = f"vr_{index:016x}"
                self.receipt(dispatch, verification_run_id=vr, **overrides)
                with self.assertRaises(verification_receipt.ReceiptError):
                    native_dispatch.verify(
                        self.root, dispatch["dispatch_id"], run_id="run-v8",
                        verification_run_id=vr)

    def test_tampered_record_and_missing_receipt_are_rejected(self):
        dispatch = self.collected()
        receipt_id = self.receipt(dispatch, rewrite_record=True)
        with self.assertRaises((native_dispatch.DispatchError,
                                verification_receipt.ReceiptError)):
            native_dispatch.verify(
                self.root, dispatch["dispatch_id"], run_id="run-v8",
                verification_run_id=receipt_id)
        with self.assertRaises(verification_receipt.ReceiptError):
            native_dispatch.verify(
                self.root, dispatch["dispatch_id"], run_id="run-v8",
                verification_run_id="vr_ffffffffffffffff")

    def test_receipt_becomes_stale_when_head_moves(self):
        dispatch = self.collected()
        receipt_id = self.receipt(dispatch)
        (self.root / "next.txt").write_text("next\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "next"],
                       check=True, capture_output=True)
        with self.assertRaises(verification_receipt.ReceiptError):
            native_dispatch.verify(
                self.root, dispatch["dispatch_id"], run_id="run-v8",
                verification_run_id=receipt_id)

    def test_artifact_changed_after_collection_is_rejected(self):
        dispatch = self.collected()
        (self.root / "out.txt").write_text("changed\n", encoding="utf-8")
        receipt_id = self.receipt(dispatch)
        with self.assertRaises(native_dispatch.DispatchError):
            native_dispatch.verify(
                self.root, dispatch["dispatch_id"], run_id="run-v8",
                verification_run_id=receipt_id)

    def test_running_never_fabricated_without_child(self):
        dispatch = self.create()
        native_dispatch.request(self.root, dispatch["dispatch_id"],
                                run_id="run-v8")
        native_dispatch.confirm(self.root, dispatch["dispatch_id"],
                                run_id="run-v8")
        with self.assertRaises(native_dispatch.DispatchError) as caught:
            native_dispatch.start(self.root, dispatch["dispatch_id"],
                                  run_id="run-v8")
        self.assertIn("native child", str(caught.exception))
        record = native_dispatch.list_dispatches(self.root, "run-v8")[0]
        self.assertEqual(record["state"], "dispatch_confirmed")

    def test_duplicate_active_task_and_overlapping_scope_are_rejected(self):
        self.create(task="build", scope=["src/"])
        with self.assertRaises(native_dispatch.DispatchError):
            self.create(task="build", scope=["src/"])
        with self.assertRaises(native_dispatch.DispatchError):
            self.create(task="test", scope=["src/api.py"])

    def test_dependency_must_be_verified(self):
        first = self.collected(task="build")
        with self.assertRaises(native_dispatch.DispatchError):
            self.create(task="test", scope=["tests/"], dependencies=["build"])
        native_dispatch.verify(
            self.root, first["dispatch_id"], run_id="run-v8",
            verification_run_id=self.receipt(first))
        second = self.create(task="test", scope=["tests/"],
                             dependencies=["build"])
        self.assertEqual(second["state"], "queued")

    def test_recover_never_marks_completed_and_clears_verification(self):
        dispatch = self.collected()
        native_dispatch.recover(self.root, dispatch["dispatch_id"],
                                run_id="run-v8", reason="lifecycle lost")
        record = native_dispatch.list_dispatches(self.root, "run-v8")[0]
        self.assertEqual(record["state"], "recovery_required")
        self.assertIsNone(record["verification_run_id"])
        self.assertFalse(record["stop_ack"])
        self.assertFalse(record["native_cancel_ack"])

    def test_absolute_and_traversal_scopes_are_rejected(self):
        for scope in ("/src", "../src", "C:/src", "//server/share", "~/.ssh"):
            with self.subTest(scope=scope):
                with self.assertRaises(native_dispatch.DispatchError):
                    self.create(task=f"t-{abs(hash(scope))}", scope=[scope])

    def test_symlink_and_external_output_are_rejected(self):
        dispatch = self.create()
        native_dispatch.request(self.root, dispatch["dispatch_id"],
                                run_id="run-v8")
        native_dispatch.confirm(self.root, dispatch["dispatch_id"],
                                run_id="run-v8", native_child_ref="c")
        native_dispatch.start(self.root, dispatch["dispatch_id"],
                              run_id="run-v8")
        native_dispatch.complete(self.root, dispatch["dispatch_id"],
                                 run_id="run-v8", result_ref="out.txt")
        self.output()
        link = self.root / "link.txt"
        try:
            link.symlink_to(self.root / "out.txt")
        except (OSError, NotImplementedError):
            link = None
        if link is not None:
            with self.assertRaises(native_dispatch.DispatchError):
                native_dispatch.collect(
                    self.root, dispatch["dispatch_id"], run_id="run-v8",
                    evidence_ref="link.txt")
        with self.assertRaises(native_dispatch.DispatchError):
            native_dispatch.collect(
                self.root, dispatch["dispatch_id"], run_id="run-v8",
                evidence_ref="out.txt", output_dir="/tmp")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
        parent_link = self.root / "linked"
        try:
            parent_link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            parent_link = None
        if parent_link is not None:
            with self.assertRaises(native_dispatch.DispatchError):
                native_dispatch.collect(
                    self.root, dispatch["dispatch_id"], run_id="run-v8",
                    evidence_ref="linked/secret.txt")
        with self.assertRaises(native_dispatch.DispatchError):
            native_dispatch.collect(
                self.root, dispatch["dispatch_id"], run_id="run-v8",
                evidence_ref="/etc/passwd")

    def test_oversized_or_corrupt_receipt_is_rejected(self):
        dispatch = self.collected()
        receipt_id = self.receipt(dispatch)
        path = (self.root / ".auto-orchestrator" / "verification" /
                "receipts" / f"{receipt_id}.json")
        path.write_bytes(b" " * (verification_receipt.MAX_BYTES + 1))
        with self.assertRaises(verification_receipt.ReceiptError):
            verification_receipt.read(self.root, receipt_id)
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(verification_receipt.ReceiptError):
            verification_receipt.read(self.root, receipt_id)


class DispatchCliTests(DispatchBase):
    def cli(self, *args, expect=0):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = universal.main(["--root", str(self.root), "--platform",
                                   "codex", *args])
        self.assertEqual(code, expect, err.getvalue()[:400])
        text = out.getvalue().strip()
        return json.loads(text) if text else {}

    def test_cli_create_request_confirm_start_status(self):
        self.cli("ide", "activate")
        created = self.cli(
            "native-dispatch", "create", "--run-id", "run-cli",
            "--task-id", "build", "--write-scope", "src/",
            "--owner-session-id", "session-a")
        dispatch = created["dispatch"]
        self.assertEqual(dispatch["state"], "queued")
        self.assertTrue(dispatch["commit"])
        self.cli("native-dispatch", "request", "--run-id", "run-cli",
                 "--dispatch-id", dispatch["dispatch_id"])
        self.cli("native-dispatch", "confirm", "--run-id", "run-cli",
                 "--dispatch-id", dispatch["dispatch_id"],
                 "--native-child-ref", "child-1")
        started = self.cli(
            "native-dispatch", "start", "--run-id", "run-cli",
            "--dispatch-id", dispatch["dispatch_id"])
        self.assertEqual(started["dispatch"]["state"], "running")
        self.assertEqual(started["dispatch"]["child_provenance"], "declared")
        status = self.cli("native-dispatch", "status", "--run-id", "run-cli")
        self.assertEqual(status["dispatch"]["counts"]["running"], 1)
        self.assertTrue(status["read_only"])


class ConcurrencyTests(DispatchBase):
    def test_concurrent_overlapping_create_has_one_winner(self):
        script = (
            "import sys\n"
            "from tools.native_dispatch import create\n"
            "try:\n"
            "    create(sys.argv[1], run_id='run-v8', task_id=sys.argv[2],\n"
            "           role='implementer', ide='codex',\n"
            "           owner_session_id='s', write_scope=['src/'])\n"
            "    print('ok')\n"
            "except Exception as exc:\n"
            "    print('err', type(exc).__name__)\n"
            "    raise SystemExit(1)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO)
        processes = [subprocess.Popen(
            [sys.executable, "-c", script, str(self.root), f"task-{i}"],
            cwd=str(REPO), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace")
            for i in range(2)]
        codes = []
        for process in processes:
            process.communicate(timeout=60)
            codes.append(process.returncode)
        self.assertEqual(sorted(codes), [0, 1])
        self.assertEqual(len(native_dispatch.list_dispatches(self.root,
                                                            "run-v8")), 1)


if __name__ == "__main__":
    unittest.main()
