"""V8.0 native dispatch lifecycle and ownership checks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import native_dispatch
from tools import universal_teamwork as universal

REPO = Path(__file__).resolve().parent.parent


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
        (self.root / "README.md").write_text("x\n")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "i"],
                       check=True, capture_output=True)

    def create(self, task="build", scope=None, dependencies=None, ide="codex"):
        return native_dispatch.create(
            self.root, run_id="run-v8", task_id=task, role="implementer",
            ide=ide, owner_session_id="session-a", write_scope=scope or ["src/"],
            read_scope=["src/"], dependencies=dependencies or [])

    def evidence(self, dispatch, *, verified=True, task="build",
                 run_id="run-v8", verification_run_id="vr_test"):
        path = (self.root / ".auto-orchestrator" / "verification" /
                "dispatch.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "kind": "native-verification",
            "verified": verified,
            "run_id": run_id,
            "task_id": task,
            "verification_run_id": verification_run_id,
        }), encoding="utf-8")
        return path.relative_to(self.root).as_posix()

    def output(self):
        path = self.root / "out.txt"
        path.write_text("result\n", encoding="utf-8")
        return "out.txt"


class LifecycleTests(DispatchBase):
    def test_happy_path_requires_observed_child_and_real_evidence(self):
        dispatch = self.create()
        self.assertEqual(dispatch["state"], "queued")
        native_dispatch.request(self.root, dispatch["dispatch_id"], run_id="run-v8")
        native_dispatch.confirm(self.root, dispatch["dispatch_id"],
                                run_id="run-v8")
        with self.assertRaises(native_dispatch.DispatchError):
            native_dispatch.start(self.root, dispatch["dispatch_id"],
                                  run_id="run-v8")
        native_dispatch.confirm(self.root, dispatch["dispatch_id"],
                                run_id="run-v8", native_child_ref="child-1")
        # confirm from the wrong state is rejected
        with self.assertRaises(native_dispatch.DispatchError):
            native_dispatch.confirm(self.root, dispatch["dispatch_id"],
                                    run_id="run-v8")
        native_dispatch.start(self.root, dispatch["dispatch_id"],
                              run_id="run-v8")
        native_dispatch.permission(self.root, dispatch["dispatch_id"],
                                   run_id="run-v8", waiting=True)
        native_dispatch.permission(self.root, dispatch["dispatch_id"],
                                   run_id="run-v8", waiting=False)
        native_dispatch.complete(self.root, dispatch["dispatch_id"],
                                 run_id="run-v8", result_ref="out.txt")
        native_dispatch.collect(self.root, dispatch["dispatch_id"],
                                run_id="run-v8", evidence_ref=self.output())
        evidence = self.evidence(dispatch)
        verified = native_dispatch.verify(
            self.root, dispatch["dispatch_id"], run_id="run-v8",
            verification_run_id="vr_test", evidence_ref=evidence)
        self.assertEqual(verified["state"], "verified")
        self.assertTrue(verified["output_digest"])
        self.assertEqual(verified["verification_run_id"], "vr_test")

    def test_running_never_fabricated_without_child(self):
        dispatch = self.create()
        native_dispatch.request(self.root, dispatch["dispatch_id"], run_id="run-v8")
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
        first = self.create(task="build", scope=["src/"])
        with self.assertRaises(native_dispatch.DispatchError):
            self.create(task="test", scope=["tests/"], dependencies=["build"])
        native_dispatch.request(self.root, first["dispatch_id"], run_id="run-v8")
        native_dispatch.confirm(self.root, first["dispatch_id"],
                                run_id="run-v8", native_child_ref="c")
        native_dispatch.start(self.root, first["dispatch_id"], run_id="run-v8")
        native_dispatch.complete(self.root, first["dispatch_id"],
                                 run_id="run-v8", result_ref="out.txt")
        native_dispatch.collect(self.root, first["dispatch_id"],
                                run_id="run-v8", evidence_ref=self.output())
        native_dispatch.verify(
            self.root, first["dispatch_id"], run_id="run-v8",
            verification_run_id="vr_test",
            evidence_ref=self.evidence(first))
        second = self.create(task="test", scope=["tests/"],
                             dependencies=["build"])
        self.assertEqual(second["state"], "queued")

    def test_forged_or_wrong_task_verification_is_rejected(self):
        dispatch = self.create()
        native_dispatch.request(self.root, dispatch["dispatch_id"], run_id="run-v8")
        native_dispatch.confirm(self.root, dispatch["dispatch_id"],
                                run_id="run-v8", native_child_ref="c")
        native_dispatch.start(self.root, dispatch["dispatch_id"], run_id="run-v8")
        native_dispatch.complete(self.root, dispatch["dispatch_id"],
                                 run_id="run-v8", result_ref="out.txt")
        native_dispatch.collect(self.root, dispatch["dispatch_id"],
                                run_id="run-v8", evidence_ref=self.output())
        for verified, task, run_id, vr in (
                (False, "build", "run-v8", "vr_test"),
                (True, "other", "run-v8", "vr_test"),
                (True, "build", "other", "vr_test"),
                (True, "build", "run-v8", "vr_wrong")):
            with self.subTest(verified=verified, task=task, run=run_id,
                              vr=vr):
                evidence = self.evidence(
                    dispatch, verified=verified, task=task,
                    run_id=run_id, verification_run_id=vr)
                with self.assertRaises(native_dispatch.DispatchError):
                    native_dispatch.verify(
                        self.root, dispatch["dispatch_id"], run_id="run-v8",
                        verification_run_id="vr_test",
                        evidence_ref=evidence)

    def test_missing_output_evidence_fails(self):
        dispatch = self.create()
        native_dispatch.request(self.root, dispatch["dispatch_id"], run_id="run-v8")
        native_dispatch.confirm(self.root, dispatch["dispatch_id"],
                                run_id="run-v8", native_child_ref="c")
        native_dispatch.start(self.root, dispatch["dispatch_id"], run_id="run-v8")
        native_dispatch.complete(self.root, dispatch["dispatch_id"],
                                 run_id="run-v8", result_ref="out.txt")
        with self.assertRaises(native_dispatch.DispatchError):
            native_dispatch.collect(self.root, dispatch["dispatch_id"],
                                    run_id="run-v8", evidence_ref="missing.txt")

    def test_recover_never_marks_completed(self):
        dispatch = self.create()
        native_dispatch.request(self.root, dispatch["dispatch_id"], run_id="run-v8")
        recovered = native_dispatch.recover(
            self.root, dispatch["dispatch_id"], run_id="run-v8",
            reason="lifecycle lost")
        self.assertEqual(recovered["state"], "recovery_required")
        self.assertFalse(recovered["stop_ack"])
        self.assertFalse(recovered["native_cancel_ack"])
        status = native_dispatch.status(self.root, run_id="run-v8")
        self.assertEqual(status["counts"]["recovery_required"], 1)
        self.assertEqual(status["verified"], [])


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
        self.cli("native-dispatch", "request", "--run-id", "run-cli",
                 "--dispatch-id", dispatch["dispatch_id"])
        self.cli("native-dispatch", "confirm", "--run-id", "run-cli",
                 "--dispatch-id", dispatch["dispatch_id"],
                 "--native-child-ref", "child-1")
        started = self.cli(
            "native-dispatch", "start", "--run-id", "run-cli",
            "--dispatch-id", dispatch["dispatch_id"])
        self.assertEqual(started["dispatch"]["state"], "running")
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
