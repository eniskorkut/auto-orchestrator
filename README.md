# Auto-Orchestrator (V4)

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
| OpenCode classic | `.opencode/plugins/auto_orchestrator.js` (system instructions, session + native `task` tool lifecycle) | OpenCode V2 is supported by the separate native `setup(ctx)` adapter (`adapters/opencode-v2/`); V1 and V2 require different plugin APIs. |
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
- **OpenCode** agent definitions follow the current classic Markdown agent config. OpenCode V1 and V2 have different plugin APIs; select `--opencode-version` matching the installed client.
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

## V3 — global installation and IDE testing

V3 adds a global, reversible setup for **Codex, OpenCode Classic/V2, and Antigravity IDE/CLI**; it supplements the project-local V2 installer.

```bash
python tools/install_global.py --platform all --opencode-version v2 --codex-model inherit --dry-run
python tools/install_global.py --platform all --opencode-version v2 --codex-model inherit
python tools/doctor.py --platform all
# Remove only managed, unchanged assets (preserves user-edited files):
python tools/install_global.py --uninstall
```

For Codex Luna Max, pass `--codex-model luna-max`. If your existing `~/.codex/config.toml` requires edits, the global installer **fails safely** and provides instructions rather than rewriting it. Global OpenCode V2 uses a native `setup(ctx)` plugin with event subscriptions; V1 uses its legacy plugin. User files are not overwritten. See [IDE_TESTING.md](IDE_TESTING.md) for three independent manual smoke scenarios.

**Limits:** global installation creates Codex/Antigravity hook files only when they do not already exist; existing hook files are extended with only the Auto-Orchestrator hook groups/namespace; existing user hook entries are preserved and injected entries are removed on uninstall. Conflicting or invalid hook configurations stop installation safely. OpenCode V2 plugin loading requires an IDE restart. Native telemetry remains unverified until observed in the running client. The `doctor` command checks file readiness, **not** real model choice, native subagent spawning or human time savings. Hosted CI tests mocked lifecycle contracts; actual IDE integration still needs manual smoke testing.

**Project-local OpenCode version:** `python tools/install.py --target /your/project --platform opencode --opencode-version v2` (default). For Classic use `--opencode-version v1`; a previously installed plugin requires `--force-generated` to switch safely with backup.


## V4 — Native task supervisor, skill router, safe parallel integration

V4 introduces **three standalone Python 3.11+ helpers** (stdlib only) available from both project and global installations:

| Tool | Purpose | Important limit |
|---|---|---|
| `tools/supervisor.py` | Durable SQLite task IDs, dependency-aware claiming, max 3 active native agents, attempt-specific leases, timeout/cancel acknowledgement, max 2 retries, structured results | Does not spawn or terminate IDE agents |
| `tools/router.py` | Discover installed `SKILL.md` metadata and produce minimal per-task context with up to 2 relevant skills | Does not ingest private conversations or execute skill text |
| `tools/worktree.py` | Create isolated Git worktrees, preview merge conflicts without changing the main tree, safely clean worktrees | Does not automatically merge or delete branches |

### Minimal demonstration (project-local installation)

Run these commands in your **target project**, after installing Auto-Orchestrator:

```bash
python tools/supervisor.py init --run-id example-001 --plan examples/v4-plan.json
python tools/supervisor.py claim --run-id example-001
python tools/router.py --root . brief --plan examples/v4-plan.json --task-id docs
# Spawn the claimed task(s) through your IDE's native subagent tool.
# On confirmed native completion with successful checks:
python tools/supervisor.py completed --run-id example-001 --task-id docs --lease LEASE --result examples/v4-result.json
python tools/supervisor.py status --run-id example-001
```

The sample files are included in **this repository**. For another target project, use your own scoped JSON plan and result; copy sample files only as a starting template. The leased task must be executed by the native IDE tool; the helper never fakes its execution.

Isolated worktree operations (in a clean Git project):

```bash
python tools/worktree.py --repo . create --task-id backend
# Make and commit changes in the returned worktree path.
python tools/worktree.py --repo . preview --task-id backend
```

A clean preview is only a conflict preflight, **not a verified/tested merge**. Main agent still reviews changes, runs tests, and decides integration. If native cancellation or completion cannot be observed, the Supervisor keeps contested scope reserved.

Read [the V4 protocol](.agents/skills/auto-orchestrator/references/supervision.md) and [IDE test guide](IDE_TESTING.md). GitHub Actions runs deterministic unit and mock integration tests; **it does not constitute authenticated real IDE end-to-end evidence**.

## Benchmark and OpenCode V2 telemetry

Read [BENCHMARK.md](BENCHMARK.md) to collect matched serial/orchestrated runs. Project installations now include `tools/benchmark.py`; global installation places it under the platform's `auto-orchestrator/tools` directory.

For **global OpenCode V2**, run `python <global-opencode-config>/auto-orchestrator/tools/perf.py --root <your-project> report --latest --json` (replace placeholders with actual paths). The plugin writes local run events in the project. Restart OpenCode after installing to load the V2 plugin, then confirm that new `.auto-orchestrator/runs/*.jsonl` files appear. A passing `doctor` or mock plugin test alone does not prove runtime telemetry is active.

Paths protected by the installers are checked for symlink traversal; concurrent external filesystem mutation is outside this local installer guarantee. Credentials used for Supervisor attempt leases are local control values, not a cross-user authorization mechanism.


## Codex / Antigravity native-hook compatibility

Codex user hooks reside at `~/.codex/hooks.json` and Antigravity user hooks at `~/.gemini/config/hooks.json`. When these files already exist, the global installer merges **only** our native hook entries with the existing JSON, preserves user groups, and remembers the injected entries for safe uninstall. Dry runs make no changes. Conflicting Antigravity `auto-orchestrator` namespace entries or malformed hook files cause a safe installation error instead of an overwrite. After setup, review/trust newly configured hooks inside the native client; restarting the IDE and passing `doctor` do not prove runtime execution.

The hook bridge now uses Codex's `cwd` or Antigravity's single active `workspacePaths` path when provided so event logs are saved under the active project; otherwise it falls back to the hook process working directory. For multiple Antigravity workspaces, it intentionally does not guess an active project.

**Important:** Codex `SubagentStart`/`SubagentStop` can supply native subagent durations; Antigravity's hook-based telemetry still tracks sessions, **not** confirmed subagent wall time. Do not derive subagent time savings from Antigravity session hooks. Review [IDE_TESTING.md](IDE_TESTING.md) and platform diagnostics before claiming live E2E success.
