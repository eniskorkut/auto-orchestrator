"""V7.9 structured test evidence parsing."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools import test_evidence

JUNIT = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="auth">
    <testcase classname="tests.test_auth" name="test_login"/>
    <testcase classname="tests.test_auth" name="test_refresh">
      <failure message="expired"/>
    </testcase>
    <testcase classname="tests.test_auth" name="test_logout">
      <skipped/>
    </testcase>
  </testsuite>
</testsuites>
"""


class TestEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_junit_statuses_are_normalized(self):
        path = self.write("junit.xml", JUNIT)
        result = test_evidence.parse_file(path)
        self.assertEqual(result["format"], "junit")
        self.assertEqual(result["totals"],
                         {"passed": 1, "failed": 1, "skipped": 1, "total": 3})
        self.assertEqual(result["passed_names"], ["tests.test_auth.test_login"])
        self.assertEqual(result["failed_names"],
                         ["tests.test_auth.test_refresh"])
        self.assertEqual(result["skipped_names"],
                         ["tests.test_auth.test_logout"])

    def test_doctype_and_entity_payloads_are_rejected(self):
        path = self.write("bad.xml", (
            '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]>'
            "<testsuite><testcase name=\"x\"/></testsuite>"))
        with self.assertRaises(test_evidence.TestEvidenceError):
            test_evidence.parse_file(path)

    def test_malformed_xml_and_json_are_rejected(self):
        with self.assertRaises(test_evidence.TestEvidenceError):
            test_evidence.parse_file(self.write("bad.xml", "<testsuite>"))
        with self.assertRaises(test_evidence.TestEvidenceError):
            test_evidence.parse_file(self.write("bad.json", "{"))

    def test_pytest_json_list_and_dict_forms(self):
        path = self.write("pytest.json", json.dumps({
            "tests": [
                {"nodeid": "tests/test_a.py::test_ok", "outcome": "passed"},
                {"nodeid": "tests/test_a.py::test_bad", "outcome": "failed"},
                {"nodeid": "tests/test_a.py::test_skip", "outcome": "skipped"},
            ]}))
        result = test_evidence.parse_file(path, "pytest-json")
        self.assertEqual(result["totals"]["passed"], 1)
        self.assertEqual(result["totals"]["failed"], 1)
        self.assertEqual(result["totals"]["skipped"], 1)
        listing = self.write("pytest-list.json", json.dumps([
            {"name": "a", "outcome": "passed"}]))
        self.assertEqual(test_evidence.parse_file(listing)["totals"]["passed"], 1)

    def test_unknown_status_is_rejected(self):
        path = self.write("pytest.json", json.dumps({
            "tests": [{"nodeid": "a", "outcome": "maybe"}]}))
        with self.assertRaises(test_evidence.TestEvidenceError):
            test_evidence.parse_file(path)

    def test_oversized_file_is_rejected(self):
        path = self.root / "large.json"
        path.write_bytes(b" " * (test_evidence.MAX_BYTES + 1))
        with self.assertRaises(test_evidence.TestEvidenceError):
            test_evidence.parse_file(path)

    def test_symlink_is_rejected(self):
        target = self.write("junit.xml", JUNIT)
        link = self.root / "link.xml"
        link.symlink_to(target)
        with self.assertRaises(test_evidence.TestEvidenceError):
            test_evidence.parse_file(link)


if __name__ == "__main__":
    unittest.main()
