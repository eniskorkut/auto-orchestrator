# V7.4 — Adaptive Teamwork and True Parallel Execution

V7.4 builds on V7.3. It keeps the milestone engine, managed OpenCode adapter,
worktree isolation, repair loop and dashboard. The changes target four measured
problems:

1. Independent managed tasks were started, but the coordinator polled them in a
   way that delayed completion/dependency release.
2. Every task paid for the full V7.3 verification chain.
3. There was no native single-chat entry point.
4. The benchmark called an interval union "overlap" and lost attempt-level
   evidence.

## 1. Event-driven scheduler

`tools/scheduler.py` is a bounded poll scheduler:

- at most `max_parallel` watcher threads;
- each watcher blocks on the adapter's bounded `poll()` and pushes completion
  to a `queue.Queue`;
- the coordinator waits on the queue and immediately fills released slots;
- a task deadline produces a `timed_out` completion record;
- adapter exceptions become failed completion evidence;
- the scheduler never starts or kills a process itself.

`TeamworkRunner` now submits every started handle to this scheduler instead of
looping over active tasks with a sleep. The adapter still owns process
lifecycle and cancellation.

### Real concurrency evidence

Local live OpenCode 2.1.1, model `opencode-go/deepseek-v4.1-flash`:

| Probe | Value |
|---|---|
| Two managed sessions with a controlled 5s wait | completed |
| Wall time | 8.65 s |
| Serial expectation | 10 s |
| `max_concurrent_agents` | 2 |
| `concurrent_overlap_seconds` | 8.41 s |
| `agent_span_union_seconds` | 8.65 s |

Saved in [`v74-concurrency-probe.json`](assets/v74-concurrency-probe.json).
This is a live provider test, not a mock. The 5s waits are intentionally
controlled; the real application run is separately measured below.

## 2. Adaptive modes

`tools/repo_profile.py` produces a bounded structural profile of the repository:
file/module/test counts, languages, backend/frontend directories, manifests,
AGENTS.md presence and suggested test commands. It never reads source contents
or exposes absolute paths.

`tools/teamwork_planner.py` now supports:

| Mode | Plan shape | Verification |
|---|---|---|
| `fast` | one milestone, one/two parallel builds + tester | targeted tests, no critic/challenger/auditor |
| `balanced` | build → verify → risk-based review/audit | tester, reviewer, auditor |
| `rigorous` | discovery → build → verify → critic/challenger/auditor → success audit | full V7.3 chain |
| `auto` | chooses from profile and risk factors | falls back to balanced when facts are missing |

The assessment records its score, confidence, factors and reason. It is a
coordination-budget aid, not a claim of certainty. Explicit `/teamwork
--mode ...` always overrides the assessment.

## 3. Single-chat bridge

OpenCode supports custom commands in `.opencode/commands/teamwork.md`. The
command prompt sends the request to:

```bash
python tools/teamwork_chat.py --root . start --mode auto --request "<request>"
```

The bridge:

- profiles the repository;
- generates and validates the plan;
- initializes the durable Supervisor run;
- starts the managed runner in a separate process;
- returns the run id, mode, risk assessment and log path immediately;
- supports `status`, `agents`, `milestones`, `cancel` and `resume`.

Child OpenCode sessions remain separate managed sessions with their own
worktrees and managed-session records. They are not folded into the main chat.
`cancel` writes a local cancellation flag; the runner reports
`native_cancel_ack: false`.

The bridge was exercised with real temporary projects. The first run proved
start/status/agents/milestones and local cancellation. A second FAST run
(`Add a backend health endpoint and a unit test`) started through the bridge,
failed verification, repaired the backend, and finished **passed**: 62.3 s wall,
$0.023, 4 managed attempts, positive tool counts. Evidence:
[`v74-chat-bridge-e2e.json`](assets/v74-chat-bridge-e2e.json). The command file is
installed by the project/global OpenCode installer.

## 4. Benchmark correctness

The old `overlap_seconds()` computed the union of intervals, which counts any
instant with at least one agent. V7.4 schema 2 separates:

| Metric | Definition |
|---|---|
| `agent_span_union_seconds` | union of all agent-active instants |
| `concurrent_overlap_seconds` | seconds with at least two agents active |
| `max_concurrent_agents` | peak observed concurrency |
| `average_concurrency` | agent-seconds / union span |
| `critical_path_seconds` | longest declared task DAG path with measured durations |
| `queue_wait_seconds`, `dispatch_latency_ms` | coordinator latency |
| `scheduler_idle_seconds` | optional measured coordinator idle |
| `verification_overhead_seconds`, `repair_overhead_seconds` | optional measured slices |
| cost/token fields | `None`/unmeasured, never silently zero |

Historical note: the V7.3 success evidence was produced before the final
tool-count aggregation fix and shows `tool_count: 0` for some sessions. That
historical file is not rewritten. V7.4 FAST and chat-bridge runs record positive
tool counts (e.g. 14, 41, 5, 29 and 10, 5, 20, 9). Missing measurements remain
`None`; a zero is only shown when the adapter explicitly observed zero.

`tools/teamwork_benchmark.py` now computes true overlap with a sweep line and
adds `critical_path_seconds()` with cycle detection. Old schema-1 JSON files are
left as historical artifacts; they are not silently reinterpreted.

### Measured V7.4 FAST run

A real FAST run on the disposable task-manager sample:

| Metric | Value |
|---|---|
| Status | passed |
| Milestones | 1 |
| Managed sessions | 4 attempts |
| Backend tests | 33 passed |
| Browser E2E | passed |
| Wall span | 247.8 s |
| Cost | $0.0528 |
| True overlap | 23.6 s |
| Peak concurrency | 2 |
| Critical path | 247.7 s |
| Repairs | 1 (test-command scope issue) |

Artifacts: [`v74-fast-e2e.json`](assets/v74-fast-e2e.json), sample code in
[`examples/v74-fast-teamwork-sample`](../examples/v74-fast-teamwork-sample).

### Comparison (single live sample, local macOS)

| Run | Wall | Cost | Tests | True overlap | Peak |
|---|---:|---:|---:|---:|---:|
| Serial single agent | 180.8 s | $0.0222 | 21 | 0 | 1 |
| V7.3 balanced | 1566.1 s | $0.1404 | 60 | 77.3 s | 2 |
| V7.4 FAST | 247.8 s | $0.0528 | 33 | 128.6 s* | 2 |

Attempt-level overhead in the new schema-2 file:

| Run | Verification overhead | Repair overhead |
|---|---:|---:|
| V7.3 balanced | 982.0 s | 414.3 s |
| V7.4 FAST | 65.8 s | 23.0 s |

Verification/repair overhead is the sum of managed attempt durations in those
roles, not a wall-time decomposition; attempts can overlap. Serial overhead was
not separately observable in one session and is left `null`.

\* V7.4 true overlap is computed from all managed attempts (including the
repair rerun); the primary FAST implementation overlap was 23.6 s between
backend and frontend. The repair increased the total overlap share.

V7.4 FAST reduced V7.3 wall time by **84%** and cost by **62%** on this sample,
but it intentionally omitted critic, challenger, auditor and success-auditor
sessions. It is **not quality-equivalent** to V7.3 balanced. The serial run was
faster than V7.4 FAST, so no time-saving claim is made against serial.

Full data: [`v74-benchmark.json`](assets/v74-benchmark.json). This is one live
sample per condition and is not a statistical benchmark; provider cache and
model latency were not normalized.

## 5. Recovery and cancellation

- `TeamworkRunner(recover_orphans=True)` reconciles `running` tasks left by a
  previous runner: it records an orphan reason, marks the managed session
  failed/orphaned, and lets the repair loop start a new attempt with the prior
  session id when available.
- No native cancellation acknowledgement is fabricated. The orphan record says
  the process was not reattached.
- `cancel` is a local signal/file path; the UI and CLI display
  `local_signal_only: true`.
- Unit tests cover an orphaned running task, duplicate prevention, cancellation,
  and restart state reload.
- A real restart smoke killed the runner while `build-backend` was running.
  `resume --recover` marked the attempt orphaned, started a new managed attempt
  and did not duplicate the completed build task. The quick generic request then
  failed product verification (the generated backend lacked the requested
  endpoint), so the smoke proves reconciliation/duplicate handling but **not** a
  fully successful product continuation. This is recorded as a gap, not a pass.

## 6. Virtual Office and dashboard

- Task Board shows adaptive mode (`FAST`/`BALANCED`/`RIGOROUS`), active/queued
  sessions, repair state, measured cost, milestone roles and a bounded task
  queue/timeline.
- Waiting tasks show their unresolved dependencies; no fake progress is shown.
- Office header shows the mode and current milestone.
- Managed session inspector shows model, tool count, cost, timestamps and a
  session hash; native stop remains explicitly unclaimed.
- Screenshots: `docs/assets/v74-teamwork-board.png`,
  `docs/assets/v74-managed-office.png`; real-session V7.3 screenshots remain in
  `docs/assets/v73-managed-office.png` and `v73-teamwork-board.png`.

## 7. Limits

- The OpenCode command is a real custom command file, but this environment did
  not automate a click-through of the desktop TUI. The bridge and command
  contract were exercised directly.
- Codex managed execution remains contract-tested only; Antigravity and Cursor
  managed execution remain not implemented.
- The live comparison is one sample per condition, not three repetitions.
- FAST/BALANCED/RIGOROUS have different verification depth; wall-time ratios
  across modes are not quality-adjusted claims.
- A real process that survives a runner crash is reported as an orphan; it is
  not silently killed or claimed as cancelled.
