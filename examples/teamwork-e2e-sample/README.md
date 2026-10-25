# Teamwork E2E sample (generated)

This directory is the **artifact produced by the V7.3 managed teamwork run**, not
hand-written application code.

- Scaffold before the run: `README.md` + `CONTRACT.md` + Git repository only.
- Product code and tests were written by real OpenCode managed sessions.
- Implementers worked in isolated worktrees; the orchestrator committed and
  integrated their branches.
- The tester, critic, challenger, auditor and success auditor ran in separate
  managed sessions.
- The challenger added the 43 adversarial tests under `tests/challenger/`.
- Final evidence: **60 backend tests pass**, **browser E2E passes**, all three
  milestone handoffs present.

See [docs/V73_TEAMWORK.md](../../docs/V73_TEAMWORK.md) and the sanitized
[`v73-e2e-evidence.json`](../../docs/assets/v73-e2e-evidence.json).

Run locally:

```bash
python3.11 -m unittest discover -s tests -v
NODE_PATH=/path/to/auto-orchestrator/node_modules \
  AO_CHROMIUM_EXECUTABLE="/path/to/chrome" \
  node e2e/check.cjs
```
