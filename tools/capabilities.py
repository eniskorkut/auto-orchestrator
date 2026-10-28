#!/usr/bin/env python3
"""Evidence-based native capability and Teamwork-entry matrix (stdlib only).

The matrix answers only questions that this repository can support with its
shipped adapters, documented native contracts and deterministic tests. It is
deliberately narrower than vendor marketing: a mark that a native client can
spawn an agent does **not** mean an external orchestrator can spawn one, and a
tool-call completion is not a child stop. The `teamwork_entry` section describes
shipped entry files and native surfaces; it is not proof that a client loaded
them or that a session was bound. The `ide_ownership` section describes only
local ownership/handoff coordination, not a native connection.

Run ``python tools/capabilities.py show`` for a human-readable view or
``python tools/capabilities.py show --json`` for the exact machine-readable
document consumed by documentation and tests.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCHEMA_VERSION = 1

PLATFORM_ORDER = ("codex", "opencode", "antigravity", "cursor")

# -- V7.9 runtime detection -------------------------------------------------
# The detection block is a local probe, not a native-session claim. Every
# field below is either a fact observed on this machine or ``None`` when the
# question was not tested. The detector writes no files, makes no direct
# network request and never reads a credential store; version/auth checks are
# delegated to the installed CLI with API-key environment variables stripped.
DETECTION_SCHEMA_VERSION = 1
DETECTION_KIND = "ide-capability-detection"
DETECTION_EVIDENCE = "local-probe"

DETECTION_FIELDS = (
    "installed",
    "version_detected",
    "cli_available",
    "authenticated",
    "skill_discovered",
    "command_discovered",
    "native_agent_available",
    "lifecycle_observable",
    "child_completion_authoritative",
    "handoff_supported",
    "grill_supported",
    "live_e2e_verified",
)

_DETECTION_MAX_TIMEOUT = 30.0
_DETECTION_AUTH_TIMEOUT = 5.0
_DETECTION_MAX_OUTPUT = 8192
_DETECTION_MAX_VERSION = 80

_VERSION_TOKEN_RE = re.compile(
    r"(?<![0-9A-Za-z._-])"
    r"(v?\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z][0-9A-Za-z.-]{0,63})?)"
)

_CURSOR_UNSUPPORTED_MARKERS = (
    "unknown command",
    "unknown option",
    "unrecognized command",
    "no such command",
    "invalid command",
)

_CURSOR_AUTH_REQUIRED_MARKERS = (
    "authentication required",
    "not authenticated",
    "not logged in",
    "login required",
    "please log in",
    "please login",
    "agent login",
    "cursor agent login",
    "login",
    "unauthorized",
    "invalid api key",
)

# Environment variables that would silently authenticate a probe. They are
# stripped before a subprocess is started; no token is passed, printed or
# returned in the detection document.
_SENSITIVE_ENV_KEYS = frozenset({
    "CURSOR_API_KEY",
    "CURSOR_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENCODE_API_KEY",
    "CODEX_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
})

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
            ".agents/agents/*.md",
            ".gemini/config/agents/*/agent.md",
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

# -- V7.8 IDE ownership --------------------------------------------------
# Ownership is a local coordination record shared through project files. It
# does not create a native child, does not attach to a native session and is
# never a second supervisor. Cursor and Antigravity remain contract-only:
# their native parent owns dispatch and permissions.
IDE_OWNERSHIP_FIELDS = ("native_execution", "native_child_liveness",
                        "stop_ack")

IDE_OWNERSHIP_DESCRIPTION = (
    "Per-client truth for local IDE ownership and handoff. A local ownership "
    "file, fence or generation is coordination metadata, not a native "
    "connection claim. No platform starts a second supervisor."
)

IDE_OWNERSHIP_DISCLAIMER = (
    "IDE ownership describes what this repository can observe and record "
    "locally. It does not prove that a native IDE is connected, that a child "
    "is alive, that a stop was acknowledged by the client or that a handoff "
    "completed inside the IDE."
)

IDE_OWNERSHIP = {
    "schema_version": 1,
    "description": IDE_OWNERSHIP_DESCRIPTION,
    "disclaimer": IDE_OWNERSHIP_DISCLAIMER,
    "fields": list(IDE_OWNERSHIP_FIELDS),
    "ownership_transport": "local-project-file",
    "owner_identity": "host-and-session-hash-only",
    "owner_token_exposed": False,
    "snapshots": "metadata-only",
    "platforms": {
        "codex": {
            "native_execution": "observed-hook",
            "native_child_liveness": "hook-pair-conditional",
            "stop_ack": False,
            "stop_ack_evidence": "observed-hook-pair",
            "second_supervisor": False,
            "native_lock": False,
            "contract_only": False,
            "connection_claimed": False,
            "note": (
                "SubagentStart/SubagentStop may prove one child only when the "
                "project hooks are trusted and the IDs match. The parent Codex "
                "session controls sandbox and approval; ownership remains a "
                "local file lock."
            ),
        },
        "opencode": {
            "native_execution": "observed-plugin",
            "native_child_liveness": "foreground-only",
            "stop_ack": False,
            "stop_ack_evidence": "foreground-tool-call",
            "second_supervisor": False,
            "native_lock": False,
            "contract_only": False,
            "connection_claimed": False,
            "note": (
                "The V2 plugin observes foreground subagent tool spans in "
                "process. Background dispatch is not child liveness or a stop "
                "acknowledgement, and the stream is not exposed externally."
            ),
        },
        "cursor": {
            "native_execution": "contract-only",
            "native_child_liveness": "start-only",
            "stop_ack": False,
            "stop_ack_evidence": "matching-id-only",
            "second_supervisor": False,
            "native_lock": False,
            "contract_only": True,
            "connection_claimed": False,
            "note": (
                "Cursor remains contract-only: agent files and the universal "
                "CLI are shipped, subagentStart may expose an ID, and a stop "
                "must match it exactly. No managed session, no second "
                "supervisor and no invented command are claimed."
            ),
        },
        "antigravity": {
            "native_execution": "contract-only",
            "native_child_liveness": "no",
            "stop_ack": False,
            "stop_ack_evidence": "none",
            "second_supervisor": False,
            "native_lock": False,
            "contract_only": True,
            "connection_claimed": False,
            "note": (
                "Antigravity owns /teamwork-preview and /grill-me. Our layer "
                "stores memory, contracts and handoffs only; session hooks do "
                "not identify an individual child and no second supervisor is "
                "started."
            ),
        },
    },
}

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
        "ide_ownership": {
            **copy.deepcopy(IDE_OWNERSHIP),
            "platforms": {
                key: copy.deepcopy(IDE_OWNERSHIP["platforms"][key])
                for key in keys
            },
        },
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


def as_json(platform: str | None = None, detection: dict | None = None) -> str:
    """Return deterministic pretty-printed JSON.

    ``detection`` is optional and, when supplied, is emitted as a separate
    top-level ``detection`` block. The static document is unchanged otherwise.
    """
    document = matrix(platform)
    if detection is not None:
        document["detection"] = detection
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _iter_values(record: dict):
    for field in CAPABILITY_FIELDS:
        yield field, record["capabilities"][field]


def render_text(platform: str | None = None, detection: dict | None = None) -> str:
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
    lines.append("IDE ownership (local coordination, not a native connection claim):")
    for record in document["platforms"]:
        ownership = document["ide_ownership"]["platforms"][record["platform"]]
        lines.append(
            f"  {record['platform']}: native_execution="
            f"{ownership['native_execution']}, native_child_liveness="
            f"{ownership['native_child_liveness']}, stop_ack="
            f"{str(ownership['stop_ack']).lower()} "
            f"({ownership['stop_ack_evidence']}), second_supervisor="
            f"{str(ownership['second_supervisor']).lower()}"
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
    if detection is not None:
        lines.append("")
        lines.append(render_detection(detection).rstrip())
    return "\n".join(lines).rstrip() + "\n"


def _clean_probe_env(env: dict | None = None) -> dict:
    """Return a copy of the environment without token-bearing variables.

    The default probe environment is ``os.environ``. API keys and auth tokens
    are removed so a probe cannot accidentally authenticate with a secret this
    process happened to inherit. The function does not read a credential file.
    """
    source = dict(os.environ if env is None else env)
    cleaned = {}
    for key, value in source.items():
        upper = str(key).upper()
        if upper in _SENSITIVE_ENV_KEYS:
            continue
        if upper.endswith("_API_KEY") or upper.endswith("_AUTH_TOKEN"):
            continue
        cleaned[key] = value
    return cleaned


def _default_runner(argv, timeout):
    """Run one bounded, non-interactive probe with a sanitized environment."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=_clean_probe_env(),
        check=False,
    )


def _run_probe(argv, timeout, runner=None):
    """Run a probe and collapse every failure mode to ``None``.

    ``runner`` is an injection point for tests. It is called as
    ``runner(argv, timeout)`` and may return a ``subprocess.CompletedProcess``
    or raise. No output is printed here.
    """
    try:
        if runner is None:
            return _default_runner(argv, timeout)
        return runner(argv, timeout)
    except Exception:
        # Missing binaries, timeouts, permission errors and broken fakes are
        # all "no evidence", never an exception the CLI has to crash on.
        return None


def _probe_auth(binary, args, timeout, runner, *, success=(), failure=(),
                accept_nonempty=False):
    """Return True/False only from bounded local output; never store it."""
    if not binary:
        return None
    result = _run_probe([binary, *args], timeout, runner)
    if result is None:
        return None
    output = _combined_output(result).lower()
    if any(marker in output for marker in failure):
        return False
    code = _returncode(result)
    if code == 0:
        if any(marker in output for marker in success):
            return True
        if accept_nonempty and output.strip():
            return True
    if code != 0 and output.strip():
        return False
    return None


def _returncode(result) -> int:
    try:
        value = getattr(result, "returncode", 1)
    except Exception:
        return 1
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _combined_output(result) -> str:
    """Return bounded stdout+stderr without retaining it anywhere else."""
    if result is None:
        return ""
    parts = []
    for attribute in ("stdout", "stderr"):
        try:
            value = getattr(result, attribute, "") or ""
        except Exception:
            value = ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        if not isinstance(value, str):
            value = str(value)
        parts.append(value)
    return ("\n".join(parts))[:_DETECTION_MAX_OUTPUT]


def _extract_version(result) -> str | None:
    """Extract only a version token, never the rest of a probe's output.

    Returning a matched token (for example ``0.164.0``) instead of raw output
    means a token or banner accidentally printed by a CLI cannot be persisted
    in the detection document.
    """
    text = _combined_output(result)
    match = _VERSION_TOKEN_RE.search(text)
    if match is None:
        return None
    return match.group(1)[:_DETECTION_MAX_VERSION]


def _looks_like_model_list(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if any(marker in lowered for marker in ("no models", "not available")):
        return False
    if stripped[0] in "[{":
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError):
            return False
        if isinstance(payload, list):
            return bool(payload)
        if isinstance(payload, dict):
            for key in ("models", "data", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return bool(value)
            return bool(payload)
        return False
    return any(line.strip() for line in stripped.splitlines())


def _safe_which(which, name: str) -> str | None:
    try:
        value = which(name)
    except Exception:
        return None
    if not value:
        return None
    try:
        return os.fspath(value)
    except TypeError:
        return None


def _is_executable(path) -> bool:
    try:
        candidate = Path(path)
        return candidate.is_file() and os.access(candidate, os.X_OK)
    except OSError:
        return False


def _project_file(root: Path, relative: str) -> bool:
    try:
        return (root / relative).is_file()
    except OSError:
        return False


def _project_entry(root: Path, relative: str) -> bool:
    try:
        return (root / relative).exists()
    except OSError:
        return False


def _project_glob(root: Path, pattern: str) -> bool:
    try:
        return any(item.is_file() for item in root.glob(pattern))
    except (OSError, ValueError):
        return False


def _cursor_candidates(home: Path, which, apps_root: Path) -> list:
    candidates = []
    seen = set()

    def add(path):
        if not path:
            return
        try:
            text = os.fspath(path)
        except TypeError:
            return
        if text in seen:
            return
        seen.add(text)
        candidates.append(text)

    for name in ("cursor-agent", "cursor"):
        add(_safe_which(which, name))
    local_agent = home / ".local" / "bin" / "cursor-agent"
    if _is_executable(local_agent):
        add(local_agent)
    app_agent = apps_root / "Cursor.app/Contents/Resources/app/bin/cursor"
    if _is_executable(app_agent):
        add(app_agent)
    return candidates


def _probe_cursor(candidates, run_version: bool, timeout: float, runner):
    """Return (version, cli_available, authenticated) for Cursor.

    ``agent`` support is verified with ``cursor agent --version`` and, when
    that fails, ``cursor agent --help``. Authentication is only probed after
    support is established. No ``--api-key`` argument is ever passed.
    """
    if not candidates:
        return None, False, None
    if not run_version:
        return None, True, None

    saw_unknown = False
    for candidate in candidates:
        result = _run_probe(
            [candidate, "agent", "--version"], timeout, runner
        )
        if result is None:
            # A timeout is unknown, not proof that the candidate is unusable.
            saw_unknown = True
            break
        lowered = _combined_output(result).lower()
        if any(marker in lowered for marker in _CURSOR_UNSUPPORTED_MARKERS):
            continue
        if _returncode(result) == 0:
            version = _extract_version(result)
            authenticated = _probe_cursor_auth(candidate, timeout, runner)
            return version, True, authenticated
        help_result = _run_probe(
            [candidate, "agent", "--help"], timeout, runner
        )
        if help_result is not None and _returncode(help_result) == 0:
            help_text = _combined_output(help_result).lower()
            if "cursor agent" in help_text or "usage:" in help_text:
                return None, True, _probe_cursor_auth(
                    candidate, timeout, runner
                )
    return None, (None if saw_unknown else False), None


def _probe_cursor_auth(candidate: str, timeout: float, runner):
    """Return True/False/None from a real ``cursor agent --list-models`` call."""
    auth_timeout = min(timeout, _DETECTION_AUTH_TIMEOUT)
    result = _run_probe(
        [candidate, "agent", "--list-models"], auth_timeout, runner
    )
    if result is None:
        return None
    text = _combined_output(result)
    lowered = text.lower()
    if any(marker in lowered for marker in _CURSOR_AUTH_REQUIRED_MARKERS):
        return False
    if _returncode(result) == 0 and _looks_like_model_list(text):
        return True
    return None


def _probe_versioned_cli(name, which, run_version: bool, timeout: float, runner):
    """Return (installed, version, cli_available) for a PATH CLI."""
    path = _safe_which(which, name)
    if path is None:
        return False, None, False
    version = None
    if run_version:
        result = _run_probe([path, "--version"], timeout, runner)
        if result is not None and _returncode(result) == 0:
            version = _extract_version(result)
    return True, version, True


def _live_ides(root: Path):
    """Read the V7.8 live-handoff evidence, if present, without writing it."""
    path = root / "docs" / "assets" / "v78-live-handoff.json"
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("live_ides")
    if not isinstance(raw, list):
        return None
    live = set()
    for item in raw:
        if isinstance(item, str):
            key = item.strip().lower()
            if key in PLATFORM_ORDER:
                live.add(key)
    return live


def _bounded_timeout(timeout) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return 5.0
    if not math.isfinite(value) or value <= 0:
        return 5.0
    return min(value, _DETECTION_MAX_TIMEOUT)


def detect_environment(*, root=None, home=None, which=None, run_version=True,
                       timeout=5.0, runner=None, now=None, apps_root=None) -> dict:
    """Probe the local machine for IDE capability evidence.

    The function is read-only in its own process: it checks project files,
    asks ``which`` for executable paths, and runs bounded
    ``--version``/``--list-models`` probes. It writes no file and makes no
    direct network request; the probed CLI may perform its own local version
    or authentication check. It never reads a credential store and never
    stores raw command output. Unknown questions stay ``None``; ``False`` is
    used only when a local probe establishes the negative (for example, no
    candidate binary exists).

    ``which`` and ``runner`` are injection points for deterministic tests.
    ``runner`` is called as ``runner(argv, timeout)`` and should return an
    object with ``returncode``, ``stdout`` and ``stderr``.
    """
    try:
        root_path = (
            Path(root) if root is not None
            else Path(__file__).resolve().parents[1]
        )
    except (OSError, TypeError, ValueError):
        root_path = Path(__file__).resolve().parents[1]
    if home is None:
        try:
            home_path = Path.home()
        except Exception:
            home_path = root_path
    else:
        try:
            home_path = Path(home)
        except (OSError, TypeError, ValueError):
            home_path = root_path
    if apps_root is None:
        apps_path = Path("/Applications")
    else:
        try:
            apps_path = Path(apps_root)
        except (OSError, TypeError, ValueError):
            apps_path = Path("/Applications")
    which_fn = shutil.which if which is None else which
    probe_timeout = _bounded_timeout(timeout)
    do_version = bool(run_version)
    live = _live_ides(root_path)

    codex_installed, codex_version, codex_cli = _probe_versioned_cli(
        "codex", which_fn, do_version, probe_timeout, runner
    )
    opencode_installed, opencode_version, opencode_cli = _probe_versioned_cli(
        "opencode", which_fn, do_version, probe_timeout, runner
    )
    codex_path = _safe_which(which_fn, "codex")
    opencode_path = _safe_which(which_fn, "opencode")
    codex_auth = None
    if codex_cli and do_version:
        codex_auth = _probe_auth(
            codex_path, ["login", "status"], probe_timeout, runner,
            success=("logged in",), failure=(
                "not logged in", "login required", "not authenticated",
                "signed out", "no credentials"))
    opencode_auth = None
    if opencode_cli and do_version:
        opencode_auth = _probe_auth(
            opencode_path, ["auth", "list"], probe_timeout, runner,
            success=("stored", "api key", "credentials"),
            failure=("no credentials", "not authenticated",
                     "no providers", "signed out"),
            accept_nonempty=True)

    cursor_candidates = _cursor_candidates(home_path, which_fn, apps_path)
    cursor_version, cursor_cli, cursor_auth = _probe_cursor(
        cursor_candidates, do_version, probe_timeout, runner
    )

    antigravity_cli_path = _safe_which(which_fn, "antigravity")
    antigravity_cli = antigravity_cli_path is not None
    antigravity_version = None
    if antigravity_cli and do_version:
        result = _run_probe(
            [antigravity_cli_path, "--version"], probe_timeout, runner
        )
        if result is not None and _returncode(result) == 0:
            antigravity_version = _extract_version(result)
    antigravity_app = _project_entry(apps_path, "Antigravity.app")

    def live_value(key: str):
        if live is None:
            return None
        return key in live

    def handoff_value(key: str):
        if live is not None and key in live:
            return True
        return None

    # Lifecycle observation requires a shipped observer file, and even then it
    # is unproven until a live session is exercised. Missing observer files are
    # a definite negative, not a guess.
    codex_observer = _project_file(root_path, ".codex/hooks.json")
    opencode_observer = _project_file(
        root_path, "adapters/opencode-v2/auto_orchestrator.js"
    )
    cursor_observer = _project_file(root_path, ".cursor/hooks.json")

    codex = {
        "installed": codex_installed,
        "version_detected": codex_version,
        "cli_available": codex_cli,
        "authenticated": codex_auth,
        "skill_discovered": _project_file(
            root_path, ".codex/skills/teamwork/SKILL.md"
        ),
        "command_discovered": _project_file(
            root_path, ".codex/commands/teamwork.md"
        ),
        "native_agent_available": _project_glob(
            root_path, ".codex/agents/*.toml"
        ),
        "lifecycle_observable": None if codex_observer else False,
        "child_completion_authoritative": None if codex_observer else False,
        "handoff_supported": handoff_value("codex"),
        "grill_supported": _project_entry(
            root_path, ".agents/skills/grill-me"
        ),
        "live_e2e_verified": live_value("codex"),
    }

    opencode = {
        "installed": opencode_installed,
        "version_detected": opencode_version,
        "cli_available": opencode_cli,
        "authenticated": opencode_auth,
        "skill_discovered": _project_entry(
            root_path, ".opencode/skills/teamwork"
        ),
        "command_discovered": _project_file(
            root_path, ".opencode/commands/teamwork.md"
        ),
        "native_agent_available": _project_glob(
            root_path, ".opencode/agents/*.md"
        ),
        "lifecycle_observable": None if opencode_observer else False,
        "child_completion_authoritative": None if opencode_observer else False,
        "handoff_supported": handoff_value("opencode"),
        "grill_supported": (
            _project_file(root_path, ".opencode/commands/grill-me.md")
            or _project_entry(root_path, ".opencode/skills/grill-me")
        ),
        "live_e2e_verified": live_value("opencode"),
    }

    cursor = {
        "installed": bool(cursor_candidates),
        "version_detected": cursor_version,
        "cli_available": cursor_cli,
        "authenticated": cursor_auth,
        "skill_discovered": _project_file(
            root_path, ".agents/skills/auto-orchestrator/SKILL.md"
        ),
        "command_discovered": _project_file(
            root_path, ".cursor/commands/teamwork.md"
        ),
        "native_agent_available": _project_glob(
            root_path, ".cursor/agents/*.md"
        ),
        "lifecycle_observable": None if cursor_observer else False,
        "child_completion_authoritative": None if cursor_observer else False,
        "handoff_supported": handoff_value("cursor"),
        "grill_supported": _project_entry(
            root_path, ".agents/skills/grill-me"
        ),
        "live_e2e_verified": live_value("cursor"),
    }

    antigravity = {
        "installed": antigravity_app or antigravity_cli,
        "version_detected": antigravity_version,
        "cli_available": antigravity_cli,
        "authenticated": None,
        "skill_discovered": _project_file(
            root_path, ".agents/skills/auto-orchestrator/SKILL.md"
        ),
        "command_discovered": _project_file(
            root_path, ".agents/commands/teamwork.md"
        ),
        "native_agent_available": _project_glob(
            root_path, ".agents/agents/*.md"
        ),
        "lifecycle_observable": False,
        "child_completion_authoritative": False,
        "handoff_supported": handoff_value("antigravity"),
        "grill_supported": (
            _project_entry(root_path, ".agents/skills/ao-grill-me")
            or _project_entry(root_path, ".agents/skills/grill-me")
        ),
        # The V7.9 requirement is explicit: without a CLI entry the native
        # commands are unobservable, so live execution is not claimed.
        "live_e2e_verified": (
            live_value("antigravity") if live is not None else False
        ),
    }

    generated_at = time.time()
    if now is not None:
        try:
            generated_at = float(now)
        except (TypeError, ValueError):
            pass

    platforms = {
        "codex": codex,
        "opencode": opencode,
        "cursor": cursor,
        "antigravity": antigravity,
    }
    return {
        "schema_version": DETECTION_SCHEMA_VERSION,
        "kind": DETECTION_KIND,
        "generated_at": generated_at,
        "platforms": platforms,
        "evidence": DETECTION_EVIDENCE,
    }


def _render_detection_value(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_detection(detection: dict) -> str:
    """Render a detection block as text; never invents a live claim."""
    lines = [
        "Runtime IDE capability detection (local probe; no live execution "
        "claimed):",
        "  schema_version: " + str(detection.get("schema_version", 1)),
        "  kind: " + str(detection.get("kind", DETECTION_KIND)),
        "  generated_at: "
        + _render_detection_value(detection.get("generated_at")),
        "  evidence: " + str(detection.get("evidence", DETECTION_EVIDENCE)),
        "  note: live_e2e_verified reflects recorded evidence; this probe runs "
        "no agent.",
    ]
    platforms = detection.get("platforms", {})
    for key, record in platforms.items():
        if not isinstance(record, dict):
            continue
        lines.append(f"  {key}:")
        for field in DETECTION_FIELDS:
            lines.append(
                f"    {field}: {_render_detection_value(record.get(field))}"
            )
    return "\n".join(lines) + "\n"


def _filter_detection(detection: dict, platform: str | None) -> dict:
    if platform is None:
        return detection
    filtered = copy.deepcopy(detection)
    filtered["platforms"] = {
        platform: filtered["platforms"][platform]
    }
    return filtered


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
    show.add_argument(
        "--detect",
        dest="detect",
        action="store_true",
        help=(
            "append a read-only local IDE detection block; no live execution "
            "and no credential-store access"
        ),
    )
    show.add_argument(
        "--no-detect",
        dest="detect",
        action="store_false",
        help="keep the static matrix only (default)",
    )
    show.set_defaults(detect=False)
    args = parser.parse_args(argv)
    detection = None
    if args.detect:
        try:
            detection = _filter_detection(
                detect_environment(), args.platform
            )
        except Exception:
            print(
                "CAPABILITY DETECTION ERROR: local probe failed",
                file=sys.stderr,
            )
            return 1
    try:
        output = (
            as_json(args.platform, detection=detection)
            if args.json
            else render_text(args.platform, detection=detection)
        )
    except CapabilityError as exc:
        print("CAPABILITY ERROR: " + str(exc), file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
