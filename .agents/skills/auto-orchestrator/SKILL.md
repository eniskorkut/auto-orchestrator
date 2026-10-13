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
2. After you have actually verified tests and integration, and **before the final answer/Stop hook**, run `python tools/perf.py verified --latest`. This is not an automatic success assumption; do not run it on partial/failed validation.
3. At completion inspect `python tools/perf.py report --latest`. Append one performance sentence **only if** `has_valid_estimate` is true. A lack of a verified estimate is not evidence of savings.
4. Codex hook-derived run IDs are opaque SHA-256 prefixes of session/turn. OpenCode plugin derives opaque run IDs from its session and nonce; don't infer these IDs from task text.
5. If native hooks are not trusted/active, do not claim they captured timing. No prompts, code, raw session or agent IDs go to the telemetry log.

If installation lacks `perf.py` or the agent cannot reliably track timing, omit metrics. You can always report what actually ran.

For full details see `references/delegation.md`, `references/verification.md`, `references/reporting.md`.
