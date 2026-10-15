# OpenCode V5 Desktop shell compatibility

OpenCode Desktop 2.0.28 native shell results report integer process status in `event.result.metadata.exit` and their effective working directory in `event.input.workdir`. Older compatible `exitCode` and `cwd` fields are also supported.

Only pre-approved shell commands with an explicitly matching project working directory can produce verification evidence. A command with a missing or foreign workdir does not count.

Shell commands other than the approved test are conservatively treated as possible file mutations, including commands that appear read-only. Previously passed test evidence becomes stale; rerun the approved test after such a shell command. Repeating a test also invalidates its older result until the new invocation reports a native integer exit status.

This makes the quality gate conservative, not omniscient: processes operating outside the native hook path and file changes made by an approved test itself are not independently observed. Do not claim every code change is detected or claim time savings without a matched serial baseline.

Live integration checks should exercise native exit 0 and nonzero, missing exit, different workdir, a shell edit after a test, and a retest after such a shell edit. Test only in disposable projects.
