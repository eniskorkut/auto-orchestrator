---
name: teamwork
description: OpenCode Teamwork entry that keeps /teamwork and /grill-me as the chat commands, surfaces origin/platform/execution provider, and separates native plugin evidence from managed execution.
---

# Teamwork in OpenCode

The project commands `/teamwork` and `/grill-me` remain the entry points. This
skill is the longer-lived contract they reference; it does not replace them and
does not create a second supervisor.

## Execution identity

Always surface all of these fields when reporting a run:

- `origin_platform` — the IDE that started the plan (normally `opencode`);
- `execution_provider` — the managed provider (`opencode` or `codex`);
- `native_or_managed` — `managed` for a real managed runner session,
  `foreground-native` only for an observed foreground native subagent span, and
  `unknown` otherwise;
- `mode` — SOLO, FAST, BALANCED, RIGOROUS or AUTO;
- `contract_status` — `verified`, `partially_verified`, `not_verified`,
  `failed` or `absent`.

The V2 plugin observes in-process native events. A background subagent dispatch
return is not child completion. A managed session completion is not a native
hook stop. Never present either as a native ACK.

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
