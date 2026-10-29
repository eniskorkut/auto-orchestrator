# OpenCode V2 integration

Auto-Orchestrator supports OpenCode V2 with a native `setup(ctx)` plugin, the
`/teamwork` and `/grill-me` commands, shared skills and per-role agent
definitions. OpenCode V1 uses a different plugin API; install exactly one
version for the client you run.

## What gets installed

Project-local install (`--platform opencode --opencode-version v2`):

```text
.agents/skills/auto-orchestrator/       shared coordination skill
.agents/skills/grill-me/                requirement interview skill
.agents/agents/                         shared agent profiles
.opencode/agents/                       OpenCode agent definitions
.opencode/commands/teamwork.md          /teamwork entry point
.opencode/commands/grill-me.md          /grill-me entry point
.opencode/skills/teamwork/              longer Teamwork contract
.opencode/plugins/auto_orchestrator.js  V2 native plugin
tools/                                  runtime CLI, dashboard and modules
AGENTS.md                               one marked activation section
```

Global install adds the same files under `~/.config/opencode/` and places the
runtime in `~/.config/opencode/auto-orchestrator/tools/`.

The plugin observes the native `subagent` tool and session lifecycle. It does
not spawn agents itself, does not store prompts or source code, and writes only
opaque IDs, timestamps, platform and status to
`.auto-orchestrator/runs/*.jsonl`.

## Version check

```bash
opencode --version
```

Use `--opencode-version v2` for a V2 client. The V2 plugin exports a default
object with `id: 'auto-orchestrator'` and `setup(ctx)`. The V1 plugin is a
class-based `AutoOrchestratorPlugin`; it is not compatible with the V2 loader.

If a workspace has both a project-local and a global plugin for the same
client, remove one. The installer does not delete an unmanaged plugin and
cannot prevent your client from loading two independent copies.

## Install and restart

```bash
python3.11 tools/install.py \
  --target /path/to/project \
  --platform opencode \
  --opencode-version v2 \
  --dry-run

python3.11 tools/install.py \
  --target /path/to/project \
  --platform opencode \
  --opencode-version v2
```

Restart OpenCode after the install. A new session is normally required for
command and skill discovery. Then check the entry points in the OpenCode chat:

```text
/teamwork status
/grill-me status
```

`/teamwork status` is read-only. If the project has no run yet, it should say so
rather than inventing one. `/grill-me status` reports the current interview or
that none exists.

## What the plugin observes

- Native session start and terminal events, with a fresh authoritative
  `ctx.session.get` lookup before a run is attributed to this project.
- Foreground `subagent` tool-call start and stop, correlated by native call ID.
- Code edits and configured check commands that the native client actually
  executed with an integer exit code.
- Session moves and lost ownership as diagnostics, not as success.

A background `subagent` call returns when dispatch is acknowledged. That return
is **not** proof that the child finished. Background child completion remains
unsupported unless the native client exposes an authoritative stop reference.

## Quality-gate verification

Automatic verification is opt-in. Copy `examples/v5-verification.json` to
`.auto-orchestrator/verification.json` and approve the exact commands:

```json
{
  "version": 1,
  "auto_verify": true,
  "required_checks": [
    {"id": "unit", "tool": "shell", "command": "python3.11 -m unittest"},
    {"id": "lint", "tool": "shell", "command": "python3.11 -m compileall tools"}
  ]
}
```

The plugin never runs these commands. It records a check only when the native
client already ran the exact command with exit code `0` after the last observed
edit or child completion. Missing exit codes, explicit errors, a different
command and stale checks do not verify a run.

## Commands and skills

| Surface | Purpose |
|---|---|
| `/teamwork` | Start or control a run from the OpenCode chat. Managed OpenCode execution or an explicit native plan. |
| `/grill-me` | One-question-at-a-time requirement interview. Recommendations are never treated as answers. |
| `auto-orchestrator` skill | Coordination policy, dependency and ownership rules, verification expectations. |
| `teamwork` skill | Longer-lived Teamwork contract used by the commands. |
| `grill-me` skill | Portable interview contract for clients without the command. |

The command text treats user arguments as untrusted data. `/teamwork` writes the
request to a validated UTF-8 file and uses `--request-file`; it never
interpolates the request into a shell command.

## Verify the runtime from the target project

These checks prove the installed Python runtime, not that the plugin loaded:

```bash
python3.11 tools/universal_teamwork.py --root . --platform opencode --help
python3.11 tools/universal_teamwork.py --root . --platform opencode brief
python3.11 tools/universal_teamwork.py --root . --platform opencode native-dispatch status --run-id smoke
python3.11 tools/dashboard.py --help
```

For the dashboard:

```bash
python3.11 tools/dashboard.py --root . --open
```

A real plugin-load check requires an OpenCode session that emits native events.
Inspect `.auto-orchestrator/runs/` after a tool call or subagent run. If the
directory stays empty, the plugin did not load, the session belonged to another
workspace, or the event was deliberately ignored as cross-project traffic.
The dashboard and `doctor` cannot prove runtime plugin loading.

## Known limits

- OpenCode Desktop must load the plugin; hosted tests simulate event shapes.
- Background child completion is not authoritative.
- A plugin receives an in-process event stream; there is no external native
  spawn/cancel/resume API.
- A local process with the same OS user can read project files. Ownership
  fencing is a coordination control, not an OS sandbox.
- OpenCode V1 and V2 plugins must not be loaded simultaneously.

See [Getting started](GETTING_STARTED.md), [Architecture](ARCHITECTURE.md) and
the [capability matrix](CAPABILITY_MATRIX.md).
