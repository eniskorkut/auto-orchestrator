---
name: teamwork
description: Native Codex Teamwork coordination with the universal Auto-Orchestrator CLI. Use for non-trivial coding work that benefits from native subagents, persistent project memory, verified requirements contracts and bounded IDE handoffs.
---

# Teamwork in Codex

Codex is the parent session. It owns its own subagents, sandbox, approvals and
permissions. This skill coordinates that native work; it never starts another
IDE or an external orchestrator process.

## Entry point

1. Read `AGENTS.md` and `.agents/skills/auto-orchestrator/SKILL.md`.
2. Load durable state before planning:

   ```bash
   python3.11 tools/universal_teamwork.py --root . --platform codex context
   ```

3. For a new non-trivial task, write the exact request to a validated request
   file and run the universal CLI. Do not interpolate request text into a shell
   line:

   ```bash
   python3.11 tools/teamwork_chat.py --root . request-path
   # write the exact text to the returned relative_path
   python3.11 tools/universal_teamwork.py --root . --platform codex start --mode auto --request-file <relative_path>
   ```

   A normal start runs the Assumption Guard. If it returns
   `{"status":"interview_required"}`, present exactly the returned question and
   do not create a plan or start code. Answers are recorded through
   `universal_teamwork.py ... grill ...`; approval creates the versioned
   requirements contract.

4. Managed Codex execution is an explicit opt-in and is read-only by default:

   ```bash
   python3.11 tools/universal_teamwork.py --root . --platform codex --provider codex \
     --codex-sandbox read-only start --mode auto --request-file <relative_path>
   ```

   `--codex-sandbox workspace-write` is a separate explicit choice. Approval
   bridging is not implemented.

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

## Sandbox and approval boundary

Sandbox and approval are controlled by the **parent Codex session**. Per-agent
`sandbox_mode` keys are not relied on and are intentionally absent from the
shipped `.codex/agents/*.toml` files. Do not describe a delegated subagent as
having a different sandbox or approval policy unless the parent session can
prove it from a real event.

## State and reporting

Use `status`, `agents`, `milestones`, `context`, `handoff`, `pause`, `cancel`
and `resume` from the universal CLI. Report:

- origin platform and execution provider;
- native vs managed execution;
- run id, mode and current milestone;
- verified progress vs estimated progress (never invent one);
- contract status (`verified`, `partially_verified`, `not_verified`, `failed`);
- measured token/cost values or explicit `unknown`;
- recovery state and stale-memory reasons.

Inline comment "cancel" is local-signal-only unless a native acknowledgement is
actually observed. A native subagent ID may be bound only after a real start
event and a matching stop event; missing evidence stays missing.

## No unsupported claims

There is no `/teamwork` slash command shipped for Codex. Use this skill through
the client's supported skill invocation or by reading the file. A skill file,
plan or handoff is not proof that Codex launched, completed, cancelled or
resumed a native session.
