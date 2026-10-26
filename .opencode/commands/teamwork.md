---
description: Start or control Auto-Orchestrator Teamwork from this chat
agent: build
subtask: false
---
The user invoked `/teamwork` with these arguments:

$ARGUMENTS

You are the Supervisor. Use the real local Teamwork engine and do not invent
execution, cancellation, test or completion evidence. Child sessions are separate
managed OpenCode sessions; never merge them into this chat.

Run exactly one shell command with the shell tool:

1. If the arguments start with `status`, `agents`, `milestones`, `cancel` or
   `resume`, run:

   `python3.11 tools/teamwork_chat.py --root . <subcommand>`

2. Otherwise parse an optional leading `--mode fast|balanced|rigorous|auto`
   (default `auto`). Remove it from the request and run:

   `python3.11 tools/teamwork_chat.py --root . start --mode <mode> --request "<remaining request>"`

3. If there is already an active run and the user did not explicitly ask for a
   new one, run `status` instead of creating a duplicate run.

After the command returns:
- report the run id, selected mode, risk assessment, current milestone, active
  agent count and verified progress;
- state that child agent sessions are separate managed sessions;
- state that cancellation is local-signal-only and never a native ACK;
- do not wait synchronously for the child run to finish. The user can ask
  `/teamwork status`, `/teamwork agents` or `/teamwork milestones`.
