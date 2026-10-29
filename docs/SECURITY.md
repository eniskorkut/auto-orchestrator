# Security and verification

Auto-Orchestrator is a local developer tool. It coordinates native IDE agents
and records evidence; it is not a sandbox, a secrets manager or a multi-user
authorization service.

## What is protected

### HMAC verification receipts

`native-verify` issues a receipt under
`.auto-orchestrator/verification/receipts/<id>.json`. The receipt is an
HMAC-SHA-256 over a canonical JSON payload with a per-project key at
`.auto-orchestrator/ide/verification.key` (mode `0600`).

The payload binds:

- project, run, task and dispatch identity;
- commit, contract digest and plan digest;
- verification run id, exit code and `verified` state;
- test-result and test-output digests;
- dispatch artifact digest and size;
- the verification record digest and reference;
- issue time.

A dispatch cannot be promoted by pointing at an unrelated JSON file. The
receipt must exist in the protected registry, match the same project/run/task/
commit/contract/plan and carry a valid MAC. Receipts are single-use: consuming a
receipt for one dispatch prevents replay for another dispatch.

**Trust boundary:** the key protects against accidental or file-only forgery by
an agent that cannot execute code in this project. A same-user process that can
read the key and import the module can forge a receipt. That is an OS-level
trust boundary, not a cryptographic one. The repository documents this instead
of presenting the receipt as an OS sandbox.

### Run-bound test evidence

`native-verify` runs the approved command itself and writes its output to a
per-verification directory. It parses structured JUnit XML or pytest JSON,
rejects pre-existing output files, refuses empty or all-skipped test runs and
refuses inconsistent counts. The result is bound to the current commit and the
approved command policy.

A test command succeeds only when:

- the command is on the typed test-runner allowlist;
- the exit code is a real integer `0`;
- the parsed evidence is consistent;
- no code mutation invalidated earlier checks;
- the merged tree is clean at the gate.

A green test command does not prove full product correctness.

### File ownership and fencing

Write scopes are canonicalized before comparison. Symlink parents and case
aliases are resolved so two tasks cannot claim the same file through different
spellings. The ownership state machine uses an owner token and a fence
compare-and-swap on every transition. A stale heartbeat is `unknown`, not dead.
Unknown ownership blocks transfer and requires an explicit recovery record.

Recovery records `liveness: unknown`, `stop_ack: false` and
`native_cancel_ack: false`; it never fabricates a native cancellation.

**Limit:** ownership fencing is a coordination control. It does not stop a
process that ignores the protocol or a user with the same OS permissions.

### Installer safety

Both installers:

- refuse to overwrite an existing file whose content differs, unless the
  manifest proves it is installer-managed or `--force-generated` is explicit;
- back up generated files before a forced replacement;
- guard destination paths against symlink traversal;
- preserve user-owned hooks and inject only their own managed groups;
- record managed files for safe uninstall;
- fail closed on invalid or conflicting hook configuration.

The global installer does not read, rewrite or delete provider credentials,
session history or unrelated OpenCode settings. It creates only the files needed
for Auto-Orchestrator and leaves existing files alone.

### Dashboard

The dashboard binds to loopback and is read-only. It never starts, stops or
messages an agent, claims a task, merges a worktree or writes evidence. It
serves local project data to a local browser; it does not upload telemetry.
Run it on a trusted machine and do not expose the loopback port through a proxy
or tunnel.

### Telemetry and privacy

Run events contain timestamps, opaque agent/run IDs, platform, status and
verification facts. They do not contain prompts, source code, commands, tool
output, API keys or raw native IDs. Native IDs are hashed before persistence.
The optional OpenTelemetry adapter exports only whitelisted attributes and is a
no-op when OpenTelemetry is not installed.

Do not commit `.auto-orchestrator/` to Git. It contains local run logs, SQLite
state and possibly the verification key.

## Trust levels

| Claim | Required evidence |
|---|---|
| A native span started/stopped | Matching native event IDs from the client. |
| A managed run completed | Real runner events with a terminal state. |
| Tests passed | A run-bound parsed test result with a real exit code. |
| Completion verified | Valid HMAC receipt, matching bindings, intact artifact, clean merged tree. |
| Review was independent | `declared` or `accepted` role evidence; `observed` is reserved. |

`declared` review independence is not a cryptographic attestation. A caller can
create a review record; only the absence of contrary evidence and the recorded
role make it useful.

## What this is not

- Not an OS sandbox or container.
- Not a multi-user permission system.
- Not a provider credential store.
- Not a guarantee that a vendor client followed the dispatch protocol.
- Not a benchmark of token, cost or human time savings.
- Not proof that an unknown process did not modify files.

## Reporting a vulnerability

Open a private security advisory on the GitHub repository or contact the
maintainer directly. Do not include API keys, session tokens or proprietary
source code in a public issue. Include the affected version, a minimal
reproduction and whether the issue requires local code execution.

## Related documents

- [Architecture and trust levels](ARCHITECTURE.md)
- [V8.0 native dispatch](V80_NATIVE_DISPATCH.md)
- [Capability matrix](CAPABILITY_MATRIX.md)
- [OpenCode V2 integration](OPENCODE_V2.md)
