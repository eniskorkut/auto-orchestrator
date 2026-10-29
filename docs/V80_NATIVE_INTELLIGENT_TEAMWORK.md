# V8.0 — Native Intelligent Teamwork and Grill-Me

V8.0 turns the V7.9 verified-evidence foundation into a typed native dispatch
and continuity layer. The owning IDE's parent session still performs native
dispatch; Auto-Orchestrator records the contract, lifecycle and evidence and
refuses to certify states that were not observed.

## 1. What is new

### Native dispatch lifecycle

`tools/native_dispatch.py` stores one append-only JSON record per dispatch
under `.auto-orchestrator/native-dispatches/<run>/<dispatch>.json`:

```
queued → dispatch_requested → dispatch_confirmed → running
running ⇄ waiting_for_permission
running → completed_claimed → output_collected → verification_pending → verified
any active → failed | cancelled
any active → recovery_required
```

- `running` requires a parent-provided native child reference; a dispatch
  request is not a running child.
- `completed_claimed` is not completion; `collect` requires the artifact and
  records its SHA-256.
- `verify` requires a matching `native-verification` record (`verified: true`,
  same run/task/verification_run_id) and advances only then.
- `recover` records `recovery_required`, `stop_ack: false` and
  `native_cancel_ack: false`; unknown lifecycle never becomes complete.
- `create` enforces one active dispatch per task, at most three active,
  disjoint write scopes and only verified dependencies. Concurrent creates are
  serialized by the existing project lock.

The CLI exposes `native-dispatch create|request|confirm|start|permission|
complete|collect|verify|fail|cancel|recover|status`; `status` is read-only.
`native-verify --dispatch-id` advances the dispatch only when the verification
record is genuinely verified.

### Question quality (Grill-Me 5.0)

`tools/question_quality.py` computes deterministic relevance, information
gain, redundancy, risk coverage and user burden for each question, and ranks
eligible questions. Critical questions are never dropped; optional low-gain
questions can be filtered by adapters. `summary()` exposes the scores. The
score is advisory metadata, not a user confirmation.

### Continuity

The read-only `brief` now includes native dispatch counts (active, verified,
unknown) in addition to ownership, run/milestone/task, next action, failed
tests, interview state and verification. Unknown dispatches are an explicit
resume blocker.

### Verification and review

V7.9's run-bound test evidence is preserved: a per-verification output
directory, post-run parsing, typed test-runner allowlist, pre-existing-file
rejection, empty/skipped test refusal, merged-tree gate and demo/benchmark
non-certification. `native-audit` review evidence separates `declared`,
`accepted` and (reserved) `observed` independence; declared reviews never
satisfy `review_required`.

## 2. Ide matrix

| IDE | Teamwork entry | Native dispatch | Child lifecycle | Handoff | Label |
|---|---|---|---|---|---|
| Codex | skill + agents + CLI | typed dispatch records | vendor-observed only; `unknown` if unobserved | live-verified (V7.8/V7.9) | live handoff; child lifecycle not-tested |
| OpenCode V2 | `/teamwork`, `/grill-me`, plugin | typed dispatch records | foreground tool spans only | live-verified | live handoff; background child completion unsupported |
| Cursor | agents + skill + agent CLI | protocol ready | hooks not verified | simulated | blocked-auth |
| Antigravity | native `/teamwork-preview` + `/grill-me` | bridge only | unsupported | simulated | contract-only |

No IDE is launched by another IDE. Native child identity is never invented.

## 3. Validation

- Python: 849 tests OK (1 skipped) at the V8.0 feature commit; clean-install
  regression tests were added afterwards.
- Node: 96/96.
- Playwright functional: 22 passed, 4 visual snapshots skipped.
- Dispatch tests: happy path, no-child start refusal, duplicate/overlap,
  dependency gating, forged verification, missing output, recovery,
  concurrent create, CLI round trip, native-verify integration.
- V7.9 live handoff: `docs/assets/v79-live-handoff.json`.
- Capability probe: `docs/assets/v79-capability-detection.json`.

CI runs the existing Ubuntu/Windows/macOS × Python 3.11–3.13 matrix, Node and
browser jobs. V8 CI results are reported on its pull request.

## 4. Limits

- The dispatch protocol is advisory; a vendor tool can ignore it.
- Native child creation/completion/stop is not observable through a
  vendor-neutral API here. Those fields stay `unknown` unless the parent
  supplies an observed reference; this is not a second supervisor.
- Cursor execution is blocked by CLI authentication on this machine.
- Antigravity has no CLI adapter; native commands remain vendor-authoritative.
- Token/cost and cross-mode benchmark measurements are not fabricated.
- `declared` review independence is role-level evidence, not a cryptographic
  attestation.
