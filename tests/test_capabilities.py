"""Contract tests for the conservative native capability matrix."""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tools import capabilities


EXPECTED = {
    "codex": {
        "native_spawn_exposed_to_external_orchestrator": False,
        "exact_child_identity": "conditional",
        "native_start_stop_proof": "conditional",
        "cancel_support": False,
        "background_tracking": False,
        "resume_support": False,
        "live_terminal_stream": False,
        "session_event_stream": "hook-callbacks",
    },
    "opencode": {
        "native_spawn_exposed_to_external_orchestrator": False,
        "exact_child_identity": "foreground-only",
        "native_start_stop_proof": "foreground-only",
        "cancel_support": False,
        "background_tracking": False,
        "resume_support": False,
        "live_terminal_stream": False,
        "session_event_stream": "plugin-events",
    },
    "antigravity": {
        "native_spawn_exposed_to_external_orchestrator": False,
        "exact_child_identity": "no",
        "native_start_stop_proof": "no",
        "cancel_support": False,
        "background_tracking": False,
        "resume_support": False,
        "live_terminal_stream": False,
        "session_event_stream": "hook-callbacks",
    },
    "cursor": {
        "native_spawn_exposed_to_external_orchestrator": False,
        "exact_child_identity": "start-only",
        "native_start_stop_proof": "conditional",
        "cancel_support": False,
        "background_tracking": False,
        "resume_support": False,
        "live_terminal_stream": False,
        "session_event_stream": "hook-callbacks",
    },
}


class CapabilityMatrixTests(unittest.TestCase):
    def test_schema_has_every_required_platform_and_field(self):
        document = capabilities.matrix()
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(
            [record["platform"] for record in document["platforms"]],
            list(capabilities.PLATFORM_ORDER),
        )
        self.assertEqual(
            set(document["boolean_fields"]) | set(document["scoped_fields"]),
            set(capabilities.CAPABILITY_FIELDS),
        )
        self.assertFalse(
            set(document["boolean_fields"]) & set(document["scoped_fields"])
        )
        self.assertEqual(
            [record["platform"] for record in document["platforms"]],
            ["codex", "opencode", "antigravity", "cursor"],
        )
        for record in document["platforms"]:
            self.assertEqual(tuple(record["capabilities"]), capabilities.CAPABILITY_FIELDS)
            self.assertEqual(set(record["details"]), set(capabilities.CAPABILITY_FIELDS))
            for field in capabilities.BOOLEAN_CAPABILITY_FIELDS:
                self.assertIs(record["capabilities"][field], False, field)
            for field in capabilities.SCOPED_CAPABILITY_FIELDS:
                self.assertIn(record["capabilities"][field], capabilities.STATUS_VALUES)

    def test_values_are_conservative_and_match_documented_contracts(self):
        for record in capabilities.matrix()["platforms"]:
            platform = record["platform"]
            with self.subTest(platform=platform):
                self.assertEqual(record["capabilities"], EXPECTED[platform])
                # Strict booleans must stay literally false: no proven external
                # spawn/cancel/background/resume/terminal integration.
                for field in capabilities.BOOLEAN_CAPABILITY_FIELDS:
                    self.assertIs(record["capabilities"][field], False, field)
                # No capability is advertised as unconditional proof.
                self.assertNotIn("yes", record["capabilities"].values())
                self.assertNotIn(True, record["capabilities"].values())

    def test_platform_specific_limits_are_not_smoothed_over(self):
        codex = capabilities.get_platform("codex")
        self.assertEqual(codex["capabilities"]["exact_child_identity"], "conditional")
        self.assertEqual(codex["capabilities"]["native_start_stop_proof"], "conditional")
        self.assertIn("trusted", codex["details"]["exact_child_identity"])

        opencode = capabilities.get_platform("opencode")
        self.assertEqual(opencode["capabilities"]["exact_child_identity"], "foreground-only")
        self.assertEqual(opencode["capabilities"]["native_start_stop_proof"], "foreground-only")
        self.assertIn("background", opencode["details"]["background_tracking"])

        antigravity = capabilities.get_platform("antigravity")
        self.assertEqual(antigravity["capabilities"]["exact_child_identity"], "no")
        self.assertEqual(antigravity["capabilities"]["native_start_stop_proof"], "no")
        self.assertIn("per-child", antigravity["details"]["native_start_stop_proof"])

        cursor = capabilities.get_platform("cursor")
        self.assertEqual(cursor["capabilities"]["exact_child_identity"], "start-only")
        self.assertEqual(cursor["capabilities"]["native_start_stop_proof"], "conditional")
        self.assertIn("may omit", cursor["details"]["exact_child_identity"])

    def test_evidence_is_named_and_live_smoke_is_not_claimed(self):
        for record in capabilities.matrix()["platforms"]:
            with self.subTest(platform=record["platform"]):
                self.assertTrue(record["evidence_source"])
                self.assertIn("simulated", record["evidence_type"])
                self.assertFalse(record["live_smoke_tested"])
                self.assertTrue(record["notes"])
                for reference in record["references"]:
                    self.assertTrue(reference.startswith("https://"))

    def test_field_descriptions_cover_every_field(self):
        document = capabilities.matrix()
        self.assertEqual(set(document["field_descriptions"]), set(capabilities.CAPABILITY_FIELDS))
        for description in document["field_descriptions"].values():
            self.assertTrue(description.strip())

    def test_managed_execution_matrix_is_separate_and_honest(self):
        document = capabilities.matrix()
        managed = document["managed_execution"]
        self.assertEqual(managed["platforms"]["opencode"]["status"], "live-verified-local")
        self.assertTrue(managed["platforms"]["opencode"]["live_smoke_tested"])
        self.assertFalse(managed["platforms"]["opencode"]["native_cancel_ack"])
        self.assertEqual(managed["platforms"]["codex"]["status"], "live-verified-local")
        self.assertTrue(managed["platforms"]["codex"]["live_smoke_tested"])
        self.assertFalse(managed["platforms"]["codex"]["native_cancel_ack"])
        self.assertIn("resume_scope", managed["platforms"]["codex"])
        self.assertFalse(managed["platforms"]["codex"]["resume_live_verified"])
        self.assertFalse(managed["platforms"]["codex"]["write_mode_tested"])
        self.assertEqual(managed["platforms"]["codex"]["approval_policy_tested"], "never")
        for key in ("antigravity", "cursor"):
            self.assertEqual(managed["platforms"][key]["status"], "not-implemented")
            self.assertFalse(managed["platforms"][key]["managed_session"])
        filtered = capabilities.matrix("opencode")["managed_execution"]["platforms"]
        self.assertEqual(list(filtered), ["opencode"])

    def test_teamwork_entry_files_are_real_and_do_not_shadow_native_surfaces(self):
        root = Path(__file__).resolve().parents[1]
        expected = {
            "codex": [".codex/skills/teamwork/SKILL.md"],
            "opencode": [".opencode/commands/teamwork.md",
                         ".opencode/commands/grill-me.md",
                         ".opencode/skills/teamwork/SKILL.md"],
            "antigravity": [".agents/skills/ao-grill-me/SKILL.md",
                            ".agents/agents/auto_planner.md",
                            ".agents/agents/auto_implementer.md",
                            ".agents/agents/auto_tester.md",
                            ".agents/agents/auto_reviewer.md"],
            "cursor": [".cursor/agents/auto_planner.md",
                       ".cursor/agents/auto_implementer.md",
                       ".cursor/agents/auto_tester.md",
                       ".cursor/agents/auto_reviewer.md"],
        }
        for platform, files in expected.items():
            with self.subTest(platform=platform):
                for relative in files:
                    self.assertTrue((root / relative).is_file(), relative)
        # Codex has a skill file but no claimed slash command file.
        self.assertFalse((root / ".codex/commands/teamwork.md").exists())
        # Antigravity's portable interview must not reuse the native name.
        self.assertFalse((root / ".gemini/antigravity-cli/skills/grill-me").exists())
        self.assertFalse((root / ".gemini/config/skills/grill-me").exists())

    def test_teamwork_entry_statuses_and_binding_limits_are_conservative(self):
        document = capabilities.matrix()
        self.assertEqual(
            document["teamwork_entry_statuses"],
            ["native-skill", "native-slash", "plugin-command", "contract-only"],
        )
        self.assertEqual(set(document["teamwork_entry_descriptions"]),
                         set(document["teamwork_entry_statuses"]))
        expected = {
            "codex": "native-skill",
            "opencode": "plugin-command",
            "antigravity": "native-slash",
            "cursor": "contract-only",
        }
        for record in document["platforms"]:
            platform = record["platform"]
            with self.subTest(platform=platform):
                entry = record["teamwork_entry"]
                self.assertEqual(entry["status"], expected[platform])
                self.assertIn(entry["status"], capabilities.TEAMWORK_ENTRY_STATUSES)
                self.assertTrue(entry["entry_points"])
                self.assertTrue(entry["native_or_managed"])
                limits = entry["binding_limits"]
                self.assertFalse(limits["external_spawn"])
                self.assertFalse(limits["native_cancel_ack"])
                self.assertFalse(limits["native_session_resume"])
                self.assertFalse(limits["native_session_claimed"])
                self.assertFalse(limits["background_authoritative"])
                self.assertIn("note", limits)
        # Codex must not claim a slash command merely because a skill exists.
        codex = capabilities.get_platform("codex")["teamwork_entry"]
        self.assertEqual(codex["slash_commands"], [])
        self.assertIn("No /teamwork slash command", codex["authoritative_source"])
        self.assertIn("parent", codex["parent_session_boundary"].lower())
        # Antigravity's native /grill-me stays authoritative and our own
        # interview must be named differently.
        antigravity = capabilities.get_platform("antigravity")["teamwork_entry"]
        self.assertIn("/teamwork-preview", antigravity["slash_commands"])
        self.assertIn("/grill-me", antigravity["slash_commands"])
        self.assertTrue(any("ao-grill-me" in item for item in antigravity["entry_points"]))
        self.assertNotIn("grill-me", [item for item in antigravity["entry_points"]
                                      if item.endswith("/grill-me")])
        # Cursor ships no invented command format.
        cursor = capabilities.get_platform("cursor")["teamwork_entry"]
        self.assertEqual(cursor["slash_commands"], [])
        for role in ("planner", "implementer", "tester", "reviewer"):
            self.assertTrue(any(role in item for item in cursor["entry_points"]), role)

    def test_ide_ownership_is_separate_and_never_claims_a_native_connection(self):
        document = capabilities.matrix()
        ownership = document["ide_ownership"]
        self.assertEqual(ownership["schema_version"], 1)
        self.assertEqual(set(ownership["fields"]),
                         set(capabilities.IDE_OWNERSHIP_FIELDS))
        self.assertEqual(ownership["ownership_transport"],
                         "local-project-file")
        self.assertEqual(ownership["owner_identity"],
                         "host-and-session-hash-only")
        self.assertFalse(ownership["owner_token_exposed"])
        self.assertTrue(ownership["disclaimer"])
        self.assertEqual(set(ownership["platforms"]),
                         set(capabilities.PLATFORM_ORDER))
        for platform, record in ownership["platforms"].items():
            with self.subTest(platform=platform):
                self.assertEqual(set(record) >= set(capabilities.IDE_OWNERSHIP_FIELDS),
                                 True)
                self.assertFalse(record["second_supervisor"], platform)
                self.assertFalse(record["native_lock"], platform)
                self.assertFalse(record["connection_claimed"], platform)
                self.assertIs(record["stop_ack"], False, platform)
                self.assertTrue(record["stop_ack_evidence"])
                self.assertTrue(record["note"])
                self.assertNotIn(record["native_execution"], ("yes", "true"))
        for platform in ("cursor", "antigravity"):
            record = ownership["platforms"][platform]
            self.assertEqual(record["native_execution"], "contract-only")
            self.assertTrue(record["contract_only"])
            self.assertFalse(record["second_supervisor"])
        self.assertFalse(ownership["platforms"]["codex"]["contract_only"])
        self.assertEqual(ownership["platforms"]["codex"]["native_execution"],
                         "observed-hook")
        self.assertEqual(ownership["platforms"]["opencode"]["native_execution"],
                         "observed-plugin")
        # The ownership section is filtered with the rest of the matrix.
        filtered = capabilities.matrix("cursor")["ide_ownership"]["platforms"]
        self.assertEqual(list(filtered), ["cursor"])
        # It is a defensive copy, like every other section.
        filtered["cursor"]["second_supervisor"] = True
        self.assertFalse(
            capabilities.matrix()["ide_ownership"]["platforms"]["cursor"]
            ["second_supervisor"])

    def test_platform_filter_and_unknown_platform(self):
        document = capabilities.matrix("CODEX")
        self.assertEqual(len(document["platforms"]), 1)
        self.assertEqual(document["platforms"][0]["platform"], "codex")
        with self.assertRaises(capabilities.CapabilityError):
            capabilities.get_platform("not-a-client")
        with self.assertRaises(capabilities.CapabilityError):
            capabilities.matrix("")

    def test_matrix_is_defensive_and_deterministic(self):
        first = capabilities.matrix()
        second = capabilities.matrix()
        self.assertEqual(first, second)
        first["platforms"][0]["capabilities"]["cancel_support"] = True
        self.assertFalse(
            capabilities.matrix()["platforms"][0]["capabilities"]["cancel_support"]
        )

    def test_json_cli_is_exact_and_parseable(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = capabilities.main(["show", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue()), capabilities.matrix())

    def test_json_cli_platform_filter(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = capabilities.main(["show", "--json", "--platform", "cursor"])
        self.assertEqual(code, 0)
        document = json.loads(stdout.getvalue())
        self.assertEqual([item["platform"] for item in document["platforms"]], ["cursor"])

    def test_text_cli_mentions_limits(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = capabilities.main(["show"])
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        for platform in capabilities.PLATFORM_ORDER:
            self.assertIn(platform, text)
        self.assertIn("No live IDE smoke test is claimed", text)
        self.assertIn("native_start_stop_proof: foreground-only", text)
        self.assertIn("native_spawn_exposed_to_external_orchestrator: false", text)
        self.assertIn("Teamwork entry points", text)
        self.assertIn("codex: native-skill", text)
        self.assertIn("antigravity: native-slash", text)
        self.assertIn("IDE ownership", text)
        self.assertIn("cursor: native_execution=contract-only", text)
        self.assertIn("second_supervisor=false", text)
        self.assertIn("stop_ack=false", text)

    def test_cli_rejects_unknown_platform(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            capabilities.main(["show", "--platform", "emacs"])
        self.assertEqual(caught.exception.code, 2)


class RuntimeDetectionTests(unittest.TestCase):
    """V7.9 detection is deterministic with injected probes."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.base = Path(self._temporary.name)
        self.root = self.base / "project"
        self.home = self.base / "home"
        self.apps = self.base / "Applications"
        for path in (self.root, self.home, self.apps):
            path.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._temporary.cleanup()

    def _detect(self, **overrides):
        arguments = {
            "root": self.root,
            "home": self.home,
            "apps_root": self.apps,
            "which": lambda name: None,
            "runner": mock.Mock(),
            "now": 123.0,
        }
        arguments.update(overrides)
        return capabilities.detect_environment(**arguments)

    def _touch(self, relative, content="x"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _create_project_surfaces(self):
        for relative in (
            ".codex/skills/teamwork/SKILL.md",
            ".codex/agents/auto_planner.toml",
            ".codex/hooks.json",
            ".codex/commands/not-teamwork.md",
            ".agents/skills/grill-me/SKILL.md",
            ".agents/skills/auto-orchestrator/SKILL.md",
            ".agents/skills/ao-grill-me/SKILL.md",
            ".agents/agents/auto_planner.md",
            ".opencode/skills/teamwork/SKILL.md",
            ".opencode/commands/teamwork.md",
            ".opencode/commands/grill-me.md",
            ".opencode/agents/auto_planner.md",
            ".cursor/agents/auto_planner.md",
            ".cursor/hooks.json",
        ):
            self._touch(relative)

    @staticmethod
    def _completed(argv, returncode, stdout="", stderr=""):
        return subprocess.CompletedProcess(
            argv, returncode, stdout=stdout, stderr=stderr
        )

    def _snapshot(self):
        entries = []
        for path in sorted(self.base.rglob("*")):
            relative = str(path.relative_to(self.base))
            if path.is_file():
                entries.append((relative, "file", path.read_bytes()))
            else:
                entries.append((relative, "dir", b""))
        return entries

    def test_detection_shape_and_platform_order_are_exact(self):
        result = self._detect()
        self.assertEqual(
            list(result),
            ["schema_version", "kind", "generated_at", "platforms", "evidence"],
        )
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["kind"], "ide-capability-detection")
        self.assertEqual(result["evidence"], "local-probe")
        self.assertEqual(result["generated_at"], 123.0)
        self.assertEqual(
            list(result["platforms"]),
            ["codex", "opencode", "cursor", "antigravity"],
        )
        for platform, record in result["platforms"].items():
            with self.subTest(platform=platform):
                self.assertEqual(
                    tuple(record), capabilities.DETECTION_FIELDS
                )
                self.assertEqual(len(record), 12)

    def test_absent_binaries_and_apps_are_null_or_false(self):
        runner = mock.Mock()
        result = self._detect(runner=runner)
        runner.assert_not_called()
        for platform in ("codex", "opencode", "cursor", "antigravity"):
            record = result["platforms"][platform]
            with self.subTest(platform=platform):
                self.assertIs(record["installed"], False)
                self.assertIs(record["cli_available"], False)
                self.assertIsNone(record["version_detected"])
                self.assertIsNone(record["authenticated"])
                self.assertIs(record["skill_discovered"], False)
                self.assertIs(record["command_discovered"], False)
                self.assertIs(record["native_agent_available"], False)
                self.assertIs(record["grill_supported"], False)
        # No V7.8 evidence file in this temporary root.
        self.assertIsNone(result["platforms"]["codex"]["live_e2e_verified"])
        self.assertIsNone(result["platforms"]["opencode"]["live_e2e_verified"])
        self.assertIsNone(result["platforms"]["cursor"]["live_e2e_verified"])
        self.assertIs(
            result["platforms"]["antigravity"]["live_e2e_verified"], False
        )
        self.assertIs(
            result["platforms"]["antigravity"]["lifecycle_observable"], False
        )
        self.assertIs(
            result["platforms"]["antigravity"][
                "child_completion_authoritative"
            ],
            False,
        )
        for platform in ("codex", "opencode", "cursor"):
            self.assertIsNone(
                result["platforms"][platform]["handoff_supported"]
            )

    def test_real_project_files_are_discovered_under_root(self):
        repository = Path(__file__).resolve().parents[1]
        result = capabilities.detect_environment(
            root=repository,
            home=self.home,
            apps_root=self.apps,
            which=lambda name: None,
            runner=mock.Mock(),
            now=5.0,
        )
        codex = result["platforms"]["codex"]
        self.assertTrue(codex["skill_discovered"])
        self.assertFalse(codex["command_discovered"])
        self.assertTrue(codex["native_agent_available"])
        self.assertTrue(codex["grill_supported"])

        opencode = result["platforms"]["opencode"]
        self.assertTrue(opencode["skill_discovered"])
        self.assertTrue(opencode["command_discovered"])
        self.assertTrue(opencode["native_agent_available"])
        self.assertTrue(opencode["grill_supported"])

        cursor = result["platforms"]["cursor"]
        self.assertTrue(cursor["skill_discovered"])
        self.assertFalse(cursor["command_discovered"])
        self.assertTrue(cursor["native_agent_available"])
        self.assertTrue(cursor["grill_supported"])

        antigravity = result["platforms"]["antigravity"]
        self.assertTrue(antigravity["skill_discovered"])
        self.assertFalse(antigravity["command_discovered"])
        self.assertTrue(antigravity["native_agent_available"])
        self.assertTrue(antigravity["grill_supported"])
        # With no PATH entries and injected empty app root, no CLI is claimed.
        for path in ("codex", "opencode", "cursor", "antigravity"):
            self.assertFalse(result["platforms"][path]["cli_available"])

    def test_cursor_auth_true_false_and_null_from_canned_output(self):
        which = lambda name: "cursor-agent" if name == "cursor-agent" else None

        def runner_for(models):
            def runner(argv, timeout):
                if argv[-1] == "--version":
                    return self._completed(
                        argv, 0, "2026.10.22-27dcf5f\n", ""
                    )
                if argv[-1] == "--list-models":
                    return models(argv)
                raise AssertionError("unexpected cursor probe: " + repr(argv))
            return runner

        cases = {
            "authenticated": (
                lambda argv: self._completed(
                    argv, 0, "gpt-5\nclaude-4\n", ""
                ),
                True,
            ),
            "authentication-required": (
                lambda argv: self._completed(
                    argv,
                    1,
                    "",
                    "Error: Authentication required. Run 'cursor agent "
                    "login', pass --api-key/--auth-token, or set "
                    "CURSOR_API_KEY/CURSOR_AUTH_TOKEN.\n",
                ),
                False,
            ),
            "login-marker-with-zero-exit": (
                lambda argv: self._completed(
                    argv, 0, "Authentication required\n", ""
                ),
                False,
            ),
            "unclear-error": (
                lambda argv: self._completed(
                    argv, 2, "", "network unavailable\n"
                ),
                None,
            ),
        }
        for name, (models, expected) in cases.items():
            with self.subTest(case=name):
                result = self._detect(
                    which=which, runner=runner_for(models)
                )
                cursor = result["platforms"]["cursor"]
                self.assertIs(cursor["installed"], True)
                self.assertIs(cursor["cli_available"], True)
                self.assertEqual(
                    cursor["version_detected"], "2026.10.22-27dcf5f"
                )
                self.assertIs(cursor["authenticated"], expected)

    def test_cursor_auth_is_null_on_timeout_and_no_auth_probe_without_agent(self):
        which = lambda name: "cursor-agent" if name == "cursor-agent" else None

        def timeout_runner(argv, timeout):
            if argv[-1] == "--version":
                return self._completed(argv, 0, "2026.10.22-27dcf5f\n", "")
            raise subprocess.TimeoutExpired(argv, timeout)

        result = self._detect(which=which, runner=timeout_runner)
        cursor = result["platforms"]["cursor"]
        self.assertIs(cursor["cli_available"], True)
        self.assertIsNone(cursor["authenticated"])

        calls = []

        def unsupported_runner(argv, timeout):
            calls.append(list(argv))
            return self._completed(argv, 1, "", "unknown command: agent\n")

        result = self._detect(which=which, runner=unsupported_runner)
        cursor = result["platforms"]["cursor"]
        self.assertIs(cursor["installed"], True)
        self.assertIs(cursor["cli_available"], False)
        self.assertIsNone(cursor["version_detected"])
        self.assertIsNone(cursor["authenticated"])
        # Both version and help were tried; --list-models was not.
        self.assertFalse(
            any("--list-models" in argv for argv in calls), calls
        )

    def test_cursor_app_bundle_and_home_candidates_are_probed(self):
        def install_candidate(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
            return path

        app_candidate = install_candidate(
            self.apps
            / "Cursor.app/Contents/Resources/app/bin/cursor"
        )
        home_candidate = install_candidate(
            self.home / ".local/bin/cursor-agent"
        )

        def runner_for(seen):
            def runner(argv, timeout):
                seen.append(list(argv))
                if argv[-1] == "--version":
                    return self._completed(
                        argv, 0, "2026.10.22-27dcf5f\n", ""
                    )
                if argv[-1] == "--list-models":
                    return self._completed(argv, 0, "gpt-5\n", "")
                raise AssertionError(
                    "unexpected cursor probe: " + repr(argv)
                )
            return runner

        # The home candidate is found before the app bundle, so it wins.
        home_seen = []
        result = self._detect(
            which=lambda name: None, runner=runner_for(home_seen)
        )
        cursor = result["platforms"]["cursor"]
        self.assertIs(cursor["installed"], True)
        self.assertIs(cursor["cli_available"], True)
        self.assertEqual(
            cursor["version_detected"], "2026.10.22-27dcf5f"
        )
        self.assertIs(cursor["authenticated"], True)
        self.assertIn(
            str(home_candidate), {argv[0] for argv in home_seen}
        )

        home_candidate.unlink()
        app_seen = []
        result = self._detect(
            which=lambda name: None, runner=runner_for(app_seen)
        )
        cursor = result["platforms"]["cursor"]
        self.assertIs(cursor["installed"], True)
        self.assertIs(cursor["cli_available"], True)
        self.assertIn(
            str(app_candidate), {argv[0] for argv in app_seen}
        )

    def test_antigravity_app_presence_without_cli_is_not_a_cli_claim(self):
        (self.apps / "Antigravity.app").mkdir(parents=True, exist_ok=True)
        result = self._detect()
        antigravity = result["platforms"]["antigravity"]
        self.assertIs(antigravity["installed"], True)
        self.assertIs(antigravity["cli_available"], False)
        self.assertIsNone(antigravity["version_detected"])
        self.assertIsNone(antigravity["authenticated"])
        self.assertIs(antigravity["lifecycle_observable"], False)
        self.assertIs(
            antigravity["child_completion_authoritative"], False
        )
        self.assertIs(antigravity["live_e2e_verified"], False)
        self.assertIsNone(antigravity["handoff_supported"])

    def test_run_version_false_does_not_spawn_and_keeps_paths(self):
        mapping = {
            "codex": "/fake/codex",
            "opencode": "/fake/opencode",
            "cursor-agent": "/fake/cursor-agent",
        }
        runner = mock.Mock()
        result = self._detect(
            which=lambda name: mapping.get(name),
            run_version=False,
            runner=runner,
        )
        runner.assert_not_called()
        for platform in ("codex", "opencode", "cursor"):
            record = result["platforms"][platform]
            self.assertIs(record["installed"], True)
            self.assertIs(record["cli_available"], True)
            self.assertIsNone(record["version_detected"])
            self.assertIsNone(record["authenticated"])

    def test_probe_errors_and_timeouts_become_unknown_not_crashes(self):
        mapping = {
            "codex": "/fake/codex",
            "opencode": "/fake/opencode",
            "cursor-agent": "/fake/cursor-agent",
        }

        def broken_runner(argv, timeout):
            raise RuntimeError("probe exploded")

        result = self._detect(
            which=lambda name: mapping.get(name), runner=broken_runner
        )
        for platform in ("codex", "opencode"):
            record = result["platforms"][platform]
            self.assertIs(record["cli_available"], True)
            self.assertIsNone(record["version_detected"])
        cursor = result["platforms"]["cursor"]
        self.assertIs(cursor["installed"], True)
        self.assertIsNone(cursor["cli_available"])
        self.assertIsNone(cursor["version_detected"])

        result = self._detect(
            which=lambda name: mapping.get(name),
            runner=lambda argv, timeout: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(argv, timeout)
            ),
        )
        self.assertIsNone(
            result["platforms"]["codex"]["version_detected"]
        )
        self.assertIsNone(
            result["platforms"]["cursor"]["authenticated"]
        )

    def test_live_e2e_fields_come_only_from_v78_evidence(self):
        self._touch(
            "docs/assets/v78-live-handoff.json",
            json.dumps({"live_ides": ["codex", "opencode", "emacs"]}),
        )
        result = self._detect()
        self.assertIs(
            result["platforms"]["codex"]["live_e2e_verified"], True
        )
        self.assertIs(
            result["platforms"]["codex"]["handoff_supported"], True
        )
        self.assertIs(
            result["platforms"]["opencode"]["live_e2e_verified"], True
        )
        self.assertIs(
            result["platforms"]["cursor"]["live_e2e_verified"], False
        )
        self.assertIs(
            result["platforms"]["antigravity"]["live_e2e_verified"], False
        )
        self.assertIsNone(
            result["platforms"]["cursor"]["handoff_supported"]
        )
        self.assertIsNone(
            result["platforms"]["antigravity"]["handoff_supported"]
        )

    def test_missing_or_invalid_live_evidence_stays_unknown(self):
        for content in (None, "{not json", json.dumps({"live_ides": "codex"})):
            with self.subTest(content=content):
                path = self.root / "docs/assets/v78-live-handoff.json"
                if content is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                result = self._detect()
                for platform in ("codex", "opencode", "cursor"):
                    self.assertIsNone(
                        result["platforms"][platform]["live_e2e_verified"]
                    )
                    self.assertIsNone(
                        result["platforms"][platform]["handoff_supported"]
                    )
                self.assertIs(
                    result["platforms"]["antigravity"][
                        "live_e2e_verified"
                    ],
                    False,
                )

    def test_no_token_strings_are_captured(self):
        mapping = {
            "codex": "/fake/codex",
            "opencode": "/fake/opencode",
            "cursor-agent": "/fake/cursor-agent",
        }
        secrets = (
            "sk-live-CODEX-SECRET",
            "opencode-SECRET-TOKEN",
            "ghp_CURSOR_SECRET",
            "Bearer CURSOR-AUTH-SECRET",
        )

        def runner(argv, timeout):
            if argv[:1] == ["/fake/codex"]:
                return self._completed(
                    argv,
                    0,
                    "codex-cli 0.164.0\nsk-live-CODEX-SECRET\n",
                    "",
                )
            if argv[:1] == ["/fake/opencode"]:
                return self._completed(
                    argv,
                    0,
                    "opencode v2.1.1\nopencode-SECRET-TOKEN\n",
                    "",
                )
            if argv[-1] == "--version":
                return self._completed(
                    argv,
                    0,
                    "2026.10.22-27dcf5f\ngithub ghp_CURSOR_SECRET\n",
                    "",
                )
            if argv[-1] == "--list-models":
                return self._completed(
                    argv,
                    0,
                    "gpt-5\nAuthorization: Bearer CURSOR-AUTH-SECRET\n",
                    "",
                )
            raise AssertionError("unexpected probe: " + repr(argv))

        result = self._detect(
            which=lambda name: mapping.get(name), runner=runner
        )
        serialized = json.dumps(result)
        for secret in secrets:
            self.assertNotIn(secret, serialized)
        self.assertNotIn("CURSOR_API_KEY", serialized)
        self.assertNotIn("CURSOR_AUTH_TOKEN", serialized)
        self.assertEqual(
            result["platforms"]["codex"]["version_detected"], "0.164.0"
        )
        self.assertEqual(
            result["platforms"]["opencode"]["version_detected"], "v2.1.1"
        )
        self.assertEqual(
            result["platforms"]["cursor"]["version_detected"],
            "2026.10.22-27dcf5f",
        )

    def test_probe_environment_strips_keys_and_auth_tokens(self):
        cleaned = capabilities._clean_probe_env({
            "PATH": "/bin",
            "CURSOR_API_KEY": "secret",
            "CURSOR_AUTH_TOKEN": "secret",
            "OPENAI_API_KEY": "secret",
            "SOME_VENDOR_API_KEY": "secret",
            "HARMLESS": "kept",
        })
        self.assertEqual(
            cleaned, {"PATH": "/bin", "HARMLESS": "kept"}
        )

    def test_detection_never_writes_files(self):
        self._create_project_surfaces()
        mapping = {
            "codex": "/fake/codex",
            "opencode": "/fake/opencode",
            "cursor-agent": "/fake/cursor-agent",
        }

        def runner(argv, timeout):
            if argv[-1] == "--version":
                return self._completed(argv, 0, "1.2.3\n", "")
            if argv[-1] == "--list-models":
                return self._completed(argv, 0, "model-a\n", "")
            raise AssertionError("unexpected probe: " + repr(argv))

        before = self._snapshot()
        self._detect(which=lambda name: mapping.get(name), runner=runner)
        after = self._snapshot()
        self.assertEqual(before, after)

    def test_timeout_is_bounded(self):
        seen = []
        mapping = {"codex": "/fake/codex"}

        def runner(argv, timeout):
            seen.append(timeout)
            return self._completed(argv, 0, "0.1.0\n", "")

        self._detect(
            which=lambda name: mapping.get(name),
            timeout=10_000,
            runner=runner,
        )
        self.assertTrue(seen)
        self.assertTrue(all(value <= 30.0 for value in seen), seen)

    def test_cli_detect_json_keeps_static_sections_and_adds_detection(self):
        detection = self._detect()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
            capabilities, "detect_environment", return_value=detection
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = capabilities.main(["show", "--detect", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["detection"], detection)
        static = capabilities.matrix()
        for key in static:
            self.assertIn(key, document)
        self.assertEqual(document["platforms"], static["platforms"])

    def test_cli_no_detect_keeps_static_json_exact(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = capabilities.main(["show", "--no-detect", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()), capabilities.matrix()
        )

    def test_cli_detect_text_is_explicit_about_unknowns(self):
        detection = self._detect()
        stdout = io.StringIO()
        with mock.patch.object(
            capabilities, "detect_environment", return_value=detection
        ):
            with redirect_stdout(stdout):
                code = capabilities.main(["show", "--detect"])
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("Runtime IDE capability detection", text)
        self.assertIn("no live execution claimed", text)
        self.assertIn("authenticated: null", text)
        self.assertIn("cli_available: false", text)
        self.assertIn("live_e2e_verified: false", text)

    def test_cli_detect_platform_filter_limits_detection_block(self):
        detection = self._detect()
        stdout = io.StringIO()
        with mock.patch.object(
            capabilities, "detect_environment", return_value=detection
        ):
            with redirect_stdout(stdout):
                code = capabilities.main([
                    "show", "--detect", "--json", "--platform", "cursor"
                ])
        self.assertEqual(code, 0)
        document = json.loads(stdout.getvalue())
        self.assertEqual(
            [record["platform"] for record in document["platforms"]],
            ["cursor"],
        )
        self.assertEqual(list(document["detection"]["platforms"]), ["cursor"])

    def test_cli_detect_does_not_crash_with_absent_probes(self):
        detection = self._detect()
        stdout = io.StringIO()
        with mock.patch.object(
            capabilities, "detect_environment", return_value=detection
        ):
            with redirect_stdout(stdout):
                code = capabilities.main(["show", "--detect", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue())["detection"], detection
        )


if __name__ == "__main__":
    unittest.main()
