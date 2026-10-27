#!/usr/bin/env python3
"""Evidence-based native capability and Teamwork-entry matrix (stdlib only).

The matrix answers only questions that this repository can support with its
shipped adapters, documented native contracts and deterministic tests. It is
deliberately narrower than vendor marketing: a mark that a native client can
spawn an agent does **not** mean an external orchestrator can spawn one, and a
tool-call completion is not a child stop. The `teamwork_entry` section describes
shipped entry files and native surfaces; it is not proof that a client loaded
them or that a session was bound.

Run ``python tools/capabilities.py show`` for a human-readable view or
``python tools/capabilities.py show --json`` for the exact machine-readable
document consumed by documentation and tests.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys

SCHEMA_VERSION = 1

PLATFORM_ORDER = ("codex", "opencode", "antigravity", "cursor")

CAPABILITY_FIELDS = (
    "native_spawn_exposed_to_external_orchestrator",
    "exact_child_identity",
    "native_start_stop_proof",
    "cancel_support",
    "background_tracking",
    "resume_support",
    "live_terminal_stream",
    "session_event_stream",
)

# Strict yes/no capabilities. These are ``false`` everywhere because this
# repository has no proven external dispatch, cancellation, background-child
# tracking, child resume or terminal stream. Keeping them boolean avoids
# turning a missing integration into an ambiguous "partial" claim.
BOOLEAN_CAPABILITY_FIELDS = (
    "native_spawn_exposed_to_external_orchestrator",
    "cancel_support",
    "background_tracking",
    "resume_support",
    "live_terminal_stream",
)

# Conditional capabilities use the status vocabulary below.
SCOPED_CAPABILITY_FIELDS = (
    "exact_child_identity",
    "native_start_stop_proof",
    "session_event_stream",
)

# Status vocabulary. ``yes`` is intentionally unused: every non-``no`` value
# below depends on a documented setup or applies only to foreground execution.
STATUS_VALUES = (
    "no",
    "conditional",
    "foreground-only",
    "start-only",
    "hook-callbacks",
    "plugin-events",
    "unknown",
)

# How a native IDE actually exposes Teamwork. These are entry-point contracts,
# not execution claims: a skill file or slash command does not prove that the
# client loaded it or that an agent session was spawned or bound.
TEAMWORK_ENTRY_STATUSES = (
    "native-skill",
    "native-slash",
    "plugin-command",
    "contract-only",
)

TEAMWORK_ENTRY_DESCRIPTIONS = {
    "native-skill": (
        "The client discovers a shipped skill/agent instruction file natively. "
        "No slash command and no external dispatch are claimed."
    ),
    "native-slash": (
        "The client owns the slash command or preview surface. This repository "
        "documents that native entry point and does not shadow it."
    ),
    "plugin-command": (
        "A project/plugin command file is shipped and loaded by the client. It "
        "is a command bridge, not proof of a native subagent session."
    ),
    "contract-only": (
        "Only shared skills, agent files and the universal CLI contract are "
        "shipped. No client-specific command or managed adapter is claimed."
    ),
}

TEAMWORK_ENTRY = {
    "codex": {
        "status": "native-skill",
        "authoritative_source": (
            "Codex-native skill and agent files. No /teamwork slash command "
            "file is shipped for Codex."
        ),
        "entry_points": [
            ".codex/skills/teamwork/SKILL.md",
            ".codex/agents/*.toml",
            "universal_teamwork.py",
        ],
        "slash_commands": [],
        "native_or_managed": (
            "Native Codex subagents by default. Managed execution requires an "
            "explicit --provider codex and remains read-only unless the parent "
            "session opts into workspace-write."
        ),
        "parent_session_boundary": (
            "Sandbox and approval are controlled by the parent Codex session. "
            "Per-agent sandbox_mode keys are not relied on and are removed from "
            "the shipped agent files."
        ),
        "binding_limits": {
            "status": "trusted-hook-pair-only",
            "native_id_required": True,
            "requires_observed_start": True,
            "requires_matching_stop": True,
            "background_authoritative": False,
            "external_spawn": False,
            "native_cancel_ack": False,
            "native_session_resume": False,
            "native_session_claimed": False,
            "note": (
                "A Supervisor task may be bound only after a real native ID is "
                "observed and a matching SubagentStop is received from trusted "
                "hooks. The file does not prove that a session exists."
            ),
        },
    },
    "opencode": {
        "status": "plugin-command",
        "authoritative_source": (
            "Project command files /teamwork and /grill-me plus the V2 plugin."
        ),
        "entry_points": [
            ".opencode/commands/teamwork.md",
            ".opencode/commands/grill-me.md",
            "adapters/opencode-v2/auto_orchestrator.js",
        ],
        "slash_commands": ["/teamwork", "/grill-me"],
        "native_or_managed": (
            "Managed OpenCode sessions with an observed plugin event stream and "
            "a foreground-only native subagent span. TUI click-through is not "
            "automated."
        ),
        "parent_session_boundary": (
            "The parent OpenCode session owns native subagent dispatch. The "
            "plugin observes in-process events; it does not spawn children."
        ),
        "binding_limits": {
            "status": "foreground-tool-call-only",
            "native_id_required": True,
            "requires_observed_start": True,
            "requires_matching_stop": True,
            "background_authoritative": False,
            "external_spawn": False,
            "native_cancel_ack": False,
            "native_session_resume": False,
            "native_session_claimed": False,
            "note": (
                "Only a foreground subagent tool-call ID may be matched. A "
                "background dispatch return is not child completion and cannot "
                "release a supervised task."
            ),
        },
    },
    "antigravity": {
        "status": "native-slash",
        "authoritative_source": (
            "Antigravity-native /teamwork-preview and /grill-me are "
            "authoritative. Our layer stores memory, contracts and handoffs."
        ),
        "entry_points": [
            "native /teamwork-preview",
            "native /grill-me",
            ".agents/skills/ao-grill-me/SKILL.md",
            "universal_teamwork.py",
        ],
        "slash_commands": ["/teamwork-preview", "/grill-me"],
        "native_or_managed": (
            "Native Antigravity agents. No managed adapter is implemented; the "
            "non-native interview is shipped as ao-grill-me so it cannot shadow "
            "the native /grill-me surface."
        ),
        "parent_session_boundary": (
            "The native parent session controls subagent dispatch, sandbox and "
            "approval. No second supervisor and no cross-IDE spawn are started."
        ),
        "binding_limits": {
            "status": "unsupported",
            "native_id_required": False,
            "requires_observed_start": False,
            "requires_matching_stop": False,
            "background_authoritative": False,
            "external_spawn": False,
            "native_cancel_ack": False,
            "native_session_resume": False,
            "native_session_claimed": False,
            "note": (
                "Session hooks identify a conversation, not an individual "
                "child. invoke_subagent dispatch is not child completion, so no "
                "native-supervised binding is claimed."
            ),
        },
    },
    "cursor": {
        "status": "contract-only",
        "authoritative_source": (
            "Shared skills plus .cursor/agents. No supported client-specific "
            "command format is claimed without local evidence."
        ),
        "entry_points": [
            ".cursor/agents/auto_planner.md",
            ".cursor/agents/auto_implementer.md",
            ".cursor/agents/auto_tester.md",
            ".cursor/agents/auto_reviewer.md",
            ".agents/skills/auto-orchestrator/SKILL.md",
            "universal_teamwork.py",
        ],
        "slash_commands": [],
        "native_or_managed": (
            "Native Cursor subagents when the client exposes them; otherwise a "
            "CLI contract. No managed adapter or live Cursor run is claimed."
        ),
        "parent_session_boundary": (
            "The Cursor parent owns dispatch and permissions. Project-local "
            "hooks observe start; a stop without a matching ID is not completion."
        ),
        "binding_limits": {
            "status": "start-id-only",
            "native_id_required": True,
            "requires_observed_start": True,
            "requires_matching_stop": True,
            "background_authoritative": False,
            "external_spawn": False,
            "native_cancel_ack": False,
            "native_session_resume": False,
            "native_session_claimed": False,
            "note": (
                "subagentStart documents an ID; subagentStop may omit it. Only "
                "an exact matching stop may release a task, and background "
                "stop is never inferred."
            ),
        },
    },
}

TEAMWORK_ENTRY_DISCLAIMER = (
    "Teamwork entry statuses describe shipped files and documented native "
    "surfaces. They do not prove that a client loaded the entry point, spawned "
    "an agent, bound a native session, cancelled it or resumed it."
)

FIELD_DESCRIPTIONS = {
    "native_spawn_exposed_to_external_orchestrator": (
        "Whether this repository can ask the native client to spawn a child "
        "agent through a documented external API. This is a strict boolean; "
        "the native parent remains the only entity that spawns its own agents."
    ),
    "exact_child_identity": (
        "Whether the adapter can attribute a lifecycle event to one child. A "
        "tool-call/dispatch identifier is not automatically a child-session "
        "identity."
    ),
    "native_start_stop_proof": (
        "Whether an observed start and a matching observed stop for the same "
        "child can be established. Partial values never authorize background, "
        "tool-dispatch or session-level completion."
    ),
    "cancel_support": (
        "Whether the external orchestrator can issue a native cancel and "
        "observe its acknowledgement. Strict boolean; a Supervisor cancel "
        "request is local state only."
    ),
    "background_tracking": (
        "Whether background child execution can be tracked authoritatively "
        "until completion (not merely until the spawn call returns). Strict "
        "boolean."
    ),
    "resume_support": (
        "Whether a child session can be resumed by the orchestrator with "
        "preserved identity and correlation. Strict boolean."
    ),
    "live_terminal_stream": (
        "Whether an authenticated live terminal/PTY stream is attached to the "
        "orchestrator. Strict boolean; lifecycle events are not a terminal."
    ),
    "session_event_stream": (
        "What the adapter actually receives: discrete hook callbacks, an "
        "in-process plugin event subscription, or no stream. In-process "
        "events are not exposed to an external orchestrator."
    ),
}

MANAGED_EXECUTION = {
    "schema_version": 1,
    "description": (
        "Managed execution is separate from native hook observation. A managed "
        "session is created by this repository through a documented CLI/API; "
        "its completion is managed evidence, not a native subagent stop."
    ),
    "platforms": {
        "opencode": {
            "status": "live-verified-local",
            "transport": "opencode run --format json",
            "managed_session": True,
            "event_stream": True,
            "tool_activity": True,
            "resume": True,
            "native_cancel_ack": False,
            "terminal_stream": False,
            "live_smoke_tested": True,
            "evidence": "real local OpenCode 2.1.1 session; adapter tests; V7.3 E2E run",
        },
        "codex": {
            "status": "live-verified-local",
            "transport": "codex app-server JSON-RPC",
            "managed_session": True,
            "event_stream": True,
            "tool_activity": True,
            "resume": True,
            "native_cancel_ack": False,
            "terminal_stream": False,
            "live_smoke_tested": True,
            "sandbox_tested": "read-only",
            "approval_policy_tested": "never",
            "write_mode_tested": False,
            "resume_live_verified": False,
            "file_modification_checked": False,
            "evidence": ("real local Codex CLI 0.164.0 app-server; gpt-6.1-sol; "
                         "V7.5 protocol/adapter/Teamwork smoke; no native cancel ACK"),
            "resume_scope": "codex thread/resume is implemented and contract-tested; no live resume smoke in V7.5",
        },
        "antigravity": {
            "status": "not-implemented",
            "transport": None,
            "managed_session": False,
            "event_stream": False,
            "tool_activity": False,
            "resume": False,
            "native_cancel_ack": False,
            "terminal_stream": False,
            "live_smoke_tested": False,
            "evidence": "no documented external managed-session API integrated",
        },
        "cursor": {
            "status": "not-implemented",
            "transport": None,
            "managed_session": False,
            "event_stream": False,
            "tool_activity": False,
            "resume": False,
            "native_cancel_ack": False,
            "terminal_stream": False,
            "live_smoke_tested": False,
            "evidence": "no documented external managed-session API integrated",
        },
    },
}

DISCLAIMER = (
    "Capabilities describe adapters shipped in this repository and their "
    "documented native contracts. No live IDE smoke test is claimed here; "
    "simulated contract tests are not native end-to-end evidence."
)

_PLATFORMS = {
    "codex": {
        "platform": "codex",
        "label": "Codex",
        "capabilities": {
            "native_spawn_exposed_to_external_orchestrator": False,
            "exact_child_identity": "conditional",
            "native_start_stop_proof": "conditional",
            "cancel_support": False,
            "background_tracking": False,
            "resume_support": False,
            "live_terminal_stream": False,
            "session_event_stream": "hook-callbacks",
        },
        "details": {
            "native_spawn_exposed_to_external_orchestrator": (
                "The main Codex agent must call its native subagent tool. The "
                "Supervisor can claim and bind a task, but it never spawns a "
                "Codex agent."
            ),
            "exact_child_identity": (
                "SubagentStart and SubagentStop expose agent_id only when the "
                "project hooks are trusted and actually execute. The ID is "
                "hashed before it is persisted in telemetry."
            ),
            "native_start_stop_proof": (
                "Usable only as a matching start+stop pair with the same "
                "agent_id from trusted hooks. A stop alone, an unmatched ID or "
                "a hook that never fires is rejected; the Supervisor keeps the "
                "task reserved."
            ),
            "cancel_support": (
                "No external cancel API. The Supervisor can record a local "
                "cancel request, but the native client must perform and "
                "acknowledge the cancellation."
            ),
            "background_tracking": (
                "No authoritative background-child completion is established. "
                "Do not treat a spawn return or a parent Stop as child proof."
            ),
            "resume_support": (
                "No child-session resume is exposed or correlated by this "
                "repository. Native client session resume remains outside "
                "orchestration evidence."
            ),
            "live_terminal_stream": (
                "Not attached. The adapter receives hook JSON on stdin, not a "
                "live PTY stream."
            ),
            "session_event_stream": (
                "Discrete hook callbacks (UserPromptSubmit, SubagentStart, "
                "SubagentStop, Stop), not a persistent event stream."
            ),
        },
        "evidence_source": (
            ".codex/hooks.json -> tools/hook_bridge.py (SubagentStart/"
            "SubagentStop); tests/test_platform_contracts.py; "
            "docs/NATIVE_SUPERVISOR_PLATFORM_MATRIX.md"
        ),
        "evidence_type": "documented hook contract + simulated Python tests",
        "live_smoke_tested": False,
        "notes": [
            "Hooks must be enabled and trusted in the native client before "
            "any lifecycle event is available.",
            "A missing hook is missing evidence, never inferred completion.",
        ],
        "references": [
            "https://developers.openai.com/codex/hooks",
            "https://developers.openai.com/codex/subagents",
        ],
    },
    "opencode": {
        "platform": "opencode",
        "label": "OpenCode (V2 plugin)",
        "capabilities": {
            "native_spawn_exposed_to_external_orchestrator": False,
            "exact_child_identity": "foreground-only",
            "native_start_stop_proof": "foreground-only",
            "cancel_support": False,
            "background_tracking": False,
            "resume_support": False,
            "live_terminal_stream": False,
            "session_event_stream": "plugin-events",
        },
        "details": {
            "native_spawn_exposed_to_external_orchestrator": (
                "The OpenCode agent invokes its native subagent tool. The V2 "
                "plugin observes the call in-process; it does not dispatch "
                "agents for the orchestrator."
            ),
            "exact_child_identity": (
                "A foreground native subagent tool call exposes a tool-call "
                "ID. A background dispatch return does not identify an "
                "authoritative child session."
            ),
            "native_start_stop_proof": (
                "Foreground native subagent tool begin/end can be matched by "
                "ID. Background spawn completion is only dispatch "
                "acknowledgement, not child termination."
            ),
            "cancel_support": (
                "No external cancel API. The plugin does not terminate native "
                "agents; cancellation must happen in the native client."
            ),
            "background_tracking": (
                "Not trustworthy. The background spawn call returns before "
                "the child finishes, and no authoritative background stop is "
                "established."
            ),
            "resume_support": (
                "No child-session resume is exposed or correlated by the "
                "plugin."
            ),
            "live_terminal_stream": (
                "No terminal stream is exposed to the orchestrator. The "
                "plugin receives native event objects only."
            ),
            "session_event_stream": (
                "The V2 plugin subscribes to in-process native session/tool "
                "events. The stream is not exposed outside the plugin, and an "
                "interrupted stream is retained as incomplete evidence."
            ),
        },
        "evidence_source": (
            "adapters/opencode-v2/auto_orchestrator.js native subagent/session "
            "events; tests/js/test_opencode.mjs; README V6/V6.1 notes"
        ),
        "evidence_type": "documented plugin event contract + simulated Node tests",
        "live_smoke_tested": False,
        "notes": [
            "Foreground and background dispatch have different evidence "
            "strength; only foreground lifecycle events can match a task.",
            "The plugin must actually be loaded by the installed Desktop "
            "build; a passing mock test does not prove that.",
        ],
        "references": [
            "https://opencode.ai/docs/agents",
            "https://docs.opencode.ai/docs/skills/",
        ],
    },
    "antigravity": {
        "platform": "antigravity",
        "label": "Google Antigravity",
        "capabilities": {
            "native_spawn_exposed_to_external_orchestrator": False,
            "exact_child_identity": "no",
            "native_start_stop_proof": "no",
            "cancel_support": False,
            "background_tracking": False,
            "resume_support": False,
            "live_terminal_stream": False,
            "session_event_stream": "hook-callbacks",
        },
        "details": {
            "native_spawn_exposed_to_external_orchestrator": (
                "The Antigravity agent invokes invoke_subagent when available. "
                "No external spawn API is integrated by this repository."
            ),
            "exact_child_identity": (
                "SessionStart/Stop identify the conversation, not a child. "
                "PreToolUse/PostToolUse describe tool dispatch, not which child "
                "finished."
            ),
            "native_start_stop_proof": (
                "Not supported. The documented hooks do not provide an "
                "authoritative per-child start/stop pair, so a supervised task "
                "cannot be released on this evidence."
            ),
            "cancel_support": (
                "No external cancel API. Native cancellation is outside the "
                "orchestrator's control."
            ),
            "background_tracking": (
                "Not supported. Tool dispatch and session idle state do not "
                "prove which child is still running."
            ),
            "resume_support": (
                "No child-session resume is exposed or correlated."
            ),
            "live_terminal_stream": (
                "No terminal stream is attached; hooks are session-scoped "
                "callbacks."
            ),
            "session_event_stream": (
                "Discrete SessionStart, PreInvocation, PreToolUse, PostToolUse "
                "and Stop hook callbacks. They are session-scoped, not a "
                "per-child stream."
            ),
        },
        "evidence_source": (
            ".agents/hooks.json -> tools/hook_bridge.py session hooks; "
            "tests/test_platform_contracts.py; "
            "docs/NATIVE_SUPERVISOR_PLATFORM_MATRIX.md"
        ),
        "evidence_type": "documented hook contract + simulated Python tests",
        "live_smoke_tested": False,
        "notes": [
            "invoke_subagent dispatch is not a child completion proof.",
            "Multi-workspace sessions are ambiguous; the hook bridge refuses "
            "to guess a project root.",
        ],
        "references": [
            "https://www.antigravity.google/docs/hooks",
            "https://www.antigravity.google/docs/subagents/",
        ],
    },
    "cursor": {
        "platform": "cursor",
        "label": "Cursor IDE",
        "capabilities": {
            "native_spawn_exposed_to_external_orchestrator": False,
            "exact_child_identity": "start-only",
            "native_start_stop_proof": "conditional",
            "cancel_support": False,
            "background_tracking": False,
            "resume_support": False,
            "live_terminal_stream": False,
            "session_event_stream": "hook-callbacks",
        },
        "details": {
            "native_spawn_exposed_to_external_orchestrator": (
                "The Cursor agent invokes its native Task subagent. The "
                "project-local hooks only observe; they never dispatch."
            ),
            "exact_child_identity": (
                "subagentStart documents subagent_id. subagentStop may omit the "
                "ID, so identity is reliable on start only."
            ),
            "native_start_stop_proof": (
                "Only a stop carrying the exact same subagent_id as an "
                "observed start is usable. A stop without an ID, a different "
                "ID, or a background stop that never fires is rejected."
            ),
            "cancel_support": (
                "No external cancel API. Native subagent cancellation remains "
                "a client operation."
            ),
            "background_tracking": (
                "Not trustworthy. Some Cursor versions do not emit a stop for "
                "background subagents, and the documented stop payload may "
                "lack an identity."
            ),
            "resume_support": (
                "No child-session resume is exposed or correlated."
            ),
            "live_terminal_stream": (
                "No terminal stream is attached; only project-local hook "
                "callbacks are observed."
            ),
            "session_event_stream": (
                "Discrete project-local hook callbacks (sessionStart, "
                "subagentStart, subagentStop, sessionEnd), not a persistent "
                "stream. User-level hooks are intentionally not enabled."
            ),
        },
        "evidence_source": (
            ".cursor/hooks.json -> tools/hook_bridge.py project-local "
            "subagentStart/subagentStop; tests/test_platform_contracts.py; "
            "docs/NATIVE_SUPERVISOR_PLATFORM_MATRIX.md"
        ),
        "evidence_type": "documented hook contract + simulated Python tests",
        "live_smoke_tested": False,
        "notes": [
            "Global Cursor installation ships skills/agents without global "
            "hooks because user-level hook stdin does not attest a workspace.",
            "An unmatched stop leaves the task reserved; no task text or "
            "transcript path is used to infer identity.",
        ],
        "references": [
            "https://cursor.com/docs/hooks",
        ],
    },
}


class CapabilityError(ValueError):
    """Raised for an unknown platform or malformed matrix request."""


def _validated_platform(platform: str) -> str:
    if not isinstance(platform, str):
        raise CapabilityError("platform must be a string")
    key = platform.strip().lower()
    if key not in _PLATFORMS:
        raise CapabilityError(
            "unknown platform: " + platform + "; choose " + ", ".join(PLATFORM_ORDER)
        )
    return key


def matrix(platform: str | None = None) -> dict:
    """Return a defensive copy of the capability document.

    ``platform`` may be one of :data:`PLATFORM_ORDER`. ``None`` returns every
    platform. The result contains no timestamps, so it is deterministic and
    can be compared byte-for-byte across runs.
    """
    keys = [_validated_platform(platform)] if platform is not None else list(PLATFORM_ORDER)
    platforms = []
    for key in keys:
        record = copy.deepcopy(_PLATFORMS[key])
        record["teamwork_entry"] = copy.deepcopy(TEAMWORK_ENTRY[key])
        platforms.append(record)
    return {
        "schema_version": SCHEMA_VERSION,
        "disclaimer": DISCLAIMER,
        "fields": list(CAPABILITY_FIELDS),
        "boolean_fields": list(BOOLEAN_CAPABILITY_FIELDS),
        "scoped_fields": list(SCOPED_CAPABILITY_FIELDS),
        "field_descriptions": dict(FIELD_DESCRIPTIONS),
        "status_values": list(STATUS_VALUES),
        "teamwork_entry_statuses": list(TEAMWORK_ENTRY_STATUSES),
        "teamwork_entry_descriptions": dict(TEAMWORK_ENTRY_DESCRIPTIONS),
        "teamwork_entry_disclaimer": TEAMWORK_ENTRY_DISCLAIMER,
        "platforms": platforms,
        "managed_execution": {
            **copy.deepcopy(MANAGED_EXECUTION),
            "platforms": {
                key: copy.deepcopy(MANAGED_EXECUTION["platforms"][key])
                for key in keys
            },
        },
    }


def get_platform(platform: str) -> dict:
    """Return one platform's capability record."""
    return matrix(platform)["platforms"][0]


def as_json(platform: str | None = None) -> str:
    """Return deterministic pretty-printed JSON."""
    return json.dumps(matrix(platform), indent=2, ensure_ascii=False) + "\n"


def _iter_values(record: dict):
    for field in CAPABILITY_FIELDS:
        yield field, record["capabilities"][field]


def render_text(platform: str | None = None) -> str:
    """Render a readable matrix without pretending partial support is full."""
    document = matrix(platform)
    lines = [
        "Auto-Orchestrator native capability matrix (schema "
        + str(SCHEMA_VERSION)
        + ")",
        document["disclaimer"],
        "",
        "Status values (scoped fields): " + ", ".join(document["status_values"]),
        "Strict boolean fields: " + ", ".join(document["boolean_fields"]),
        "",
    ]
    lines.append("Managed execution (separate from native observation):")
    for key in [record["platform"] for record in document["platforms"]]:
        managed = document["managed_execution"]["platforms"][key]
        lines.append(f"  {key}: {managed['status']} (live={str(managed['live_smoke_tested']).lower()})")
    lines.append("")
    lines.append("Teamwork entry points (files, not session proof):")
    for record in document["platforms"]:
        entry = record["teamwork_entry"]
        limits = entry["binding_limits"]
        lines.append(
            f"  {record['platform']}: {entry['status']} "
            f"(binding={limits['status']}, native_session_claimed="
            f"{str(limits['native_session_claimed']).lower()})"
        )
    lines.append("")
    for record in document["platforms"]:
        lines.append(record["label"] + " (" + record["platform"] + ")")
        lines.append("-" * len(lines[-1]))
        for field, value in _iter_values(record):
            rendered = ("true" if value else "false") if isinstance(value, bool) else value
            lines.append(f"  {field}: {rendered}")
            detail = record["details"].get(field)
            if detail:
                lines.append(f"      {detail}")
        lines.append("  evidence_source: " + record["evidence_source"])
        lines.append("  live_smoke_tested: " + str(record["live_smoke_tested"]).lower())
        for note in record.get("notes", []):
            lines.append("  note: " + note)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    show = subparsers.add_parser("show", help="show the capability matrix")
    show.add_argument(
        "--platform",
        choices=PLATFORM_ORDER,
        default=None,
        help="limit output to one platform",
    )
    show.add_argument(
        "--json",
        action="store_true",
        help="emit the machine-readable document instead of text",
    )
    args = parser.parse_args(argv)
    try:
        output = as_json(args.platform) if args.json else render_text(args.platform)
    except CapabilityError as exc:
        print("CAPABILITY ERROR: " + str(exc), file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
