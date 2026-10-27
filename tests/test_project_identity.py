from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.project_identity import (
    ProjectIdentityError,
    ensure_project_identity,
    read_project_identity,
)


class ProjectIdentityTests(unittest.TestCase):
    def test_identity_is_stable_across_copy_and_instance_is_local(self):
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "one"
            first.mkdir()
            a = ensure_project_identity(first)
            self.assertEqual(a["project_id"], ensure_project_identity(first)["project_id"])
            second = Path(temp) / "two"
            shutil.copytree(first, second)
            b = ensure_project_identity(second)
            self.assertEqual(a["project_uuid"], b["project_uuid"])
            self.assertEqual(a["project_id"], b["project_id"])
            self.assertNotEqual(a["instance_id"], b["instance_id"])
            self.assertEqual(read_project_identity(second)["project_uuid"], a["project_uuid"])

    @unittest.skipIf(os.name == "nt", "Windows subprocess lock stress is platform-specific")
    def test_concurrent_creation_yields_one_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            code = ("from tools.project_identity import ensure_project_identity\n"
                    "print(ensure_project_identity(%r)['project_id'])" % str(root))
            processes = [subprocess.Popen(
                [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(6)]
            outputs = []
            for process in processes:
                out, err = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, err)
                outputs.append(out.strip())
            self.assertEqual(len(set(outputs)), 1)
            self.assertEqual(read_project_identity(root)["project_id"], outputs[0])

    def test_legacy_id_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = "p_" + "a" * 16
            identity = ensure_project_identity(root, legacy_project_id=legacy)
            self.assertEqual(identity["project_id"], legacy)
            manifest = json.loads((root / ".auto-orchestrator" / "project.json").read_text())
            self.assertEqual(manifest["id_scheme"], "legacy")
            self.assertEqual(ensure_project_identity(root)["project_id"], legacy)

    def test_symlinked_state_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            outside = Path(temp) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / ".auto-orchestrator").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ProjectIdentityError):
                ensure_project_identity(root)
            self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ".auto-orchestrator" / "project.json"
            path.parent.mkdir()
            path.write_text('{"version": 1, "project_uuid": "bad"}')
            with self.assertRaises(ProjectIdentityError):
                ensure_project_identity(root)


if __name__ == "__main__":
    unittest.main()
