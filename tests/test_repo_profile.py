from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.repo_profile import RepoProfileError, profile_repository


class RepoProfileTests(unittest.TestCase):
    def test_profile_is_bounded_and_has_no_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "backend").mkdir()
            (root / "frontend").mkdir()
            (root / "tests").mkdir()
            (root / "backend" / "app.py").write_text("x = 1\n", encoding="utf-8")
            (root / "frontend" / "app.js").write_text("x = 1\n", encoding="utf-8")
            (root / "tests" / "test_app.py").write_text("x = 1\n", encoding="utf-8")
            (root / "package.json").write_text("{}", encoding="utf-8")
            (root / "AGENTS.md").write_text("rules", encoding="utf-8")
            profile = profile_repository(root)
            self.assertEqual(profile["files"], 5)
            self.assertEqual(profile["tests"], 1)
            self.assertEqual(profile["languages"], ["python", "javascript"])
            self.assertTrue(profile["backend"])
            self.assertTrue(profile["frontend"])
            self.assertTrue(profile["has_manifest"])
            self.assertTrue(profile["has_agents_md"])
            serialized = str(profile)
            self.assertNotIn(str(root), serialized)
            self.assertIn("python3.11 -m unittest", profile["test_commands"][0])

    def test_bounds_and_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(5):
                (root / f"f{index}.py").write_text("x", encoding="utf-8")
            profile = profile_repository(root, max_files=2)
            self.assertEqual(profile["files"], 2)
            self.assertTrue(profile["truncated"])
        with self.assertRaises(RepoProfileError):
            profile_repository(Path("/definitely/missing"))
        with self.assertRaises(RepoProfileError):
            profile_repository(Path(temp), max_files=0)


if __name__ == "__main__":
    unittest.main()
