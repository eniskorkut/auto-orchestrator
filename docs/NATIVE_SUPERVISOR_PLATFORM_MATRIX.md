# Native Supervisor integration — platform contracts and limits

This document distinguishes **what CI simulates** from **what an installed native IDE demonstrates**. No native application is launched in GitHub Actions. A green mock/contract test does not mean an authenticated native IDE E2E run passed.

## Core invariant

The optional `native_supervision: true` plan uses SQLite transactional DAG claims, attempt-specific leases and scoped write conflicts. A claimed task does not create an IDE agent. The parent must spawn through the actual native IDE tool and bind the returned native identifier to the claim. Supervisor requires native start AND matching native stop observations, then a lease-bearing, scope-checked structured result before task completion. On interruption, no stop, mismatched ID, timeout or untrusted tool return, dependent tasks remain blocked. Supervisor does not itself cancel a native agent, auto-merge a worktree or guarantee model-level compliance.

Native hook start can arrive before the parent learns the agent ID. Recent hashed unmatched starts are buffered for up to 600 seconds and accepted on binding **only** if they occurred no earlier than two seconds before that task's claim. Stop-before-start is never buffered or accepted. Observations older than that interval are unusable; if a hook never fires, fail closed rather than guessing completion.

Existing Supervisor SQLite databases are migrated additively. Legacy stop-only attestations are invalidated and must be obtained again through a native start/stop cycle before releasing a supervised task. This may leave a previously running task blocked; do not synthesize a hook to bypass it.

## Platform matrix

| Platform | Native delegation | Native evidence usable by Supervisor | Current limits |
| --- | --- | --- | --- |
| Codex | spawn_agent / wait where available | SubagentStart + SubagentStop containing matching agent_id | Hooks must be enabled/trusted; model capabilities and live CLI parity require IDE tests. A stop without a matching observed start is rejected. |
| OpenCode V2 | native subagent tool | Tool start/stop observation and Quality Gate | Background spawn tool return does not prove child completed; never use spawn completion alone to unlock Supervisor. Live desktop tests on the installed version are required. |
| Cursor IDE | native Task subagents | Project-scoped subagentStart ID; subagentStop only when exact same authoritative ID is provided | Documented subagentStop payload may omit subagent_id, and some Cursor versions fail to fire background stop. When missing, hold lease. User-level hooks are deliberately not enabled because workspace cannot be attested by global stdin alone. |
| Antigravity | invoke_subagent when available | SessionStart/Stop only; insufficient for per-child release | PreToolUse/PostToolUse measure invoke dispatch, not child lifecycle; never treat them as per-child stop evidence or silently auto-complete Supervisor tasks. |

## Native smoke acceptance

1. Install into a disposable repository, verify hooks are explicitly trusted, run the client and inspect actual native callback payload fields. Do not use mocked events as live proof.
2. Claim two disjoint write tasks and one dependent task with native_supervision enabled. Capture native IDs; bind only exact IDs. Native start may precede bind; it is buffered for a limited time.
3. Verify concurrent disjoint claims, scope conflicts, dependency waiting, an unmatched/missing stop, timeout, cancellation acknowledgment and stale retry rejection. Never fabricate a native stop to force progress.
4. For Codex and Cursor: test real per-child start/stop; for Cursor separately inspect foreground and background behaviors. For Antigravity, mark native per-child completion **not supported** unless the installed runtime exposes a documented unique child stop identity. For OpenCode V2, independently confirm child lifetime rather than background dispatch duration.
5. Verify the JSONL contains only hashed IDs, status and times; no prompts, source, tokens, plaintext native IDs, or other project logs.
6. Repeat installation/uninstallation with pre-existing user-owned hooks and configuration. Check the original bytes and contents are preserved.
7. Never run smoke tests against a repository with live uncommitted user work; use separate throwaway workspaces.

## Upstream references

- Codex native hooks: https://developers.openai.com/codex/hooks
- Codex subagents: https://developers.openai.com/codex/subagents
- Cursor hooks and native subagent payloads: https://cursor.com/docs/hooks
- Cursor documented/acknowledged background stop limitations: https://forum.cursor.com/t/subagentstop-never-fires-for-background-subagents-documented-summary-modified-files-agent-transcript-path-missing-or-null/166681
- Antigravity hooks: https://www.antigravity.google/docs/hooks
- Antigravity custom subagents: https://www.antigravity.google/docs/subagents/

Quality Gate remains opt-in. Benchmarked serial-versus-parallel speedup is intentionally out of scope.
