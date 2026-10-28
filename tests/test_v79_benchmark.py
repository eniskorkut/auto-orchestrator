"""V7.9 benchmark corpus and measurement honesty."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tools import v79_benchmark

REPO = Path(__file__).resolve().parent.parent


class BenchmarkCorpusTests(unittest.TestCase):
    def test_corpus_has_twelve_unique_categories(self):
        report = v79_benchmark.validate_corpus()
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["total"], 12)
        ids = [item["id"] for item in v79_benchmark.CORPUS]
        self.assertEqual(len(ids), len(set(ids)))
        categories = {item["category"] for item in v79_benchmark.CORPUS}
        self.assertEqual(len(categories), 12)

    def test_validation_detects_a_missing_acceptance(self):
        corpus = [{"id": "x", "category": "bugfix", "risk": "low",
                   "request": "r", "acceptance": [],
                   "test_command": "python3 -m unittest"}]
        report = v79_benchmark.validate_corpus(corpus)
        self.assertFalse(report["ok"])
        self.assertTrue(report["errors"])

    def test_list_contains_no_fabricated_metrics(self):
        process = subprocess.run(
            [sys.executable, str(REPO / "tools" / "v79_benchmark.py"),
             "--list"], capture_output=True, text=True, timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr)
        payload = json.loads(process.stdout)
        text = json.dumps(payload).lower()
        for forbidden in ("tokens", "cost", "savings", "speedup"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(len(payload["tasks"]), 12)

    def test_run_requires_a_prepared_project(self):
        process = subprocess.run(
            [sys.executable, str(REPO / "tools" / "v79_benchmark.py"),
             "--run", "small-bugfix"], capture_output=True, text=True,
            timeout=60)
        self.assertEqual(process.returncode, 2)
        self.assertIn("--project", process.stderr)

    def test_run_measures_only_real_output(self):
        root = Path(tempfile.mkdtemp())
        (root / "tests").mkdir()
        (root / "tests" / "test_ok.py").write_text(
            "import unittest\n"
            "class Ok(unittest.TestCase):\n"
            "    def test_ok(self): self.assertTrue(True)\n",
            encoding="utf-8")
        # The corpus command is the repository's; use a temporary copy with a
        # command that exists in this fixture.
        task = dict(v79_benchmark.CORPUS[0])
        task["test_command"] = f"{sys.executable} -m unittest tests.test_ok"
        with mock.patch.object(v79_benchmark, "CORPUS",
                                        [task]):
            import io
            from contextlib import redirect_stdout
            output = io.StringIO()
            with redirect_stdout(output):
                code = v79_benchmark.main([
                    "--run", "small-bugfix", "--project", str(root)])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["measured"]["exit_code"] == 0)
        self.assertGreaterEqual(payload["measured"]["seconds"], 0)
        self.assertNotIn("tokens", payload)
        self.assertNotIn("cost", payload)
        self.assertNotIn("tokens", payload["measured"])
        self.assertNotIn("cost", payload["measured"])


if __name__ == "__main__":
    unittest.main()
