---
name: ao-grill-me
description: Explicit import bridge for Antigravity native Grill-Me answers. Use only after the native /grill-me interview has user-confirmed answers that must be stored in shared Auto-Orchestrator project memory; never shadows or overrides the native command.
---

# Auto-Orchestrator import bridge (Antigravity)

Antigravity's native `/teamwork-preview` and native `/grill-me` are
authoritative. This skill is **not** an interview UI and **not** a replacement
for `/grill-me`. It only imports answers the user has already confirmed in the
native interview into the same project memory.

## What is imported

The native export must be a JSON object with exactly the explicit user
confirmations:

```json
{
  "platform": "antigravity",
  "interview_id": "optional-native-id",
  "answers": [
    {
      "question_key": "auth_session",
      "decision": "JWT",
      "rationale": "the user selected JWT",
      "confirmed": true,
      "confirmed_by": "user",
      "requirements": ["JWT access and refresh tokens"]
    }
  ]
}
```

Run:

```bash
python3.11 tools/universal_teamwork.py --root . --platform antigravity \
  grill import-native --file <native-export.json>
```

Every answer must have `confirmed: true` and `confirmed_by: "user"`. The import
rejects unconfirmed, recommended, preselected or model-generated fields and
never records prose as a decision. A rejected import writes nothing.

## Rules

1. Do not ask the interview questions again in this layer. Use native
   `/grill-me`; this bridge is for durable memory only.
2. Do not create, rename or override a native `/grill-me` command.
3. Do not treat a recommendation as selected. Do not import an option the user
   did not explicitly confirm.
4. After import, `context` shows the confirmed decisions and requirements. The
   existing contract and approval rules still apply; a plan approval is still a
   separate explicit `approve-plan` step.
5. Write authority is project-scoped. Check owner/fence with `ide status`;
   read-only context is always allowed, but a foreign owner must not be raced.
6. The native parent session owns subagent dispatch, sandbox, approvals and
   native session evidence. Do not create a second supervisor, start another
   IDE or launch a foreign provider.
7. Report `origin_platform=antigravity`, the execution provider if explicitly
   selected, and `execution=native` or `unknown`. Never claim a native session
   was bound, cancelled or resumed from this skill file.
