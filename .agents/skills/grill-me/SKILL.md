---
name: grill-me
description: Interview the user one question at a time before a risky or underspecified Teamwork task. Use when requirements are ambiguous, authentication/data/security/API decisions are open, or the user asks for grill-me.
---

# Grill-Me with persistent project memory

Use the local `tools/universal_teamwork.py` command. This skill is portable
across OpenCode, Codex, Cursor and Antigravity; the same project memory is used
when the IDE changes.

## Rules

1. Read project context first:
   `python3.11 tools/universal_teamwork.py --root . --platform <platform> context`
2. Do not ask a question that the code, docs or confirmed project memory already
   answers. Code-derived facts are not user confirmations.
3. Ask one main question at a time. Put the recommended option first, but never
   mark it selected or confirmed without the user's answer.
4. A normal `/teamwork` start may also return `status: interview_required`
   from the central Assumption Guard. Treat that as a hard stop; no plan or
   runner exists yet.
5. Start an interview with a validated request file, never a shell string:

   `python3.11 tools/teamwork_chat.py --root . request-path`

   Write the exact request to the returned `relative_path`, then run:

   `python3.11 tools/universal_teamwork.py --root . --platform <platform> grill start --grill-mode auto --request-file <relative_path>`

6. Use the returned `question`. Present its options; wait for an answer.
7. Record the answer:
   `... grill answer --question-id <id> --option <option>`
   For free text use `--text`.
8. Support `skip`, `back`, `cancel`, `resume`, `summary` and `approve`.
   Skipped critical questions stay unresolved_blocking.
9. Show the summary and ask for plan approval before code when risk is high.
   Approval creates a digest-bound, versioned requirements contract; the
   contract is what the planner and verifier consume, not an agent claim.
10. Start Teamwork only from an approved interview, and pass the original
   request again so the full text is verified, not truncated:
   `... start --interview-id <id> --request-file <relative_path> --mode <mode>`.
   A different request is rejected. The plan carries the approved contract and
   its version; the runner rejects a stale or contradictory contract.

## Platform notes

- OpenCode: `/grill-me` and `/teamwork --grill` are project commands.
- Codex: invoke this skill by name (`$grill-me` where the client supports skill
  invocation; otherwise reference `.agents/skills/grill-me/SKILL.md`).
- Cursor: skills are discovered from `.agents/skills/` and `.cursor/skills/`.
- Antigravity: native `/teamwork-preview` and native `/grill-me` are
  authoritative. This portable `grill-me` skill must not be installed as a
  second `grill-me` under Antigravity's skill roots; the non-native fallback is
  `ao-grill-me`, which imports decisions into the same project memory instead of
  duplicating the interview UI.

Do not claim native IDE delegation, execution or completion that was not
observed. A contract/skill file is not a live execution test.
