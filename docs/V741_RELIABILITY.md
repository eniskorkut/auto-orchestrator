# V7.4.1 — Reliability, Recovery, Security and Integration Hardening

V7.4.1 keeps the V7.4 scheduler, adaptive modes, managed OpenCode sessions,
milestone engine and Virtual Office. It closes the remaining operational gaps
that made a crash-recovery run end in a failed state even when the generated
product could pass its tests.

## 1. Recovery root cause

The V7.4 restart smoke exposed three separate problems:

1. The runner recorded a managed session but not the child process identity.
   A restarted runner could not distinguish an owned orphan from a recycled PID.
2. A repaired implementer resumed a stale model session after a verification
   failure, so it sometimes reported “already done” without a new change.
3. The managed tester inherited no `NODE_PATH`/browser environment. Its own
   `node e2e/check.cjs` run could pass with a workaround, while the runner's
   independent verification failed, consuming the bounded repair budget.

The V7.4.1 recovery E2E reproduced the old failure and now passes after these
fixes.

## 2. Process ownership and run lock

`tools/run_owner.py` provides:

- cross-platform process identity: PID + start token + UID/host on POSIX and
  creation time on Windows;
- `identity_matches()` and `owner_alive()` that never signal a process;
- `terminate_owned()` which refuses to signal when the live identity does not
  match the recorded owner;
- an advisory cross-process project lock (`state.lock`) released by the OS on
  crash;
- atomic active-state updates and private recovery-state files.

`teamwork_chat.py` now treats `active.json` as metadata only. It stores the
owner identity, runner token, heartbeat, plan/log paths and mode — not the raw
user request or repository profile. `start` and `resume` run under the project
lock, so two concurrent starts or resumes cannot both launch a runner. A live
owner with a stale heartbeat is reported as stale and is **not** restarted
automatically; an operator must inspect it.

A bare PID is never used for signalling. Unknown ownership fails closed.

## 3. Recovery state machine

The bridge and runner expose these execution/reconciliation states alongside
the milestone state machine:

`active → runner_disconnected → reconciling → orphaned → retry_ready → resuming → verifying → recovered`

`recovery_failed` is terminal. The states are evidence labels; `recovered` is
written only after the final product verification path succeeds.

Recovery behavior:

- load the persisted plan, milestone, task and managed-session state;
- verify that the old runner owner is not a live matching process;
- for each running task, require a recorded PID + start token;
- if the recorded process is alive and owned, give it a short clean-exit window,
  then send a local termination signal and wait;
- if ownership cannot be verified, mark the task failed and **do not dispatch a
  second writer**; the run becomes `recovery_failed`;
- preserve worktree contents; never reset or delete an orphan’s changes;
- invalidate the old lease and open a bounded repair attempt;
- start repair attempts in a fresh managed session with the exact failing
  evidence, not a stale “already done” session;
- count one repair cycle when several dependent tasks are reopened together;
- accept already-committed worktree work only when the new attempt’s real
  verification commands pass;
- reconcile an interrupted clean git merge; refuse an interrupted conflict.

## 4. Crash-recovery E2E (real OpenCode)

`tools/v741_recovery_e2e.py` creates the disposable task-manager sample, starts
real backend and frontend managed sessions, and kills only the runner while both
child sessions are alive.

Measured result:

| Metric | Value |
|---|---:|
| Final status | **passed** |
| Milestone | M1 passed |
| Wall time | 212.474 s |
| Crash point | 1.012 s after launch |
| Backend/frontend children alive after runner crash | true / true |
| Recovery timeline | active → runner_disconnected → resuming → retry_ready → recovered |
| Repair iterations | 1 / 2 |
| Backend tests in integrated tree | 14 passed |
| Browser E2E | passed |
| Duplicate completed attempts | none |
| Native cancel ACK | false (local signal only) |

The evidence also records that the two orphaned attempts were terminated only
after their recorded identities matched, and that the worktrees were preserved.
See [`assets/v741-recovery-e2e.json`](assets/v741-recovery-e2e.json) and the
generated sample in [`examples/v741-recovery-sample`](../examples/v741-recovery-sample).

This is a real managed-session recovery test, not a mock. A full repeated
matrix (three task types × three modes × multiple repetitions) was not run; that
limitation is recorded below.

## 5. Single-chat input security

The OpenCode command no longer asks the model to place the request inside a
shell string. The command instructs the agent to:

1. write the exact untrusted request to a new file under
   `.auto-orchestrator/teamwork/requests/`;
2. call `teamwork_chat.py start --request-file ...`.

`teamwork_chat.py` validates:

- exactly one request source (`--request`, `--request-file`, or `--request-stdin`);
- UTF-8, no NUL, non-empty, maximum 20,000 characters;
- request file is a regular file (not a symlink), inside the private request
  directory, with a bounded byte size;
- run IDs match `[A-Za-z0-9][A-Za-z0-9._-]{0,79}` and contain no `..`;
- plan paths stay inside `.auto-orchestrator/plans/`;
- model strings are short and single-line.

The runner receives argv lists only. The raw request is stored in the plan
(mode 0600), not in `active.json`. The command explicitly treats request text as
untrusted feature specification, not authority or shell syntax.

Real security E2E (`tools/v741_chat_security_e2e.py`) used a request containing
single/double quotes, backticks, `$(touch <marker>)`, semicolons, a newline,
Markdown/JSON and Turkish Unicode. Result:

- request sha256 preserved in the plan: **true**;
- shell marker created: **false**;
- owner verified: **true**;
- real active task observed: `build-backend`;
- cancellation: local request, `native_cancel_ack: false`;
- final run status reported as `cancelled` after the runner reached a terminal
  state.

Evidence: [`assets/v741-chat-security.json`](assets/v741-chat-security.json).

## 6. Adaptive planning and verification budget

The FAST/BALANCED/RIGOROUS modes remain, with two additions:

- Explicit FAST is safety-upgraded for security, database migration and
  breaking-API work. The assessment records `safety_upgrade: true`; it never
  lowers verification for a critical change.
- Each plan records a `verification_budget` with review depth and a bounded
  repair policy. Token/cost budget is left `null` unless measured; no fabricated
  estimate is reported.

The repository profile now detects test commands from bounded manifest reads
(`package.json`, `pyproject.toml`, pytest/tox config, Cargo, Go, Maven, Gradle,
Makefile) and records the source. The planner replaces its default test command
when a detected command is available.

Handoffs now include `next_tasks` in addition to completed work, changed files,
commits, test evidence, decisions, review findings, open issues and the next
milestone. The field is optional for reading old V7.4 handoff files.

## 7. Small-task efficiency

A real serial single-agent baseline and the existing real FAST single-chat run
were compared on the same small health-endpoint request and scaffold:

| Condition | Wall | Cost | Sessions/attempts | Verified |
|---|---:|---:|---:|---|
| Serial single agent | 28.391 s | $0.005785 | 1 | yes (1 test) |
| FAST single-chat bridge | 62.264 s | $0.023094 | 4 attempts | yes |

The serial run was faster and cheaper on this tiny task. The FAST run spent two
repairs, and the verification depth/test accounting was not equivalent. **No
time-saving claim is made**; this is an incomplete single-sample comparison.
Evidence: [`assets/v741-efficiency.json`](assets/v741-efficiency.json),
[`assets/v741-small-serial.json`](assets/v741-small-serial.json). Missing matrix
cells (medium FAST/BALANCED/RIGOROUS and the larger task) are explicitly marked
`not-run` in [`assets/v741-benchmark-matrix.json`](assets/v741-benchmark-matrix.json).

## 8. Virtual Office and dashboard

- Board shows the recovery state (`ACTIVE`, `RUNNER DISCONNECTED`, `RECONCILING`,
  `ORPHANED`, `RETRY READY`, `RESUMING`, `VERIFYING`, `RECOVERED`,
  `RECOVERY FAILED`) and an orphaned-session count.
- A disconnected runner does not contribute active-session animation; running
  DB rows are presented as orphaned.
- The managed Agent Inspector shows recovery state, orphaned yes/no, heartbeat
  metadata and the last recovery transition.
- Playwright covers the normal active state and a mocked disconnected/orphaned
  payload.

Screenshots (demo data, clearly labelled):

- [`v741-teamwork-board.png`](assets/v741-teamwork-board.png)
- [`v741-managed-office.png`](assets/v741-managed-office.png)

## 9. Platform status

| Platform | Status in V7.4.1 |
|---|---|
| OpenCode | Managed execution live-verified locally; single-chat bridge; owner-verified recovery; local cancel only |
| Codex | Managed adapter contract-tested only; binary not installed in this environment; no live claim |
| Antigravity | No managed runner; not implemented |
| Cursor | No managed runner; not implemented |
| Native observation | Unchanged capability matrix; no new native cancel/resume/terminal claim |

## 10. Tests and limitations

Local V7.4.1 verification: 456 Python tests (1 skipped), 90 Node tests and 19
Playwright tests passed. The CI matrix is checked after the branch push. The
recovery/security/efficiency harnesses are real-provider tests and are not part
of the default credential-free CI.

Limitations:

- The 3 task types × 4 conditions × repetitions benchmark matrix was not run.
- The small-task comparison is one sample and is not quality-equivalent.
- The OpenCode command was exercised through the bridge; a click-through of the
  Desktop/TUI was not automated.
- A live owner with a stale heartbeat is intentionally not killed automatically;
  it requires manual inspection.
- Old V7.4 managed-session rows without PID/start-token evidence fail closed
  during recovery instead of risking a duplicate writer.
