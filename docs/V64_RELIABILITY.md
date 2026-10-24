# V6.4 — Execution reliability, explicit test variants and honest benchmarking

## Design evidence

OpenCode V2 publishes durable `session.execution.started/succeeded/failed/interrupted` events.
The plugin must never infer a successful completion from a spawned child, process exit, `session.idle`, or connection loss.
Reference: https://dev.opencode.ai/v2/docs/build/plugins/ and https://github.com/anomalyco/opencode/blob/dev/specs/v2/session.md

## Native event stream reliability

On unexpected event subscription close/error, the OpenCode plugin emits only a short fixed diagnostic reason code and reconnects with bounded exponential backoff (200–5000 ms). On project session ownership loss or plugin unload it records a nonterminal diagnostic. Existing unfinished runs remain unfinished. Missed terminal events cannot be manufactured without a reliable per-session replay cursor; do not claim that a resubscription retrospectively proves completion.

Use:

```sh
python tools/perf.py --root PROJECT diagnose --latest --json
python tools/perf.py --root PROJECT report --latest --json
```

Possible classifications: `terminal_recorded`, `missing_terminal_open_native_spans`, `missing_terminal_subscription_interrupted`, `missing_terminal_session_ownership_lost`, `missing_terminal_plugin_unloaded`, `missing_terminal_reason_unknown`.
All diagnostic metadata is categorical. No raw prompts, API keys, native IDs or exception strings are persisted.

## Quality Gate: exact explicit command alternatives

To accept a second native shell command spelling, declare it explicitly:

```json
{
  "version": 1, "auto_verify": true,
  "required_checks": [{
    "id": "unit", "tool": "shell",
    "command": "python -m unittest -q",
    "alternates": ["python -m unittest -q ."]
  }]
}
```

Each check accepts at most four distinct additional literal commands. This is NOT fuzzy matching. A changed shell command can change test discovery or mutate files, so automatic normalization would be unsafe. Existing policy files with no `alternates` remain valid.

## Performance methodology

`tools/benchmark.py` accepts explicitly unfinished rows with `status: "incomplete"`, `verified: false`, `elapsed_seconds: null`. Their durations cannot be used as success medians; incomplete/failed runs count toward total attempt rates. A speedup figure requires at least three successful verified repeats **and no failed/incomplete attempts** for either mode in the matched group. An observed tool-span overlap is not a benchmark of time savings.

## Remaining uncertainty

CI validates simulation and static contracts, not actual Mac Desktop event timing. A real OpenCode V2 session must reproduce the previously missing `finish` cases and establish whether the cause is an interruption, a lost subscription, a moved/deleted session, a background child that never stopped, or another native issue.

Native subagent dispatch remains a model decision. Router long-tail performance and independent reviewer identity are separate open work; no such accuracy or proof is claimed by this patch.
