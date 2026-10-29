# Release history

This page preserves the release context that used to fill the README. Current
installation and product guidance lives in [Getting started](GETTING_STARTED.md)
and the main [README](../README.md). Version notes describe what was true at the
time; they are not a promise that every claim applies to the current client.

The previous chronological README is still available in Git at
`b6f01b0:README.md` for a verbatim record.

## V1 — portable native coordination

- One shared `.agents/skills/auto-orchestrator/SKILL.md` for OpenCode, Codex and
  Antigravity.
- A short, idempotent activation rule in the target project's `AGENTS.md`.
- Explorer, implementer, tester and reviewer agent profiles.
- Calling model inherited by default; optional Codex `gpt-6-luna` at `max`
  effort for subagents only.
- Concurrency up to three independent native agents, guarded by dependency and
  file-ownership rules.
- A zero-dependency `perf.py` CLI for real wall times and observed agent
  overlap. It reports an estimated parallelism benefit, not measured human time
  saved.

## V2 — project-local instrumentation

- Codex `.codex/hooks.json` + `hook_bridge.py` (`UserPromptSubmit`,
  `SubagentStart`, `SubagentStop`, `Stop`).
- OpenCode classic plugin for system instructions and native `task` lifecycle.
- Antigravity `.agents/hooks.json` (`SessionStart`, `PreInvocation`, `Stop`).
- Hooks are advisory and best-effort. They do not bypass approval or client
  permissions, and Antigravity session hooks do not establish per-child runtime.

## V3 — global installation and IDE testing

- `install_global.py` for Codex, OpenCode Classic/V2 and Antigravity IDE/CLI.
- `doctor.py` checks file readiness, not model choice or native spawning.
- Existing Codex `config.toml` is preserved; incompatible settings fail safely.
- OpenCode V1 and V2 use different plugin APIs and must not be loaded together.
- `IDE_TESTING.md` records three manual smoke scenarios.

## V4 — Supervisor, router and worktrees

- `supervisor.py`: durable SQLite tasks, dependency-aware claims, leases,
  retries and structured results. It does not spawn IDE agents.
- `router.py`: discovers `SKILL.md` metadata and emits a minimal context subset.
- `worktree.py`: creates isolated Git worktrees and previews merge conflicts.
- A clean preview is a preflight, not a verified merge.

## V5 — evidence-based verification

- Separates execution completion from verification.
- OpenCode V2 can observe minimal check and mutation events.
- Auto-verification is disabled by default and requires
  `.auto-orchestrator/verification.json`.
- The plugin observes already-executed commands with integer exit codes; it
  never runs the configured commands itself.
- Native tool status without an integer exit code is not verification.

## V6 — native supervision and platform contracts

- Correlates Supervisor attempts with observed native subagent events.
- A stop without a matching start is rejected; unsupported platforms keep the
  scope reserved rather than guessing completion.
- Cursor project hooks are supported; user-level hooks are not installed because
  their payload does not attest a workspace.
- V6.1 documented OpenCode 2.1.x live compatibility and the limits of implicit
  cwd, background dispatch and late binding.
- V6.2 added conservative skill routing and opt-in task review.
- V6.3 added prompt-time routing without writing raw prompts to telemetry.
- V6.4 stabilized the native event stream and made unfinished attempts visible
  in reports instead of dropping them.

## V7.1 — multi-project evidence console

- Local project registry and fleet overview.
- Each project keeps its own `.auto-orchestrator/` evidence; identical run IDs
  in two projects remain separate.
- Dashboard remains loopback-only and read-only.
- Virtual Office desk grid and Agent Inspector introduced.
- Controlled planner/executor adds acceptance criteria, write-scope checks and
  a dispatch manifest; optional OpenTelemetry adapter.

See [Multi-project](MULTIPROJECT.md), [Controlled execution](CONTROLLED_EXECUTION.md)
and [Virtual Office](VIRTUAL_OFFICE.md).

## V7.2 — continuous Virtual Office

- Replaced the card grid with one continuous Canvas 2D pixel-art office per
  project.
- A* pathfinding, department rooms, per-project layouts, layout editor,
  keyboard roster and reduced motion.
- Evidence before animation: unlinked sessions are ghosts, demo sessions are
  labelled `DEMO DATA`, and a dropped SSE connection cannot create completion.
- Agent Inspector keeps the evidence model and never renders fake terminal
  output.

See [Virtual Office 2.0](VIRTUAL_OFFICE.md).

## V7.3 — Teamwork engine and managed execution

- Milestones and a task DAG, implementers in isolated worktrees, testers,
  critics, challengers, auditors and a bounded repair loop.
- Milestone state machine: `planned -> ready -> running -> verifying -> passed`
  plus repair/failure/block states; progress counts only verified milestones.
- OpenCode managed execution live-verified via `opencode run --format json`.
- A disposable task-manager sample was built, reviewed and repaired after a
  controlled frontend fault.
- Virtual Office characters map to real managed sessions; managed completion is
  never claimed as a native stop.

See [Teamwork engine](V73_TEAMWORK.md).

## V7.4 — adaptive teamwork

- Event-driven bounded scheduler, FAST/BALANCED/RIGOROUS/AUTO modes and a
  single-chat `/teamwork` bridge.
- Correct concurrency metrics: interval union is no longer called overlap;
  schema 2 exposes true overlap, peak/average concurrency and critical path.
- Orphaned running tasks are explicitly reconciled; local cancel is never shown
  as native ACK.
- Live two-session probe: 8.65 s wall and 8.41 s true overlap, peak 2.
- FAST sample: 33 backend tests and browser E2E passed in 247.8 s and $0.0528,
  versus V7.3's 1566.1 s/$0.1404 on the same sample, with lower verification
  depth. These are historical, context-specific measurements.

See [Adaptive teamwork](V74_ADAPTIVE_TEAMWORK.md).

## V7.4.1 — recovery and secure input

- Real crash-recovery pass: a two-implementer OpenCode run was killed while
  children were alive; the bridge resumed, verified owned orphans, preserved
  worktrees, repaired once and finished with 13 backend tests plus browser E2E.
- PID + start token + UID/time ownership, atomic project locks, heartbeat and
  fail-closed stale-owner handling.
- Recovery states include `runner_disconnected`, `reconciling`, `orphaned`,
  `retry_ready`, `resuming`, `verifying`, `recovered` and `recovery_failed`.
- `/teamwork` writes untrusted input to a validated UTF-8 request file instead
  of interpolating it into a shell command.
- On a tiny health-endpoint task, serial was faster and cheaper than FAST; no
  speed-saving claim was made.

See [Reliability and security](V741_RELIABILITY.md) and
`docs/assets/v741-recovery-e2e.json`.

## V7.5 — Codex managed execution

- Codex CLI 0.164.0 with ChatGPT login and `gpt-6.1-sol`.
- Real app-server `initialize -> initialized -> model/list -> thread/start ->
  turn/start -> turn/completed`.
- Real adapter and Supervisor/Teamwork reviewer runs passed; token usage and
  tool-item counts recorded.
- Codex managed execution marked `live-verified-local`; native cancel ACK,
  IDE-native subagent status and live resume remain unclaimed.

See [Codex managed execution](V75_CODEX_MANAGED.md).

## V7.6 — Universal Teamwork and Grill-Me

- Portable project identity in `.auto-orchestrator/project.json`.
- Durable SQLite project memory and a Decision/Requirements Ledger.
- Persistent one-question-at-a-time Grill-Me interview across IDE changes.
- Common `universal_teamwork.py` command surface.
- Project-level writer lease; a recommendation is never treated as a
  confirmation; starting from Codex/Cursor/Antigravity never silently launches
  OpenCode without `--provider opencode`.

See [Universal Teamwork](V76_UNIVERSAL_TEAMWORK.md).

## V7.6.1 — Assumption Guard and verified requirements

- Every new development start is preflighted against memory and bounded
  code/config facts.
- Critical ambiguity opens a persistent interview; no plan or runner is written
  before approval.
- Approval publishes a digest-bound, versioned requirements contract.
- Confirmed decisions become acceptance criteria verified against real
  code/test evidence; criteria without executable evidence stay `not_verified`.
- Git post-commit capture is an opt-in managed hook.

See [Assumption Guard](V761_ASSUMPTION_GUARD.md).

## V7.7 — Verified Completion 2.0

- Three evidence levels (implementation, behavioral, conformance) and seven
  criterion states.
- Real test-ID signals, commit/contract binding, independent review and explicit
  `verified_completion`.
- SOLO and risk-aware AUTO modes with BALANCED/RIGOROUS floors for auth,
  payments, retention, database and API-compatibility work.
- Grill-Me 2.0 metadata and a 40-case TR/EN benchmark.
- Native entry points for OpenCode, Codex, Cursor and Antigravity; contract-only
  platforms are labelled as such.

See [Intelligent Teamwork](V77_INTELLIGENT_TEAMWORK.md).

## V7.8 — Sequential IDE handoff

- Ownership state machine: `inactive`, `active`, `handoff_prepared`, `released`,
  `transferred`, `recovery_required`, `blocked`.
- Owner token + fence compare-and-swap, heartbeat TTL, PID identity and explicit
  recovery.
- Native execution is separate from managed execution. `start --native` writes
  an inspectable plan and engine state without spawning a provider or PID.
- Phase 1 approval is bound to the plan digest; a changed or stale approval is
  refused.
- Handoff snapshot v1 carries project/run/milestone/task state, branch/HEAD,
  dirty digest, failed tests, contract/interview state, blockers and next action.
- Fail-closed liveness: a stale heartbeat is `unknown`, not dead.
- Per-IDE truth: OpenCode and Codex ship native skills/commands; Cursor and
  Antigravity remain contract-only and report `stop_ack: false`.
- Nine simulated handoff scenarios and a real Codex -> OpenCode -> Codex loop
  were recorded in `docs/assets/v78-*.json`.

See [Sequential IDE handoff](V78_SEQUENTIAL_IDE_HANDOFF.md).

## V7.9 — Advanced Intelligent Teamwork

- Teamwork 3.0 plan artifact with `scope_contract`, `file_ownership`,
  `ownership_conflicts`, `integrity_policy` and `adaptive_decision`.
- Read-only continuity `brief` for a new IDE session.
- Verified Completion 3.0: run-bound test evidence, JUnit/pytest parsing,
  commit/contract/run/task binding and a merged-tree gate.
- Grill-Me 4.0: editing an answer invalidates only dependent branches, keeps
  history and blocks approval while a branch is unanswered.
- Runtime capability detection for installed CLIs, versions and authentication
  without reading credential stores.
- `v79_benchmark.py` ships a 12-task corpus and refuses to measure without a
  prepared project. Token/cost fields are absent rather than zero.

See [Advanced Intelligent Teamwork](V79_ADVANCED_INTELLIGENT_TEAMWORK.md).

## V8.0 — typed dispatch and verified evidence

- Typed native dispatch records under
  `.auto-orchestrator/native-dispatches/`, with a lifecycle from `queued` to
  `verified` and explicit failure/recovery states.
- `running` requires a parent-supplied native child reference;
  `completed_claimed` is not completion; `collect` records the artifact digest
  and size.
- `native-verify --dispatch-id` advances only after a matching verification
  record is genuinely verified.
- HMAC receipts bind project/run/task/dispatch/commit/contract/plan/artifact and
  are single-use.
- Canonical write scopes reject path aliases and symlink escapes.
- Ownership-aware scheduling: one active dispatch per task, at most three
  active dispatches, verified dependencies and disjoint scopes.
- `brief` reports active/verified/unknown dispatches and blocks resume on an
  unknown lifecycle.
- Grill-Me 5.0 exposes deterministic question-quality signals; a score is never
  a confirmation.
- Dashboard shows native dispatch counts in the ownership panel.

See [Native dispatch](V80_NATIVE_DISPATCH.md) and
[V8.0 overview](V80_NATIVE_INTELLIGENT_TEAMWORK.md).

## Release references

| Release | Reference |
|---|---|
| V7.8 merge | `25f1652` |
| V7.9 merge | `d5605cb` |
| V8.0 feature commit | `2e649f01` |
| V8.0 merge (PR #37) | `b6f01b0` |

Historical evidence assets remain under `docs/assets/`. Their labels and dates
are part of the record; do not reuse a historical benchmark as a current
performance claim.
