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

    def test_manifest_specific_test_commands_are_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text(
                '{"scripts": {"test": "node --test"}}\n', encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "app.js").write_text("x", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "app.test.js").write_text("x", encoding="utf-8")
            profile = profile_repository(root)
            self.assertEqual(profile["test_commands"][0], "npm test")
            self.assertEqual(profile["test_command_sources"][0]["source"],
                             "package.json scripts.test")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text(
                "[tool.pytest.ini_options]\naddopts = '-q'\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_app.py").write_text("x", encoding="utf-8")
            profile = profile_repository(root)
            self.assertEqual(profile["test_commands"][0],
                             "python3.11 -m pytest -q")

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
