# Controlled project execution (V7.1)

This layer sits on top of the existing Supervisor. It does not spawn native IDE
agents, does not create a second task database and does not merge worktrees.
Its job is to make the dispatch loop explicit, validate acceptance criteria, and
prevent an unverified result from becoming `completed`.

```text
authored request / plan
        ↓
project_planner.py        deterministic DAG + scope + acceptance validation
        ↓
Supervisor SQLite         claims, dependencies, leases, retries, native evidence
        ↓
executor.py next          dispatch manifest for the native IDE
        ↓
native agent execution    OpenCode / Codex / Cursor / Antigravity
        ↓
executor.py complete      structured result + acceptance evidence
        ↓
executor.py audit         tests, acceptance, native stop, review
        ↓
worktree preview          conflict preflight only; no automatic merge
```

## Why a separate layer?

The Supervisor already owns durable task state. The planner and executor add
the missing contract around it:

- every task declares **acceptance criteria**;
- unordered tasks may not write overlapping scopes;
- completion requires one passed acceptance record per criterion;
- a missing native stop or review attestation stays visible to the audit;
- the dispatch manifest separates the task prompt from the control lease;
- the worktree step previews only. Integration remains an explicit main-agent
  decision.

## Plan

```bash
python tools/project_planner.py validate --plan examples/v71-plan.json --json
python tools/project_planner.py init --plan examples/v71-plan.json --json
```

The plan is richer than the Supervisor schema:

```json
{
  "run_id": "checkout-retry",
  "max_parallel": 2,
  "tasks": [{
    "id": "api",
    "role": "implementer",
    "objective": "Make checkout retries idempotent.",
    "depends_on": ["schema"],
    "acceptance": ["Duplicate delivery does not create a second charge."],
    "read_scope": ["src/checkout/"],
    "write_scope": ["src/checkout/"],
    "timeout_seconds": 1800,
    "max_retries": 2
  }]
}
```

Validation rules:

- Supervisor DAG validation still applies (cycle, scope, retry and timeout
  bounds).
- Every task has 1–10 unique acceptance criteria.
- Two tasks without an ordering relationship may not write overlapping scopes.
- An unordered read/write overlap is rejected as well.
- Absolute paths, `..`, backslashes and glob characters are rejected by the
  existing scope validator.
- The acceptance artifact is written to
  `.auto-orchestrator/plans/<run_id>.json` with mode 0600 on POSIX.

## Dispatch

```bash
python tools/executor.py next --run-id checkout-retry --json
```

`next` calls the Supervisor's dependency-aware claim and writes a local dispatch
manifest under `.auto-orchestrator/dispatch/`. The manifest contains:

- the task objective, scopes, dependencies and acceptance criteria;
- the attempt lease in a separate `control` block;
- an instruction to dispatch through the native IDE tool.

The lease must not be placed in a subagent prompt. It is an operator control
value.

Bind the real native ID after the native client exposes it:

```bash
python tools/executor.py bind --run-id checkout-retry --task-id api \
  --lease REAL_LEASE --platform codex --native-id REAL_NATIVE_ID
```

For OpenCode, the plugin forwards a platform-scoped SHA-256 hash; use
`--native-hash` when correlating the installed bridge.

## Complete and audit

```bash
python tools/executor.py complete --run-id checkout-retry --task-id api \
  --lease REAL_LEASE --result examples/v71-result.json

python tools/executor.py status --run-id checkout-retry --json
python tools/executor.py audit --run-id checkout-retry --json
python tools/executor.py preview --run-id checkout-retry --json
```

`complete` adds the validated acceptance records to the structured result and
then delegates to the Supervisor. A missing/failed acceptance record is
rejected before the task can become `completed`.

`audit` reports:

- failing or missing tests in a completed result;
- incomplete acceptance evidence;
- missing native stop when native supervision is enabled;
- missing review attestation when review is required;
- incomplete plans.

`preview` calls the existing non-destructive worktree preview. It never merges,
resets, cleans or deletes a branch.

## Native limits

The executor is honest about what the IDE exposes. See
[the capability matrix](CAPABILITY_MATRIX.md). In particular:

- a database lease is not a spawned agent;
- a native start is not a completion;
- a background dispatch return is not a child stop;
- an open span is not proof that a process is still running;
- an unavailable native ID must not be synthesized.

The main agent remains responsible for dispatching through the native tool. The
executor makes the contract, claims and evidence deterministic; it does not
pretend to replace the native runtime.

## Optional OpenTelemetry

`tools/telemetry.py` is opt-in and no-op-safe when OpenTelemetry is not
installed. The planner and executor already wrap their control points:

- `task.plan` around plan initialization;
- `task.dispatch` around a Supervisor claim and manifest write;
- `task.verification` around acceptance validation and completion;
- `worktree.integration` around a non-destructive preview.

The spans carry `run_id`, `task_id`, and `project_id` when the planner artifact
provides one. They do not carry prompts, code, commands, terminal output or
tokens. A missing collector never blocks the product and never replaces the
Supervisor database.

## Demo limitation

The sample plan and result are illustrative. They are not evidence that a real
checkout task ran. Use the CLI against a disposable project and record the real
native events before treating a run as verified.
