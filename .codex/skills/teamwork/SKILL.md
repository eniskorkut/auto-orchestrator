---
name: teamwork
description: Native Codex Teamwork coordination with the universal Auto-Orchestrator CLI. Use for non-trivial coding work that benefits from native subagents, persistent project memory, verified requirements contracts and explicit sequential IDE handoffs.
---

# Teamwork in Codex

Codex is the parent session. It owns its subagents, sandbox, approvals and
permissions. Teamwork in Codex is **native execution**: the universal CLI writes
an inspectable plan and state, Codex dispatches its own subagents, and the
parent session reports real evidence. Never launch OpenCode or another IDE from
Codex, and never start an external orchestrator process.

## Entry point

1. Read `AGENTS.md` and `.agents/skills/auto-orchestrator/SKILL.md`.
2. Load durable state before planning (read-only ownership is always allowed):

   ```bash
   python3.11 tools/universal_teamwork.py --root . --platform codex context
   python3.11 tools/universal_teamwork.py --root . --platform codex ide status
   ```

3. Write the exact request to a validated request file. Do not interpolate
   request text into a shell line:

   ```bash
   python3.11 tools/teamwork_chat.py --root . request-path
   # write the exact text to the returned relative_path
   python3.11 tools/universal_teamwork.py --root . --platform codex \
     start --native --mode auto --request-file <relative_path>
   ```

   `start --native` returns `{"status":"native_plan_ready","execution":"native"}`.
   It writes the plan and engine state and **starts no provider, runner or
   PID**. The plan approval block is `pending`. A normal start runs the
   Assumption Guard first; `interview_required` is a hard stop until the
   persistent interview is approved and the original request hash matches.

4. Approve the exact plan digest explicitly. Never auto-approve:

   ```bash
   python3.11 tools/universal_teamwork.py --root . --platform codex \
     approve-plan --run-id <run_id> --approver <human identity>
   ```

   This stores `{status:"approved", approver, approved_at, plan_sha256}`. Then
   accept the approved plan without spawning anything:

   ```bash
   python3.11 tools/universal_teamwork.py --root . --platform codex \
     start --plan <run_id>
   ```

   `start --plan` refuses a missing approval, a changed digest or a stale
   contract. There is no implicit approval path. Small, low-risk SOLO work may
   use `plan` + `approve-plan` + `start --plan`, but the approval is still
   explicit:

   ```bash
   python3.11 tools/universal_teamwork.py --root . --platform codex \
     plan --mode solo --request-file <relative_path>
   ```

## Native subagent delegation

- Use Codex's native subagent tools and the definitions in `.codex/agents/`.
- Choose `auto_planner`, `auto_implementer`, `auto_tester` and `auto_reviewer`
  by scope, not as a fixed roster.
- At most three concurrent subagents by default.
- Give each subagent a bounded objective, exact read/write scope, dependencies,
  acceptance checks and result format.
- Serialize writes to shared files and contracts. Never let two subagents edit
  the same file.
- The parent session verifies the integrated result. A subagent's self-report
  is not test evidence.

Record native evidence without claiming a native session id or verification:

```bash
python3.11 tools/universal_teamwork.py --root . --platform codex \
  native-report --run-id <run_id> --task-id <task_id> \
  --status passed --verdict passed --tests <tests.json>
```

`native-report` rejects prose-only success, stores the evidence and leaves the
task unverified until Supervisor acceptance and reviewer evidence exist.

## Sequential IDE handoff

Only one IDE owns write authority at a time. Read-only `context`, `status` and
`ide status` are always allowed. A foreign owner must be rejected, not raced.

1. Prepare a handoff from the current owner (explicit; never automatic on task
   completion):

   ```bash
   python3.11 tools/universal_teamwork.py --root . --platform codex \
     ide prepare --target-platform <opencode|cursor|antigravity> \
     --confirm-idle user --summary "<next action>"
   ```

   `ide prepare` requires an explicit drain acknowledgement (`--confirm-idle
   user|owner`) and a clean worktree by default. A fresh heartbeat is not
   proof that native children are idle. Pass `--allow-dirty` only when the
   uncommitted work is intentional. It does not start the target IDE.

2. Release only after the handoff snapshot exists:

   ```bash
   python3.11 tools/universal_teamwork.py --root . --platform codex \
     ide release --handoff <handoff_id> --confirm-idle user
   ```

   An accept is only valid after this release. `handoff_prepared` alone never
   authorizes a transfer.

3. The target IDE accepts the exact snapshot digest:

   ```bash
   python3.11 tools/universal_teamwork.py --root . --platform <target> \
     ide accept --handoff <handoff_id> --digest <digest>
   ```

4. `ide heartbeat`, `ide recover` and `ide status` inspect or maintain the
   ownership record. Owner commands refresh the heartbeat automatically; in a
   very long session call `ide heartbeat` explicitly. Never release
   automatically because a runner or subagent finished. If a handoff can no
   longer be accepted, `ide recover --apply --evidence <json>
   --discard-handoff` abandons it and returns the project to `inactive` without
   claiming that a native child stopped.

## Sandbox and approval boundary

Sandbox and approval are controlled by the **parent Codex session**. Per-agent
`sandbox_mode` keys are not relied on and are intentionally absent from the
shipped `.codex/agents/*.toml` files. Do not describe a delegated subagent as
having a different sandbox or approval policy unless the parent session can
prove it from a real event.

## State and reporting

Use `status`, `agents`, `milestones`, `context`, `handoff`, `pause`, `cancel`,
`resume` and `ide status` from the universal CLI. Report:

- origin platform and execution provider;
- native vs managed execution;
- run id, mode and current milestone;
- verified progress vs estimated progress (never invent one);
- contract status (`verified`, `partially_verified`, `not_verified`, `failed`);
- measured token/cost values or explicit `unknown`;
- recovery state, owner/fence and stale-memory reasons.

`pause`, `cancel` and `resume` require the current owner. Cancellation is a
local signal only unless a native acknowledgement is actually observed. A
native subagent ID may be bound only after a real start event and a matching
stop event; missing evidence stays missing.

## No unsupported claims

There is no `/teamwork` slash command shipped for Codex. Use this skill through
the client's supported skill invocation or by reading the file. A skill file,
plan or handoff is not proof that Codex launched, completed, cancelled or
resumed a native session. Codex never launches OpenCode or another IDE to
continue work.
