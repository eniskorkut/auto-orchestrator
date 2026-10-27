# Teamwork from the main chat (V7.4)

Use the local bridge instead of asking the user to open another chat or terminal:

```bash
python tools/teamwork_chat.py --root . request-path
# write the exact request to the returned relative_path
python tools/universal_teamwork.py --root . --platform <platform> start --mode auto --request-file <relative_path>
# Codex read-only managed execution (write mode is not verified):
python tools/universal_teamwork.py --root . --platform codex --provider codex \
  --codex-sandbox workspace-write start --mode auto --request-file <relative_path>
# A normal start runs the central Assumption Guard and may return
# {"status":"interview_required"}; present the question and do not start code.
python tools/universal_teamwork.py --root . --platform <platform> status
python tools/universal_teamwork.py --root . --platform <platform> context
python tools/universal_teamwork.py --root . --platform <platform> handoff
python tools/universal_teamwork.py --root . --platform <platform> cancel
python tools/universal_teamwork.py --root . --platform <platform> resume
python tools/universal_teamwork.py --root . --platform <platform> grill start --grill-mode auto --request-file <relative_path>
python tools/universal_teamwork.py --root . memory install-hook
```

OpenCode exposes `/teamwork` and `/grill-me` through project commands. The
bridge creates the plan, initializes the durable milestone run, starts the
managed runner in a separate process and returns immediately. Child OpenCode
sessions are separate managed sessions and must never be presented as the main
chat. A Codex/Cursor/Antigravity origin does not silently launch the OpenCode
bridge; that requires an explicit `--provider opencode`. Codex managed tasks are
read-only with `approvalPolicy: never`. Project memory and Grill-Me state are shared by every supported IDE that
reads the same project root; native Codex, Cursor and Antigravity execution is
only claimed when that platform's local capability is actually verified.

Modes:

- `solo`: one implementer task, one milestone, deterministic verification only.
- `fast`: one milestone, targeted verification, no critic/challenger.
- `balanced`: build, tester and risk-based review/audit.
- `rigorous`: explorer, parallel build, tester, critic, challenger, auditor and
  success auditor.
- `auto`: repository profile plus explicit risk factors; missing repository
  facts fall back to `balanced`.

V7.6.1 adds a central Assumption Guard on every start/init/run/resume and a
versioned Verified Requirements Contract. Approved Grill-Me decisions become acceptance
criteria that are checked against real code/test evidence; unverifiable criteria
remain `not_verified` and cannot be reported as passed.

Cancellation is a local process signal unless a native ACK is explicitly
present. A child session is not a native hook start/stop proof.

## Native Teamwork 2.0 (V7.7)

Entry points are intentionally different per IDE; they share state, not a
process:

- **OpenCode** keeps `/teamwork` and `/grill-me`. Report origin platform,
  execution provider, mode and whether execution is native or managed.
- **Codex** uses `.codex/skills/teamwork/SKILL.md` and native subagents. Sandbox
  and approval belong to the parent session; per-agent sandbox keys are not
  claimed. There is no Codex `/teamwork` file.
- **Cursor** uses `.cursor/agents/` for planner, implementer, tester and
  reviewer roles plus this shared skill. It is a contract-only entry; no local
  Cursor CLI or managed adapter is assumed.
- **Antigravity** keeps native `/teamwork-preview` and native `/grill-me`
  authoritative. The fallback interview is `ao-grill-me`, not a second
  `grill-me`; our layer stores memory, contracts and handoffs.

Binding rules stay conservative: Codex needs a trusted matching hook pair,
OpenCode is foreground-tool-call-only, Cursor is start-ID-only and Antigravity
has no authoritative per-child pair. No entry point claims an external spawn,
native cancel ACK, background completion or resume from a file.
