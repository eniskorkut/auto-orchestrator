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

If Python is available, and you can access the installed `tools/perf.py` in the project root:

1. For **delegated** tasks call `python tools/perf.py start --run-id <opaque-unique-run-id> --platform <codex|opencode|antigravity>` at the task start.
2. Call `agent-start` just before native spawn and `agent-stop` when the corresponding job completes; use unique opaque agent IDs (not prompts or names containing personal data). If event timings are not observable, do not fabricate them.
3. After integration/testing, call `finish --status success --verified` only if verification truly passed. Otherwise finish with `--status failed` (no `--verified`).
4. Call `report` and append a **single sentence** only when it provides a positive estimate. When missing evidence, omit the performance sentence. No event storage outside `.auto-orchestrator/`.

If installation lacks `perf.py` or the agent cannot reliably track timing, omit metrics. You can always report what actually ran.

For full details see `references/delegation.md`, `references/verification.md`, `references/reporting.md`.
