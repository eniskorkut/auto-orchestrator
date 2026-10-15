# Reporting policy

All reported timings come from the monotonic elapsed duration implied by actual event timestamps. Tool event data may be incomplete. Never infer durations from token counts or guess.

For a **verified successful** run with >=2 fully matched distinct subagents, compute:

- `elapsed`: time between run start and finish
- `agent_sum`: sum of per-agent active wall times
- `estimated_parallelism_benefit`: max(0, agent_sum - elapsed)

This is an approximation of overlapped work, not an experimentally proven user time saving. If <=0, omit the claim. If evidence is incomplete, omit the claim. A comparative serial benchmark would be needed for measured savings.

**Result line** (only if report CLI emits a valid result):

`Estimated parallelism benefit: X min (3 subagents; Y min elapsed; not benchmarked).`

No extra footer for trivial jobs, failures, or uncertain timestamps. Never include sensitive task content in metrics files.

## Lifecycle capture in V2

- Codex: `UserPromptSubmit` creates a per-turn run, `SubagentStart/Stop` track actual runtime, `Stop` finalizes it. Hook trust is required via `/hooks`.
- OpenCode classic: plugin tracks native `task` tool spans; `session.idle` finalizes the current run. OpenCode V2 captures native subagent and execution lifecycle; V5 adds an optional verified evidence gate.
- Antigravity: `SessionStart/PreInvocation` and `Stop` capture a session scope. No native subagent-finish timing is inferred from `PostToolUse`.
- For every platform, completion is not verification. Run `python tools/perf.py verified --latest` only after real tests pass, before the final response. If the hook fires earlier or timing is incomplete, omit the benefit.

`python tools/perf.py report --latest` shows the last run. A missing/negative benefit does not justify a promotional footer.


## V5 native evidence and late verification

OpenCode V2 listens to the native subagent tool and session.execution.succeeded/failed/interrupted (not session.idle). A successful execution does not establish test success. An optional project-owned .auto-orchestrator/verification.json policy explicitly opts in to test verification and names exact native shell commands; see examples/v5-verification.json.

The plugin NEVER executes the commands itself. It observes already executed commands with reliable integer exit codes. Every required check must succeed **after** the last observed edit or native subagent completion. Missing exit codes, unmatched agent spans, unsuccessful execution, stale checks, changed verification policy and foreign-project events keep the run unverified.

V5 logs may now contain start, agent spans, check/mutation, finish, then verified. An evidence-backed verification can be added after finish, but only when the native evidence quality gate passes. V4 logs remain readable. Verified means configured native checks passed, not that complete task correctness is independently proven. Matched serial/orchestrated benchmarks are still required to report time savings.
