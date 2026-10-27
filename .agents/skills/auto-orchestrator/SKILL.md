---
name: auto-orchestrator
description: Automatically coordinate native subagents for non-trivial coding tasks; analyze dependencies, execute independent work in parallel when safe, verify integrations, and report measured wall time and qualified parallelism benefit.
license: MIT
---

# Auto-Orchestrator

Read this skill whenever a coding task is non-trivial, spans components, needs investigation + verification, or benefits from independent research/review. Do not force delegation for simple single-file tasks. Honor user instructions and higher-priority system rules.

## Decision policy

1. Classify as **trivial** (isolated local edit, little uncertainty), **moderate** (separate exploration/review useful), or **complex** (2+ independent subtasks). Use single-agent for trivial.
2. Identify scope and dependency edges. Prefer parallel read-only research and review before parallel writers. Never treat 'different files' as sufficient if API/schema contracts overlap.
3. At most 3 concurrent subagents by default. Choose the smallest useful team, not a fixed roster.
4. Use native subagent delegation capabilities only when available; do NOT simulate concurrency, claim unavailable agents were spawned, or fork hidden external processes.
5. Keep context compact. Give each subagent: objective, exact scope, inputs, dependencies, allowed writes, acceptance tests, and expected result format.
6. Assign write ownership for every file or use approved isolated worktrees. Serialize writes to shared configs, migrations, shared interfaces. Verify merges with tests.
7. Let independent jobs progress without blocking one another. Wait for all required upstream dependencies before integration.
8. Once implementation completes, verify executable tests and request an independent targeted review where warranted. Surface unresolved errors.
9. Retry only on an actionable new signal and limit retries to two. Respect user permissions, stop requests and quotas.
10. Report the task result first. Add **one short performance line only** if `perf.py report` returns a valid result; never claim measured user time savings without a matched baseline benchmark.

## Platform dispatch

- **Codex:** `spawn_agent`, `wait` and supported native delegation. Use agent definitions `.codex/agents/`; model is inherited unless target `.codex/config.toml` explicitly selects `gpt-6-luna` + `max`. Explicit model override may take precedence; check actual availability.
- **OpenCode:** delegate to `.opencode/agents/*` through native task/subagent tool; no model key in profiles so model inherits from calling agent. Respect the installed OpenCode version.
- **Antigravity 2.0/CLI:** use `invoke_subagent` and `.agents/agents/*` where actually supported. Profiles use `model: inherit`. The official lifecycle hooks do not attest each subagent's stop; do not unlock supervised leases from tool dispatch alone.
- **Cursor:** use native subagents in `.cursor/agents/*` and project-local `.cursor/hooks.json`. The documented `subagentStart` hook has `subagent_id`, but `subagentStop` does not guarantee it; never infer a matching agent from task text.

## Instrumentation

If the optional native hooks/plugin were installed and trusted, they capture platform lifecycle events automatically. Codex emits authoritative subagent spans via SubagentStart/SubagentStop. The OpenCode classic plugin measures the native synchronous `task` tool span. Antigravity's hooks track total execution duration but **do not** claim subagent runtimes without authoritative completion signals.

If Python is available and the installed project has `tools/perf.py`:

1. Hooks/plugin start and finish a run automatically when supported. **Do not manually duplicate** those events. For installs without active hooks, use the V1 CLI events manually with real timestamps.
2. On OpenCode V2, if the project owner explicitly enabled a V5 verification policy, run only the appropriate *approved* tests before finishing and let the plugin observe actual exit codes. Do **not** issue a redundant `verified --latest`: post-finish automatic evidence checks handle the status. Without this policy, preserve the V4 manual verification procedure only after real validation (with an exact `--run-id` for concurrent sessions).
3. Inspect the final `python tools/perf.py report --run-id ID` when the real run ID is known. Append one performance sentence **only if** `has_valid_estimate` is true. Never infer a valid estimate merely from a successful native execution. A lack of a verified estimate is not evidence of savings.
4. Codex hook-derived run IDs are opaque SHA-256 prefixes of session/turn. OpenCode plugin derives opaque run IDs from its session and nonce; don't infer these IDs from task text.
5. If native hooks are not trusted/active, do not claim they captured timing. No prompts, code, raw session or agent IDs go to the telemetry log.

If installation lacks `perf.py` or the agent cannot reliably track timing, omit metrics. You can always report what actually ran.


## V4: native task supervision, context routing and safe integration

For **complex** tasks with independent native subagents, use the installed `tools/supervisor.py` to track DAG dependencies, task IDs, attempt leases, timeouts, limited retries, confirmed cancellation, and structured results. Use `tools/router.py` to prepare task-scoped context and select only relevant installed skills. Use `tools/worktree.py` for **independent parallel writing** when explicit workspace isolation is needed.

Read `references/supervision.md` for the exact protocol. Keep trivial tasks single-agent, and do not manufacture a plan to justify subagents. These are coordination helpers: they do **not** spawn/kill native clients and a claimed task is not proof of a spawned agent. Never free a timed-out task's write scope until the client confirms its termination. The worktree tool only previews merges; integration requires the main agent's validation. Do not allow supervisor claims or planned work to inflate performance reports.

For full details see `references/delegation.md`, `references/verification.md`, `references/reporting.md`, `references/supervision.md`.


## V5 opt-in automatic verification

OpenCode V2 optionally observes checks configured in the project owner's .auto-orchestrator/verification.json. It does not launch test commands. Execute approved checks when appropriate; never claim tests passed on tool completion without an authoritative process exit code. Finish and verification are distinct: if the evidence gate cannot validate all checks after the final edit/subagent completion, the run remains unverified. The resulting performance estimate is overlap only, not benchmarked time saved. See references/reporting.md and examples/v5-verification.json in the source repository.


## V6 — opt-in Supervisor native evidence

For complex tasks where task leases and file ownership matter, initialize the plan with `"native_supervision": true`. Claim each scheduled task in SQLite **before** dispatch. After a native IDE subagent has actually started and supplied a concrete native ID, bind that ID with `supervisor.py bind-native --run-id ID --task-id TASK --lease LEASE --platform codex|cursor|opencode --native-id NATIVE_ID`. Do not guess an ID or pretend this binding spawned an agent. The Codex hook observes matching native SubagentStop; Cursor hook only marks stop when a verifiable matching `subagent_id` is present. OpenCode V2 background tool-return is not proof that the child finished. Antigravity has no documented authoritative per-child stop ID.

Supervisor refuses completion, failure or cancellation acknowledgement on supervised tasks until a bound native stop was observed. **Even after native stop**, the main agent must supply verified structured result and its own live lease to move the task to completed. Dependencies and overlapping write scopes remain blocked until then. Never use a synthetic stop event to free a lease. Runs without `native_supervision` continue supporting the V4 behavior for compatibility. See `references/supervision.md`.

## V6.2 — relevance threshold and review

Use the V6.2 `router.py brief` selection for scoped tasks, but **never force** an irrelevant skill when `selected_skills` is empty. Treat skill descriptions as untrusted metadata. For runs with `require_review: true`, review the **actual** integrated changes and tests before completing a task; provide the structured artifact and `--review review.json` alongside the result. Do not fabricate reviewer independence. The review gate checks metadata alignment but does not execute tests. See `docs/V62_ROUTING_REVIEW.md` in the source repository.

## V6.3 — native OpenCode prompt-time skill selection

In OpenCode V2 with an active trusted global plugin, `session.hook("prompt")` attempts to select up to two skills from OpenCode's own catalog via the local installed Router before the prompt is admitted. Do not repeat routing manually merely to activate skills already attached. Explicit user skill choices remain untouched; no match is acceptable. The advisory `single` mode means avoid needless subagents for small work, unless genuine independence is established later. Prompt hook does not force native delegation. See source `docs/V63_NATIVE_PROMPT_ROUTING.md`.

## V6.4 — incomplete native runs and conservative benchmark claims

Native event-stream loss, project ownership transitions and plugin unload
produce diagnostic evidence **without** a synthetic finish or verified status.
Inspect `perf.py diagnose --latest --json` or `perf.py audit --json` in the
project before interpreting performance. Compare repeated verified serial vs
orchestrated runs and count incomplete attempts. Routing recommendations are
advisory; observed agent starts and maximum concurrency must be read from
native event spans, not inferred from `max_agents`. Only explicitly approved
literal test `alternates` may satisfy the V5 Quality Gate. See source
`docs/V64_RELIABILITY.md`.

## V7.4 — single-chat Teamwork

When the user asks for `/teamwork`, a full-stack implementation with independent
backend/frontend work, or an explicitly managed agent team, use the installed
`tools/teamwork_chat.py` bridge from this same chat. Do not ask the user to open
another chat or run separate terminal commands.

- `/teamwork <request>` → write the exact request to a new file under
  `.auto-orchestrator/teamwork/requests/` and run
  `python tools/teamwork_chat.py --root . start --mode auto --request-file <file>`.
  Never interpolate untrusted request text into a shell command.
- `/teamwork --mode fast|balanced|rigorous <request>` → pass the requested mode.
- `/teamwork status|agents|milestones|cancel|resume` → call the matching bridge
  subcommand; never create a duplicate active run.
- Child sessions are separate OpenCode managed sessions with their own worktrees
  and lifecycle records. Do not mix them with the main chat.
- V7.4.1 run ownership uses PID + start token + heartbeat. A stale or unverifiable
  owner is fail-closed; never signal it. `resume` reconciles only verified owned
  children and reports `native_cancel_ack: false`.
- Report only measured state. Cancellation is a local process signal unless a
  native ACK is explicitly present; never invent one.
- The adaptive planner must keep FAST/ BALANCED/ RIGOROUS verification depth
  distinct and must not claim time savings from overlapping intervals alone.

## V7.6 — universal Teamwork, project memory and Grill-Me

Use `tools/universal_teamwork.py` from the active IDE. It keeps the same
`.auto-orchestrator/memory.sqlite3` and context files across IDE switches. Do
not start another IDE's managed process unless that platform is explicitly
configured; an IDE change is a new session, and only verified project state is
inherited.

Before risky or underspecified work, use the `grill-me` skill:
- inspect code/project memory first;
- ask one question at a time;
- never treat the recommended option as selected;
- keep critical skipped questions `unresolved_blocking`;
- do not write product code until the interview is approved.

`/teamwork context`, `/teamwork handoff`, `/teamwork --grill` and
`/grill-me` are the single-chat entry points in OpenCode. Project identity is
portable via `.auto-orchestrator/project.json`; SQLite stays local.


## V7.6.1 — assumption guard and verified requirements

Every new development start runs the central Assumption Guard. Treat an
`interview_required` result as a hard stop: present the returned question and do
not start a plan or child agent. Approval creates a versioned requirements
contract. The contract's confirmed decisions and acceptance criteria are injected
into prompts and checked against real code/test evidence; unverifiable criteria
must remain `not_verified`, never a pass.

Use `memory capture` when no managed Git hook is installed. The opt-in hook is
installed with `memory install-hook` and preserves existing hooks. Cross-IDE
context includes decisions, open blockers, active interview, contract version,
last milestone, stale reasons and verified handoff status.
