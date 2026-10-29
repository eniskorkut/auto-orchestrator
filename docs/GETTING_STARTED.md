# Getting started

Auto-Orchestrator adds native multi-agent coordination, verified completion and
IDE continuity to an existing project. It does not host a model, start another
IDE or replace your client's own subagent system.

For a first install, use **one scope**: project-local (recommended for a single
repository) or global (shared by many repositories). Loading both copies in the
same workspace can produce duplicate hooks and telemetry.

## Requirements

- Python **3.11+** for the installer, CLI and dashboard.
- Git for project memory, handoff and ownership checks.
- One supported client: OpenCode V2, Codex, Cursor or Google Antigravity.
- Node.js is only needed to run the browser/plugin tests in this repository.

The runtime is standard-library Python. The dashboard has no server framework
dependency and binds to loopback only.

## OpenCode V2 — project-local install

Run these commands from a clone of this repository. Replace the target path
with the project you want to instrument.

```bash
python3.11 tools/install.py \
  --target /path/to/your/project \
  --platform opencode \
  --opencode-version v2 \
  --dry-run

python3.11 tools/install.py \
  --target /path/to/your/project \
  --platform opencode \
  --opencode-version v2
```

The dry run prints every file it would create or update. The real install:

- writes shared skills under `.agents/skills/` and the OpenCode-specific
  command/skill/agent files under `.opencode/`;
- copies the installed runtime to `tools/` (including
  `universal_teamwork.py`, `native_dispatch.py`, `verification_receipt.py`,
  `test_evidence.py`, `question_quality.py` and the dashboard assets);
- installs the OpenCode V2 plugin at
  `.opencode/plugins/auto_orchestrator.js`;
- appends one marked section to the target project's `AGENTS.md` and adds
  `.auto-orchestrator/` to `.gitignore` when needed.

It does **not** modify your OpenCode provider credentials, session history or
unrelated settings. Existing files that differ are refused unless the manifest
proves they are installer-managed; use `--force-generated` only when you intend
to replace generated profiles (a `.bak` backup is created).

Restart OpenCode after installation. In the target project, verify the runtime
before starting work:

```bash
python3.11 tools/universal_teamwork.py --root . --platform opencode --help
python3.11 tools/universal_teamwork.py --root . --platform opencode brief
python3.11 tools/universal_teamwork.py --root . --platform opencode native-dispatch status --run-id smoke
python3.11 tools/dashboard.py --help
```

`brief` is read-only. It should report the project, IDE ownership, the current
run/milestone/task and any open question or unknown dispatch. The other three
commands only prove that the installed runtime imports and starts; they do not
prove that the IDE loaded the plugin.

## Global install (optional)

The global installer is for users who want the same skills and runtime in many
projects. Choose it **instead of** a project-local install for a workspace,
unless you have a specific reason to load both.

```bash
python3.11 tools/install_global.py \
  --platform opencode \
  --opencode-version v2 \
  --dry-run

python3.11 tools/install_global.py \
  --platform opencode \
  --opencode-version v2

python3.11 tools/doctor.py --platform opencode
```

Global paths for OpenCode V2:

```text
~/.config/opencode/skills/auto-orchestrator/
~/.config/opencode/skills/grill-me/
~/.config/opencode/skills/teamwork/
~/.config/opencode/agents/
~/.config/opencode/commands/
~/.config/opencode/plugins/auto_orchestrator.js
~/.config/opencode/auto-orchestrator/tools/
```

The doctor checks file presence and basic structure. It does **not** prove that
the plugin was loaded, that a model delegated work, or that a test command ran.
Use the live checks in [OPENCODE_V2.md](OPENCODE_V2.md).

## Codex, Cursor and Antigravity

Project-local examples:

```bash
# Codex
python3.11 tools/install.py --target /path/to/project --platform codex --dry-run
python3.11 tools/install.py --target /path/to/project --platform codex

# Cursor (project hooks are required for observation)
python3.11 tools/install.py --target /path/to/project --platform cursor --dry-run
python3.11 tools/install.py --target /path/to/project --platform cursor

# Antigravity
python3.11 tools/install.py --target /path/to/project --platform antigravity --dry-run
python3.11 tools/install.py --target /path/to/project --platform antigravity
```

`--platform all` installs every adapter and defaults to OpenCode V2. Use
`--opencode-version v1` only for an OpenCode Classic client; the V1 and V2
plugins use different APIs and must not be loaded together.

Codex, Cursor and Antigravity have different live-verification levels. Read the
[supported IDE matrix](../README.md#supported-ides) and the
[capability matrix](CAPABILITY_MATRIX.md) before treating a contract-only
adapter as a working native integration.

## Uninstall

The installers preserve user-owned files. They remove only files recorded in
their manifest whose content is still unchanged; a modified file is retained.

```bash
# Project-local
python3.11 tools/install.py --target /path/to/project --uninstall --dry-run
python3.11 tools/install.py --target /path/to/project --uninstall

# Global
python3.11 tools/install_global.py --uninstall --dry-run
python3.11 tools/install_global.py --uninstall
```

Review the dry-run list before the real removal. Backups created by
`--force-generated` are not automatically deleted.

## Clean-install smoke test

A clean install is only verified when the installed copy works without the
source repository on `PYTHONPATH`:

```bash
cd /path/to/your/project
env -u PYTHONPATH python3.11 tools/universal_teamwork.py --root . --platform opencode --help
env -u PYTHONPATH python3.11 tools/universal_teamwork.py --root . --platform opencode brief
env -u PYTHONPATH python3.11 tools/dashboard.py --help
```

This repository's installer tests run the same commands from a temporary
target. They catch missing runtime modules, but they do not authenticate to an
IDE.

## Next steps

- [OpenCode V2 integration](OPENCODE_V2.md)
- [How the system works](ARCHITECTURE.md)
- [Security and verification](SECURITY.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Evidence dashboard](DASHBOARD.md)
- [Native capability matrix](CAPABILITY_MATRIX.md)
