---
name: ao-grill-me
description: Non-native Auto-Orchestrator interview for Antigravity. Use only when the native /grill-me is unavailable or when the user explicitly asks for durable project memory, contracts and handoffs; the native surfaces remain authoritative.
---

# Auto-Orchestrator interview (Antigravity, non-native)

Antigravity's native `/teamwork-preview` and `/grill-me` are authoritative.
This skill is the **ao-grill-me** layer: it stores the interview answers,
decisions, requirements contract and handoff in the shared project memory. It
does not shadow, replace or duplicate the native `/grill-me` command.

## Rules

1. Prefer native `/grill-me` for the interview UI. If the user uses it, import
   the resulting decisions into project memory rather than asking again.
2. If native memory is unavailable, or the user explicitly asks for the
   portable layer, run one question at a time through:

   ```bash
   python3.11 tools/universal_teamwork.py --root . --platform antigravity \
     grill start --grill-mode auto --request-file <relative_path>
   ```

3. Never treat the recommended option as selected. Never record a
   recommendation as a user answer.
4. Critical skipped questions stay `unresolved_blocking` and block approval.
5. Approval creates a digest-bound requirements contract. `not_automated`
   criteria remain `not_verified`; they are not counted as passes.
6. The native parent session owns subagent dispatch, sandbox and approval. Do
   not create a second supervisor or launch another IDE's process.
7. Report `origin_platform=antigravity`, the execution provider if one was
   explicitly selected, and whether execution is native or managed. If unknown,
   say unknown.

Do not claim that a native session was bound, cancelled or resumed from this
skill file.
