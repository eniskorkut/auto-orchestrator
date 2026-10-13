# IDE testing and readiness (V3)

## Global installation

Run these from a clone of `eniskorkut/auto-orchestrator` (Python 3.11+):

```bash
python tools/install_global.py --platform all --opencode-version v2 --codex-model inherit --dry-run
python tools/install_global.py --platform all --opencode-version v2 --codex-model inherit
python tools/doctor.py --platform all
```

If Codex should spawn `gpt-6-luna` at `max` reasoning, use `--codex-model luna-max`. If an existing `~/.codex/config.toml` lacks those settings, the installer refuses to overwrite it. Edit the existing `[agents]` section manually before rerunning. This mode is a requested model configuration, not proof the selected model is used.

For OpenCode Classic, use `--opencode-version v1`. Do not load the V1 and V2 plugin simultaneously. `--uninstall` removes only unchanged files owned by our installer and its marked text in global `AGENTS.md`; it preserves user-modified files.

Global skill locations: `~/.codex/skills`, `~/.config/opencode/skills`, and `~/.gemini/config/skills` (Antigravity IDE/2.0) plus `~/.gemini/antigravity-cli/skills` (CLI). Restart clients and grant hook trust where applicable. The Antigravity IDE's support for native customizable subagents is version-dependent.

## Test task A — simple (single agent expected)

Ask each IDE: `Find the README heading and tell me its first line. Do not edit files.` There should be no needless implementation agent spawned.

## Test task B — parallel delegation

In a throwaway clone, ask: `Investigate README inaccuracies and test coverage weaknesses in parallel, then recommend changes with file and line evidence. Run at least two independent read-only investigations when native subagents are supported; do not modify code.`

Check the IDE's **actual subagent timeline/tool calls** for overlapping work, not just prose claims. Check the selected active model in the client's diagnostic/session view if exposed. The telemetry system only qualifies parallelism benefit if 2+ completed spans and an explicit verified event exist.

## Test task C — isolated coding and verification

In a disposable project, ask: `Add input validation and tests to a small sample API, independently review the change and run tests.` Confirm tests, review and main agent integration. Do not give secrets or production repositories to this smoke test.

## Reporting proof

- `python tools/perf.py --root /path/to/project report --latest --json`
- Do not label summed-agent-time overlaps as human hours saved.
- No success claim when events are missing, model inaccessible or tests failed.

## Honest CI boundary

GitHub Actions runs Python unit tests, mocked lifecycle events, OpenCode V2 plugin contract tests and OS matrix tests. Hosted runners do **not** verify authenticated sessions in Codex, OpenCode or Antigravity IDE. Complete the three manual smoke tasks before marking native E2E verified.