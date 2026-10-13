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
