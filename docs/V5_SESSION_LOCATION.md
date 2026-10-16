# V5 OpenCode session ownership and moved-parent telemetry

The OpenCode V2 plugin runtime can deliver session and tool events to more than one loaded project plugin. The location in `ctx.location.directory` identifies **the plugin instance**, not necessarily the event's current session. An event's shell `input.workdir` is tool input, not proof of session ownership.

For V2 clients exposing `ctx.session.get({sessionID})`, the adapter now checks the native `Session.Info.location.directory` on each relevant lifecycle and tool event. Each plugin instance accepts events only for the session currently belonging to that instance's project location. The adapter **never writes into a different project directory**. This permits a resumed parent with a native, updated session location to be observed by the receiving project's plugin, even when its event omits a location or carries a stale location from before `session_move`.

If session lookup throws or has no authoritative directory, the adapter does not create or modify a run. When an older client provides no session.get method at all, it retains the prior explicit event-location requirement; lack of location is not authorization. It does not fall back to event metadata when a native session lookup fails.

A session opened under Default Project but executing a shell with workdir set to another project is **not** moved automatically. Its native session location still controls which plugin instance owns its telemetry, and its shell workdir mismatch does not qualify as a V5 approved check. To work in another project, create a new OpenCode session in that project or use a supported session move, not just a shell working-directory override.

Moving a session while an existing run is open can leave a partial run under the original plugin instance. For safety, the old instance stops modifying that run once native ownership changes. A new run in the destination project can begin with the first observed native event; no historical event replay or fabricated completion is attempted. This boundary is not the same as guaranteeing seamless turn migration.

## Acceptance checklist

- Directly created root session, with true native session lookup and matching location: start, tool spans, finish, opt-in verification.
- Session moved into the project with missing or stale event location: current authoritative session lookup owns the events, destination plugin records them, origin plugin does not.
- Cross-project event carrying forged/stale event location: native session ownership wins.
- Wrong shell workdir under a native Default Project session: no redirection, no foreign verification.
- Missing/error native session lookup: no writes.
- Session moved away mid-run: old plugin instance stops updating the run; do not claim that run completed.
- Repeat existing V5 checks: verified only with exact approved native exit-code evidence after last observed mutation, no user content leaked.

CI executes simulated hook/session.get shapes; it cannot prove that a specific Windows OpenCode Desktop build routes all moved-session callbacks to the destination instance. Run real Desktop smoke tests before claiming complete session_move support.
