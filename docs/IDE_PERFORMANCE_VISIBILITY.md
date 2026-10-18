# OpenCode V2 — IDE visibility and honest time metrics

## What appears inside the IDE?

On verified OpenCode V2 runs, the native plugin calls `ctx.tui.showToast` (if the current IDE exposes this SDK method). A brief notification confirms that the V5 Quality Gate validated the run. When at least two completed native **tool-call spans** overlap, the notification also displays their **observed scheduling overlap in seconds**, explicitly labeled **not actual time saved**.

A toast is best-effort and ephemeral; a supported client may dismiss or not expose it. Headless OpenCode and some Desktop clients may not expose `ctx.tui`. A failed toast does not fail the task, emit a fabricated verification, or publish telemetry elsewhere. The local JSONL remains the source of the record. Nothing is uploaded.

Persistent report in any IDE terminal:

```powershell
$root = (Get-Location).Path
$perf = Join-Path $env:USERPROFILE ".config\opencode\auto-orchestrator\tools\perf.py"
py -3 $perf --root $root report --latest --json
```

Or from the installed project-local tooling: `python tools/perf.py --root . report --latest --json`.

The report distinguishes:
- `observed_overlap_seconds`: overlap computed from union of recorded native tool spans when a run is complete and verified. An observed interval can represent **background spawn/tool-return time only**, not the child's lifetime. It is **not actual time saved**.
- `estimated_parallelism_seconds`: conservative prior V4/V5 measure, `max(0,sum(native tool spans) - parent run elapsed)`. It is suppressed unless evidence supports the estimate. This is still not a serial-vs-parallel benchmark.
- `elapsed_seconds`: measured parent-run wall duration.
- `verified`: native-test evidence accepted by the explicit project Quality Gate policy. Not a claim of defect-free code.

## How to measure actual savings?

Real saved wall time requires matched comparable single-agent and orchestrated runs for equivalent tasks, models, quality gates, hardware, prompts, cost and variance. An overlap notification alone **does not establish** a speedup. The real-work benchmark is intentionally postponed.

## Safety and scope

- Notifications appear only on successfully persisted `finish` and `verified` evidence, and only when `ctx.tui.showToast` exists.
- Failure, cancellation, missing policy, stale tests, untrusted exit, incomplete spans, missing permissions: no success notification.
- No task text, raw native agent/session IDs, source code, tokens or prompts are included in toast or JSONL.
- No global user preferences, IDE display settings or other plugins are overwritten by the visibility feature.
- Cursor, Codex and Antigravity are currently served by terminal-readable reports, not proven inline IDE notifications.

A real OpenCode Desktop smoke test must confirm `ctx.tui.showToast` exists in the installed release. Simulated hook tests and SDK documentation do not prove toast rendering in that Desktop build.
