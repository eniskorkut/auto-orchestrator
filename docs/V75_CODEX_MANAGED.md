# V7.5 — Codex Managed Execution (V7.5 branch)

This branch follows the V7.4.1 main merge. It validates the Codex CLI/app-server
managed path that V7.3 marked as contract-only.

## What was verified locally

- Codex CLI **0.164.0** installed through the official installer.
- Login: **ChatGPT** (no API key or token is stored by this project).
- Protocol default model: **gpt-6.1-sol** (7 models returned, default true).
- `codex app-server` official JSON-RPC protocol:
  `initialize` → `initialized` → `model/list` → `thread/start` →
  `turn/start` → live events → `turn/completed`.
- Default model selected from `model/list`: **gpt-6.1-sol**.
- Real read-only review task completed; no file modifications.
- Real `CodexAdapter` managed run completed with **73,718 tokens**, **1 tool
  item**, and no errors.
- Real Teamwork reviewer task completed through the Supervisor: **passed**, JSON
  verdict `pass`, **97,014 tokens**, **2 tool items**, milestone M1 passed.
- The protocol smoke recorded **110 events**, `turn/completed`, and **84,661
  tokens**; adapter and Teamwork are separate managed executions.

## Evidence

See [`assets/v75-codex-smoke.json`](assets/v75-codex-smoke.json). The file
contains sanitized metadata only: model IDs, hashed thread IDs, event counts,
token counts, statuses and durations. It contains no prompt, raw thread ID,
auth token, assistant text or local absolute path.

## OpenCode-side model check

OpenCode V2 has no OpenAI provider login on this machine. `opencode auth list`
shows an **OpenCode Go API key**; it is not a ChatGPT/OpenAI provider login.
`opencode models` exposes `opencode-go/gpt-6-luna`, which was used for a real
short read-only review of `calc.py` and returned the expected bug (360 ms event
window). This is **OpenCode using an OpenCode Go hosted GPT model**, not the
OpenAI provider and not Codex App-server. Evidence:
[`assets/v75-opencode-gpt-model.json`](assets/v75-opencode-gpt-model.json).

## Adapter changes

- Parse `thread/tokenUsage/updated` into input/output/reasoning/cache tokens.
- Count `item/started` events for `commandExecution`, `fileChange`, `mcpToolCall`
  and `webSearch` as tool activity.
- Include `tools`, `tool_count`, `tokens`, `cost` and cache breakdown in the
  Codex snapshot.
- Optional `sandbox` and `approvalPolicy` on `thread/start`; Teamwork tasks may
  set `sandbox` / `approval_policy`. A read-only reviewer uses
  `sandbox=read-only`, `approvalPolicy=never`.
- Capability matrix marks Codex managed execution `live-verified-local` for this
  local version. Resume remains implemented/contract-tested but is not claimed
  as live-smoked. Native cancellation ACK and terminal streaming remain false.

## Boundaries

These are distinct and must not be conflated:

1. OpenCode using an OpenCode Go GPT model (`opencode-go/...`): tested separately
   inside OpenCode. It is **not** the OpenAI provider or ChatGPT login.
2. Codex CLI managed process: tested through app-server.
3. Codex app-server managed session: tested through JSON-RPC and the adapter.
4. IDE-native Codex subagent: **not tested**; no IDE-native lifecycle claim.

## Limitations

- Cost is not emitted by the app-server notification stream; no cost is invented.
- One local sample; no repeated latency benchmark.
- Native Codex cancellation acknowledgement is not claimed.
- `thread/resume` is implemented and contract-tested, but not live-smoked here.
- Codex app-server approval requests are not interactively bridged in Teamwork;
  read-only tasks explicitly use `approvalPolicy: never`.
- Codex write-mode Teamwork integration is not part of this smoke.

## Reproduce

```bash
python3.11 tools/v75_codex_smoke.py --phase protocol --json-out /tmp/codex-protocol.json
python3.11 tools/v75_codex_smoke.py --phase adapter --json-out /tmp/codex-adapter.json
python3.11 tools/v75_codex_smoke.py --phase teamwork --json-out /tmp/codex-teamwork.json
```

Run each phase in a fresh process. A second app-server turn in the same process
can be delayed by provider/hook startup state. The harness also supports
`--phase all` for a single combined run, but separate phases are the reliable
form. It requires a locally authenticated Codex CLI.
