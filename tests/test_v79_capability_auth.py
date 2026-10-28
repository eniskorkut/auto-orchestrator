"""V7.9 authentication detection for Codex/OpenCode CLIs."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools import capabilities


def completed(args, code=0, stdout="", stderr=""):
    return type("R", (), {"returncode": code, "stdout": stdout,
                          "stderr": stderr})()


class AuthProbeTests(unittest.TestCase):
    def _detect(self, runner):
        paths = {"codex": "/fake/codex", "opencode": "/fake/opencode"}

        def which(name):
            return paths.get(name)

        return capabilities.detect_environment(
            root=Path("/nonexistent/project"), home=Path("/nonexistent/home"),
            apps_root=Path("/nonexistent/apps"), which=which, runner=runner,
            timeout=1.0)

    def test_codex_and_opencode_authenticated_from_real_cli_output(self):
        def runner(argv, timeout):
            if argv[1:] == ["--version"]:
                return completed(argv, 0, "codex-cli 9.9.9\n")
            if argv[0].endswith("codex"):
                return completed(argv, 0, "Logged in using ChatGPT\n")
            return completed(argv, 0, "OpenCode Go API key stored\n")

        result = self._detect(runner)
        self.assertTrue(result["platforms"]["codex"]["authenticated"])
        self.assertTrue(result["platforms"]["opencode"]["authenticated"])
        text = json.dumps(result)
        self.assertNotIn("ChatGPT", text)
        self.assertNotIn("API key", text)

    def test_negative_markers_mean_not_authenticated(self):
        def runner(argv, timeout):
            if argv[1:] == ["--version"]:
                return completed(argv, 0, "v1\n")
            if argv[0].endswith("codex"):
                return completed(argv, 1, "Not logged in\n")
            return completed(argv, 1, "No credentials found\n")

        result = self._detect(runner)
        self.assertFalse(result["platforms"]["codex"]["authenticated"])
        self.assertFalse(result["platforms"]["opencode"]["authenticated"])

    def test_empty_or_broken_probe_stays_unknown(self):
        def runner(argv, timeout):
            if argv[1:] == ["--version"]:
                return completed(argv, 0, "v1\n")
            raise OSError("boom")

        result = self._detect(runner)
        self.assertIsNone(result["platforms"]["codex"]["authenticated"])
        self.assertIsNone(result["platforms"]["opencode"]["authenticated"])

    def test_detection_fields_are_exact(self):
        def runner(argv, timeout):
            return completed(argv, 0, "v1\n")

        result = self._detect(runner)
        expected = set(capabilities.DETECTION_FIELDS)
        for platform in result["platforms"].values():
            self.assertEqual(set(platform), expected)


if __name__ == "__main__":
    unittest.main()
