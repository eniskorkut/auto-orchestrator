# Teamwork from the main chat (V7.4)

Use the local bridge instead of asking the user to open another chat or terminal:

```bash
python tools/teamwork_chat.py --root . start --mode auto --request "<request>"
python tools/teamwork_chat.py --root . status
python tools/teamwork_chat.py --root . agents
python tools/teamwork_chat.py --root . milestones
python tools/teamwork_chat.py --root . cancel
python tools/teamwork_chat.py --root . resume
```

OpenCode exposes `/teamwork` through `.opencode/commands/teamwork.md`; the
command prompt tells the main agent to invoke this bridge. The bridge creates
the plan, initializes the durable milestone run, starts the managed runner in a
separate process and returns immediately. Child OpenCode sessions are separate
managed sessions and must never be presented as the main chat.

Modes:

- `fast`: one milestone, targeted verification, no critic/challenger.
- `balanced`: build, tester and risk-based review/audit.
- `rigorous`: explorer, parallel build, tester, critic, challenger, auditor and
  success auditor.
- `auto`: repository profile plus explicit risk factors; missing repository
  facts fall back to `balanced`.

Cancellation is a local process signal unless a native ACK is explicitly
present. A child session is not a native hook start/stop proof.
