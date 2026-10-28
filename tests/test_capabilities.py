"""Contract tests for the conservative native capability matrix."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
