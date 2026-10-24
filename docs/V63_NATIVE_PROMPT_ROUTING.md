# V6.3 — OpenCode V2 native prompt routing

## What changed

The installed OpenCode V2 plugin now registers the documented
`ctx.session.hook("prompt")` handler. For a **primary, verified project
session**, on user prompt admission it reads the native skill catalog via
`ctx.skill.list()`, then sends a bounded JSON payload to the **installed
trusted local** `tools/router.py select` helper over stdin.

No user message, raw session ID, skill description, secret, or chosen
skill name is stored in AO telemetry. The user text is never placed on a
process command line. The helper returns only selected skill **names**,
`abstained`, and an advisory delegation mode. The plugin maps names to
the exact skill IDs supplied by OpenCode and appends native
`{ "id": "..." }` prompt skill attachments.

- A user's existing skill attachments are **never replaced or augmented**.
- Missing APIs, invalid location ownership, missing Python, invalid skill
  list, timeout, or unavailable helper all skip auto-routing safely.
- Non-primary child sessions do not receive the parent routing hook.
- Maximum 2 auto-selected skills; the Router can return zero matches.
- Python helper subprocess timeout: 1800 ms; payload: 100 KB maximum;
  prompt length: 12,000 characters; catalog size: 128 skills.
- A compact `route` JSONL event records only the number of selected
  skills, recommended max agents, and a routing mode, not user content.

## Conservative delegation

The Router emits `delegation.mode` of `single` or
`consider-parallel`. It defaults to **one agent**. Only explicitly
marked, enumerated independent tasks (or separated file targets)
yield a suggestion of 2–3 agents. This addresses V6.2's measured
single-run regression for small work (29.3s vs 44.9s); it is **not**
evidence that the regression is now fixed. OpenCode's model still
decides whether to actually spawn native subagents, subject to user
instructions, available tools, and permission checks.

The `context` hook includes brief guidance for the active primary
session, without modifying the persisted user prompt or native tool
permissions.

## Validation

`python -m unittest discover -s tests -v`

`node --test tests/js/test_opencode.mjs tests/js/test_opencode_v2.mjs`

Hosted Node tests exercise plugin + actual installed-style Python helper
through stdin and use simulated SDK events. They **do not establish**
OpenCode Desktop compatibility until a real V2.1.x smoke test is performed.

## Live OpenCode verification

Use a fresh disposable project. Restart OpenCode after a global install.
With a temporary backend skill in native OpenCode's skill list, submit
an ordinary backend task **without mentioning Router**. Confirm
`route` JSONL metadata was recorded and inspect the native prompt's
activated skills; do not rely on the model claiming it used a skill.

Test user-pinned skill attachments, child sessions, another project,
noncoding prompts, missing python and a large prompt. Confirm no
cross-project telemetry and no prompt or sensitive text in logs.

Then compare one-agent vs multi-agent work with matched repeated
runs, not inferred wall-time savings from overlapping tool spans.

## Known limits

- Native prompt hooks insert skills, but do **not** force subagent
  dispatch; the model can still decide to work alone or delegate.
- The router remains deterministic and does not improve the blind3
  long-tail evaluation set by itself.
- It does not add external embedding/LLM calls or change user's API
  provider settings.
- No independent review-agent identity attestation or Desktop toast is
  added by this feature.
- The hook is a supported V2 API according to upstream documentation;
  local Desktop 2.1.x compatibility still requires live confirmation.

Sources: https://dev.opencode.ai/v2/docs/build/plugins/
