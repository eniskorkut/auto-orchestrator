# Troubleshooting

Start with the read-only diagnostics before changing files:

```bash
python3.11 tools/universal_teamwork.py --root . --platform opencode brief
python3.11 tools/universal_teamwork.py --root . --platform opencode native-dispatch status --run-id <run>
python3.11 tools/capabilities.py show --detect --json
python3.11 tools/doctor.py --platform opencode
```

`doctor` checks installed files. It does not prove that a plugin loaded, that a
model delegated work or that a test ran.

## `ModuleNotFoundError` after a clean install

Symptom:

```text
ModuleNotFoundError: No module named 'test_evidence'
```

This was a V8.0 installer packaging bug: `universal_teamwork.py` imported
`native_dispatch`, `verification_receipt`, `test_evidence` and
`question_quality`, but the project and global runtime source lists did not copy
them. Reinstall with the current release:

```bash
python3.11 tools/install.py --target /path/to/project --platform opencode --opencode-version v2
```

Verify from the target project without the source repository on `PYTHONPATH`:

```bash
cd /path/to/project
env -u PYTHONPATH python3.11 tools/universal_teamwork.py --root . --platform opencode --help
env -u PYTHONPATH python3.11 tools/universal_teamwork.py --root . --platform opencode brief
```

If the module is still missing, the install was interrupted or a stale
`.auto-orchestrator/install-manifest.json` owns the file. Run the installer
again; it refuses to overwrite user-modified files.

## `/teamwork` or `/grill-me` is not discovered

1. Confirm the client is OpenCode V2 (or the correct client for the installed
   adapter).
2. Confirm the files exist:
   - project: `.opencode/commands/teamwork.md` and `grill-me.md`;
   - global: `~/.config/opencode/commands/`.
3. Restart OpenCode and open a new session.
4. Check that only one plugin is loaded. A V1 `AutoOrchestratorPlugin` and a V2
   `setup(ctx)` plugin in the same workspace is a conflict.
5. If a project-local and a global install both exist, remove one.

The installer records managed files in `.auto-orchestrator/install-manifest.json`
(project) or its global manifest. Uninstall with `--dry-run` first to see what
would be removed.

## Plugin files exist but no run log appears

A passing file check is not a plugin-load check. After a real tool call or
subagent run, inspect:

```bash
ls -la .auto-orchestrator/runs/
```

If the directory is empty:

- the plugin did not load in this client build;
- the session belongs to another workspace and was ignored as cross-project
  traffic;
- the event stream was interrupted or the session moved;
- the client has not trusted the plugin/hook yet.

OpenCode V2 events are in-process. There is no external spawn API to test from a
terminal. Do not infer success from `doctor`.

## `/grill-me` returns no question

Grill-Me is risk-driven. A small, unambiguous request may produce no question.
To force an interview for a risky change:

```bash
python3.11 tools/universal_teamwork.py --root . --platform opencode grill start \
  --grill-mode required \
  --request "Add a user login system with sessions."
```

If the request is already covered by a confirmed decision or by code/config
facts, the engine may correctly suppress the question. A recommendation is not
an answer; answer explicitly with `--option` or `--text`.

Check the current state:

```bash
python3.11 tools/universal_teamwork.py --root . --platform opencode grill status
python3.11 tools/universal_teamwork.py --root . --platform opencode grill summary
```

## A run cannot be verified

Common causes:

- the worktree is dirty or HEAD moved after verification;
- the verification receipt is missing, malformed or already consumed;
- the receipt belongs to another run/task/dispatch;
- the test command is not on the typed allowlist;
- the test output is empty, skipped or inconsistent;
- a code mutation happened after the last check;
- the dispatch artifact digest or size does not match;
- the review is `declared`, not `accepted`/`observed`, where review is required.

Run:

```bash
python3.11 tools/universal_teamwork.py --root . --platform opencode native-verify --help
python3.11 tools/universal_teamwork.py --root . --platform opencode native-report --help
```

Do not edit a receipt or fabricate a test result to clear the gate. Unknown
evidence stays unknown.

## Dashboard shows no data

- The dashboard reads `.auto-orchestrator/runs/`, not the source repository.
- Use `--root /path/to/project` or register the project.
- A new project has no runs until a session emits events.
- Demo mode is synthetic and clearly labelled:

```bash
python3.11 tools/dashboard.py --demo --open
```

A demo screenshot is not a benchmark and must not be presented as a real run.

## Cursor is blocked by authentication

On this machine the Cursor agent CLI is installed but requires authentication.
That is an environment limitation, not a code failure. Do not mark Cursor live
E2E as passed. Use project-local Cursor hooks in a workspace you control, or
report `blocked-auth`.

## Antigravity has no CLI adapter

Antigravity remains contract-only for external managed execution. Its native
`/teamwork-preview` and `/grill-me` are authoritative. The `ao-grill-me` skill
only imports explicitly user-confirmed answers. Do not infer a native child
stop from a tool-dispatch hook.

## Windows notes

- Use `py -3` if `python3` is not available.
- The installer normalizes paths and backslashes; do not mix shell path styles.
- Hook commands use the Python executable that ran the installer. If that
  interpreter moves, reinstall.
- Long paths can exceed legacy limits; enable long-path support or use a
  shorter project root.

## Collecting a useful report

Include:

- Auto-Orchestrator version and commit;
- client and version (`opencode --version`, Codex version, etc.);
- install scope (project/global) and `--platform`/`--opencode-version`;
- the exact command and its output;
- whether the failure reproduces in a clean temporary project;
- `brief` and `native-dispatch status` JSON, with secrets removed.

Never paste provider tokens, API keys, session cookies or proprietary source
code into a public issue.
