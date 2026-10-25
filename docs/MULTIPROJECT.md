# Multi-project evidence console (V7.1)

V7.1 lets one local dashboard read evidence from several project roots without mixing their runs. The **registry is metadata**; each project's evidence stays in that project's own `.auto-orchestrator/` directory. The dashboard remains a loopback-only, read-only viewer. It does not upload evidence, start agents or write project evidence.

![Global overview of three registered projects](assets/global-overview-demo.png)

- [Virtual Office and Agent Inspector](VIRTUAL_OFFICE.md)
- [Native capability matrix](CAPABILITY_MATRIX.md)
- [Evidence dashboard reference](DASHBOARD.md)

## Registry

### Location

| Setting | Path |
|---|---|
| Default with `AUTO_ORCHESTRATOR_HOME` | `$AUTO_ORCHESTRATOR_HOME/projects.json` |
| Fallback | `~/.auto-orchestrator/projects.json` |

`AUTO_ORCHESTRATOR_HOME` changes the registry location only. It does not move a project's run logs, Supervisor database, bindings or worktrees. `--registry PATH` overrides the location for one CLI/dashboard process.

### Schema version 2

```json
{
  "version": 2,
  "projects": [{
    "project_id": "p_<16 lowercase hex>",
    "name": "Research Lab",
    "description": "",
    "root": "/canonical/absolute/path",
    "theme": "ai",
    "created_at": 0,
    "updated_at": 0,
    "agents": [{
      "agent_definition_id": "a_<12 lowercase hex>",
      "name": "Ada",
      "role": "RAG Engineer",
      "platform": "opencode",
      "model": null,
      "color": "#7dd3fc",
      "created_at": 0
    }]
  }]
}
```

- `project_id` is `p_` plus 16 lowercase hex characters. It is derived deterministically from the canonical root path (SHA-256 over a normalized path), so the same root yields the same ID on the same platform.
- `agent_definition_id` is `a_` plus 12 lowercase hex characters and is unique within its project.
- `theme` is `ai`, `security`, `web` or `custom`.
- `platform` is `opencode`, `codex`, `antigravity`, `cursor` or `unknown`.
- `color` is a `#rrggbb` value and is presentation metadata, not evidence.
- Timestamps are numeric seconds. `0` means “not recorded”; it is not a real start time.

The registry is bounded (1000 projects, 64 agents per project, 4 MiB registry, 256 KiB bindings). It stores no prompts, source code, run content, raw native IDs or leases.

### Atomic writes and permissions

Writes use a temporary file in the registry directory, `flush` + `fsync`, then `os.replace`; on POSIX the file is mode `0600` and a newly created parent directory is `0700`. A symbolic-link registry path is rejected. Uninstall/removal of project metadata never deletes project evidence or Git worktrees.

## Identity chain

V7.1 keeps metadata and observations separate:

```text
project_id                         registry project (canonical root)
  └─ agent_definition_id            registered agent metadata (name, role, platform)
       └─ explicit binding          .auto-orchestrator/agent-bindings.json
            ├─ native_hash          SHA-256 of the native child/tool-call ID
            ├─ run_id               recorded native run
            ├─ task_id              optional Supervisor task
            └─ session_id           optional safe local identifier
                 └─ run_id          .auto-orchestrator/runs/<run_id>.jsonl
                      └─ task_id    Supervisor task in .auto-orchestrator/supervisor.sqlite3
                           └─ attempt_id   attempt-specific lease and native observation
```

| Identifier | Source | Purpose |
|---|---|---|
| `project_id` | Registry | Selects exactly one project root and one evidence namespace. |
| `agent_definition_id` | Registry | Names a registered agent profile. It is not a native agent ID and does not prove a run. |
| `native_hash` | Explicit binding | Opaque SHA-256 correlation value. The raw native ID is never persisted. |
| `run_id` | Run log | One recorded native/tool lifecycle. Safe ID matching `[A-Za-z0-9][A-Za-z0-9._-]{0,79}`. |
| `task_id` | Supervisor | A durable plan task. Optional in a binding. |
| `attempt_id` | Supervisor | One attempt/lease of a task. Native observations are attempt-scoped. |
| `session_id` | Binding/event | Optional safe local identifier; not a raw vendor session dump. |

### Bindings are explicit

Project-local bindings live at:

```text
<project>/.auto-orchestrator/agent-bindings.json
```

```json
{
  "version": 1,
  "bindings": [{
    "agent_definition_id": "a_...",
    "native_hash": "<64 lowercase hex>",
    "run_id": "...",
    "session_id": null,
    "task_id": null,
    "bound_at": 0
  }]
}
```

Rules enforced by [`tools/project_registry.py`](../tools/project_registry.py):

- A binding must name a registered agent in that project and a valid 64-hex `native_hash`.
- One binding per agent definition; a native hash cannot be bound to two different agents.
- Binding replacement is explicit; `unbind-agent` removes one binding.
- Bindings are never inferred from role, task text, timing, transcript path or model name.
- The bindings file is project-local, bounded and rejected if it is a symlink, malformed, duplicated or over-size.

The virtual office only shows `working`/`completed`/`failed` evidence for an agent when the binding matches recorded evidence. A binding alone is not proof of life.

## CLI

Global options (`--registry`, `--json`) come **before** the subcommand:

```bash
# Add and list projects
python tools/project_registry.py add --root /work/research --name "Research Lab" --theme ai
python tools/project_registry.py list
python tools/project_registry.py --json list
python tools/project_registry.py show p_0123456789abcdef
python tools/project_registry.py update p_0123456789abcdef --theme security

# Register existing evidence-bearing projects below a path (only directories
# containing .auto-orchestrator are discovered)
python tools/project_registry.py discover /work --max-depth 3

# Agents and explicit bindings
python tools/project_registry.py add-agent p_0123456789abcdef \
  --name Ada --role "RAG Engineer" --platform opencode --color "#7dd3fc"
python tools/project_registry.py bind-agent p_0123456789abcdef a_0123456789ab \
  --native-hash <64-lowercase-hex> --run-id run-123 --task-id backend
python tools/project_registry.py unbind-agent p_0123456789abcdef a_0123456789ab

# Schema migration (registry only; evidence is untouched)
python tools/project_registry.py --json migrate

# Removal is metadata-only and explicitly forced
python tools/project_registry.py remove p_0123456789abcdef --force
```

`bind-agent` requires the real 64-hex native hash; the placeholder above is not executable. Obtain the hash from the recorded native event or the Supervisor bridge, not from a prompt or task name. `add` is idempotent for an already registered canonical root (`created: false`).

`discover` does **not** scan every directory recursively forever: the default maximum depth is 3 and it skips common dependency/build directories and hidden directories. It registers only directories that already contain a real `.auto-orchestrator` directory.

## Dashboard startup

```bash
# Serve one project and register it if needed
python tools/dashboard.py --root /work/research --open

# Serve a specific registry and make one project the default
python tools/dashboard.py --registry /path/projects.json --root /work/research

# Serve a project without writing the registry
python tools/dashboard.py --root /work/research --no-register

# Clearly labelled synthetic data; reads no registry/project evidence
python tools/dashboard.py --demo --open
```

`--root` is auto-registered unless `--no-register` is set. Without `--root`, the first registered project is the default. With no projects and no `--root`, the dashboard registers the current working directory. Demo mode does not read the real registry. The server binds `127.0.0.1` only and accepts only `GET` for API routes (`POST` returns 405).

## Migration

| Input | Behavior |
|---|---|
| Registry missing | An empty version-2 registry is returned in memory; a dashboard start with a root can create the file. `migrate` writes a version-2 file explicitly. |
| Version `2` | Validated and used as-is (subject to field checks). |
| Version missing or `1` | Migrated: roots are canonicalized when possible, project IDs are derived, required fields/agents receive defaults, and agent IDs are generated if absent. Only the registry is rewritten. |
| Unknown version | Explicit `RegistryError`; the registry is not guessed or overwritten. |
| Malformed JSON / non-object | Explicit error; no evidence files are touched. |
| Version-2 project ID not matching its canonical root | Rejected (`Project id does not match the canonical root`). |

Migration never rewrites `.auto-orchestrator/runs/`, `supervisor.sqlite3`, `agent-bindings.json` or Git worktrees. If a project root is missing at runtime, the dashboard reports that project as unavailable in the global overview rather than deleting its registry entry. A moved root is a **new root identity**: add it explicitly, then remove the stale metadata if desired. Evidence is never moved automatically.

## Duplicate and symlink handling

| Case | Behavior |
|---|---|
| Same canonical root added twice | Existing project is returned; no duplicate is created. |
| Different roots producing the same deterministic ID | Fails closed; the second registration is refused. |
| Root is a symlink (final component) | Rejected. |
| Symlink registry path or bindings file | Rejected; no write through the link. |
| Root missing or not a directory | `canonical_root` rejects a new registration; an existing registry entry may remain and is surfaced as unavailable. |
| Duplicate project ID / agent ID in a payload | Rejected. |
| Duplicate canonical roots in a payload | Rejected. |
| Native hash bound to another agent | Rejected. |
| Symlinked evidence/log file | Dashboard skips or rejects it; it is not followed. |

Canonical comparison uses `os.path.normpath(os.path.abspath(...))` plus `normcase`, so case-insensitive volumes do not create two identities. Symlink validation is point-in-time; do not mutate managed paths concurrently while installing or registering.

## Project isolation

- Every project gets its own evidence `Store` rooted at its canonical path. The catalog is keyed by `project_id`, not by the display name or root string.
- The same `run_id` in two projects is two separate runs. Per-project routes include the project ID and never fall back to another project.
- Bindings are project-local; an identical agent definition ID in another project is a different definition.
- SSE hubs are per project. Each event carries `project_id`; an unknown project returns 404 and no cross-project stream is merged.
- The global overview is an aggregate of per-project reads. A failed project is marked `status: "error"` with a bounded error message; it does not contribute a guessed run or agent state.
- Project removal deletes metadata only. Evidence, Supervisor state, bindings and worktrees remain on disk.

## HTTP API

All routes are read-only `GET` unless noted. The legacy routes remain compatible for the single-project console.

### Legacy routes

| Route | Result |
|---|---|
| `GET /api/state` | The default project's prior state shape, plus `project_id`, `project_meta` (registry metadata) and `demo`. The historical `project` field remains the root path string. ETag/304 supported. |
| `GET /api/run/<run_id>` | Default project's prior run-detail shape; project-scoped metadata is available from the project route. |
| `GET /api/health` | `{"ok": true}`. |
| `GET /assets/*` | Static dashboard assets; no external assets. |

### Multi-project routes

| Route | Result |
|---|---|
| `GET /api/global` | Fleet overview with totals and one row per project. ETag/304 supported. |
| `GET /api/projects` | `{version, registry_path, projects: [metadata]}`. |
| `GET /api/projects/<project_id>/state` | Project state with `project_id` and `project` metadata. ETag/304 supported. |
| `GET /api/projects/<project_id>/runs/<run_id>` | Run detail with `project_id` and `project`. |
| `GET /api/projects/<project_id>/office` | Desks, unlinked sessions and summary. See [VIRTUAL_OFFICE.md](VIRTUAL_OFFICE.md). |
| `GET /api/projects/<project_id>/agents/<agent_definition_id>` | Agent Inspector payload. |
| `GET /api/projects/<project_id>/events` | Per-project Server-Sent Events stream. |

An unknown/malformed project ID returns 404. A project whose root is unavailable returns 503 for project-scoped state/office/events, appears as `status: "error"` in `/api/global`, and never causes another project's evidence to be substituted.

### Global overview shape

```json
{
  "version": 1,
  "fingerprint": "<aggregate fingerprint>",
  "totals": {
    "projects": 0, "runs": 0, "verified": 0, "open": 0,
    "agents": 0, "active_tasks": 0, "blockers": 0, "errors": 0
  },
  "projects": [{
    "project_id": "p_...",
    "name": "...",
    "description": "",
    "root": "/canonical/path",
    "theme": "ai",
    "overview": {
      "runs": 0, "verified": 0, "open": 0, "live": 0, "agents": 0,
      "tasks": 0, "active_tasks": 0, "blockers": 0,
      "last_activity": null, "verified_rate": 0
    },
    "agent_count": 0,
    "linked_agent_count": 0,
    "status": "ok",
    "error": null
  }]
}
```

- `overview.agents` counts recorded native agent **spans**; `agent_count` counts registered agent **definitions**; `linked_agent_count` counts definitions with an explicit binding. These are different facts and are not interchangeable. `totals.agents` sums the observed spans, not the roster.
- `overview.verified` counts gate-verified runs. Self-reported claims are not verified runs.
- `overview.active_tasks` counts Supervisor tasks in `running`, `cancel_requested` or `timed_out`; `blockers` counts failed/timed-out tasks.
- `status` is `ok` or `error`; `error` carries a short reason. `totals.errors` counts projects with `status: "error"`. An error project's zeroes are placeholders, not evidence; read `error`.
- `last_activity` is the latest recorded timestamp, or `null`. `null` does not mean “idle”.
- `fingerprint` changes as evidence and display-only liveness decay change; it is not a promise that a run is live.

### Server-Sent Events

`GET /api/projects/<project_id>/events` returns `Content-Type: text/event-stream` with `Cache-Control: no-cache` and `retry: 2500`.

- On a new connection (no valid `Last-Event-ID`) the first event is `snapshot`. If the supplied `Last-Event-ID` equals the current sequence, no replay is sent; the next event is an `update` when the fingerprint changes.
- Every event has `id: <monotonic integer>` and data of the shape `{type, project_id, sequence_id, state, reconnect?}`.
- Heartbeat comment frames are sent every 15 seconds.
- `Last-Event-ID` replays the bounded 32-event buffer when available. If the requested ID is too old or invalid, the client receives a fresh snapshot with `reconnect: true`; no completion is fabricated.
- Hubs are per project, bounded, and shared by multiple tabs. A slow subscriber is disconnected and must reconnect with `Last-Event-ID`.
- If SSE fails, the existing ETag/304 polling path remains the fallback. A missing stream is not treated as a finished run.

SSE is observation, not control. It does not spawn, stop, cancel or resume an agent.

## Security and limits

- Loopback only; no external assets, analytics, fonts or telemetry upload. CSP remains strict.
- The dashboard is read-only; registry/binding writes happen through the explicit CLI, not through HTTP.
- Raw provider-native IDs, leases, prompts, source code, command text and tool output are not persisted in the registry/bindings. Run logs and API payloads contain only opaque agent IDs and SHA-256 correlation hashes.
- Request `Host` and `Origin`/`Sec-Fetch-Site` are validated to reject DNS-rebinding and cross-site browser reads. This is not authentication; do not expose the port.
- The registry/bindings are local trust state, not a multi-user authorization boundary. A process running as the same OS user can edit them.
- Registry and bindings are bounded and validated; malformed binding evidence makes the office/inspector unavailable rather than silently becoming “no binding”.
- Project evidence counts are not benchmark results, correctness proofs or human time savings. See [CAPABILITY_MATRIX.md](CAPABILITY_MATRIX.md) and [DASHBOARD.md](DASHBOARD.md).
