# Auto-Orchestrator (V2)

Portable, opt-in native-subagent orchestration for **Codex**, **OpenCode**, and **Google Antigravity**.

The project uses **one common skill and per-platform agent definitions**. It does not host its own LLM or promise to force models to delegate. It instructs native agents to delegate when beneficial, and provides an evidence-based time-reporting CLI.

## What V1 does

- Installs one `.agents/skills/auto-orchestrator/SKILL.md` across all three supported clients.
- Adds a short, idempotent activation rule to the **target project's** `AGENTS.md`.
- Provides explorer, implementer, tester and reviewer agent profiles.
- Uses the calling model by default. Optional **Codex** mode sets `gpt-6-luna` with `max` effort for subagents only (subject to client/model availability).
- Supports concurrency up to **3** independent native agents, guarded by dependency and file ownership rules.
- Records real wall times and agent overlaps with a zero-dependency local CLI. Reports *estimated parallelism benefit*, NOT measured human time saved.

## Installation

Python **3.11+** is required for installer and reporting CLI; agent skills themselves are Markdown.

```bash
python tools/install.py --target /path/to/your/project --platform all --codex-model inherit
# Optional, Codex only:
python tools/install.py --target /path/to/your/project --platform all --codex-model luna-max
# Preview without writing:
python tools/install.py --target /path/to/your/project --platform all --dry-run
```

Windows PowerShell:

```powershell
py -3 tools/install.py --target C:\\work\\my-project --platform all --codex-model luna-max
```

`--platform` supports `all`, `codex`, `opencode`, `antigravity`. Installer **never replaces unrelated user files**: it creates files, recognizes unchanged files, or refuses conflicts. For its own marked `AGENTS.md` section it updates only that section. Existing `.codex/config.toml` content is preserved; the installer refuses an incompatible existing `[agents]` config and explains manual editing. Use `--force-generated` to explicitly replace generated agent profiles (creates `.bak` backups). Installation is project-local. For other repositories, run it again with a different `--target`.

New chats/sessions may be necessary for configuration to be discovered. The skill is discovered automatically but a given model may still decline delegation; no hook-based hard enforcement is claimed.

## Automatic activation and telemetry (V2)

Installer now includes the following project-local instrumentation by default:

| Client | Activation/telemetry | Limit |
|---|---|---|
| Codex | `.codex/hooks.json` + `tools/hook_bridge.py` (`UserPromptSubmit`, `SubagentStart`, `SubagentStop`, `Stop`) | Trust the project and hooks with `/hooks` before they run. |
| OpenCode classic | `.opencode/plugins/auto_orchestrator.js` (system instructions, session + native `task` tool lifecycle) | OpenCode V2 plugin API is different; V2 adapter not yet provided. |
| Antigravity | `.agents/hooks.json` + hook bridge (`SessionStart`, `PreInvocation`, `Stop`) | Measures main execution only; native subagent run-time events not established. |

The hooks are advisory and do not bypass user approval, client permissions, or native capabilities. Some client installs only discover new hooks after restart. Auto-activation is best-effort, not guaranteed spawning. Hook tool commands assume `python` is available and run from the project root; adjust the command on installations with only `python3`.

## Reporting

Hook-aware mode begins/ends runs automatically where supported. After successful validation, **before the final answer**:

```bash
python tools/perf.py verified --latest
python tools/perf.py report --latest
```

Manual fallback (no enabled hooks/plugin):

```bash
python tools/perf.py start --run-id demo --platform codex
python tools/perf.py agent-start --run-id demo --agent-id explorer
python tools/perf.py agent-stop --run-id demo --agent-id explorer
python tools/perf.py agent-start --run-id demo --agent-id tester
python tools/perf.py agent-stop --run-id demo --agent-id tester
python tools/perf.py verified --run-id demo
python tools/perf.py finish --run-id demo --status success
python tools/perf.py report --run-id demo
```

By default the run log is placed inside the target repository under `.auto-orchestrator/runs`, which the installer adds to `.gitignore` if necessary. To avoid recording any prompts, source code, API keys or user secrets, event files contain **only timestamps, opaque IDs, platform, status and verification**. No telemetry is uploaded. Optional `--root /path/to/project` picks the target project. `--time` exists **for deterministic testing and importing externally measured timestamps**; routine tasks should omit it.

When 2+ subagent durations are complete, the task is verified, and the calculated advantage is positive, `report` prints:

> Estimated parallelism benefit: 12m (not independently benchmarked human time saved)

Formula: `sum(agent wall times) - elapsed run wall time` (conservative approximation; may be negative). In case of insufficient evidence it prints **Not available**. This is a scheduling overlap indicator, **not** an experimental single-agent speedup measurement. Baseline benchmarking is a later milestone.

## Caveats and roadmap

- **Antigravity** native custom subagents are documented for **2.0/CLI**; IDE skill discovery is supported, but native subagent parity needs verification on installed IDE versions.
- **OpenCode** agent definitions follow the current classic Markdown agent config. V2 may require adapting permissions/schema depending on installed client version.
- **Codex** subagent model override is best-effort configuration; model ID and access should be checked on an actual run.
- **V2** adds Codex/Antigravity lifecycle hook adapters and OpenCode classic plugin capture, but has no background daemon, standalone DAG runner, automatic Git worktree operations, or cross-platform guaranteed active-model telemetry. Native-client end-to-end tests have **not** been run.

## Sources

- Codex: https://developers.openai.com/codex/subagents
- OpenCode: https://opencode.ai/docs/agents and https://docs.opencode.ai/docs/skills/
- Antigravity: https://www.antigravity.google/docs/skills and https://antigravity.google/docs/subagents

## Tests

```bash
python -m unittest discover -s tests -v
node --test tests/js/test_opencode.mjs
```

Integration tests replay real documented hook input shapes using subprocesses and simulate OpenCode events in Node. They do not authenticate to or launch Codex, OpenCode, or Antigravity clients.

Licensed under MIT (see LICENSE).
