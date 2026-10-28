"""V8.0 stabilization stress: repeated cross-process dispatch races."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import native_dispatch

REPO = Path(__file__).resolve().parent.parent


class DispatchStressTests(unittest.TestCase):
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

    def test_twenty_concurrent_create_rounds_have_one_winner_each(self):
        script = (
            "import sys\n"
            "from tools.native_dispatch import create\n"
            "try:\n"
            "    create(sys.argv[1], run_id=sys.argv[2], task_id='build',\n"
            "           role='implementer', ide='codex',\n"
            "           owner_session_id='s', write_scope=['src/'])\n"
            "    print('ok')\n"
            "except Exception as exc:\n"
            "    print('err', type(exc).__name__)\n"
            "    raise SystemExit(1)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO)
        for round_index in range(20):
            run_id = f"stress-{round_index:02d}"
            processes = [subprocess.Popen(
                [sys.executable, "-c", script, str(self.root), run_id],
                cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, errors="replace")
                for _ in range(2)]
            codes = []
            for process in processes:
                process.communicate(timeout=60)
                codes.append(process.returncode)
            with self.subTest(run_id=run_id):
                self.assertEqual(sorted(codes), [0, 1])
                records = native_dispatch.list_dispatches(self.root, run_id)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["state"], "queued")


    def test_alias_scope_race_has_one_winner(self):
        script = (
            "import sys\n"
            "from tools.native_dispatch import create\n"
            "try:\n"
            "    create(sys.argv[1], run_id='alias-race', task_id=sys.argv[2],\n"
            "           role='implementer', ide='codex',\n"
            "           owner_session_id='s', write_scope=[sys.argv[3]])\n"
            "    print('ok')\n"
            "except Exception as exc:\n"
            "    print('err', type(exc).__name__)\n"
            "    raise SystemExit(1)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO)
        processes = [subprocess.Popen(
            [sys.executable, "-c", script, str(self.root), f"alias-{i}", scope],
            cwd=str(REPO), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace")
            for i, scope in enumerate(("src/api.py", "src/./api.py"))]
        codes = []
        for process in processes:
            process.communicate(timeout=60)
            codes.append(process.returncode)
        self.assertEqual(sorted(codes), [0, 1])
        records = native_dispatch.list_dispatches(self.root, "alias-race")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["write_scope"], ["src/api.py"])


if __name__ == "__main__":
    unittest.main()
