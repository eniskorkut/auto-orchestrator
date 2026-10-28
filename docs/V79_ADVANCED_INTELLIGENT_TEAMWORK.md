# V7.9 — Advanced Intelligent Teamwork

This document is the V7.9 umbrella. Sections other than Grill-Me are
placeholders owned by their respective changes.

## 1. Overview

V7.9 hardens the V7.8 sequential handoff and adds the missing Teamwork,
verification and continuity layers:

- **Teamwork 3.0 plan artifact** — `scope_contract`, `file_ownership`,
  `ownership_conflicts`, `integrity_policy` and `adaptive_decision` with the
  AUTO risk score.
- **Continuity brief** — a read-only, bounded resume summary for a new IDE.
- **Verified Completion 3.0 evidence** — `native-verify` runs the approved test
  command itself, consumes JUnit XML/pytest JSON, binds commit/contract/run and
  enforces a merged-tree gate. `native-audit` records a declared-role review.
- **Grill-Me 4.0** — answer edits invalidate only the affected branch, preserve
  history and reopen applicable follow-ups.
- **Runtime capability detection** — installed/version/auth probes per IDE with
  `null` for anything unproven.
- **Benchmark integrity** — a 12-task corpus that refuses to measure without a
  prepared project and never fabricates token/cost numbers.

The V7.8 ownership invariant is unchanged: one project, one owner, one fence,
release before accept, explicit idle confirmation, no IDE launching another.

## 2. Grill-Me decision invalidation and edit flow

Grill-Me now has an explicit edit history instead of silently overwriting an
answered question. The behavior is implemented in `tools/grill_me.py` with
additive provenance support in `tools/project_memory.py`.

### Answer changes

`answer()` accepts an optional `reason` (also exposed as `change_reason`,
maximum 500 characters). When the same `question_id` is answered again after
`back()` — or answered directly with a different option/text — the engine
compares the new answer with the newest recorded answer and reports
`changed: true`. Repeating the identical answer is idempotent: no decision or
requirement is invalidated and the existing confirmed decision is reused.

On a real change the engine:

- marks only decisions created from that question (`source_ref
  question:<question_id>`) as `invalidated`; the row is retained, never deleted,
  and `confirmed_by` / `confirmed_at` history is preserved;
- marks requirements whose `source_ref` is the question or one of those
  decisions as `invalidated` when their category/status was `confirmed` or
  `unresolved_*`;
- sets the interview back to `open` before re-settling it to `open`, `blocked`
  or `ready` from the remaining questions;
- reopens answered or skipped dependents whose `depends_on` contains the
  changed question key, invalidating their old decisions so they cannot count
  as confirmed while open;
- invalidates a dependent branch that cannot apply under the new answer (for
  example `jwt_refresh` after switching `auth_session` from JWT to a session
  cookie) instead of asking it; `session_expiry` is the question that is offered
  instead;
- propagates through further dependents with a bounded walk (no cycles, one
  visit per question key).

Unrelated decisions and requirements are untouched. A dependent branch that
becomes applicable again (switching back to cookies) is reopened with its old
answer retained only as history.

### Back and history

`back(question_id, reason=...)` reopens the question, clears its active answer
fields and appends a `back` audit event containing the old option/text. The
decision owned by that question is marked `invalidated` so it can never remain
in the active list while the question is open; re-answering with the identical
option/text re-confirms the same decision row (preserving `confirmed_by` and
`confirmed_at`) and records `decision_reconfirmed` / `requirement_reconfirmed`
audit events. A different answer retires the old row and creates the new one.

`summary()` exposes the same data through:

- `history` — bounded chronological audit events;
- `reopened_questions` — open questions that have a previous answer or a
  `back` event, including `previous_answer`;
- `invalidated_questions` — invalidated questions with
  `invalidated_reason`;
- `decisions` / `active_decisions` — active decisions only, so an invalidated
  decision is never reported as confirmed;
- `invalidated_decisions` — retained invalidated decisions;
- `all_decisions` — the full interview-linked decision set for inspection.

`answer()`, `back()` and `resume()` results also carry
`reopened_questions`, `invalidated_questions` and `invalidated_decisions` when a
change report is available. `next_question()` attaches a bounded
`change_report` to the returned question when one exists, and always returns the
next applicable question, never an invalidated branch.

### Approval and contracts

`approve()` still refuses while any critical question is `open` or `skipped`,
including after an invalidation. Once all remaining questions are answered it
retires invalidated questions as superseded and builds the contract from
confirmed decisions only; invalidated decisions are excluded by the existing
requirements-contract builder. Resumed cross-IDE sessions (`resume()` from
another IDE engine) return the exact open question id/key.

### Migration and audit trail

No destructive migration is required. V7.9 adds one append-only SQLite table,
`grill_audit`, through `CREATE TABLE IF NOT EXISTS` in the memory schema. It is
created lazily for v1/v2 databases and stores:

- `answer` / `answer_changed` / `skipped` / `back` events;
- `question_reopened` / `question_invalidated` events;
- `decision_invalidated` / `decision_reconfirmed` and
  `requirement_invalidated` / `requirement_reconfirmed` events;
- the previous/new status, option and text plus the bounded change reason.

`invalidated` is now a first-class decision and requirement status for export
and import. The audit table is local provenance and is not included in portable
memory export bundles.

### Limitations

- Answers recorded by builds before this change have no audit row, so the first
  re-answer after upgrading cannot be compared with the pre-upgrade answer.
- The audit table is bounded in reads (500 rows per interview query, 100 rows
  returned in a change report) and is not exported/imported.
- Code/config evidence is never invalidated by an answer edit; only user
  decisions and requirements owned by the edited question or its dependents are
  affected.
- Approval converts invalidated question rows to the existing `superseded`
  status, so post-approval summaries show them as retired rather than
  invalidated.
- The `grill answer`/`grill back` CLI exposes `--reason`; Python callers can
  also pass `reason=`/`change_reason=` to the engine directly.
- Invalidated questions are retired and no longer count as handoff blockers;
  open/skipped critical questions still do.

## 3. Native and managed execution

### 3.1 Plan artifact v2

`tools/teamwork_planner.py` now emits `plan_version: 2` with:

- `scope_contract`: objective, in-scope paths, non-goals, requirements,
  deliverables, rollback and contract constraints.
- `file_ownership`: task id, role, milestone, write scope, dependencies and
  `exclusive: true`.
- `ownership_conflicts`: same-milestone tasks whose write scopes overlap
  without a dependency path. A plan with a real conflict is rejected when
  `max_parallel > 1`, so two implementers cannot write the same file.
- `integrity_policy`: `development`, `demo` or `benchmark`. Only
  `development` can certify production completion; `demo`/`benchmark` keep the
  evidence but report `verified: false` with a reason.
- `adaptive_decision`: requested/chosen mode, score, confidence, factors,
  safety upgrade and AUTO floor rationale.

`validate_teamwork_plan` carries these fields through the engine snapshot and
rejects unknown integrity policies. The CLI exposes
`plan/start --integrity-policy`.

### 3.2 Continuity brief

`universal_teamwork.py brief` is read-only (no identity/DB creation) and returns
active/previous IDE, ownership, run/milestone/task, completed and pending tasks,
failed tests, open Grill-Me question, next action, requirement counts,
confirmed decisions, the latest native verification and explicit
`resume_steps`. A foreign owner can still read it; a mutation still requires the
owner credential and an explicit handoff.

### 3.3 Native verification and structured evidence

`native-verify` is the verifier, not the agent:

1. load the approved native plan and its requirements contract;
2. run the approved test command with `subprocess` and capture the real exit
   code;
3. optionally parse JUnit XML or pytest JSON (`tools/test_evidence.py`),
   rejecting DOCTYPE/ENTITY, oversized, symlinked and malformed evidence;
4. bind HEAD, contract id/version/digest, run/task/attempt and changed files;
5. feed `completion_engine.evaluate_completion` with the real test result and
   any recorded reviews;
6. write `.auto-orchestrator/verification/<run>-<task>-*.json` plus a project
   memory observation.

A dirty worktree or a test command that leaves changes behind forces
`verified_completion: false` (merged-tree gate). A native self-report alone
stays `verified: false`. `native-audit` records a reviewer verdict bound to the
same HEAD; independence is labelled `declared_role_separation`, not a
cryptographic attestation.

Structured results with a failure while the command exited 0 are refused as
inconsistent.

### 3.4 Capability detection

`python tools/capabilities.py show --detect --json` probes the local machine
without reading credential stores or retaining raw output. On 2026-10-28
(macOS):

| IDE | CLI / version | authenticated | Label |
|---|---|---|---|
| Codex | `codex 0.164.0` | `true` (`codex login status`) | live handoff evidence |
| OpenCode | `opencode v2.1.1` | `true` (`opencode auth list`) | live handoff evidence |
| Cursor | `/Applications/Cursor.app/.../cursor agent`, `2026.10.22` | `false` — “Authentication required” | blocked, not contract-only-without-CLI |
| Antigravity | app only, no CLI | `null` | blocked; native commands vendor-authoritative |

Evidence: `docs/assets/v79-capability-detection.json`. Detection cannot prove a
hook/plugin was loaded, a native child was spawned or stopped, or a handoff
completed inside the IDE; those fields stay `null`.

### 3.5 Benchmark corpus

`tools/v79_benchmark.py` defines 12 tasks across bugfix, backend, frontend
integration, authentication, migration, API compatibility, concurrency, repair,
multi-module, security, ambiguous requirements and handoff continuation.
`--validate` checks the corpus, `--list` emits it, and `--run` requires
`--project` with a prepared fixture. It records only real exit codes and wall
time; token and cost fields are absent, not zero.

## 4. Dashboard and operator visibility

The ownership panel keeps the V7.8 fields and now reads two real artifacts:

- `verification`: latest native verification (`verified`, aggregate status,
  commit, exit code, merged-tree flag, timestamp);
- `integrity`: latest plan (`integrity_policy`, chosen mode, risk score,
  objective, plan version).

Missing, oversized or invalid artifacts render as `unknown`; no value is
fabricated. Owner tokens are never exposed, host/session are hashes, and
`native.connected` stays `null` without client evidence.

## 5. Validation

Local results on the V7.9 branch (macOS, Python 3.11.15):

| Suite | Result |
|---|---|
| Python `unittest discover` | 819 tests OK (1 skipped) |
| Node (`npm run test:js`) | 96/96 |
| Playwright functional (`AO_SKIP_VISUALS=1`) | 22 passed, 4 visual snapshots skipped |
| Simulated V7.8 handoff E2E | 9/9 scenarios |
| Live Codex → OpenCode → Codex handoff | passed; `docs/assets/v79-live-handoff.json` |
| Capability detection | real local probe; `docs/assets/v79-capability-detection.json` |
| Benchmark corpus validation | 12 tasks, 12 categories, no errors |

Targeted Grill-Me validation:

```
python3.11 -m unittest tests.test_grill_me tests.test_v79_grill \
  tests.test_assumption_guard tests.test_requirements_contract -q
```

Targeted V7.9 validation:

```
python3.11 -m unittest tests.test_v79_plan tests.test_v79_brief \
  tests.test_v79_test_evidence tests.test_v79_benchmark \
  tests.test_v79_capability_auth tests.test_v79_dashboard \
  tests.test_v78_native_verify -q
```

Cross-platform CI (Windows/macOS/Linux, Python 3.11-3.13, Node, browser) is
recorded on the V7.9 pull request; local results above are not a substitute for
it.

Targeted Grill-Me validation for this section:

```
python3.11 -m unittest tests.test_grill_me tests.test_v79_grill \
  tests.test_assumption_guard tests.test_requirements_contract -q
```
