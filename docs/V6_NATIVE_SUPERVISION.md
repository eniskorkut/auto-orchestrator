# V6 — native evidence Supervisor integration and platform simulations

## Trust and lifecycle

A claim is only a lease reservation. It is NOT a spawned native agent. Initialize the Supervisor with `native_supervision: true` to opt in. Native task IDs and attempt leases are scoped to the user's project SQLite database, where `BEGIN IMMEDIATE` prevents parallel claim races. Do not put a lease in a prompt sent to a subagent.

1. Main agent initializes the run and claims eligible tasks. It must respect the returned dependencies/read-write scopes.
2. It dispatches through its IDE's real native subagent tool; no dummy process is launched by Supervisor.
3. When the native IDE returns a real agent ID, bind with `supervisor.py --root PROJECT bind-native --run-id RUN --task-id TASK --lease LEASE --platform codex|cursor|opencode --native-id NATIVE_ID`. Each attempt has one immutable native ID mapping; collision, stale lease and duplicate mapping are rejected. No raw native ID is written to SQLite; only SHA-256 is persisted.
4. On a **matching authenticated native stop event** the hook records `native_stop_observed=true`. This is termination evidence, not proof that tests passed. Native stop does not mark `completed` or free dependency/write-scope reservations.
5. The main agent reviews actual changes and tests, then supplies a bounded structured result and its attempt lease to the Supervisor's existing `completed` command. A supervised transition fails unless that attempt has a bound observed native stop. Failure/cancel acknowledgement likewise needs a trustworthy stop.
6. A timeout only marks `timed_out`, and cancellation still requires native acknowledgement; retry limits remain enforced. `pending` tasks after retry cannot inherit a previous attempt's stop evidence.

This mechanism requires trusted hook installation and native events. It does not prevent a process running under the same OS account from manipulating its own files, SQLite or hook stdin. It is not a cross-user authorization system.

**Known race:** A very short native agent may finish before the parent obtains and binds its native agent ID. In that case the earlier stop is not matched and the task remains reserved; do not synthesize a stop to unblock it. Improve through a verified client-native dispatch API only when supported. **Native orchestration is not yet a fully automatic DAG executor:** claim, dispatch and bind are currently explicit agent operations.

## Platform matrix (documented contract, not real IDE smoke tests)

| Client | Native event evidence | Automatic supervised stop | Limitation |
|---|---|---|---|
| Codex | `SubagentStart` / `SubagentStop` hook with agent ID | Yes, if ID is bound while hook is active | Native trust/activation, lifecycle and model availability require live test. |
| Cursor project-local | `subagentStart` ID, `subagentStop` status/duration | Only if the client exposes same exact ID on stop | Documented stop payload does not guarantee ID; do not correlate by task text/type. |
| OpenCode V2 | Native subagent tool events and child sessions | Not automatically linked to SQLite in V6 | Background tool completion is only spawn acknowledgement. |
| Antigravity | `SessionStart`, `PreToolUse`, `PostToolUse`, `Stop` | No authoritative per-child stop | Tool dispatch/fullyIdle are not proof of which child terminated. |

Cursor global install is **skills/agent profiles only**, intentionally no `~/.cursor/hooks.json` edits. Project-level hooks are needed for scoped telemetry; hook user permissions are never auto-approved. For Cursor IDE/CLI/Cloud feature coverage see the official documentation and test installed client behavior separately.

## Security and quality checks

- Native ID collision, stale attempt/lease, unbound stop, replay after terminal state, concurrency and timeout cannot release a supervised task.
- Native hooks observe identity/status only; no prompts, task text, tool output, raw session IDs or secrets are persisted in telemetry.
- Missing workspace scope in a global hook is not replaced by process.cwd(). Antigravity with multiple workspacePaths is unsupported for reliable project assignment.
- Cursor documented stop payload with no ID does not produce artificial per-agent spans.
- `verified` in the V5 performance log remains independent from Supervisor's task `completed` status. A verified status requires existing explicit native check policy where applicable.
- CI simulates the documented hook payloads and installer behavior; **it does not run Codex, Cursor, or Antigravity UI sessions**.

## Live acceptance (not completed in CI)

Use a disposable, isolated project. Enable native hooks and verify exact contract fields/IDs, activate a supervised run, claim and bind two non-overlapping tasks, and show a confirmed native stop for each before submitting structured results. Attempt a duplicate ID, stale lease, premature completed, timeout+cancel and cross-project event. Verify no other project receives new logs. Only then label native Supervisor E2E as PASS.
