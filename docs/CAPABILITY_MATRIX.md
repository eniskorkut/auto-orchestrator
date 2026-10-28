# Native capability matrix (V7.1)

[`tools/capabilities.py`](../tools/capabilities.py) is the machine-readable source of truth for what this repository's adapters can and cannot establish per native client. It is deliberately conservative: **no capability is advertised as unconditional**, and no platform is marked as supporting external agent dispatch, cancellation, background tracking, resume or a live terminal stream.

The matrix does not claim that a native IDE was launched or authenticated. It describes documented native contracts plus the adapters and simulated tests shipped here. See [Real IDE smoke tests vs simulated tests](#real-ide-smoke-tests-vs-simulated-tests).

```bash
python tools/capabilities.py show
python tools/capabilities.py show --json
python tools/capabilities.py show --platform cursor --json
```

The command performs no network access and emits no timestamps, so repeated runs are byte-for-byte stable. Installed copies live at `tools/capabilities.py` in a project-local install, or under the platform's `auto-orchestrator/tools/` directory in a global install.

## Matrix

| Capability | Codex | OpenCode V2 | Antigravity | Cursor |
|---|---|---|---|---|
| Native spawn exposed to an external orchestrator | `false` | `false` | `false` | `false` |
| Exact child identity | `conditional` | `foreground-only` | `no` | `start-only` |
| Native start/stop proof | `conditional` | `foreground-only` | `no` | `conditional` |
| Cancel support | `false` | `false` | `false` | `false` |
| Background tracking | `false` | `false` | `false` | `false` |
| Resume support | `false` | `false` | `false` | `false` |
| Live terminal stream | `false` | `false` | `false` | `false` |
| Session event stream | `hook-callbacks` | `plugin-events` | `hook-callbacks` | `hook-callbacks` |

`native_spawn_exposed_to_external_orchestrator` is `false` everywhere because the native parent remains the only entity that spawns its own agents. The Supervisor can claim and bind a task; it does not spawn, stop or terminate an IDE agent. A platform-native spawn tool is not an external orchestration API.

### Strict booleans and scoped statuses

Five questions are strict yes/no capabilities and are literal `false` on every platform: external spawn, cancel, background tracking, resume and live terminal. There is no partial native integration to advertise, so they are booleans rather than a status string.

Conditional questions use the following status vocabulary:

| Value | Meaning |
|---|---|
| `no` | Not supported, not exposed, or not implemented by this repository. |
| `conditional` | Usable only under stated conditions — for example trusted hooks and an exact matching child ID. |
| `foreground-only` | Foreground execution can be observed; background execution is not authoritative. |
| `start-only` | A child identifier is reliable at start but not guaranteed at stop. |
| `hook-callbacks` | Discrete hook invocations receive JSON; this is not a persistent stream. |
| `plugin-events` | An in-process plugin subscribes to native events; the stream is not exposed outside that plugin. |
| `unknown` | Reserved for evidence that is not established. It is not assigned in the current matrix. |

There is intentionally no `yes` value and no conditional field is `true`. A support statement without a scope would be a stronger claim than the evidence allows.

### Field semantics

| Field | Question answered |
|---|---|
| `native_spawn_exposed_to_external_orchestrator` | Can this repository ask the native client to spawn a child through a documented external API? |
| `exact_child_identity` | Can a lifecycle event be attributed to one specific child? |
| `native_start_stop_proof` | Can an observed start and a matching observed stop for the same child be established? |
| `cancel_support` | Can the external orchestrator issue a native cancel and observe its acknowledgement? |
| `background_tracking` | Can background child execution be tracked until completion, not merely until the spawn call returns? |
| `resume_support` | Can the orchestrator resume a child session with preserved identity and correlation? |
| `live_terminal_stream` | Is an authenticated live terminal/PTY stream attached to the orchestrator? |
| `session_event_stream` | What delivery does the adapter actually receive: hook callbacks, in-process plugin events or nothing? |

A Supervisor `cancel_requested` state is a local metadata transition. It is **not** native cancellation and is not counted as `cancel_support`.

## Managed execution (V7.3, separate from native observation)

Managed execution means this repository starts a session through a documented
CLI/API and reads its event stream. It is **not** native hook observation and
its completion is **not** a native subagent stop. The managed matrix is exposed
as `managed_execution` by `python tools/capabilities.py show --json`.

| Platform | Managed status | Live smoke | Cancel ACK | Resume | Terminal |
|---|---|---|---|---|---|
| OpenCode | `live-verified-local` (`opencode run --format json`); V7.4.1 owner-verified crash recovery | yes, local 2.1.1 | **false** (local process signal only) | yes (`--session`) | no |
| Codex | `live-verified-local` (`codex app-server` JSON-RPC) | yes, local 0.164.0; `gpt-6.1-sol` | **false** (local interrupt, no ACK) | adapter supports `thread/resume`; live resume not smoked | no |
| Antigravity | `not-implemented` | no | false | no | no |
| Cursor | `not-implemented` | no | false | no | no |

The OpenCode adapter records session id, sanitized tool activity, usage/cost,
resume and completion from the real CLI stream. V7.4.1 additionally records the
child PID and process start token for recovery; only an identity-verified owned
process may be signalled. It does **not** claim native cancellation
acknowledgement, background-stop proof or a terminal.

The Codex adapter now records live `thread/start`, `turn/start`, token usage,
tool-item counts and `turn/completed` from a local Codex 0.164.0 app-server using
the ChatGPT login. Native cancellation acknowledgement, IDE-native subagent
status and a live resume claim remain unsupported. Evidence:
`docs/assets/v75-codex-smoke.json` (V7.5 branch). Fake app-server tests remain as
fast contract regressions; they are not the live evidence. Antigravity and
Cursor have no external managed-session integration in this version.

## Platform evidence and limits

### Codex

**Evidence source:** `.codex/hooks.json` → `tools/hook_bridge.py` (`SubagentStart`, `SubagentStop`), with the platform contract in [NATIVE_SUPERVISOR_PLATFORM_MATRIX.md](NATIVE_SUPERVISOR_PLATFORM_MATRIX.md) and simulated payload tests in `tests/test_platform_contracts.py`.

- `SubagentStart` and `SubagentStop` can carry `agent_id`, but only when project hooks are enabled, trusted and actually executed.
- The Supervisor accepts a stop only as part of a matching start + stop pair for the same ID. A stop without an observed start is rejected. Hooks that never fire leave the task reserved.
- Raw native IDs are hashed before persistence; telemetry contains status, opaque IDs and timestamps.
- No external spawn, cancel, background-child completion, resume or terminal stream is established.

### OpenCode V2

**Evidence source:** `adapters/opencode-v2/auto_orchestrator.js` observes the native `subagent` tool and session events in-process; `tests/js/test_opencode.mjs` simulates the event shapes; the README records V6/V6.1 field-test limits.

- Foreground native subagent tool begin/end can be matched by tool-call ID.
- A background `subagent` call returns as soon as dispatch is acknowledged. That return is **not** child completion, and no authoritative background stop is established. Treating it as a stop would fabricate evidence.
- The plugin receives an in-process event stream. The stream is not exposed to the dashboard or an external orchestrator, and an interrupted stream is retained as incomplete evidence rather than repaired.
- The plugin must be loaded by the installed Desktop build. A passing mock test does not prove that it loaded.

### Google Antigravity

**Evidence source:** `.agents/hooks.json` → `tools/hook_bridge.py` session hooks; [NATIVE_SUPERVISOR_PLATFORM_MATRIX.md](NATIVE_SUPERVISOR_PLATFORM_MATRIX.md); simulated tests.

- `SessionStart` and `Stop` identify a conversation, not an individual child.
- `PreToolUse`/`PostToolUse` measure `invoke_subagent` dispatch. They do not prove which child started or stopped, and are never used as per-child completion evidence.
- Multi-workspace sessions are ambiguous; the bridge refuses to guess a project root.
- Antigravity therefore cannot release a native-supervised task on its documented hooks.

### Cursor IDE

**Evidence source:** project-local `.cursor/hooks.json` → `tools/hook_bridge.py` (`subagentStart`, `subagentStop`); [NATIVE_SUPERVISOR_PLATFORM_MATRIX.md](NATIVE_SUPERVISOR_PLATFORM_MATRIX.md); simulated tests.

- `subagentStart` documents `subagent_id`, so start identity is reliable.
- The documented `subagentStop` payload may omit the ID, and some Cursor versions do not fire a stop for background subagents. Identity is therefore `start-only`, and start/stop proof is `conditional` on an exact matching stop ID.
- An unmatched stop, a different ID, or a stop with no ID leaves the task reserved. Task text, subagent type and transcript paths are never used to guess identity.
- User-level Cursor hooks are intentionally not enabled by the global installer because their stdin does not attest a workspace. Project-local hooks are required for observation.

## Real IDE smoke tests vs simulated tests

This repository's Python and Node tests replay documented hook and plugin payloads. They exercise parsing, matching, fail-closed transitions, installer behavior and UI contracts. They **do not** launch, authenticate to or drive Codex, OpenCode, Antigravity or Cursor. A green hosted test run is not native end-to-end evidence.

At the time of V7.1, no real-IDE smoke result is recorded here. Do not write a PASS claim from a simulated test. A real smoke run must, in a disposable project:

1. Install the adapter and explicitly trust/enable its hooks or plugin in the native client.
2. Inspect the actual callback fields, including whether an ID is present, rather than assuming a documented shape.
3. Exercise foreground and background subagents separately where the client offers both.
4. Attempt a matching start/stop, a missing stop, an unmatched stop and cancellation, and verify the Supervisor keeps or releases reservations accordingly.
5. Confirm only hashed IDs, status and timestamps reach telemetry; no prompts, code, commands, tokens or raw native IDs.
6. Repeat install/uninstall with pre-existing user configuration and verify original content is preserved.

The detailed acceptance procedure is in [NATIVE_SUPERVISOR_PLATFORM_MATRIX.md](NATIVE_SUPERVISOR_PLATFORM_MATRIX.md#native-smoke-acceptance); manual client scenarios are in [IDE_TESTING.md](../IDE_TESTING.md). A simulated matrix value is evidence about the adapter contract, not proof that a specific installed client behaves the same way.

## Updating the matrix

Change `tools/capabilities.py`, its tests in `tests/test_capabilities.py`, and this document together. Downgrade a value when evidence is missing; do not upgrade it from vendor capability alone. External dispatch, cancellation, background completion, resume and terminal streaming require a documented API **and** an executed integration before they can move above `no`.

## References

- Codex hooks: <https://developers.openai.com/codex/hooks>
- Codex subagents: <https://developers.openai.com/codex/subagents>
- OpenCode agents: <https://opencode.ai/docs/agents>
- OpenCode skills: <https://docs.opencode.ai/docs/skills/>
- Cursor hooks: <https://cursor.com/docs/hooks>
- Antigravity hooks: <https://www.antigravity.google/docs/hooks>
- Antigravity subagents: <https://www.antigravity.google/docs/subagents/>

## Universal Teamwork and Grill-Me (V7.6 branch)

`tools/universal_teamwork.py` is a common command surface over the existing
managed execution layer. It does not make one IDE dispatch another IDE's agent.

| Platform | Universal CLI | Grill-Me skill | Execution claim |
|---|---|---|---|
| OpenCode V2 | yes; real local command contract | `/grill-me` command + shared skill | managed OpenCode adapter; TUI click-through not automated |
| Codex | yes; shared skill/CLI | shared `.agents/skills/grill-me` where the client discovers it | real read-only app-server smoke (V7.5); managed start requires explicit `--provider codex`; write mode unclaimed |
| Cursor | yes; shared skill/CLI | shared `.agents/skills/grill-me` | no managed adapter; native Cursor agents; explicit opt-in required for the OpenCode bridge; no local binary |
| Antigravity | yes; shared skill/CLI | shared skill; native `/grill-me` bridge documented | no managed adapter; native agents; explicit opt-in required for the OpenCode bridge; no local binary |

Project memory is shared through the project root, not through a cross-IDE
agent call. A project-level writer lease prevents two Teamwork runners from
writing concurrently; ordinary memory writes are serialized by SQLite.


## V7.6.1 — assumption guard and verified contracts

| Platform | Guard / contract | Execution |
|---|---|---|
| OpenCode V2 | Central guard, persistent interview, contract load | Managed OpenCode adapter; TUI click-through not automated |
| Codex | Shared skill, guard/contract CLI | Real app-server write E2E with explicit `--codex-sandbox workspace-write`; read-only default; approval bridging absent |
| Cursor | Shared skill/CLI contract only | No managed adapter; no local live run |
| Antigravity | Shared skill/CLI and native `/grill-me` guidance | No managed adapter; no local live run |

The guard/contract layer is shared by every platform. Platform execution
adapters remain separate; no silent cross-IDE process launch is performed.

## V7.7 — Native Teamwork 2.0 entry points and binding limits

`teamwork_entry` is a separate machine-readable section in
`python tools/capabilities.py show --json`. It describes the shipped entry
surface, not a live session. Statuses are `native-skill`, `native-slash`,
`plugin-command` and `contract-only`.

| Platform | Entry status | Shipped surface | Binding limit |
|---|---|---|---|
| Codex | `native-skill` | `.codex/skills/teamwork/SKILL.md`, `.codex/agents/*.toml`; no `/teamwork` slash file | Trusted matching hook start+stop only; parent session owns sandbox/approval |
| OpenCode V2 | `plugin-command` | `/teamwork`, `/grill-me`, V2 plugin | Foreground subagent tool-call only; background dispatch is not completion |
| Antigravity | `native-slash` | Native `/teamwork-preview` and native `/grill-me`; `ao-grill-me` for memory/contracts | Unsupported per-child binding; no second supervisor |
| Cursor | `contract-only` | `.cursor/agents/*`, shared skill, universal CLI | Start ID only; matching stop required, no invented command file |

The JSON entry for every platform contains:

- `status` — one of the four statuses above;
- `entry_points` / `slash_commands` — exactly what is shipped or native;
- `native_or_managed` — what evidence exists, without a blanket claim;
- `parent_session_boundary` — who owns dispatch, sandbox and approval;
- `binding_limits` — `external_spawn: false`, `native_cancel_ack: false`,
  `native_session_resume: false`, `native_session_claimed: false` and the
  platform-specific start/stop condition.

A skill file, command file, plan or handoff is not proof that a native session
was spawned, bound, cancelled or resumed. Codex no longer claims a per-agent
`sandbox_mode`; the parent session controls it. Antigravity's portable
interview is installed as `ao-grill-me` so it cannot shadow native `/grill-me`.
No platform advertises `/teamwork` unless a supported file or native surface
actually exists.


## V7.7 — verified completion and native entry points

| Platform | Teamwork entry | Binding limit |
|---|---|---|
| OpenCode V2 | `/teamwork`, `/grill-me`, skill, plugin lifecycle | managed OpenCode adapter; plugin native tool spans only, no authoritative child completion |
| Codex | native Teamwork skill, custom agents, app-server adapter | parent session owns sandbox/approval; workspace-write explicit; approval bridge absent |
| Cursor | `.cursor/agents`, shared skill/CLI | contract-only; no managed adapter or live local run |
| Antigravity | native `/teamwork-preview`, native `/grill-me`, `ao-grill-me` bridge | native Teamwork authoritative; our layer stores state only; no duplicate supervisor |

Completion engine separates milestone pass from requirements verification.
Only real test/subprocess evidence can verify behavioral criteria; missing tests
or independent review remain `needs_review`/`not_verified`.

## V7.8 — IDE ownership, handoff and dashboard evidence

`python tools/capabilities.py show --json` now exposes an `ide_ownership`
section. It is separate from `capabilities`, `teamwork_entry` and
`managed_execution`: it answers what this repository can observe about which
native client holds the local coordination record, never whether a native
client is connected.

| Platform | `native_execution` | `native_child_liveness` | `stop_ack` | `stop_ack_evidence` | Second supervisor |
|---|---|---|---|---|---|
| Codex | `observed-hook` | `hook-pair-conditional` | `false` | `observed-hook-pair` | `false` |
| OpenCode V2 | `observed-plugin` | `foreground-only` | `false` | `foreground-tool-call` | `false` |
| Cursor | `contract-only` | `start-only` | `false` | `matching-id-only` | `false` |
| Antigravity | `contract-only` | `no` | `false` | `none` | `false` |

`stop_ack` is a strict boolean on every platform: this repository cannot make
a native client acknowledge a stop request. `stop_ack_evidence` names the
strongest local observation (a matching hook pair, a foreground tool call, a
matching ID or none) without upgrading it to an acknowledgement.

Every platform entry also states `connection_claimed: false`,
`native_lock: false` and `second_supervisor: false`. The section itself sets
`ownership_transport: local-project-file`,
`owner_identity: host-and-session-hash-only` and
`owner_token_exposed: false`. Cursor and Antigravity are contract-only: they
ship agent files and the universal CLI contract, but this repository does not
launch, bind, cancel or resume their native subagents. Antigravity keeps its
native `/teamwork-preview` and `/grill-me`; the portable interview remains
`ao-grill-me` so it cannot shadow the native command.

### Dashboard ownership evidence

`GET /api/projects/{project_id}/ide`, the memory endpoint and the milestones
endpoint expose the same bounded ownership block. It shows:

- active IDE, previous IDE and last recorded IDE as separate values;
- local ownership state, fence and generation;
- owner host/session **hashes only** — no raw owner token, session ID or host;
- handoff age, source, target, digest and drift when the handoff module
  reports them;
- current run/milestone/task, requirements completion (`verified`, `partial`,
  `review_needed`, `failed`, `not_run` or `unknown`), recovery/blocked state,
  stale-context reasons and a next recommended action;
- the open Grill-Me question with why, rationale and alternatives;
- `native.connected: null` unless independent client evidence exists. A local
  lock file is not a native handshake, so the dashboard renders that value as
  unknown.

When `tools/ide_handoff.py` is absent, unreadable or raises, the block reports
`source: "unavailable"`, `state: "unknown"` and explicit `unknowns` entries
instead of guessing. Demo projects stay labelled `demo`; ownership evidence is
never fabricated for them.

The installers ship `ide_handoff.py` with the existing optional-module rule,
keep user files, hooks and memory untouched, and prune only byte-identical
managed integration files. No installer or dashboard code starts a second
supervisor or opens a native session.

### Live sequential handoff observed on this machine

`tools/v78_live_handoff_e2e.py` executed the real CLIs in a temporary git
project on 2026-10-27 (macOS, Python 3.11.15). Evidence:
`docs/assets/v78-live-handoff.json`.

| Surface | Observed | Label |
|---|---|---|
| Codex `codex-cli 0.164.0` | `codex exec` wrote a file, then recovered `next_action` from `ide status` after a second handoff and wrote the final file | `live-verified` for the sequential loop |
| OpenCode `v2.1.1` | `opencode run --standalone --auto` recovered `next_action` from `ide status` and wrote the marker with its own tool | `live-verified` for the sequential loop |
| Cursor | no `cursor-agent`/`cursor` CLI on this machine | `contract-only` |
| Antigravity | no CLI agent entry on this machine; native `/teamwork-preview` and `/grill-me` remain vendor-authoritative | `contract-only` |

Native verification is separate from native self-report: `native-verify` runs
the approved test command itself and binds the real exit code, commit, contract
digest and run/task to the Completion Engine. `native-audit` records a
declared-role review against the same HEAD; it is not a cryptographic
attestation. Self-report alone stays `verified: false`.

What this does **not** prove: native child liveness, a native stop
acknowledgement, per-child supervision, Cursor/Antigravity execution, or model
quality. Those remain exactly as described in the table above. One scripted
run is not a benchmark and no human time-saving number is claimed.
