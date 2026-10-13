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
- OpenCode classic: plugin tracks native `task` tool spans; `session.idle` finalizes the current run. OpenCode V2 plugin support is still pending.
- Antigravity: `SessionStart/PreInvocation` and `Stop` capture a session scope. No native subagent-finish timing is inferred from `PostToolUse`.
- For every platform, completion is not verification. Run `python tools/perf.py verified --latest` only after real tests pass, before the final response. If the hook fires earlier or timing is incomplete, omit the benefit.

`python tools/perf.py report --latest` shows the last run. A missing/negative benefit does not justify a promotional footer.
