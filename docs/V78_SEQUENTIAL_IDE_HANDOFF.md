# V7.8 — Native IDE Teamwork, Intelligent Grill-Me and Sequential IDE Handoff

V7.8 makes Teamwork and Grill-Me native in Codex, OpenCode V2, Cursor and
Antigravity while keeping one durable project memory. Exactly **one IDE owns
write/execution authority** for a project at a time. Native subagents inside the
owning IDE may run in parallel; no IDE starts another IDE to continue work.

This document is the contract for the ownership engine, the handoff snapshot
and the per-IDE evidence labels. It is intentionally explicit about what is
*verified* and what is only *contract*.

## 1. The invariant

```
one project  →  one owner record  →  one fence  →  one active writer
```

- Ownership is project-scoped, not session-scoped. A project keeps its ID after
  a clone or move (`tools/project_identity.py`).
- Read-only context inspection is always allowed: `context`, `status`,
  dashboard reads, requirement and evidence views.
- Mutating operations require the current `owner_token` **and** `fence`.
- A stale heartbeat is **not** proof that a native agent has stopped. Unknown
  liveness fails closed and requires explicit recovery evidence.
- Native session migration is not claimed. The new IDE rehydrates the project
  record, not another IDE's live chat process.

## 2. Files

| Path | Purpose |
|---|---|
| `.auto-orchestrator/ide/ownership.json` | Single ownership record per project |
| `.auto-orchestrator/ide/handoffs/<handoff_id>.json` | Immutable handoff snapshots |
| `.auto-orchestrator/teamwork/native-active.json` | Pointer to the current native plan (no PID, no token) |
| `.auto-orchestrator/memory.sqlite3` | Existing V7.6/V7.7 memory (unchanged schema contract) |
| `.auto-orchestrator/teamwork/state.lock` | Existing cross-process advisory lock |

`tools/ide_handoff.py` owns this state. It reuses `project_lock`,
`atomic_write_json`, `read_json` and process identity helpers from
`tools/run_owner.py`; it does **not** create a second supervisor, a second
memory store or a second scheduler.

No module in this path starts an IDE, provider, runner or supervisor process.
Git is invoked read-only to describe repository state; there is no
`subprocess.Popen` for an agent, no foreign provider launch and no background
IDE switch.

## 3. Ownership state machine

```
inactive ──activate──▶ active
active ──prepare──▶ handoff_prepared ──release──▶ released ──accept──▶ transferred
   │                     │                          │
   └──────────┬──────────┴──────────────┬───────────┘
              ▼                          ▼
        recovery_required            blocked
```

States: `inactive`, `active`, `handoff_prepared`, `released`, `transferred`,
`recovery_required`, `blocked`.

Ownership record v1:

```json
{
  "version": 1,
  "project_id": "p_…",
  "project_uuid": "…",
  "state": "active",
  "fence": 7,
  "owner": {
    "owner_id": "…",
    "ide": "codex",
    "session_id": "…",
    "pid_identity": null,
    "host": "…",
    "activated_at": 0.0,
    "heartbeat_at": 0.0,
    "ttl_seconds": 900,
    "token": "…"
  },
  "handoff": {
    "handoff_id": "ho_…",
    "digest": "…",
    "path": ".auto-orchestrator/ide/handoffs/ho_….json",
    "source_fence": 7,
    "target_ide": "opencode"
  },
  "previous_owner": {"ide": "codex", "owner_id": "…", "fence": 7},
  "last_transition": {"action": "activate", "at": 0.0, "actor": "codex", "fence": 7},
  "native_cancel_ack": false,
  "stop_ack": false
}
```

Rules:

- Every transition increments `fence`. A compare-and-swap with the wrong token
  or a stale fence is rejected, not silently retried.
- Repeated identical calls (same action, token, fence, digest) return
  `unchanged: true` and do not advance the fence.
- `accept` requires a matching snapshot digest and a re-read of every referenced
  digest. Any drift blocks acceptance without mutating state.
- `native_cancel_ack` and `stop_ack` are `false` for native IDEs. V7.8 never
  fabricates a cancellation acknowledgement.

## 4. Handoff snapshot v1

A handoff carries pointers and digests, not copies of prompts or logs:

| Group | Fields |
|---|---|
| Identity | `project_id`, `project_uuid`, `source_ide`, `target_ide`, session ids |
| Work | `run_id`, `milestone_id`, `task_id`, task/milestone statuses, `mode` |
| Repository | `branch`, `head_sha`, `dirty`, `dirty_digest`, `changed_files` |
| Completion | completed tasks, pending tasks, last error, failed tests, verified evidence refs |
| Contract | `contract_id`, `contract_version`, `sha256` |
| Interview | `interview_id`, `open_question_id`, `request_hash`, blocking questions |
| Decisions | confirmed decision refs (IDs, not raw text dumps) |
| Blockers | blocking requirement ids |
| Context | selector provenance and evidence refs |
| Next | `next_action` |
| Integrity | `created_at`, `schema_version`, `digest` |

`dirty_digest` covers `git status --porcelain` plus the content digest of changed
files, excluding `.auto-orchestrator/` (which contains the record itself).
The snapshot digest is a canonical SHA-256 over the body, excluding `digest`.

Constraints: maximum 64 KiB, regular file, no symlinks, must resolve inside the
handoff directory, and the project id must match the live project.

## 5. Liveness and recovery

Liveness evidence, strongest first:

1. **PID identity match** — start token / creation time, uid, host. A bare PID is
   never sufficient because PIDs are reused.
2. **Fresh heartbeat** — only valid for owners without a PID identity. A fresh
   heartbeat without identity is `unknown_live`, not `alive`.
3. **Managed lease evidence** — the existing `project_writer_lease` and
   `runner_lease` locks. Absence of the OS lock plus a token mismatch is
   evidence that a managed process ended; it says nothing about a native child.

A stale heartbeat is reported as `expired: true` and does not mutate state.
`recover --apply` requires explicit evidence, moves the record to
`recovery_required`, records `liveness: "unknown"`, `stop_ack: false` and never
writes `released` or `transferred`. The operator then decides whether to
`release` with evidence or re-`activate`.

When a handoff was prepared but can no longer be accepted (the source kept
writing, HEAD moved, the contract changed, or the owner is gone), recovery is
the way out:

- `recover --apply --evidence <json>` on a `released` record clears the stale
  handoff and returns the project to `inactive`;
- `recover --apply --discard-handoff --evidence <json>` does the same directly
  from `handoff_prepared`, recording the discarded handoff id, `liveness:
  "unknown"`, `stop_ack: false` and `native_cancel_ack: false`.

Recovery never claims that a native child stopped. It only records that the
operator decided the recorded owner is gone.

Crash windows are handled by the OS releasing the advisory lock. The record
remains; the next mutation reconciles it. No takeover is automatic.

## 6. Phase 1 — plan before code

Every non-trivial request produces an inspectable plan artifact under
`.auto-orchestrator/plans/`:

- goal and non-goals,
- scope and the files/areas allowed to change,
- acceptance checks with stable test IDs,
- role/task DAG with dependencies and write scopes,
- verification commands,
- risks and open questions,
- contract digest and version,
- an `approval` block: `{status, approver, approved_at, plan_sha256, platform}`.

`approve-plan` is a separate explicit step. `start --plan <run>` refuses a
missing approval, a changed digest or a stale contract. Small, low-risk SOLO
tasks may use the lightweight path, but the approval is still explicit.

The approved plan is **project state, not another IDE's session**. After a
sequential handoff the new owner may continue the same run with `start --plan`;
the result reports `plan_authored_by` and `handoff_continuation: true`. The
owners' native subagents then work from the plan and project memory. Approval
remains bound to the exact plan digest and approver, so a changed plan still
requires a new explicit approval.

A dirty worktree may be transferred only with `--allow-dirty`. The snapshot
records the modified paths and a content-sensitive `dirty_digest`; if either
side changes after the snapshot, acceptance is refused.

## 7. Phase 2 — risk-driven team behavior

Planner, explorer, implementer, tester, reviewer, critic, challenger, auditor
and success-auditor roles are selected by risk and mode. Native IDEs run these
as their own subagents; the parent session owns permissions and sandbox.

Native task results are recorded with `native-report`, which stores a task
verdict plus real test output references. Prose-only success claims are
rejected; a missing reviewer verdict leaves the requirement `needs_review`.

## 8. Grill-Me 3.0 across IDEs

The interview is one durable object in project memory:

- one question at a time, recommended option first with its rationale and
  alternatives, plus a write-in answer;
- dependency-aware follow-ups and `back` to an earlier answer;
- safe skip for non-critical questions; critical skips stay
  `unresolved_blocking` and block approval;
- `resume` reopens the exact open question, never repeats answered ones;
- conflicts with earlier answers are surfaced before accepting a new answer;
- approval creates a digest-bound, versioned requirements contract.

Switching IDE mid-interview keeps the same `interview_id`, answers, open
question and request hash. Deterministic Assumption Guard rules cannot be
overridden by model output.

Antigravity keeps its native `/grill-me` and `/teamwork-preview` commands. The
`ao-grill-me` skill is a bridge that imports **explicitly user-confirmed**
answers into the same memory; it never shadows or renames a native command.

## 9. Per-IDE surfaces

| IDE | Native surface | Native execution | Native child liveness | Stop ACK |
|---|---|---|---|---|
| Codex | skill `.codex/skills/teamwork`, agents in `.codex/agents` | parent session, native subagents | hook contract | unsupported |
| OpenCode V2 | `/teamwork`, `/grill-me`, skill, V2 plugin | native or managed (explicit) | foreground tool-call spans only | unsupported |
| Cursor | shared skills + `.cursor/agents`, project hooks | parent session only; no CLI agent | start-ID hook only | unsupported |
| Antigravity | vendor `/teamwork-preview`, `/grill-me`; `ao-grill-me` bridge | vendor supervisor | unsupported | unsupported |

Evidence labels used in the capability matrix and delivery report:

- `live-verified` — observed in this environment with recorded evidence.
- `simulated` — exercised through the real CLI with a scripted native surface.
- `contract-only` — file/format exists; no live execution was observed.
- `unsupported` — no documented native surface; no command was invented.

## 10. CLI

```bash
# read-only
python3.11 tools/universal_teamwork.py --root . --platform codex ide status

# take ownership, then work natively
python3.11 tools/universal_teamwork.py --root . --platform codex ide activate
python3.11 tools/universal_teamwork.py --root . --platform codex plan --request-file .auto-orchestrator/requests/req.md
python3.11 tools/universal_teamwork.py --root . --platform codex approve-plan --run-id run-…
python3.11 tools/universal_teamwork.py --root . --platform codex start --plan run-… --native

# hand off to another IDE
python3.11 tools/universal_teamwork.py --root . --platform codex ide prepare --target opencode
python3.11 tools/universal_teamwork.py --root . --platform codex ide release --handoff ho_…
python3.11 tools/universal_teamwork.py --root . --platform opencode ide accept --handoff ho_… --digest …
```

The target IDE rehydrates from `context`, `ide status` and the accepted
snapshot. `ide status` exposes the snapshot's `run_id`, milestone/task ids,
completed/pending tasks, failed tests, contract and interview refs,
`next_action` and the open Grill-Me question. It reads the next action from
project state; it does not read the previous IDE's native transcript.

Ownership credentials are per platform and stored in
`.auto-orchestrator/teamwork/ide-owner-<platform>.json` with mode `0600`.
`ide activate`/`ide accept` write it; `--owner-token`/`--fence` may be passed
explicitly. Status output never includes the raw token.

```bash
# abandon a handoff that can no longer be accepted
python3.11 tools/universal_teamwork.py --root . --platform codex ide recover \
  --reason "source kept editing" --evidence evidence.json --apply --discard-handoff
```

## 11. End-to-end evidence

`tools/v78_handoff_e2e.py` drives the real CLI in separate processes through:

1. Codex → OpenCode → Codex,
2. OpenCode → Cursor → OpenCode,
3. Codex → Antigravity → Codex,
4. an interview interrupted mid-way and resumed by another IDE,
5. an unfinished task continued by the new owner,
6. two IDEs racing for ownership,
7. dirty worktree transfer,
8. stale contract/HEAD refusal,
9. crash/restart recovery,
10. no second IDE process being started.

Simulated runs write `docs/assets/v78-handoff-sim-e2e.json` with
`"mode": "simulated"` and an explicit `live_ides` list. Live runs write a
different file and are the only ones allowed to claim `live-verified`.

### Live evidence (this machine)

`tools/v78_live_handoff_e2e.py` ran the real CLIs in a temporary git project on
2026-10-27 (macOS, Python 3.11.15):

| Step | Real surface | Observed |
|---|---|---|
| Codex takes ownership, plans and starts natively | `codex-cli 0.164.0` | plan approved, no PID/runner |
| Codex writes its part | `codex exec --sandbox workspace-write` | file created in 15.9 s |
| Handoff to OpenCode | ownership CLI | snapshot accepted |
| OpenCode recovers `next_action` and writes | `opencode v2.1.1` (`--standalone --auto`) | marker from `ide status` written in 17.2 s |
| Handoff back to Codex | ownership CLI | snapshot accepted |
| Codex continues from the second handoff | `codex exec` | final marker written in 18.8 s |

Evidence: `docs/assets/v78-live-handoff.json` (`mode: "live"`,
`live_ides: ["codex", "opencode"]`). Cursor and Antigravity had no documented
CLI agent entry on this machine, so they stayed contract-only; the script
reports them as unavailable instead of passing them. These are single scripted
runs, not a benchmark of model quality or human time saved.

## 12. Limits

- The protocol is advisory. A native IDE that ignores it can still write; the
  engine detects and reports the conflict but cannot sandbox a vendor tool.
- Native child liveness is inherently unverifiable. V7.8 fails closed instead of
  guessing.
- Cursor and Antigravity have no managed process adapter here; their rows are
  contract-only until a documented native surface is exercised.
- Native token and cost data are not observable from this repository. Managed
  runs keep their measured telemetry; native reports record only what the
  client explicitly reports, and no estimate is synthesized.
- The fence is a recorded monotonic counter, not a security boundary against a
  user who rolls back the state directory.
