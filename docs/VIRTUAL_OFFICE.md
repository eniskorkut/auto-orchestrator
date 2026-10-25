# Virtual Office and Agent Inspector (V7.1)

The Virtual Office is a **read-only visualization** of the agent definitions registered for one project and the native lifecycle evidence already recorded for that project. It is not a control plane: opening a desk, moving a desk or opening the inspector never starts, stops, focuses or messages an agent.

![Virtual Agent Office with evidence-derived status and ghost desks](assets/agent-office-demo.png)

Two payloads drive the screen:

- `GET /api/projects/<project_id>/office` — desks, unlinked sessions and office summary.
- `GET /api/projects/<project_id>/agents/<agent_definition_id>` — the Agent Inspector.

Both are derived by [`tools/office_model.py`](../tools/office_model.py) from registry metadata, explicit bindings, run logs and Supervisor state. Missing evidence stays missing. See [MULTIPROJECT.md](MULTIPROJECT.md) for registry and binding operations and [CAPABILITY_MATRIX.md](CAPABILITY_MATRIX.md) for what each platform can actually prove.

## Opening the office

```bash
python tools/dashboard.py --root /path/to/project --open
```

Select a project, then open **Office**. The dashboard is loopback-only and read-only. Demo mode (`--demo`) uses clearly labelled synthetic projects; it reads no project evidence and is not a benchmark.

## Room themes

The registry theme is one of `ai`, `security`, `web` or `custom`. It selects a visual room treatment only:

| Theme | Intended room |
|---|---|
| `ai` | Research lab |
| `security` | Security operations room |
| `web` | Development studio |
| `custom` | Neutral custom room |

The room theme can be changed in the UI per project and is remembered **in the current browser**; it is a presentation preference, not run evidence and not a rewrite of the project registry. The registry remains the authority for the project's configured theme. Desk order is likewise a browser-local arrangement preference.

## Status semantics

A desk status is evidence-mapped by the backend. It is never inferred from a role name, task text, elapsed time or a colour.

| Status | When it is emitted | What it does **not** mean |
|---|---|---|
| `unknown` | No binding; no matching native run/task/span; a stale open span whose run is no longer live; or no terminal evidence. | It is not “idle”. No binding is explicitly shown as **Unknown**. |
| `dispatching` | A linked Supervisor task is active but no native start was observed. Confidence is `inferred`. | The native child is not proven to exist. |
| `working` | A bound span is open, or a linked task has an observed native start without a stop, and the run is live. Confidence is `observed`. | It is **not proof of liveness**. The detail says start observed, stop not observed. |
| `completed` | A matched span/task is closed and the run succeeded or the task completed. | Completion is not the same as quality-gate verification. The detail distinguishes gate verified, review attested, self-reported, rejected, or unverified. |
| `failed` | The run or linked task is failed/cancelled. | No verification can be inferred from failure. |
| `reviewing`, `running-tests` | Reserved for a native event that explicitly justifies the label. | They are never inferred from a role or plan. The current office model does not assign them without such evidence. |

`idle`, `awaiting-approval` and `blocked` exist in the interface vocabulary, but the office model does not turn “no evidence” into any of them. A desk with no binding stays **Unknown/Unlinked**.

### Open spans and stale evidence

An open native span means only that a start was observed and no matching stop was recorded. It is rendered with an explicit start-observed / stop-missing warning. If the run is no longer live, the status becomes `unknown` with “Stale open span; no stop observed.” The console never upgrades an open span into “still running” or invents a completion.

### Unlinked native sessions

Native sessions with no explicit agent binding appear as **ghost desks**. They have no invented name or role; the UI labels them “Unlinked native session” and states that no agent definition is bound. Their status follows the observed run/span (`working`, `completed`, `failed` or `unknown`) but does not attach them to a registered agent. Correlating by role, task text or timing is forbidden. A task-level binding attributes the whole run; use a per-span `native_hash` binding when sibling spans need individual attribution.

### Office summary

The summary reports the number of registered desks, desks with `working`, desks with `unknown`, desks with `completed`, and unlinked sessions. It is a count of recorded states, not a productivity score.

## Agent Inspector

Selecting a desk opens the inspector drawer. Its sections are:

| Section | Contents |
|---|---|
| **Header** | Agent name, role, project and current status with evidence detail. |
| **Identity** | Name, role, project, IDE/platform, verified model, native session and explicit binding state. |
| **Activity** | Current run and task, start/last-event timestamps, observed duration, last normalized event, recent normalized events and native checks. |
| **Files & Git** | Worktree path, changed files reported by a structured task result, diff availability, commit and merge-readiness fields. |
| **Evidence** | Quality-gate verification, native stop observation, review attestation and diagnostics. A review attestation is a record, not proof of reviewer independence. |
| **Terminal** | An explicit unsupported/read-only panel. No command stream is rendered. |

### Identity honesty

- A model is shown as verified only when telemetry attests one. The current model reports `verified_model: null` with “Model is not attested by run telemetry”; a model string in the registry is an agent-definition field, not runtime proof.
- A native session is shown only when the binding/payload supplies a safe identifier. Raw provider-native IDs are not persisted; run telemetry contains opaque agent IDs and the binding chain uses a SHA-256 `native_hash`.
- “Explicitly bound” means a project-local binding exists. It does not mean the agent is currently alive.

### Activity and files

Recent events are normalized through [`tools/event_model.py`](../tools/event_model.py) with a closed field set. Event names remain technical and are shown in English in both UI languages. Checks show only recorded check IDs, pass/fail state, exit code and observation time; a check result is not a claim of full task correctness.

Changed files come from a structured task result, not from reading the repository. `diff_available` is false in the current inspector and the UI says so. A clean worktree or a manifest that reports merge-ready is **not** a tested merge; integration still requires review and tests.

### Evidence panel

- **Quality gate:** a recorded verification object, including rejected or self-reported claims. Self-reported verification is not gate attestation.
- **Native stop:** whether a matching native stop was observed. A stop is termination evidence, not proof that tests passed or that the task is complete.
- **Review:** a recorded review attestation bound to the task/result digest. It is not independent proof that a separate reviewer ran.
- **Diagnostics:** categorical reasons such as a stale or interrupted event stream. The console does not reconstruct a missing terminal event.

## Terminal limitations

V7.1 has **no authenticated live terminal/PTY stream**. The inspector therefore reports:

```json
{
  "terminal": {
    "supported": false,
    "reason": "No authenticated live terminal stream is attached. Observed lifecycle events only."
  }
}
```

The UI renders no simulated command stream, prompt, shell output or typing animation. Even if a future backend reports `supported: true`, this read-only console still renders no command stream; a terminal would require a separate authenticated design. Lifecycle events, spans and checks are not a terminal.

## Customization

- **Language:** English and Turkish, switchable from the top bar or Settings. The choice is stored as `ao-language` in browser storage. Technical event names stay in English; missing keys fall back to English.
- **Interface theme:** dark or light, stored as `ao-theme`; the system preference is used when no local choice exists.
- **Room theme:** per project, stored as `ao-office-theme:<project_id>` in this browser. It changes visuals only.
- **Desk arrangement:** use the arrow controls on each desk in arrange mode. The order is stored as `ao-desk-order:<project_id>` in this browser. The controls are buttons, not drag-only interactions.
- **Reduced motion:** an explicit toggle stored as `ao-reduced-motion`, plus the system `prefers-reduced-motion` preference when no local choice exists. It stops monitor glow, typing and bobbing animations.

Browser storage is local preference only. It is not uploaded and is not evidence.

## Accessibility

- A skip link targets the main content; navigation and project sections use semantic landmarks and labelled controls.
- Status is never conveyed by colour alone: every badge includes a text label and a symbol. Evidence source and confidence are written out.
- Desk cards are a labelled list; each desk has an accessible “open inspector” label. Arrange controls have labelled left/right/up/down actions.
- The inspector is a modal dialog: opening it moves focus to the close control, Tab is trapped inside, Escape closes it, and focus returns to the desk that opened it. Settings behaves as a labelled dialog with an explicit close control and focus return.
- Project tabs support left/right arrow navigation in addition to pointer clicks.
- Long IDs and paths use wrapping and truncation with accessible titles; do not rely on hover alone.
- The layout respects reduced motion and avoids infinite decorative animation when requested.
- The UI has no external fonts, scripts, images or analytics. It is served from loopback with a strict CSP.

## Responsive design

- The office grid uses three columns on wide screens, two below 1200 px and one below 700 px.
- The inspector is a drawer; below 860 px it becomes full width, hides the pinned-agent rail and switches field grids to one column.
- The global overview and project cards reflow rather than requiring horizontal scrolling.
- The screen is usable without a mouse. Touch targets use buttons, not hover-only affordances.

These are CSS/JS contracts. They are not a claim that every native IDE embedded browser or mobile browser was manually certified; run the dashboard in your target browser and report layout issues.

## Split view (future)

A side-by-side split view of office, inspector and terminal is **not implemented in V7.1**. The current inspector is a drawer over the office. Any future split view must preserve:

1. the same evidence-mapped statuses and Unknown/Unlinked honesty;
2. no fabricated terminal stream;
3. no project mixing when switching projects;
4. keyboard and screen-reader access at narrow widths.

Do not describe a planned split view as shipped functionality.

## Runtime and telemetry decisions

The core dashboard remains a **stdlib HTTP server**. FastAPI and an ASGI stack are not dependencies, are not required for the multi-project API, and must not replace the Supervisor's SQLite database as the durable evidence store. An optional advanced API can be added later only if it preserves the read-only, loopback-only contract.

[`tools/telemetry.py`](../tools/telemetry.py) is an optional OpenTelemetry adapter. It is a no-op when `opentelemetry-api`/`opentelemetry-sdk` are absent, requires an explicit `configure(...)` call, exports only whitelisted attributes, and never requires a collector. Enabling it does not affect the dashboard's evidence model.

## Related documentation

- [Multi-project registry and isolation](MULTIPROJECT.md)
- [Native capability matrix](CAPABILITY_MATRIX.md)
- [Evidence dashboard reference](DASHBOARD.md)
- [Native Supervisor platform contracts](NATIVE_SUPERVISOR_PLATFORM_MATRIX.md)
