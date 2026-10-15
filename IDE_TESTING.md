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

**Codex existing config caveat:** The installer now warns when an existing `~/.codex/config.toml` explicitly disables `multi_agent` or hooks, defines a subagent limit other than three, or overrides the inherited model. It does **not** rewrite user-owned settings. Resolve warnings before calling the Codex live test successful. Trusting hooks remains a separate manual action.


## V5 OpenCode Desktop live verification acceptance test

Only test in disposable repositories first. V5 auto-verification is disabled unless the project owner creates .auto-orchestrator/verification.json based on examples/v5-verification.json and intentionally approves the exact native bash/shell test commands.

1. Install/update the global OpenCode V2 adapter. Start a clean test project/session. Verify that the plugin version was loaded, not just that the file exists. Run a regular multi-file task without mentioning agents.
2. Run the exact test command configured in verification.json **after** all relevant code edits and native subagents finish. The plugin observes the tool result; it never executes commands itself.
3. Check that the project's JSONL run ends with execution finish then a separate verified event. Validate with perf.py --root PROJECT report --run-id ID --json. Verified indicates the opted-in checks passed; it is not a complete semantic proof.
4. Repeat with a failing test, a missing exit code, a test followed by a new edit, a config policy change, a cancelled session, and a second unrelated project. All should be unverified without producing cross-project telemetry.
5. Run two sessions simultaneously; use exact run IDs rather than --latest. Verify that each audit is independent.
6. Retry installation to ensure idempotence and confirm original provider keys/user plugins are unchanged.

CI simulates native API event payloads on supported OSes; it cannot establish that a specific installed OpenCode V2.0.28 build emits all expected test exit-code metadata. If the native tool result has no authoritative integer exit code, V5 intentionally refuses auto-verification. Do not force this by parsing model-generated text, fabricated metadata or tool output.
