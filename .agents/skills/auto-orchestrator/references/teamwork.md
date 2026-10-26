# Teamwork from the main chat (V7.4)

Use the local bridge instead of asking the user to open another chat or terminal:

```bash
python tools/teamwork_chat.py --root . request-path
# write the exact request to the returned relative_path
python tools/universal_teamwork.py --root . --platform <platform> start --mode auto --request-file <relative_path>
# Codex read-only managed execution (write mode is not verified):
python tools/universal_teamwork.py --root . --platform codex --provider codex start --mode auto --request-file <relative_path>
python tools/universal_teamwork.py --root . --platform <platform> status
python tools/universal_teamwork.py --root . --platform <platform> context
python tools/universal_teamwork.py --root . --platform <platform> handoff
python tools/universal_teamwork.py --root . --platform <platform> cancel
python tools/universal_teamwork.py --root . --platform <platform> resume
python tools/universal_teamwork.py --root . --platform <platform> grill start --grill-mode auto --request-file <relative_path>
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

- `fast`: one milestone, targeted verification, no critic/challenger.
- `balanced`: build, tester and risk-based review/audit.
- `rigorous`: explorer, parallel build, tester, critic, challenger, auditor and
  success auditor.
- `auto`: repository profile plus explicit risk factors; missing repository
  facts fall back to `balanced`.

Cancellation is a local process signal unless a native ACK is explicitly
present. A child session is not a native hook start/stop proof.
