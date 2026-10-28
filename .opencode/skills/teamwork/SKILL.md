---
name: teamwork
description: OpenCode Teamwork entry that keeps /teamwork and /grill-me as the chat commands, separates managed OpenCode execution from native plans, and exposes owner/fence handoffs without claiming a second provider.
---

# Teamwork in OpenCode

The project commands `/teamwork` and `/grill-me` remain the entry points. This
skill is the longer-lived contract they reference; it does not replace them and
does not create a second supervisor.

## Execution identity

Always surface all of these fields when reporting a run:

- `origin_platform` — the IDE that started the plan (normally `opencode`);
- `execution` — `managed` for a real managed runner, `native` for a native plan
  whose subagents run in this IDE, and `unknown` when evidence is missing;
- `execution_provider` — the provider the plan names; a native plan names its
  own platform and never implies a foreign provider;
- `mode` — SOLO, FAST, BALANCED, RIGOROUS or AUTO;
- `contract_status` — `verified`, `partially_verified`, `not_verified`,
  `failed` or `absent`;
- `owner` and `fence` from `ide status`.

The V2 plugin observes in-process native events. A background subagent dispatch
return is not child completion. A managed session completion is not a native
hook stop. Never present either as a native ACK.

## Managed execution (explicit)

Managed OpenCode is the default for `--platform opencode` and is explicit for
any other platform:

```bash
python3.11 tools/universal_teamwork.py --root . --platform opencode \
  start --mode auto --request-file <relative_path>
```

A managed start writes a plan and launches the managed runner. The result
contains a PID/log and `execution="managed"`. Do not pass another platform's
provider blindly; a provider mismatch is an error, not a silent fallback. Codex,
Cursor and Antigravity must not use this path to launch OpenCode.

## Native execution and plan approval

Use native mode when this IDE should run the work with its own subagents:

```bash
python3.11 tools/universal_teamwork.py --root . --platform opencode \
  start --native --mode auto --request-file <relative_path>
```

`start --native` returns `{"status":"native_plan_ready","execution":"native"}`,
writes plan/engine state and starts no provider, runner or PID. Its approval
block is `pending`. Approve the exact digest explicitly:

```bash
python3.11 tools/universal_teamwork.py --root . --platform opencode \
  approve-plan --run-id <run_id> --approver <human identity>
python3.11 tools/universal_teamwork.py --root . --platform opencode \
  start --plan <run_id>
```

`start --plan` refuses missing approval, a changed `plan_sha256` or a stale
contract. Small SOLO tasks may use `plan` + `approve-plan` + `start --plan`;
approval is still explicit. Record native evidence with `native-report`; it
never marks a task verified by itself and never claims a native session id.

## Sequential handoff

One IDE owns write authority at a time. `context`, `status` and `ide status`
are read-only and always allowed. Mutating commands reject a foreign owner.

```bash
python3.11 tools/universal_teamwork.py --root . --platform opencode ide status
python3.11 tools/universal_teamwork.py --root . --platform opencode brief
python3.11 tools/universal_teamwork.py --root . --platform opencode \
  ide prepare --target-platform codex --confirm-idle user \
  --summary "next action"
python3.11 tools/universal_teamwork.py --root . --platform opencode \
  ide release --handoff <handoff_id> --confirm-idle user
```

Prepare and release both require an explicit drain acknowledgement; a fresh
heartbeat is not proof that native children are idle. The target can accept
only after the release.

The target IDE accepts the same handoff id and digest with `ide accept`, then
reads `result.snapshot.next_action` from `ide status`. Prepare/release are
explicit; finishing a runner or subagent never releases ownership
automatically. Owner commands refresh the heartbeat; in a very long session
call `ide heartbeat` explicitly. `pause`, `cancel` and `resume` require the
owner. `ide recover --apply --evidence <json> --discard-handoff` abandons a
handoff that can no longer be accepted and never claims a native stop.

## Flow

1. `/grill-me` for risky or underspecified work; one question at a time.
2. `/teamwork <request>` writes the exact request through
   `teamwork_chat.py request-path` and starts `universal_teamwork.py`.
3. An `interview_required` result is a hard stop until the interview is
   approved and the original request hash matches.
4. Report measured tokens and provider cost, or explicit `unknown`. Never turn
   a missing measurement into zero.
5. Preserve stale and recovery labels from project memory and the runner. The
   dashboard is read-only evidence, not a live process guarantee.

The Canvas office is unchanged by Teamwork. No cross-IDE process is launched.
