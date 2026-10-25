# Evidence dashboard — read-only local console

> V7.2 adds a continuous Canvas 2D Virtual Office, room filters and a layout editor on top of this console; V7.1 added the project registry and Global Overview. See [MULTIPROJECT.md](MULTIPROJECT.md) and [VIRTUAL_OFFICE.md](VIRTUAL_OFFICE.md). The single-project behavior below remains available and backward compatible.

The Auto-Orchestrator dashboard is a local, read-only web view over the evidence your native IDE sessions already leave on disk. It joins run JSONL, Supervisor SQLite state and managed-worktree manifests into one inspection surface, while keeping claims, observations and missing evidence visibly separate.

It is deliberately **not** a control plane. The dashboard never starts or stops an agent, claims or completes a task, merges a worktree, or replaces `perf.py`, the Supervisor or `benchmark.py`. It only reads and displays local records.

![Evidence console demo: verified run, native agent timeline, overlap bands and evidence chain](assets/dashboard-demo.png)

## Quick start

Requires **Python 3.11+**, matching the rest of the project tooling. From the project root:

```bash
python tools/dashboard.py --root . --open
```

By default, open <http://127.0.0.1:8765/> if the browser does not open automatically. If you passed another port, use the URL printed at startup. The server also prints its root and mode. On Windows, use `py -3` if `python` is not on `PATH`.

| Option | Default | Meaning |
|---|---|---|
| `--root PATH` | current directory | Project whose evidence should be read |
| `--port N` | `8765` | Loopback port; `0` asks the OS for a free port |
| `--demo` | off | Serve clearly labelled synthetic evidence |
| `--open` | off | Open the dashboard in the default browser |

Try the interface without any recorded evidence:

```bash
python tools/dashboard.py --demo --open
```

Demo mode is labelled **DEMO DATA** in the header. It reads no project evidence, writes nothing to disk, and is not a benchmark.

### Installed copies

Project-local installs ship `tools/dashboard.py` and `tools/dashboard_assets/`; use the command above from the target project. Global installs place the same pair under the platform's `auto-orchestrator/tools/` directory, for example:

- Codex: `~/.codex/auto-orchestrator/tools/dashboard.py`
- OpenCode: `~/.config/opencode/auto-orchestrator/tools/dashboard.py`
- Antigravity: `~/.gemini/config/auto-orchestrator/tools/dashboard.py`
- Cursor: `~/.cursor/auto-orchestrator/tools/dashboard.py`

Run the installed copy with an explicit project root, for example:

```bash
python ~/.codex/auto-orchestrator/tools/dashboard.py --root /path/to/project --open
```

The `dashboard_assets/` directory must stay next to `dashboard.py`. If an older installation does not contain the dashboard, reinstall with the current installer. Installer regression tests (`tests/test_dashboard_install.py`) cover these placements; the dashboard itself remains a read-only runtime.

## What the dashboard reads

The console uses three local evidence sources, all optional:

| Source | Location | Used for |
|---|---|---|
| Run logs | `.auto-orchestrator/runs/*.jsonl` | Lifecycle events, native agent spans, checks, mutations, routing and verification records |
| Supervisor state | `.auto-orchestrator/supervisor.sqlite3` | Durable task state, dependencies, scopes, attempts, native-stop and review attestations |
| Worktree manifests | `.git/auto-orchestrator/worktrees/*.json` | Managed worktree branch, base commit and path |

For gate verification, it also reads the current opt-in `.auto-orchestrator/verification.json` policy. That policy is not an evidence source; it is the rule used to re-check a recorded claim rather than trust it as written.

Run IDs must match `[A-Za-z0-9][A-Za-z0-9._-]{0,79}`. Symlinked evidence files are rejected or skipped, not followed. A missing source is shown as missing; the console does not reconstruct events from prompts, source code or guesses. Demo mode bypasses these reads entirely.

## Screens

### Runs

The default tab has a fleet overview and a searchable run list.

**Overview KPIs:** runs recorded, gate-verified count and rate, self-reported claims, open and recently active runs, recorded agent spans, mutations, and the largest observed overlap in a single run. The verified card's tooltip states that verification is a re-check against the current policy; the overlap card is explicitly labelled **not time saved**.

**Run list:** filter by all, verified, open or failed, and select a run to inspect:

- **Run header** — platform, elapsed time, agent spans, peak concurrency, failed checks and terminal status.
- **Native agent timeline** — one lane per opaque agent ID. Completed spans are solid; spans with no observed stop are dashed. Shaded bands mark intervals where two or more native tool spans overlap. Mutation dots and check markers are overlaid, and the terminal event gets a marker.
- **Evidence chain** — run start, routing advice, agent spans, mutations, checks, terminal record and verification result, each shown as a separate fact with a timestamp.
- **Agent spans table** — start offset, duration, stopped/open state and the short native hash when one was recorded. Roles are not persisted in run telemetry.
- **Native checks table** — check ID, exit code, observation time and source. Only exact configured checks with native integer exit evidence can satisfy the gate.
- **Lifecycle diagnostics** — categorical reasons such as an interrupted event stream, shown without inventing a terminal event.
- **Reproduce this readout** — copyable `perf.py report` and `perf.py diagnose` commands for the same run.
- **Raw evidence** — the display event log with timestamps and recorded fields.

A torn final JSONL line is labelled **partial write** and displayed without inventing the missing event; if no finish was recorded, the run remains open. Malformed earlier lines, oversized logs or too many events produce an **Invalid log** state instead of a partial reconstruction.

### Task board

The board reads durable Supervisor runs from SQLite and derives five columns:

| Column | Derived from |
|---|---|
| **Ready** | Pending task whose dependencies are all completed |
| **Running** | `running` or `cancel_requested` |
| **Blocked** | Pending task with at least one unmet dependency |
| **Done** | `completed` |
| **Attention** | `failed`, `cancelled` or `timed_out` |

Cards show role, objective, dependencies, attempt count, write/read scopes and structured result details when present. Evidence chips distinguish an observed native stop from an unobserved one, and show whether a required review attestation exists. The board never receives attempt leases, and raw native IDs are not serialized to the page.

A recorded task state is not by itself proof that an agent ran or that tests passed. When `native_supervision` is enabled, the Supervisor requires a native stop before a supervised completion can be accepted, but the main agent still supplies the reviewed result. A review attestation is a recorded artifact bound to the result digest; it is not independent proof that a separate agent performed the review.

### Managed worktrees

This tab lists only worktrees with Auto-Orchestrator manifests, not every Git worktree. Each card shows the task, branch, short base commit and path. When the directory exists, a read-only `git status --porcelain` supplies the changed-file count; a missing path is flagged for manual reconciliation.

**Clean is a worktree status, not a tested merge.** The console never merges, cleans or deletes a branch. Integration still requires the main agent's review and tests.

### Method

The Method tab is an in-app summary of the same trust model: the three local sources, verification distinctions, overlap arithmetic, advisory routing, missing cost data and the read-only security boundary.

## Status and verification semantics

The console separates facts that are often collapsed into a single green check.

| Display | Meaning |
|---|---|
| **Gate verified** | A quality-gate event was re-checked against the current verification policy and its check IDs matched. This is evidence about the configured checks, not proof of full correctness. |
| **Self-reported** | A verification claim lacks gate attestation—for example, `finish.verified` is set without a verified event, or the verification event's source is not `quality-gate`. It is displayed as a claim and never counted in the verified total. |
| **Open / no terminal** | No finish event was recorded. Open agent spans are dashed and are not assumed to be running. |
| **Failed / cancelled** | The terminal status is failed or cancelled. No verification is inferred from a non-successful terminal state. |
| **Finished, not verified** | A successful terminal record exists without a verification event. The completion is recorded; verification is not inferred. |
| **Rejected** | A success was recorded and a quality-gate verification event exists, but re-checking rejects it—for example because required checks are missing or failed, checks are stale, verification is duplicated, or the policy hash differs. The completion remains recorded; the claim does not become evidence. |
| **Invalid log** | The run file is unreadable, malformed or beyond the size/event limits. The dashboard reports the problem rather than guessing. |

### Gate verification re-check

For a gate-verified result, the dashboard requires all of the following:

1. The run has a successful `finish`.
2. Exactly one `verified` event exists and its source is `quality-gate`.
3. The same `quality_gate.assess` check used by the CLI accepts the event stream against the **current** `.auto-orchestrator/verification.json` policy.
4. The recorded and recomputed check ID sets match.
5. Required checks have native integer exit code `0` after the last observed mutation or agent stop, and the check events carry the current policy hash.

Editing, replacing or removing the verification policy can therefore turn a previously accepted run into a rejected one. That is intentional: the console reports what the current policy supports, not what an old claim asserted. Restore the matching policy or rerun the checks; never hand-edit evidence to force a green state.

### Open and live

`Open` means no terminal event. `Live` adds a freshness test: the last event is within the display window (180 seconds in the current implementation). **Event-stream freshness is not a process heartbeat.** A recently written log may belong to a stalled process, and an open span may belong to a process that already exited without a stop event. The dashboard says "open", not "running".

### What verification does not cover

Gate verification covers the configured checks only. It does not prove:

- that the checks were comprehensive or that the task is defect-free;
- that every background child modification was observed;
- that a human or separate reviewer examined the diff;
- that an untracked file, external service or process outside the native hook path was unaffected;
- that matching serial and orchestrated runs would show a speedup.

See [V6 native supervision](V6_NATIVE_SUPERVISION.md), the [Native Supervisor platform matrix](NATIVE_SUPERVISOR_PLATFORM_MATRIX.md) and [V6.2 routing and review](V62_ROUTING_REVIEW.md) for the underlying evidence contracts.

## Overlap, estimates, routing and cost

### Observed overlap

The timeline's shaded bands are the **union of observed native tool-span overlaps**. Two spans that overlap increase observed overlap; they do not show how long a serial run would have taken. An observed interval can represent the background spawn call only, rather than the child's full lifetime.

### Estimated overlap

The CLI arithmetic shown by the console is:

```text
estimated overlap = Σ agent wall time − run wall time
```

A positive value is a scheduling indicator, qualified by the CLI as **not benchmarked human time saved**. The console may display a "benchmark-valid overlap estimate" when the CLI's qualification checks pass (successful, verified, at least two completed spans, no evidence issue, positive arithmetic). That label means the estimate is admissible under those rules; it does not mean a controlled benchmark was performed. **Only matched serial/orchestrated repeats establish a speedup.** The protocol and templates are in [BENCHMARK.md](../BENCHMARK.md).

### Routing advice

Router advice is either `single` or `consider-parallel`; parallel advice caps at two or three agents. The console compares that advice with the native starts and peak concurrency actually observed. "Followed" is a routing observation. It is not proof of independent work, correctness or efficiency.

### Cost

The current evidence schema contains no token or price field. The dashboard therefore shows **no cost figures** and does not estimate them. Adding cost would require an explicit, reviewable input per platform and model; subscription and usage-based plans must not be mixed silently.

## Teamwork milestones and managed sessions

V7.3 adds:

- `GET /api/projects/<project_id>/milestones` — project-scoped milestone runs,
  verified progress, role verdicts, handoffs and managed-session summaries.
- `GET /api/projects/<project_id>/milestones/<run_id>` — one milestone run.

The API exposes a short session hash and task/provider/model/cost metadata, not
raw managed session ids or event paths. Progress counts only passed milestones;
`estimated_progress` is always `null`. See [V73_TEAMWORK.md](V73_TEAMWORK.md).

## Privacy and security model

The dashboard is local by construction:

- **Loopback only.** It binds to `127.0.0.1`; there is no host option. It is not reachable from another machine by design. Requests with an unexpected `Host`/`Origin`/`Sec-Fetch-Site` are rejected to reduce DNS-rebinding and cross-site browser reads.
- **Read-only HTTP surface.** Evidence routes are `GET`; `POST` returns `405`; `HEAD` is limited to static assets. There is no write endpoint.
- **Run ID validation.** Run IDs are checked against a strict pattern. Encoded traversal and invalid IDs return `404` rather than touching arbitrary paths.
- **Symlink resistance.** Symlinked run logs, assets, policy files, the Supervisor database and worktree manifests are rejected or skipped. The SQLite database is opened with a read-only URI.
- **Server-side redaction.** Supervisor attempt leases are stripped before serialization. Raw native IDs are never sent to the browser; the UI uses opaque IDs and short hashes only.
- **Same-origin content.** The page has no external fonts, scripts, analytics or network calls. A restrictive Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff` and `Referrer-Policy: no-referrer` are set on responses. Assets and API data support gzip; API responses use `Cache-Control: no-store`.
- **No telemetry.** Nothing is uploaded by the dashboard.

**No authentication is provided.** Loopback is not a security boundary against other processes or users on the same machine. Do not expose the port through a reverse proxy, port forward or public tunnel. The tool is intended for one user inspecting their own local evidence.

## Performance notes

No performance benchmark is claimed here; actual latency depends on run count, log size and disk speed. The implementation avoids avoidable repeated work:

- **Conditional polling.** The browser polls `/api/state` every 2.5 seconds with `If-None-Match`; unchanged state returns `304 Not Modified` with no body. The ETag covers run files, the verification policy, Supervisor state and worktree snapshots.
- **Fingerprint caches.** Run summaries are reused until the log's size/mtime or the current verification policy changes. Uncached summaries are parsed in parallel with a bounded worker pool (up to eight workers, subject to CPU count).
- **Detail and snapshot caches.** The most recent 100 run details, the Supervisor snapshot (invalidated by the SQLite/WAL signature) and worktree snapshots (six-second TTL) are cached.
- **Compression.** JSON responses and static assets are gzip-compressed when the client advertises support; compressed assets are cached.
- **Bounds.** The console reads at most 500 run files, 10,000 events per log, 8 MiB per log, 20 Supervisor runs and 20 worktree manifests (64 KiB each). These are guardrails, not throughput guarantees.

These mechanisms reduce repeated reads and transfers; they are not a substitute for measuring your own project.

## Limitations

- **Viewer, not control plane.** It cannot spawn, cancel or inspect live processes, claim or complete Supervisor tasks, merge or clean worktrees, or change evidence.
- **No full-correctness proof.** Gate-verified means the configured checks passed under the current policy. It does not prove the task is complete, correct or secure.
- **Incomplete native streams stay incomplete.** Missing stops, absent checks and interrupted subscriptions are displayed as missing or partial. The console does not infer a finish, a stop or a verification.
- **Open is not running.** A fresh event log is not a liveness signal, and a dashed span has no observed end.
- **Review attestations are records.** The board can show that a review artifact was recorded and bound to a result digest; it cannot prove reviewer independence or that tests were genuinely executed.
- **Worktrees are previews.** A clean status is not a conflict-free or tested merge.
- **No cost data.** The current schema has no tokens or prices, so there are no cost or token KPIs.
- **Current-policy re-check.** Changing the verification policy can retroactively reject an old run. The console does not preserve historical policy snapshots.
- **No authentication.** Keep it on loopback and do not expose it.
- **Not a benchmark.** Overlap is scheduling evidence. Only matched serial/orchestrated repeats support a speedup claim; see [BENCHMARK.md](../BENCHMARK.md).
- **Not a replacement for the CLI.** `perf.py` remains the source of truth for reports and diagnostics; the Supervisor remains the durable task authority.

## Troubleshooting

| Symptom | What to check |
|---|---|
| **No runs shown** | Confirm `--root` points at the project with `.auto-orchestrator/runs/*.jsonl`, and that a hook or plugin actually wrote events. `python tools/perf.py --root . audit --json` categorizes missing terminal evidence. Use `--demo` to inspect the interface without evidence. |
| **Port already in use** | Startup prints `DASHBOARD ERROR: cannot bind 127.0.0.1:8765`. Use `--port 8766` (or another free port) or `--port 0` to let the OS choose; the actual URL is printed. |
| **Demo data looks real** | Check for the **DEMO DATA** badge and `demo-project` root. Demo is synthetic, reads no project evidence, writes nothing and is not a benchmark. Restart without `--demo` for live evidence. |
| **Another machine cannot connect** | Expected. The server binds only `127.0.0.1` and has no host option. Use a browser on the same machine; do not expose the port through a tunnel or proxy. |
| **Local browser cannot connect** | Use the exact printed URL. A local proxy configured to intercept loopback may need `127.0.0.1`/`localhost` in its bypass list. If a local firewall blocks Python from listening, allow it for local use. |
| **Task board is empty or unavailable** | The board needs `.auto-orchestrator/supervisor.sqlite3` with the expected Supervisor schema. Check the path and use `python tools/supervisor.py --root . status --run-id RUN` for the authoritative view. A locked or invalid database is reported as unavailable rather than guessed. |
| **Worktrees are missing** | Only manifests under the repository's Git directory are listed. Create a managed worktree with `python tools/worktree.py --repo . create --task-id TASK`. A missing worktree path is flagged; the console does not repair it. |
| **Verification changed to rejected** | The current `.auto-orchestrator/verification.json` policy may differ from the policy that produced the recorded checks, or the check IDs no longer match. Restore the matching policy or rerun the checks; do not edit the JSONL to force verification. |
| **Run shows Invalid log** | The JSONL is malformed before the last line or exceeds the size/event limits. A torn final line alone appears as **partial write**. A symlinked run log is omitted rather than read. Inspect a visible run with `python tools/perf.py --root . diagnose --run-id RUN`. |
| **Header says offline** | The server may have stopped, or `/api/state` polling failed. Check the terminal running the dashboard and refresh once the server is back. |

## Related documentation

- [BENCHMARK.md](../BENCHMARK.md) — matched serial/orchestrated measurement protocol; the only route to a speedup claim.
- [V6 native supervision](V6_NATIVE_SUPERVISION.md) — Supervisor claims, native start/stop and completion rules.
- [Native Supervisor platform matrix](NATIVE_SUPERVISOR_PLATFORM_MATRIX.md) — per-client evidence gaps and smoke acceptance.
- [IDE performance visibility](IDE_PERFORMANCE_VISIBILITY.md) — `perf.py` overlap fields and their limits.
- [V6.2 routing and review](V62_ROUTING_REVIEW.md) — advisory routing and review-artifact limits.
- [V6.4 reliability](V64_RELIABILITY.md) — incomplete-stream handling and audit categories.
- [Project README](../README.md) — installation, reporting and the wider workflow.
