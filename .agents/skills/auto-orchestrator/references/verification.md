# Verification and failure handling

- Run targeted tests, linters or type checks relevant to changed code.
- Request reviewer with read-only access when risk or scope warrants it.
- Validate behavior, not merely that subprocess returned exit code 0.
- Re-check shared API/schema contracts across independently edited components.
- On failed job: classify missing input, execution/tool error, incorrect patch, test failure, or integration conflict. Retry at most twice only with new signal.
- Do not silently fall back to a different model when user chose a specific subagent model; report a model availability problem.
- If native subagents cannot run, proceed serially and disclose the limitation. Do not claim parallel time savings.
