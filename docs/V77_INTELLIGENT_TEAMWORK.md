# V7.7 — Intelligent Teamwork, Verified Completion and Native IDE Integration

V7.7 keeps the V7.6.1 Supervisor, milestone DAG, managed execution, recovery,
worktrees, Assumption Guard, Grill-Me, contract and memory layers. It adds a
stricter completion model, SOLO/AUTO coordination, selected project context and
platform-native entry points.

## 1. Verified Completion Engine 2.0

Contracts built after V7.7 use `schema_version: 2`. V1 contracts remain
byte-digest compatible and retain their legacy partial verification semantics.

Each v2 criterion carries:

- requirement id and source decision id;
- critical/review-required flags;
- implementation files;
- behavioral `test_cases` with stable `test_id`, expected behavior and
  positive/negative marker;
- a contract digest and a real test command.

The engine evaluates three levels:

| Level | Evidence |
|---|---|
| Implementation | changed files, commit, write scope, real implementation presence |
| Behavioral | real test exit code plus test IDs parsed from runner output |
| Conformance | code evidence, decision freshness and independent review verdict |

Criterion states: `not_started`, `implemented_unverified`,
`partially_verified`, `needs_review`, `verified`, `failed`, `blocked`.

`milestone passed` and `requirements verified` are separate fields. A milestone
can pass while the product remains `needs_review`; critical unverified criteria
are visible in `critical_unverified` and `verified_completion` stays false.
Failed/blocked critical criteria demote the run. A run status may remain
`passed` while requirements stay open; this is an explicit separation, not a
claim that the product is verified.

Test IDs are generated only from approved decisions: JWT access/refresh/
invalid-refresh/expiry, cookie session/expiry/logout, migration/rollback and
file-access owner/unauthorized/delete. Generic decisions without executable
behavior remain `not_verified`, not successful.

The runner parses actual command output (stdout+stderr), preserves signals,
binds run/task/attempt/commit/contract digest, and writes
`completion-verification` evidence. Agent prose is never accepted as test
evidence.

## 2. Intelligent Grill-Me 2.0

The deterministic Assumption Guard remains authoritative. Interview questions
now include why, recommended rationale, alternatives, affected requirements,
critical flag and dependency. These are stored in new nullable question columns
with a v1 database migration and remain export/import-compatible.

An optional LLM seam (`tools/grill_llm.py`) is disabled by default. It accepts
only strict JSON, allowed topics/option ids, exact normalized evidence quotes,
bounded size/time and no unknown fields. Invalid or injected output falls back
to deterministic questions; model output can never create a confirmed decision.

The 40-case TR/EN benchmark (`docs/assets/v77-grill-benchmark.json`) measures
ambiguity recall, unnecessary/duplicate questions, false technology inference,
contradiction detection, unconfirmed assumptions and completion. The committed
result is the deterministic baseline; optional provider execution is explicitly
`not_run` when no provider is configured.

## 3. SOLO, FAST, BALANCED, RIGOROUS, AUTO

- **SOLO:** one implementer task, one milestone, `max_parallel=1`, deterministic
  verification commands, no reviewer/auditor.
- **FAST:** small implementation plus targeted verification.
- **BALANCED:** implementation, tester and risk-based review/audit.
- **RIGOROUS:** explorer, separated build, tester, critic, challenger, auditor,
  success auditor.
- **AUTO:** repository profile + risk topics + task shape. Authentication,
  payments, data retention and database work floor at BALANCED; migration/API
  compatibility with an approved contract floor at RIGOROUS. High-risk explicit
  SOLO/FAST is upgraded.

A small bugfix SOLO run and 12 planning cells are recorded in
`docs/assets/v77-mode-benchmark.json`. Only the measured SOLO cell contains real
wall/time/tokens; the other cells are planning-only and are not quality or
savings comparisons.

## 4. Context selection

`tools/context_selector.py` scores confirmed decisions, relevant handoff,
blockers, failed test observations, recent changes and the current contract
against the request. It returns bounded items with provenance refs and token
estimate. The runner prompt uses this selection instead of dumping the whole
ledger. The guard receipt digest is not changed by display-only selection.

## 5. Native IDE entry points

The architecture rule is explicit: one IDE never launches another IDE's agent
process to continue a run.

| IDE | Entry point | Boundary |
|---|---|---|
| OpenCode V2 | `/teamwork`, `/grill-me`, project skill, plugin lifecycle | managed OpenCode adapter; plugin lifecycle is observed, native child completion is not claimed |
| Codex | native teamwork skill, `.codex/agents`, app-server adapter | native parent session policy; managed workspace-write is explicit opt-in; approval bridging absent |
| Cursor | `.cursor/agents`, shared skill/CLI | native subagent contract; no managed adapter/live run available |
| Antigravity | native `/teamwork-preview`, native `/grill-me`, `ao-grill-me` bridge | our layer stores memory/contracts/handoffs; native Teamwork remains authoritative; no duplicate supervisor |

Cursor/Antigravity files are contract-installed, not live-IDE verified. Codex
per-agent sandbox overrides are not claimed; the parent session owns that policy.

## 6. Dashboard

The Task Board/office inspector shows:

- SOLO/FAST/BALANCED/RIGOROUS/AUTO and legacy labels;
- execution provider plus native/managed/native-planned distinction;
- contract and requirements status separately;
- `fully_verified`, critical unverified/not-verified warnings;
- active interview id/status/age;
- bounded handoff history and last IDE/milestone;
- measured tokens/cost with explicit unknown, never zero;
- stale, recovery, task/evidence/contract inspector data.

The Canvas 2D office remains; absent native activity is not animated as work.
Demo screenshots remain labelled `DEMO DATA`.

## 7. Evidence

- `docs/assets/v77-solo-e2e.json` — real SOLO bugfix, one task, tests passed,
  worktree integrated into the root, handoff created.
- `docs/assets/v77-completion-e2e.json` — access-token-only implementation
  rejected, repaired standard JWT implementation verified by a real read-only
  Codex review and real unittest signals.
- `docs/assets/v77-cross-ide-e2e.json` — real Codex feature + handoff, then real
  OpenCode feature; same project id, two handoffs, both requirements completion
  reports verified (the legacy static contract field remains partial where the
  old code-evidence checker is less precise).
- `docs/assets/v77-guard-e2e.json` — ambiguous sharing request blocked with no
  plan/active runner; JWT contract and Codex→OpenCode interview resume.
- `docs/assets/v77-grill-benchmark.json` — 40-case deterministic benchmark.
- `docs/assets/v77-mode-benchmark.json` — planning cells and measured SOLO cell.
- `docs/assets/v77-dashboard.png` — DEMO DATA dashboard with SOLO, managed
  provider, partially verified contract, requirements review warning and
  measured token/cost chips.

## 8. Known limitations

- Cursor/Antigravity live sessions were not run; their entry points are
  contract-only.
- OpenCode native subagent completion is not authoritatively observed by the
  plugin; managed child sessions remain separate.
- Codex approval bridging/native cancellation ACK remains absent.
- The optional LLM provider was not executed for the full benchmark; the report
  shows `provider.status = not_run`.
- Only one SOLO cell has a real agent measurement; other mode cells are
  planning-only.
- Completion test IDs require the implementation to emit real test names; a
  missing test remains open rather than being inferred from prose.
