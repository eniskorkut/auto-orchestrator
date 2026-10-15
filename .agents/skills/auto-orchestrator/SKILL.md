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
- **Antigravity 2.0/CLI:** use `invoke_subagent` and `.agents/agents/*`. Profiles use `model: inherit`. Prefer isolated `branch` workspace for parallel writers. IDE may not expose the same native subagent function.

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
