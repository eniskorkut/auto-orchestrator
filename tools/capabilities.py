#!/usr/bin/env python3
"""Evidence-based native capability matrix (stdlib only, no network).

The matrix answers only questions that this repository can support with its
shipped adapters, documented native contracts and deterministic tests. It is
deliberately narrower than vendor marketing: a mark that a native client can
spawn an agent does **not** mean an external orchestrator can spawn one, and a
tool-call completion is not a child stop.

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
    return {
        "schema_version": SCHEMA_VERSION,
        "disclaimer": DISCLAIMER,
        "fields": list(CAPABILITY_FIELDS),
        "boolean_fields": list(BOOLEAN_CAPABILITY_FIELDS),
        "scoped_fields": list(SCOPED_CAPABILITY_FIELDS),
        "field_descriptions": dict(FIELD_DESCRIPTIONS),
        "status_values": list(STATUS_VALUES),
        "platforms": [copy.deepcopy(_PLATFORMS[key]) for key in keys],
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
