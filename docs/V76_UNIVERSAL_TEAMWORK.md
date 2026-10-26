# V7.6 — Universal Teamwork, Cross-IDE Memory and Grill-Me

V7.6 keeps the existing Supervisor, milestone DAG, managed OpenCode execution,
Codex app-server adapter, worktrees, recovery and Virtual Office. It adds a
portable project identity, durable project memory, a persistent Grill-Me
interview engine, and a common command surface across IDEs.

## 1. Universal Teamwork contract

Common command surface:

```bash
python3.11 tools/universal_teamwork.py --root . --platform <platform> start --request-file <file>
# Codex read-only managed execution:
python3.11 tools/universal_teamwork.py --root . --platform codex --provider codex start --request-file <file>
python3.11 tools/universal_teamwork.py --root . --platform <platform> status
python3.11 tools/universal_teamwork.py --root . --platform <platform> agents
python3.11 tools/universal_teamwork.py --root . --platform <platform> milestones
python3.11 tools/universal_teamwork.py --root . --platform <platform> context
python3.11 tools/universal_teamwork.py --root . --platform <platform> handoff
python3.11 tools/universal_teamwork.py --root . --platform <platform> pause
python3.11 tools/universal_teamwork.py --root . --platform <platform> cancel
python3.11 tools/universal_teamwork.py --root . --platform <platform> resume
python3.11 tools/universal_teamwork.py --root . --platform <platform> grill ...
```

Execution follows the active platform instead of silently switching IDEs:

- `--platform opencode` uses the existing managed OpenCode adapter.
- `--platform codex` requires an explicit `--provider codex`. The plan then uses
  Codex tasks with `sandbox=read-only` and `approval_policy=never`; Codex write
  mode and approval bridging remain unverified and are not claimed.
- `--platform cursor` and `--platform antigravity` have no managed execution
  adapter in this repository. Their native agents/skills run the work; starting
  the OpenCode bridge from them requires an explicit `--provider opencode`
  opt-in. Plain `start` refuses instead of launching another IDE's process.
- An unknown/local CLI origin keeps the historical `opencode` default.

A platform change is a new session that inherits verified durable state, not a
running child session.

Shared execution metadata is persisted in the existing plan/Supervisor state and
project memory: `project_id`, `run_id`, `milestone_id`, `task_id`, `attempt_id`,
`platform`, `execution_mode`, `agent_role`, `status`, `acceptance_criteria`,
`evidence_refs` and handoff version/provenance.

## 2. Portable project identity

`.auto-orchestrator/project.json` contains only:

- schema version
- random `project_uuid`
- stable `project_id` (`p_<16 hex>`)
- id scheme (`uuid` or `legacy`)
- human project name and creation time

It contains no prompts, source, secrets or absolute machine paths and can be
committed. `.auto-orchestrator/instance.json` is local-only and distinguishes a
copied working instance. The SQLite database and run evidence are not committed.

Migration behavior: an existing registry project can be given its old
`p_<16 hex>` id with `ensure_project_identity(..., legacy_project_id=...)`; the
manifest records `id_scheme: legacy` so the id survives a move. New projects use
the UUID-derived id.

## 3. Project memory and Decision Ledger

`tools/project_memory.py` uses `.auto-orchestrator/memory.sqlite3` and projects
sanitized snapshots into:

```
.auto-orchestrator/context/
├── project-summary.md
├── architecture-decisions.json
├── requirements.json
├── recent-changes.json
├── open-tasks.json
└── current-handoff.json
```

Decision metadata:

`decision_id`, `project_id`, `topic`, `decision`, `rationale`, `alternatives`,
`source`, `source_ref`, `confirmed_by`, `confirmed_at`, `status`, `supersedes`,
`affected_tasks`, `updated_at`.

Statuses: `confirmed`, `code_derived`, `unresolved`, `superseded`, `rejected`.

A `confirmed` record requires a real confirmation identity. Values such as
`recommended`, `agent`, `auto` and `system` are rejected. Code-derived facts are
recorded as `code_derived`, never promoted silently. Superseded decisions remain
queryable.

Requirements carry the distinction between user-confirmed, code-derived,
documentation constraints, unresolved blocking/nonblocking and conflicting
items. Context files include provenance and a Git HEAD snapshot so stale memory
can be detected.

## 4. Grill-Me engine

`tools/grill_me.py` implements a persistent one-question-at-a-time interview:

- starts only after inspecting project memory and bounded repository facts;
- asks critical questions for authentication, sharing/access, storage,
  database/migration and breaking API changes;
- never treats the recommended option as selected or confirmed;
- supports answer, skip, back, cancel, resume, summary, approve;
- dependent questions are invalidated when an earlier answer changes;
- skipped critical questions stay `unresolved_blocking` and prevent approval;
- `off` does not bypass a critical question, `required` adds a scope question,
  `plan-only` stops before code;
- interview state is in project memory, so another IDE can resume it.

## 5. IDE integration status

| Platform | V7.6 status |
|---|---|
| OpenCode V2 | Real command contract: `/teamwork`, `/grill-me`; real managed adapter and CLI bridge. TUI click-through not automated. |
| Codex | Shared skill and universal CLI; V7.5 app-server adapter preserved. `$grill-me`/skill invocation depends on the installed Codex client; no IDE-native claim. |
| Cursor | Shared `.agents/skills`/grill-me skill and universal CLI are shipped; no local Cursor binary was available for a live test. |
| Antigravity | Shared `.agents/skills` and universal CLI are shipped. Native `/grill-me` remains the platform UI; a bridge imports decisions into the same memory. No local Antigravity binary was available for a live test. |

Native, managed, contract-tested and simulated evidence remain separate. No
cross-provider agent dispatch is implemented or claimed.

## 6. Cross-IDE memory evidence

Deterministic CLI E2E (`tools/v76_memory_e2e.py`):

- Codex starts an auth interview and answers the session question.
- OpenCode resumes the same interview, gets the dependent JWT question, answers
  it, approves the interview and reaches ready; the origin platform remains
  `codex`.
- Ambiguous multi-user file sharing asks `file_access` before any Teamwork plan
  exists; no decision is confirmed before the user answer.
- A repository containing JWT/PostgreSQL facts suppresses those questions and
  records `code_derived` decisions only.
- A normal Git commit is captured as an observation, not as invented intent.
- A held project-writer lease rejects a second IDE writer before any plan.
- A copied project keeps the portable project id while its local instance id
  changes; export/import preview reports no silent conflict.

Evidence: [`assets/v76-memory-e2e.json`](assets/v76-memory-e2e.json)

A real Codex managed review also created a verified handoff; OpenCode then loaded
the same project id, last platform and handoff from memory:

- Codex 0.164.0, `gpt-6.1-sol`, read-only reviewer, passed M1,
  69,981 tokens, handoff created.
- OpenCode context load: exit 0, same project id, last platform `codex`.

Evidence: [`assets/v76-codex-to-opencode.json`](assets/v76-codex-to-opencode.json)

**Scope limitation:** this is a real Codex managed review plus a real OpenCode
memory load, not a full authentication implementation or the requested
password-reset feature. The latter remains an open E2E gap.

## 7. Dashboard

The Task Board gains a Project Memory panel showing the last IDE, verified
project summary, confirmed/derived decisions, open blocking requirements,
Grill-Me interviews and the current handoff. The Canvas 2D office remains
unchanged; memory is shown in a separate panel rather than filling the office
map.

Screenshot: [`assets/v76-project-memory.png`](assets/v76-project-memory.png) (demo data, including a current Grill-Me question).

## 8. Tests and limitations

- Full Python, Node and Playwright suites are run in CI; see the final report.
- OpenAI direct provider in OpenCode is not configured; OpenCode Go GPT usage is
  separate from Codex app-server.
- Cursor and Antigravity live IDE execution was not available; only shared skill
  and contract tests are claimed.
- The full Codex → OpenCode authentication feature E2E was not completed.
- Automatic Git hook capture is implemented as an explicit `memory capture`
  command; no silent hook is claimed.
- SQLite is local; cloud sync is export/import preview plus explicit apply, not
  automatic merging.
