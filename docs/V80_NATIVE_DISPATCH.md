# V8.0 — Native Intelligent Teamwork and Dispatch

V8.0 adds a typed native dispatch layer on top of the V7.9 verified evidence
model. It does **not** spawn native agents: the owning IDE's parent session
dispatches its own subagents. Auto-Orchestrator records the dispatch contract,
the lifecycle events that can actually be observed, and the evidence required
before a task may be marked verified.

## 1. Layers and state ownership

| Layer | Owner | State |
|---|---|---|
| Requirements | project memory + contract | interviews, decisions, contract versions |
| Planning | deterministic planner | plan v2, scope contract, DAG, risk |
| Orchestration | supervisor + native dispatch | tasks, milestones, dispatch lifecycle |
| Native adapter | the active IDE | vendor child identity, permissions, sandbox |
| Ownership | `ide_handoff` | one owner token/fence per project |
| Evidence | runner + verifier | test results, review records, output digests |
| Verification | Completion Engine | criterion states, merged-tree gate |
| Presentation | CLI, skills, dashboard | read-only projections |

There is one SQLite source of truth for supervisor/memory state and one
append-only JSON record per dispatch under
`.auto-orchestrator/native-dispatches/<run_id>/<dispatch_id>.json`.

## 2. Dispatch contract

Every dispatch records:

```
dispatch_id, project_id, run_id, task_id, role, native_ide,
owner_session_id, parent_agent_id, child_agent_id, native_child_ref,
read_scope, write_scope, dependencies, sandbox_policy, permission_policy,
state, created_at, started_at, completed_at, lifecycle_status,
result_ref, evidence_ref, output_digest, verification_run_id,
retry_count, exit_reason, history
```

`create` enforces:

- a supported role and IDE;
- no second active dispatch for the same task;
- at most three active dispatches;
- no overlapping write scopes between active dispatches;
- every dependency has a dispatch record in `verified` state;
- bounded, relative, traversal-free scopes.

## 3. Lifecycle

```
queued → dispatch_requested → dispatch_confirmed → running
running ⇄ waiting_for_permission
running → completed_claimed → output_collected → verification_pending → verified
any active → failed | cancelled
any active → recovery_required
```

Rules:

- `start` requires a native child reference. A dispatch request alone is not a
  running child; without an observed reference the state stays
  `dispatch_confirmed`.
- `complete` only records a **claim**; `collect` requires the result artifact to
  exist and hashes it.
- `collect` stores the canonical workspace-relative `artifact_ref`, digest and
  size (not just the caller's filename), so `--output-dir subdir` verifies the
  same `subdir/...` file later. The artifact is hashed in chunks; symlinks,
  symlinked parents and external output directories are rejected.
- `verify` requires a **verification receipt** issued by `native-verify`:
  HMAC-protected under `.auto-orchestrator/verification/receipts/`, bound to the
  project/run/task, **dispatch id**, current HEAD, contract/plan digests, the
  dispatch artifact digest/size, and the verification record digest. It
  re-hashes the collected artifact and the record, and consumes the receipt
  atomically (`.used`) so another dispatch cannot replay it. A hand-written
  `verified:true` JSON file, a receipt for another dispatch or artifact, a
  changed artifact/record, or a stale HEAD is rejected.
- A parent-supplied `native_child_ref` is `child_provenance: declared`; no
  adapter here proves an observed vendor lifecycle, so
  `native_lifecycle_observed` stays false.
- `recover` records `recovery_required` with `stop_ack: false` and
  `native_cancel_ack: false`; it never fabricates completion.
- Out-of-order transitions are rejected.

## 4. CLI

```bash
# create and request a dispatch
python3.11 tools/universal_teamwork.py --root . --platform codex \
  native-dispatch create --run-id run-1 --task-id build \
  --write-scope src/ --owner-session-id codex-session

python3.11 tools/universal_teamwork.py --root . --platform codex \
  native-dispatch request --run-id run-1 --dispatch-id nd_…

# the parent session confirms a real native child, then starts it
python3.11 tools/universal_teamwork.py --root . --platform codex \
  native-dispatch confirm --run-id run-1 --dispatch-id nd_… \
  --native-child-ref <vendor-child-id>
python3.11 tools/universal_teamwork.py --root . --platform codex \
  native-dispatch start --run-id run-1 --dispatch-id nd_…

# evidence and verification
python3.11 tools/universal_teamwork.py --root . --platform codex \
  native-dispatch complete --run-id run-1 --dispatch-id nd_… --result-ref out.txt
python3.11 tools/universal_teamwork.py --root . --platform codex \
  native-dispatch collect --run-id run-1 --dispatch-id nd_… --evidence-ref out.txt
python3.11 tools/universal_teamwork.py --root . --platform codex \
  native-dispatch verify --run-id run-1 --dispatch-id nd_… \
  --verification-run-id vr_…
```

`native-dispatch status` is read-only.

## 5. Concurrency and file ownership

The dispatch store uses the existing cross-process `project_lock` and atomic
JSON writes. Concurrent overlapping creates are serialized and exactly one
succeeds. Scopes are compared by normalized path prefix, so `src/` overlaps
`src/api.py`. This is project-level coordination, not an OS sandbox: a vendor
tool that ignores the protocol can still write.

## 6. Honest limits

- Dispatch records are **live-verified** as a state machine and CLI.
- Native child creation, completion and stop acknowledgement are **not
  observable** through a vendor-neutral interface here. `running` requires a
  parent-provided child reference; missing lifecycle evidence becomes
  `recovery_required`, never `completed`.
- Cursor live execution remains blocked by CLI authentication on this machine.
- Antigravity native commands remain vendor-authoritative; there is no CLI
  adapter.
- No token/cost benchmark is fabricated. V7.9 measurement rules still apply.
