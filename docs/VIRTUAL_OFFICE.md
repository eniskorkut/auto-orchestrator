# Virtual Office 2.0 and Agent Inspector (V7.2)

The Virtual Office is a **read-only, evidence-derived visualization** of one project's agent definitions and the native lifecycle events already recorded for that project. It is not a control plane: panning, zooming, filtering rooms, moving a room in the layout editor or opening the inspector never starts, stops, focuses or messages an agent.

![The continuous V7.2 pixel-art office](assets/agent-office-demo.png)

Two payloads drive the screen:

- `GET /api/projects/<project_id>/office` — agents, room assignment, unlinked sessions and the office summary.
- `GET /api/projects/<project_id>/agents/<agent_definition_id>` — the Agent Inspector.

Both are derived by [`tools/office_model.py`](../tools/office_model.py) from registry metadata, explicit bindings, run logs and Supervisor state. Missing evidence stays missing. See [MULTIPROJECT.md](MULTIPROJECT.md) for registry and binding operations and [CAPABILITY_MATRIX.md](CAPABILITY_MATRIX.md) for what each platform can prove.

## One office, not a card grid

V7.2 replaces the repeated workstation cards with **one continuous Canvas 2D office per project**. The map contains department rooms, corridors, doors, desks, meeting furniture, plants, shelves, whiteboards and a central spawn lobby. Agents walk between rooms; they are never rendered in separate mini-scenes.

The renderer is split into small browser scripts under
[`tools/dashboard_assets/office/`](../tools/dashboard_assets/office/):

| File | Responsibility |
|---|---|
| `tilemap.js` | Grid, walkable tiles, walls, doors, furniture collision, room lookup. |
| `pathfinding.js` | Deterministic 4-direction A* and nearest-walkable fallback. |
| `office-scene.js` | Canvas renderer, camera, spawn/walk/sit simulation, hit testing, filters. |
| `agent-sprites.js` | Original procedural pixel-art characters; no external assets. |
| `agent-state-machine.js` | Spawn gating, conservative visual state, room/desk derivation, dedupe. |
| `office-layouts.js` | Versioned per-project layouts, themes, validation and local overrides. |
| `room-manager.js` | UI-side room and desk fallback helpers. |
| `layout-editor.js` | Grid-snapped room and desk dragging with validation. |

`office.js` is the adapter between the dashboard state and the scene. It owns the map shell, summary, room chips, roster and inspector bridge, not the rendering loop.

## Opening the office

```bash
python tools/dashboard.py --root /path/to/project --open
```

Select a project, then open **Office**. The dashboard is loopback-only and read-only. Demo mode (`--demo`) uses clearly labelled synthetic projects; it reads no project evidence and is not a benchmark.

## Spawn and lifecycle contract

A persistent agent definition and a real working session are different things.

- A character is created only for an entry with a real binding or an observed native session. The backend exposes this as `spawnable: true` and a stable `session_key`.
- A registry agent without a binding stays in the roster with **no character on the map**. It is never shown as working, idle or walking.
- Native sessions without a binding appear as **Unlinked native session** characters in the Spawn/Lobby. No name or role is invented for them.
- Duplicate snapshots or repeated events with the same `session_key` update the existing character; they do not spawn a second one.
- Demo mode may create synthetic sessions, but every demo page is labelled `DEMO DATA` and the sessions are never written to disk.

The visual sequence is:

```text
task.claimed
    ↓
native.agent.started (observed)
    ↓
character spawns in the Spawn/Lobby with a spawn animation
    ↓
walk to the assigned department room through doors and corridors
    ↓
sit at a desk in that room
    ↓
show the conservative visual state supported by evidence
```

`agent.spawn`, `agent.walk.to.room` and `agent.sit` are **animation events only**. They are not native start/stop evidence and are never written as such.

## Room assignment

Rooms are identified by lowercase department ids. The seven defaults are
`frontend`, `backend`, `qa`, `review`, `research`, `meeting` and `lobby`.
Additional lowercase ids are allowed by the schema.

Assignment is deterministic and uses no free-text guessing:

1. explicit `task.department` or `task.assigned_room`;
2. explicit agent `default_room` or `assigned_room`;
3. verified task role mapping: `tester → qa`, `reviewer → review`, `explorer → research`;
4. otherwise the Spawn/Lobby with source `unassigned`.

An `implementer` task without an explicit department stays in the lobby. Names, objectives, elapsed time and colour never select a room. The backend room is authoritative; the client fallback mirrors the same order only when the field is absent.

When a task changes to another department, the existing character walks to the new room instead of teleporting. Door tiles, walls, desks and blocked furniture are part of the A* grid.

## Visual states versus execution state

The canvas state is a presentation state. It never changes the execution record.

| Visual state | Evidence required | Not implied |
|---|---|---|
| `spawn`, `walking`, `idle` | Animation only. | Not execution state. |
| `working` | Open live native span/start with an explicit activity, or a live task start; otherwise unknown. | Not proof of liveness or correctness. |
| `reading`, `testing`, `reviewing` | An explicit native event type that supports the activity and a live span. | Never inferred from a role name or plan. |
| `awaiting` / `blocked` | Task/evidence explicitly supports it. | Not inferred from “no events”. |
| `completed` | Matched span/task closed and the run/task succeeded. | Not quality-gate verification by itself. |
| `failed` | Run or linked task failed/cancelled. | No verification inferred from failure. |
| `unknown` | Missing binding, stale open span, no live start or unsupported activity. | Never rendered as work. |

A stale open span becomes `unknown` with a start-observed/stop-missing warning. The UI does not invent a completion when an SSE connection drops; it reconnects and applies an authoritative snapshot.

The office summary (desks, working, unknown, completed, failed, unlinked ghosts) is a count of recorded states, not a productivity score.

## Agent Inspector

Clicking a character opens the inspector drawer on the right. The map resizes beside it; closing it restores the full width. The sections are:

| Section | Contents |
|---|---|
| **Header** | Agent name, role, project and status with evidence detail. |
| **Identity** | Name, role, project, IDE/platform, verified model, native session and explicit binding state. |
| **Activity** | Current run and task, start/last-event timestamps, duration, last normalized event, recent events and native checks. |
| **Files & Git** | Worktree path, changed files from structured results, diff availability, commit and merge-readiness. |
| **Evidence** | Quality-gate verification, native stop observation, review attestation and diagnostics. |
| **Terminal** | An explicit unsupported/read-only panel. No command stream is rendered. |

### Identity honesty

- A model is shown as verified only when telemetry attests one. The current model reports “not attested” and never promotes a registry string to runtime proof.
- Native IDs are not exposed raw. Bindings use run ids and SHA-256 `native_hash` prefixes.
- “Explicitly bound” does not mean the agent is currently alive.
- Worktree and merge-readiness fields are evidence, not proof that a merge was tested.

### Terminal limitations

The dashboard has **no authenticated live terminal/PTY stream**. The inspector reports:

```json
{
  "terminal": {
    "supported": false,
    "reason": "No authenticated live terminal stream is attached. Observed lifecycle events only."
  }
}
```

No simulated prompt, shell output or typing stream is rendered. Even if a future backend reports `supported: true`, this read-only UI still renders no command stream. Lifecycle events, spans and checks are not a terminal.

## Per-project layouts and themes

The registry theme is one of `ai`, `security`, `web` or `custom`. A theme selects a complete floor plan — room sizes, corridors, furniture and palette — not just a colour filter.

- `ai`: research-heavy lab with a large Research Lab.
- `security`: Security Operations layout with meeting, research, backend and QA rooms.
- `web`: frontend-heavy studio with frontend, backend, review, research, QA and meeting rooms.
- `custom`: smaller neutral office.

The theme can be changed per project in the UI. The choice is stored in this browser as `ao-office-theme:<project_id>`; it does not rewrite the project registry.

Layouts are versioned JSON (`version: 1`) with rooms, doors, desks, seats, furniture and decor. A validated per-project override is stored under:

```text
ao-office-layout:<project_id>
```

An invalid or missing override falls back to the deterministic theme layout. Project switch destroys the old scene before loading the new project's layout and agent state; positions, filters and overrides never leak between projects.

### Layout editor

**Edit layout** enables a grid overlay. Rooms can be dragged with their door and desks; a door can slide along any non-corner wall; desks can be moved inside their room. Changes are snapped to the grid and rejected when they leave bounds, overlap walls, open onto another room or make a seat unreachable. **Save layout** writes the local override; **Reset layout** removes it and reloads the theme layout. Escape cancels an active drag. The editor never mutates evidence or registry data.

## Managed sessions (V7.3)

Real OpenCode managed sessions are exposed by the office API separately from
native unlinked sessions. They appear in the roster as `task · role`, walk to
the room derived from their task department, and open a **Managed session**
inspector with task, provider, model, status, tool count, cost, timestamps and a
short session hash.

Managed completion is observed from the managed adapter stream; it is **not** a
native stop. The inspector explicitly says that native stop is not claimed, and
the native/unlinked evidence path is unchanged.

## Controls and accessibility

- **Room chips** filter the map and the roster by department. Filtering is visual only.
- **Zoom** buttons and `+` / `-` / `0` on a focused canvas; wheel zoom, drag pan and double-click reset.
- **Roster** is a keyboard-accessible list; Enter opens the same inspector as clicking a character.
- **Hover tooltip** shows agent name, status and room; it is not the only way to reach the information.
- **Reduced motion** follows `prefers-reduced-motion` or the explicit toggle stored as `ao-reduced-motion`. It removes bobbing and walk-frame animation while preserving state and position.
- **Mobile** layouts stack the roster under the map. The map supports drag panning and the zoom controls; touch targets are buttons, not hover-only affordances.
- **Status** is never conveyed by colour alone; every roster row and badge has a text label and an evidence warning where relevant.
- The inspector is a modal dialog: focus moves to the close control, Tab is trapped, Escape closes it, and focus returns to the element that opened it.
- No external fonts, scripts, images or analytics are used. The favicon is a local SVG served from `/assets/`.

These are implemented contracts, not a claim that every native IDE embedded browser or mobile browser has been manually certified. Run the dashboard in your target browser and report layout issues.

## Real-time updates

The office uses the existing project SSE stream and authoritative snapshots:

- reconnect fetches a snapshot before continuing updates;
- duplicate session keys update one character;
- a dropped connection never synthesizes a completion or a working state;
- execution state and visual pose are stored separately, so an animation ending cannot mark a task complete;
- switching project or theme destroys the old canvas loop and event wiring.

## Testing

Unit tests:

```bash
python3.11 -m unittest discover -s tests -q
npm run test:js
```

The JS suite covers layout validation, project overrides, room movement, A* through doors, wall/furniture collision, duplicate session keys, stale spans, registry-only agents and conservative visual state mapping.

Browser tests use Playwright with the system Chrome channel and the built-in demo server:

```bash
npm run test:browser
npm run test:browser:update   # refresh visual baselines intentionally
```

They cover one-canvas rendering, room filters, registry-only agents, inspector resizing, project isolation, the layout editor, reduced motion, keyboard access, mobile pan/zoom and screenshots at 1440×900, 1280×800, 768×1024 and 390×844. Baselines live in `tests/browser/screenshots/`. A passing browser test is local Chrome evidence, not proof for every platform or IDE embedded browser. Playwright is a development-only Apache-2.0 dependency; the runtime office assets are original procedural Canvas 2D drawing, with no copied AgentSpace/Pixel Agents/PixiJS code or art.

## Performance (measured)

The map is a single Canvas 2D scene. Static scenery is cached to an offscreen canvas; each frame redraws only the visible map and z-sorted characters. `scene.getStats()` reports live FPS, drawn frames, DPR and the browser's JS heap when available.

Local measurement with the 12-character demo office, Chrome 1.64 Playwright headless, after all walks settled:

| Viewport | Canvas (CSS) | FPS (2 s sample) | JS heap |
|---|---|---|---|
| 1440×900 | 1096×556 | 60 | 5.3 MB |
| 1280×800 | 936×494 | 60 | 5.8 MB |
| 768×1024 | 738×633 | 60 | 4.5 MB |
| 390×844 | 360×521 | 60 | 4.2 MB |
| 1440×900 @ DPR 2 | 2192×1112 backing | 59.9 | 4.9 MB |

These are local demo measurements, not a benchmark or a guarantee for every machine, GPU, browser or IDE embedded webview. The render loop pauses when the document is hidden and `prefers-reduced-motion` freezes decorative frames.

## Runtime decisions

The core dashboard remains a **stdlib HTTP server**. FastAPI and an ASGI stack are not dependencies and must not replace the Supervisor's SQLite database as the durable evidence store. Canvas 2D was chosen over PixiJS because the scene is small, dependency-free and does not need a WebGL renderer. The renderer, pathfinding, state machine and layout persistence remain separate modules.

## Related documentation

- [Multi-project registry and isolation](MULTIPROJECT.md)
- [Native capability matrix](CAPABILITY_MATRIX.md)
- [Evidence dashboard reference](DASHBOARD.md)
- [Native Supervisor platform contracts](NATIVE_SUPERVISOR_PLATFORM_MATRIX.md)
