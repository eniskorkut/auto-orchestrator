from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools import grill_benchmark


class GrillBenchmarkTests(unittest.TestCase):
    def test_fixture_has_forty_bilingual_labelled_cases(self):
        fixture = grill_benchmark.FIXTURE
        self.assertEqual(len(fixture), 40)
        self.assertEqual(sum(1 for item in fixture if item["lang"] == "tr"), 20)
        self.assertEqual(sum(1 for item in fixture if item["lang"] == "en"), 20)
        required = {
            "expected_topics", "expected_question_keys", "should_interview",
            "forbidden_technologies", "seeded_decisions", "contradiction",
            "expected_floor",
        }
        for case in fixture:
            self.assertTrue(required.issubset(case), case["id"])
            self.assertIsInstance(case["expected_topics"], list)
            self.assertIsInstance(case["expected_floor"], int)

    def test_deterministic_metrics_and_no_unconfirmed_assumptions(self):
        report = grill_benchmark.run_benchmark()
        baseline = report["baseline"]
        self.assertEqual(baseline["cases"], 40)
        self.assertEqual(baseline["tr_cases"], 20)
        self.assertEqual(baseline["en_cases"], 20)
        self.assertEqual(baseline["ambiguity_recall"], 1.0)
        self.assertEqual(baseline["unnecessary_question_rate"], 0.0)
        self.assertEqual(baseline["duplicate_question_rate"], 0.0)
        self.assertEqual(baseline["false_tech_inference_cases"], 0)
        self.assertEqual(baseline["contradiction_recall"], 1.0)
        self.assertEqual(baseline["unconfirmed_assumptions"], 0)
        self.assertEqual(baseline["completion"], 1.0)
        self.assertEqual(report["provider"]["status"], "not_run")
        self.assertFalse(report["provider"]["configured"])

    def test_optional_provider_is_measured_without_writing_decisions(self):
        report = grill_benchmark.run_benchmark(
            provider=lambda prompt: json.dumps({"topics": []}))
        provider = report["provider"]
        self.assertEqual(provider["status"], "run")
        self.assertTrue(provider["configured"])
        self.assertEqual(provider["decisions_written"], 0)
        self.assertEqual(provider["unconfirmed_assumptions"], 0)
        self.assertEqual(report["comparison"]["provider_authoritative"], False)

    def test_asset_report_matches_the_run(self):
        asset = grill_benchmark.REPO_ROOT / "docs" / "assets" / (
            "v77-grill-benchmark.json")
        self.assertTrue(asset.is_file())
        report = json.loads(asset.read_text(encoding="utf-8"))
        self.assertEqual(report["fixture"]["total"], 40)
        self.assertEqual(report["fixture"]["tr"], 20)
        self.assertEqual(report["fixture"]["en"], 20)
        self.assertEqual(report["baseline"]["cases"], 40)
        self.assertEqual(report["baseline"]["unconfirmed_assumptions"], 0)
        self.assertEqual(report["provider"]["status"], "not_run")

    def test_save_report_is_atomic_and_utf8(self):
        report = grill_benchmark.run_benchmark(include_cases=False)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "report.json"
            saved = grill_benchmark.save_report(target, report)
            self.assertTrue(saved["path"].endswith("report.json"))
            loaded = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(loaded["kind"], "v77-grill-benchmark")
        self.assertEqual(loaded["baseline"]["completion"], 1.0)


if __name__ == "__main__":
    unittest.main()
