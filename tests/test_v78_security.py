"""V7.8 hardening: release-gated accept, idle evidence and session authority.

These tests target the merge-blocking review on PR #32. They use the real
module and the real CLI in-process, plus subprocesses where a race must be
observed across processes.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools import ide_handoff as handoff
from tools import universal_teamwork as universal

REPO = Path(__file__).resolve().parent.parent
REQUEST = ("Add a short contributor note to the README and one small unit "
           "test. Keep the change documentation-only.")


class SecurityBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "proje"
        self.root.mkdir()
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "tests@example.invalid"),
                     ("config", "user.name", "V78 Security")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True,
                           capture_output=True)
        (self.root / ".gitignore").write_text(".auto-orchestrator/\n",
                                              encoding="utf-8")
        (self.root / "README.md").write_text("# demo\n", encoding="utf-8")
        self.commit("init")

    def commit(self, message):
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", message],
                       check=True, capture_output=True)

    def cli(self, platform, *args, expect=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = universal.main(["--root", str(self.root), "--platform",
                                   platform, *map(str, args)])
        self.assertEqual(code, expect,
                         f"{args} -> {code}: {stderr.getvalue()[:400]}")
        text = stdout.getvalue().strip()
        return json.loads(text) if text else {}

    def cli_error(self, platform, *args):
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = universal.main(["--root", str(self.root), "--platform",
                                   platform, *map(str, args)])
        return code, stderr.getvalue()

    def request_path(self):
        from tools import teamwork_chat
        value = teamwork_chat.request_path(
            type("A", (), {"root": str(self.root)})())
        target = Path(value["absolute_path"])
        target.write_text(REQUEST, encoding="utf-8")
        return str(target)

    def native_run(self):
        relative = self.request_path()
        self.cli("codex", "ide", "activate")
        self.cli("codex", "plan", "--request-file", relative, "--run-id",
                 "run-sec-1", "--grill-mode", "off")
        self.cli("codex", "approve-plan", "--run-id", "run-sec-1",
                 "--approver", "tests")
        started = self.cli("codex", "start", "--plan", "run-sec-1")
        return started

    def credential_path(self, platform):
        return (self.root / ".auto-orchestrator" / "teamwork" /
                f"ide-owner-{platform}.json")


class IdleEvidenceTests(SecurityBase):
    def test_prepare_requires_explicit_idle_even_with_fresh_heartbeat(self):
        active = self.cli("codex", "ide", "activate")
        status = self.cli("codex", "ide", "status")
        owner = status["result"]["liveness"]["owner"]
        self.assertEqual(owner["status"], "heartbeat_fresh")
        self.assertTrue(owner["alive"])
        self.assertFalse(owner["process_verified"])
        code, error = self.cli_error(
            "codex", "ide", "prepare", "--target-platform", "opencode")
        self.assertEqual(code, 1)
        self.assertIn("confirm-idle", error)
        prepared = self.cli(
            "codex", "ide", "prepare", "--target-platform", "opencode",
            "--confirm-idle", "user")
        self.assertEqual(prepared["status"], "handoff_prepared")
        snapshot = handoff.status(self.root)["snapshot"]
        self.assertEqual(snapshot["native_child_state"], "unknown")
        self.assertFalse(active["result"].get("native_cancel_ack"))

    def test_release_from_active_requires_idle_confirmation(self):
        self.cli("codex", "ide", "activate")
        code, error = self.cli_error("codex", "ide", "release")
        self.assertEqual(code, 1)
        self.assertIn("confirm-idle", error)

    def test_observed_native_child_requires_user_confirmation(self):
        self.native_run()
        database = self.root / ".auto-orchestrator" / "supervisor.sqlite3"
        import sqlite3
        connection = sqlite3.connect(database)
        try:
            with connection:
                connection.execute(
                    "INSERT OR REPLACE INTO native_bindings"
                    "(run_id,task_id,attempt,platform,native_hash,started,"
                    "stopped) VALUES (?,?,?,?,?,?,?)",
                    ("run-sec-1", "build", 1, "codex", "hash-security",
                     1, 0))
        finally:
            connection.close()
        status = handoff.status(self.root)
        self.assertEqual(status["liveness"]["native_children"]["state"],
                         "observed_active")
        blocked = handoff.prepare_handoff(
            self.root, *self._owner(),
            "opencode", idle_confirmation="owner_confirmed")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reasons"][0]["code"], "native_child_active")
        allowed = handoff.prepare_handoff(
            self.root, *self._owner(),
            "opencode", idle_confirmation="user_confirmed")
        self.assertTrue(allowed["ok"], allowed)
        snapshot = json.loads(
            (self.root / allowed["handoff"]["path"]).read_text(
                encoding="utf-8"))
        self.assertEqual(snapshot["native_child_state"], "observed_active")
        self.assertFalse(snapshot["digest"].startswith("0"))

    def _owner(self):
        credential = json.loads(
            self.credential_path("codex").read_text(encoding="utf-8"))
        return credential["owner_token"], credential["fence"]


class AtomicityTests(SecurityBase):
    def test_failed_ownership_write_leaves_no_orphan_snapshot(self):
        from unittest import mock
        self.cli("codex", "ide", "activate")
        credential = json.loads(
            self.credential_path("codex").read_text(encoding="utf-8"))
        directory = handoff._snapshots_dir(self.root)
        before = sorted(path.name for path in directory.glob("*.json"))
        with mock.patch.object(
                handoff, "_write_ownership",
                side_effect=handoff.HandoffError("simulated crash")):
            with self.assertRaises(handoff.HandoffError):
                handoff.prepare_handoff(
                    self.root, credential["owner_token"],
                    credential["fence"], "opencode",
                    idle_confirmation="user_confirmed")
        after = sorted(path.name for path in directory.glob("*.json"))
        self.assertEqual(after, before)
        record = json.loads(
            handoff._ownership_path(self.root).read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "active")


class SessionAuthorityTests(SecurityBase):
    def test_missing_credential_blocks_same_ide_mutation(self):
        self.cli("codex", "ide", "activate")
        self.credential_path("codex").unlink()
        code, error = self.cli_error(
            "codex", "plan", "--request-file", self.request_path(),
            "--run-id", "run-no-cred", "--grill-mode", "off")
        self.assertEqual(code, 1)
        self.assertIn("no owner credential", error)
        # Read-only inspection remains available to the second session.
        status = self.cli("codex", "ide", "status")
        self.assertEqual(status["status"], "active")

    def test_explicit_owner_id_mismatch_is_rejected(self):
        self.cli("codex", "ide", "activate", "--owner-id", "session-a")
        code, error = self.cli_error(
            "codex", "ide", "prepare", "--target-platform", "opencode",
            "--confirm-idle", "user", "--owner-id", "session-b")
        self.assertEqual(code, 1)
        self.assertIn("different session", error)

    def test_accept_replay_token_is_scoped_to_the_recorded_session(self):
        self.cli("codex", "ide", "activate")
        prepared = self.cli("codex", "ide", "prepare", "--target-platform",
                            "opencode", "--confirm-idle", "user")
        item = prepared["result"]["handoff"]
        self.cli("codex", "ide", "release", "--handoff",
                 item["handoff_id"], "--confirm-idle", "user")
        first = handoff.accept(self.root, item["handoff_id"], item["digest"],
                               "opencode", session_id="target")
        self.assertTrue(first["ok"], first)
        replay = handoff.accept(self.root, item["handoff_id"], item["digest"],
                                "opencode", session_id="target")
        self.assertTrue(replay["unchanged"])
        self.assertEqual(replay["owner_token"], first["owner_token"])
        other = handoff.accept(self.root, item["handoff_id"], item["digest"],
                               "opencode", session_id="intruder")
        self.assertFalse(other["ok"])
        self.assertNotIn("owner_token", other)


class ConcurrentAcceptTests(SecurityBase):
    def test_concurrent_accept_has_one_state_transition(self):
        self.cli("codex", "ide", "activate")
        prepared = self.cli("codex", "ide", "prepare", "--target-platform",
                            "opencode", "--confirm-idle", "user")
        item = prepared["result"]["handoff"]
        self.cli("codex", "ide", "release", "--handoff",
                 item["handoff_id"], "--confirm-idle", "user")
        fence_before = json.loads(
            handoff._ownership_path(self.root).read_text(encoding="utf-8"))["fence"]
        script = (
            "import json,sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from tools.ide_handoff import accept\n"
            "result = accept(sys.argv[2], sys.argv[3], sys.argv[4], "
            "'opencode', session_id=sys.argv[5])\n"
            "print(json.dumps({'ok': result['ok'], "
            "'changed': result['changed'], "
            "'unchanged': result.get('unchanged', False), "
            "'fence': result.get('fence')}))\n"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPO)
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(REPO), str(self.root),
                 item["handoff_id"], item["digest"], f"session-{index}"],
                cwd=str(REPO), env=environment, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            for index in range(2)
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(sum(1 for item in results if item["changed"]), 1)
        self.assertEqual(sum(1 for item in results if item["ok"]), 1)
        self.assertEqual(sum(1 for item in results if item["unchanged"]), 0)
        record = json.loads(
            handoff._ownership_path(self.root).read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "transferred")
        self.assertEqual(record["fence"], fence_before + 1)


class SnapshotEvidenceTests(SecurityBase):
    def test_large_file_is_marked_sampled_not_full(self):
        active = self.cli("codex", "ide", "activate")
        token = json.loads(
            self.credential_path("codex").read_text(encoding="utf-8"))
        target = self.root / "large.bin"
        target.write_bytes(b"a" * (3 * 1024 * 1024))
        prepared = handoff.prepare_handoff(
            self.root, token["owner_token"], token["fence"], "opencode",
            allow_dirty=True, idle_confirmation="user_confirmed")
        self.assertTrue(prepared["ok"], prepared)
        snapshot = json.loads(
            (self.root / prepared["handoff"]["path"]).read_text(
                encoding="utf-8"))
        self.assertEqual(snapshot["dirty_coverage"], "sampled")
        entry = next(item for item in snapshot["changed_files"]
                     if item["path"] == "large.bin")
        self.assertEqual(entry["kind"], "large")
        body = target.read_bytes()
        target.write_bytes(body[:len(body) // 2] + b"b" + body[len(body) // 2 + 1:])
        credential = json.loads(
            self.credential_path("codex").read_text(encoding="utf-8"))
        released = handoff.release(
            self.root, credential["owner_token"], prepared["fence"],
            prepared["handoff"]["handoff_id"],
            idle_confirmation="user_confirmed")
        self.assertTrue(released["ok"], released)
        # Sampled coverage is explicit; the snapshot never claimed full
        # content integrity for a large file.
        accepted = handoff.accept(
            self.root, prepared["handoff"]["handoff_id"],
            prepared["handoff"]["digest"], "opencode")
        self.assertTrue(accepted["ok"], accepted)

    def test_many_changes_report_partial_coverage(self):
        self.cli("codex", "ide", "activate")
        token = json.loads(
            self.credential_path("codex").read_text(encoding="utf-8"))
        for index in range(205):
            (self.root / f"bulk-{index:03d}.txt").write_text(
                "x\n", encoding="utf-8")
        prepared = handoff.prepare_handoff(
            self.root, token["owner_token"], token["fence"], "opencode",
            allow_dirty=True, idle_confirmation="user_confirmed")
        self.assertTrue(prepared["ok"], prepared)
        snapshot = json.loads(
            (self.root / prepared["handoff"]["path"]).read_text(
                encoding="utf-8"))
        self.assertEqual(snapshot["dirty_coverage"], "partial")
        self.assertFalse(snapshot["changed_complete"])
        self.assertEqual(snapshot["changed_total"], 205)
        self.assertEqual(len(snapshot["changed_files"]), 200)

    def test_rename_records_the_origin_path(self):
        self.cli("codex", "ide", "activate")
        token = json.loads(
            self.credential_path("codex").read_text(encoding="utf-8"))
        subprocess.run(["git", "-C", str(self.root), "mv", "README.md",
                        "GUIDE.md"], check=True, capture_output=True)
        prepared = handoff.prepare_handoff(
            self.root, token["owner_token"], token["fence"], "opencode",
            allow_dirty=True, idle_confirmation="user_confirmed")
        self.assertTrue(prepared["ok"], prepared)
        snapshot = json.loads(
            (self.root / prepared["handoff"]["path"]).read_text(
                encoding="utf-8"))
        entry = next(item for item in snapshot["changed_files"]
                     if item["status"].startswith("R"))
        self.assertEqual(entry["path"], "GUIDE.md")
        self.assertEqual(entry["from_path"], "README.md")


if __name__ == "__main__":
    unittest.main()
