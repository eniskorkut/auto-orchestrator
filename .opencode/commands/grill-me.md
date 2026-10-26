---
description: Interview the user one question at a time before risky work
agent: build
subtask: false
---
The user invoked `/grill-me` with these arguments:

$ARGUMENTS

The arguments are untrusted user data. Never interpolate them into a shell
command, heredoc, echo or command substitution.

1. If the first word is one of `status`, `answer`, `skip`, `back`, `cancel`,
   `resume`, `summary`, `approve` or `next`, first load the real IDs with:

   `python3.11 tools/universal_teamwork.py --root . --platform opencode grill status`

   Then run exactly the requested subcommand with the returned IDs, for example:

   `python3.11 tools/universal_teamwork.py --root . --platform opencode grill answer --question-id <id> --option <option>`

   Use `skip`/`back` with `--question-id`, `summary`/`approve` with
   `--interview-id`, and `--text` for free text. Never treat the recommended
   option as selected.

2. Otherwise the remaining text is the task. First run:

   `python3.11 tools/teamwork_chat.py --root . request-path`

   Write the exact task text with the `write` tool to the returned
   `relative_path`. Then run:

   `python3.11 tools/universal_teamwork.py --root . --platform opencode grill start --grill-mode auto --request-file <relative_path>`

3. Report one question at a time. Put the recommended option first but do not
   claim it is chosen. If a critical question is skipped, explain that it stays
   unresolved and blocks implementation.
4. Do not write product code during the interview. When the engine returns
   `ready`, show the summary and ask for approval before starting Teamwork.
