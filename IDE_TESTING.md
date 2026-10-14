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

In a throwaway clone, ask: `Inspect README inaccuracies, installer security risks, and test-coverage gaps. Give separate evidence-backed assessments and one integrated recommendation. Do not edit files.` **Do not ask for subagents or parallel work in the prompt**: this test must verify autonomous delegation.

Check the IDE's **actual native subagent timeline/tool calls** for overlapping work. Passing criterion: at least two independent agents overlap *without an explicit subagent instruction*. If not, record failed or unsupported; prose claims are not proof. Check the selected active model in the client's diagnostic/session view if exposed. The telemetry system only qualifies parallelism benefit if 2+ completed spans and an explicit verified event exist.

## Test task C — isolated coding and verification

In a disposable project, ask: `Add input validation and tests to a small sample API, independently review the change and run tests.` Confirm tests, review and main agent integration. Do not give secrets or production repositories to this smoke test.

## Reporting proof

- `python tools/perf.py --root /path/to/project report --latest --json`
- Do not label summed-agent-time overlaps as human hours saved.
- No success claim when events are missing, model inaccessible or tests failed.

## Honest CI boundary

GitHub Actions runs Python unit tests, mocked lifecycle events, OpenCode V2 plugin contract tests and OS matrix tests. Hosted runners do **not** verify authenticated sessions in Codex, OpenCode or Antigravity IDE. Complete the three manual smoke tasks before marking native E2E verified.
## Global hooks

When no global hook configuration exists, the installer creates Codex `~/.codex/hooks.json` and Antigravity `~/.gemini/config/hooks.json`, pointing to safe local Python bridge copies. If you already have these files, the installer preserves your existing hook definitions and injects only its own hook groups/namespace. Those injected entries are tracked and removed on uninstall; conflicting Antigravity entries or invalid JSON fail safely. Trust Codex hooks through the IDE /hooks interface before relying on event capture. Antigravity hooks can be viewed in Customizations > Hooks. Captured Antigravity session timing is **not** authoritative subagent runtime; any benefits are omitted when evidence is insufficient.


## Native Codex and Antigravity activation checks

**Codex:** Open `/hooks` in Codex CLI to review/trust the installed UserPromptSubmit, SubagentStart, SubagentStop and Stop hook commands. Start a new turn containing independent code-review subtasks without asking explicitly for agents. Inspect the native agent timeline; only native SubagentStart/SubagentStop matches count as measured subagent durations. Verify that `.auto-orchestrator/runs/` is created in the workspace from the hook payload's `cwd`. Check the actual selected subagent model and effort in the client; a config key alone isn't proof.

**Antigravity IDE:** Open Customizations > Hooks and ensure the injected SessionStart, PreInvocation and Stop hooks are present/enabled. Run a test project. The hook adapter tracks session duration only; do not claim per-subagent runtime or time saved. Check native invocation/model evidence independently. With multiple active workspaces, specify a trusted project root explicitly when running the hook bridge rather than treating the IDE process directory as the correct project.

For existing hook files: run installer `--dry-run` before upgrading and `--uninstall` to remove only managed entries. Never bypass the native client's trust/approval prompts. Workspace and global layers may both load matching hooks, so avoid installing duplicate project/global adapters in the same active workspace unless necessary.
