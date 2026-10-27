# V7.6.1 — Assumption Guard, Verified Requirements and Cross-IDE Continuity

V7.6.1 keeps the V7.6 Teamwork/Supervisor/milestone/worktree/recovery/Virtual
Office behavior and closes the reliability gaps around unverified assumptions,
approved decisions and IDE changes.

## 1. Central Assumption Guard

Every new development start now runs `tools/assumption_guard.py` before a plan,
active owner record or runner is written:

1. Read project memory and the durable Decision/Requirements Ledger.
2. Scan bounded repository code/config facts.
3. Detect bilingual (TR/EN) critical topics: authentication, authorization and
   roles, sharing/access, storage, database/migration, destructive operations,
   external data transfer, breaking API changes and payments.
4. Detect contradictions between an explicit request choice and code/confirmed
   decisions.
5. Allow clear, small tasks without an interview.
6. Reuse or create a persistent Grill-Me interview for unresolved critical
   topics. No plan or active state is written on refusal.
7. `off` still blocks critical ambiguity; `required` always interviews;
   `plan-only` produces a plan without starting code.

The guard is enforced at multiple boundaries, not only in the CLI:

- `universal_teamwork.py start`
- `teamwork_chat.py start`
- `TeamworkEngine.init` for any new plan carrying a `request`
- `TeamworkRunner` before dispatch
- `teamwork_chat.py resume` through the plan contract/decision check

The guard returns a deterministic receipt (`request_hash`, `head_sha`,
`context_digest`, status, interview id, explicit assumptions). A plan cannot
claim that it was guarded without this receipt or an approved contract.

```bash
python3.11 tools/universal_teamwork.py --root . --platform opencode \
  start --request-file <request>
# May return {"status":"interview_required", "question": ...}.
# No plan/runner is created in that case.
```

## 2. Smarter Grill-Me

`tools/grill_me.py` now delegates question generation to the same deterministic
rules used by the guard, so the decision to interview and the question asked do
not drift apart.

- Token-boundary matching prevents `auth` matching `author`; Turkish dotted `İ`
  and common TR/EN equivalents are normalized.
- Superseded decisions no longer suppress a new question.
- Repository evidence is split into `code_detected` and `docs_detected`; only
  code/config evidence can resolve a critical technology choice. README,
  comments, docstrings and test expectations do not prove production usage.
- Contradictions ask a conflict question instead of silently choosing.
- Questions are risk ordered and dependency-gated.
- Dynamic follow-ups cover JWT refresh/logout, cookie session expiry, database
  rollback, API deprecation and file deletion policy.
- Recommendations are still never recorded as user answers.

## 3. Verified Requirements Contract

On interview approval, the engine publishes a digest-bound, versioned
`.auto-orchestrator/contracts/rc_<id>-v<N>.json` and updates `current.json`.

The contract contains:

- approved decisions with ledger references,
- constraints,
- out-of-scope items,
- acceptance criteria with phase/task applicability,
- code-evidence or allowed test-command evidence,
- blockers,
- verification strategy and contract/source versions.

`plan_request(..., requirements_contract=...)` injects bounded decisions and
constraints into build prompts and acceptance criteria into tester/reviewer
tasks. The full contract and compact `contract_ref` survive validator and
snapshot round trips.

Verification is deliberately conservative:

- `code_evidence` is checked against real code/config facts after integration.
- `command` evidence is accepted only from an allowed test runner
  (`python -m unittest`, `pytest`, `node --test`, `npm test`, `go test`,
  `cargo test`, `make test/check`), never arbitrary `python -c` or shell code.
- `not_automated` criteria are reported as `not_verified`, never as passed.
- A changed/superseded decision, missing interview approval or blocking
  requirement invalidates the contract and fails the runner gate.
- A contract for a different feature/interview does not invalidate an unrelated
  run; only a newer version of the same contract supersedes it.

The run summary exposes `requirements_contract` and `quality_gate.contract`.
Unverifiable criteria remain visible instead of being converted into success.

## 4. Cross-IDE memory and handoff

`tools/project_memory.py` now exports/imports:

- decisions and requirements,
- handoffs with parsed payloads,
- normalized Git observations,
- interviews and questions (summary/hash plus structured answers),
- safe meta, source instance, truncation and bundle digest.

Import is preview-first and fail-closed:

- `add` / `unchanged` / `conflict` classification with field-level reasons,
- ID and natural-key conflicts, duplicate IDs, missing `supersedes`,
- transactional apply; apply is aborted on conflicts,
- `last_platform` is never overwritten by import,
- obvious secrets in exported free text and sensitive keys are redacted; a
  redacted handoff digest is recomputed so integrity remains checkable.

Stale detection now reports `head_mismatch`, `branch_mismatch`,
`dirty_worktree` and `missing_head`. Context files include open tasks, active
interview, contract pointer and latest verification.

An opt-in Git post-commit hook is available:

```bash
python3.11 tools/universal_teamwork.py --root . memory install-hook
python3.11 tools/universal_teamwork.py --root . memory hook-status
python3.11 tools/universal_teamwork.py --root . memory uninstall-hook
```

The hook preserves existing user hooks, has explicit begin/end markers, never
interpolates repository text into a shell command, and only calls the local
capture command. Platforms without hook support keep the explicit
`memory capture` fallback.

## 5. IDE support boundaries

| Platform | V7.6.1 status |
|---|---|
| OpenCode V2 | `/teamwork`, `/grill-me`, managed execution, context and contract load. TUI click-through is not automated. |
| Codex | Shared skill plus real app-server managed execution. Read-only is the default. `--provider codex --codex-sandbox workspace-write` is an explicit opt-in and was exercised by the real Codex feature E2E. Approval bridging is not implemented. |
| Cursor | Shared skills/CLI contract; no managed adapter. No local Cursor binary was available for a live run. |
| Antigravity | Shared skills/CLI and native `/grill-me` bridge guidance. No managed adapter. No local Antigravity binary was available for a live run. |

No IDE silently launches another IDE's process. A platform change is a new
session that inherits verified durable state; it does not pretend the previous
native conversation or child process still exists.

## 6. Dashboard

The Task Board Project Memory panel shows confirmed/derived decisions, blocking
requirements, active interview/question, approved contract version, last IDE,
last milestone, handoff with an evidence-derived verified flag, stale reasons
and latest verification. The Canvas office is unchanged; absence of native
activity is not animated as work.

## 7. Evidence

- `docs/assets/v761-guard-e2e.json` — A/B/D deterministic real CLI evidence:
  ambiguous sharing opens `file_access` with no plan; cookie-only code fails a
  confirmed JWT contract; JWT code evidence passes; Codex→OpenCode interview
  resume keeps answers.
- `docs/assets/v761-contract-rejection.json` — real Codex implementer produced
  only an access token while the approved contract required refresh; the tester
  rejected it and no handoff was created.
- `docs/assets/v761-cross-ide-e2e.json` — real Codex workspace-write feature,
  handoff, OpenCode context load and real OpenCode second feature; same project
  id, two handoffs, contract retained.
- `docs/assets/v76-memory-e2e.json` — earlier V7.6 memory/guard evidence.

## 8. Known limitations

- No live Cursor or Antigravity IDE run; only shared contracts/skills.
- Codex approval bridging and native cancellation acknowledgement are not
  implemented.
- The guard is deterministic token/phrase analysis, not general semantic NLU;
  unusual wording can require an explicit critical topic.
- Contract criteria without executable code/test evidence remain
  `not_verified`; they are not converted into passes.
- Handoffs, observations and interviews are exported, but a cloud sync service
  is not implemented.
- Plan-only output is not automatically resumable as a runner; it is a reviewed
  plan that can be started as a new run.
- The dashboard reads the contract pointer; full contract verification remains
  a runner responsibility.
