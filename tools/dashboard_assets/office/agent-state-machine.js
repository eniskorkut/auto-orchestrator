/* Auto-Orchestrator V7.2 — conservative office agent state machine.
 *
 * Evidence first. A character only exists when the backend observed a real
 * binding or an unlinked native session; a registry row alone never spawns.
 * Status mapping never inspects display names or free text. Room assignment
 * follows the contract order: backend room, explicit task room/department,
 * explicit agent default room, explicit task role, then lobby.
 */
(() => {
  'use strict';

  const ROOM_IDS = ['frontend', 'backend', 'qa', 'review', 'research', 'meeting', 'lobby'];
  const STATES = Object.freeze([
    'spawn', 'idle', 'walking', 'working', 'reading', 'testing', 'reviewing',
    'awaiting', 'blocked', 'completed', 'failed', 'unknown',
  ]);
  const ROLE_ROOMS = Object.freeze({
    tester: 'qa',
    reviewer: 'review',
    explorer: 'research',
  });
  const OPEN_ACTIVITY_STATES = new Set(['working', 'reading', 'testing', 'reviewing']);

  function text(value) {
    return typeof value === 'string' ? value.trim() : '';
  }

  /* office.js wraps payload rows as {kind, agent|session, room, status, key}.
   * The state machine accepts either the wrapper or the raw backend entry. */
  function unwrap(entry) {
    if (!entry || typeof entry !== 'object') return entry;
    if (entry.kind === 'agent' && entry.agent && typeof entry.agent === 'object') return entry.agent;
    if (entry.kind === 'session' && entry.session && typeof entry.session === 'object') return entry.session;
    if (!entry.agent_definition_id && entry.agent && typeof entry.agent === 'object') return entry.agent;
    if (!entry.agent_definition_id && !entry.run_id
        && entry.session && typeof entry.session === 'object') return entry.session;
    return entry;
  }

  function lower(value) {
    return text(value).toLowerCase();
  }

  function normalizedStatus(value) {
    const raw = lower(value);
    if (!raw) return 'unknown';
    return raw.replace(/[\s_]+/g, '-');
  }

  function bool(value) {
    return value === true;
  }

  function isSessionEntry(entry) {
    if (!entry || typeof entry !== 'object') return false;
    if (entry.kind === 'session') return true;
    if (!entry.agent_definition_id && entry.session && typeof entry.session === 'object'
        && !entry.run_id) return true;
    const raw = unwrap(entry);
    if (raw !== entry) return isSessionEntry(raw);
    if (entry.agent_definition_id || entry.binding) return false;
    return Boolean(text(entry.run_id) && (text(entry.agent_id) || text(entry.native_hash)));
  }

  function bindingObject(entry) {
    const raw = unwrap(entry);
    const binding = (raw && raw.binding) || (entry && entry.binding);
    if (!binding || typeof binding !== 'object' || Array.isArray(binding)) return null;
    return binding;
  }

  function bindingHasIdentity(binding) {
    if (!binding) return false;
    return ['native_hash', 'run_id', 'task_id', 'session_id']
      .some((key) => text(binding[key]) !== '');
  }

  /* A character is justified only by an explicit binding or an observed
   * unlinked session. Registry rows with status/name/role alone are inert. */
  function canSpawn(entry) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return false;
    if (entry.spawnable === false) return false;
    if (entry.kind === 'managed' || entry.managed) {
      return text(entry.session_key || entry.key || '') !== ''
        && Boolean(entry.managed && (entry.managed.session_hash || entry.managed.status));
    }
    const raw = unwrap(entry);
    if (isSessionEntry(entry) || isSessionEntry(raw)) {
      const source = raw || entry;
      return text(source.run_id) !== ''
        && (text(source.agent_id) !== '' || text(source.native_hash) !== '');
    }
    const binding = bindingObject(entry);
    const agentId = text(raw && raw.agent_definition_id) || text(entry.agent_definition_id);
    if (agentId) return bindingHasIdentity(binding);
    return bindingHasIdentity(binding);
  }

  function sessionKey(entry) {
    if (!entry || typeof entry !== 'object') return 'unknown';
    const raw = unwrap(entry);
    const explicit = text(entry.session_key)
      || (typeof entry.key === 'string' && entry.key.trim() ? entry.key.trim() : '')
      || text(raw && raw.session_key);
    if (explicit) return explicit;
    if (isSessionEntry(entry) || isSessionEntry(raw)) {
      const source = raw || entry;
      return `s:${text(source.run_id) || 'run'}:${text(source.agent_id) || 'agent'}`;
    }
    const binding = bindingObject(entry);
    if (binding) {
      const agentId = text(raw && raw.agent_definition_id)
        || text(entry.agent_definition_id) || 'agent';
      const runId = text(binding.run_id) || 'run';
      const digest = text(binding.native_hash);
      return `${agentId}:${runId}:${digest ? digest.slice(0, 12) : 'unhashed'}`;
    }
    const fallbackId = text(raw && raw.agent_definition_id) || text(entry.agent_definition_id);
    if (fallbackId) return `a:${fallbackId}`;
    return `unknown:${text(raw && raw.agent_id) || text(entry.agent_id)
      || text(raw && raw.run_id) || text(entry.run_id) || 'session'}`;
  }

  const dedupeKey = sessionKey;

  function dedupe(entries) {
    const list = Array.isArray(entries) ? entries : [];
    const seen = new Set();
    const result = [];
    for (const entry of list) {
      const key = sessionKey(entry);
      if (seen.has(key)) continue;
      seen.add(key);
      result.push(entry);
    }
    return result;
  }

  function spawnable(entries) {
    return dedupe(Array.isArray(entries) ? entries.filter((entry) => canSpawn(entry)) : []);
  }

  function taskContext(entry) {
    const raw = unwrap(entry) || {};
    const current = raw.current && typeof raw.current === 'object'
      ? raw.current : (entry && entry.current && typeof entry.current === 'object'
        ? entry.current : {});
    const task = raw.task && typeof raw.task === 'object'
      ? raw.task : (entry && entry.task && typeof entry.task === 'object' ? entry.task : {});
    return { current, task, raw };
  }

  function explicitTaskRoom(entry) {
    const { current, task, raw } = taskContext(entry);
    const candidates = [
      current.assigned_room,
      current.room,
      current.department,
      task.assigned_room,
      task.room,
      task.department,
      raw.assigned_room,
      raw.task_room,
      entry.assigned_room,
      entry.task_room,
    ];
    for (const value of candidates) {
      const room = lower(value);
      if (room && room !== 'unassigned') return room;
    }
    return '';
  }

  function explicitAgentRoom(entry) {
    const raw = unwrap(entry) || {};
    const candidates = [raw.default_room, raw.preferred_room, entry.default_room, entry.preferred_room];
    for (const value of candidates) {
      const room = lower(value);
      if (room && room !== 'unassigned') return room;
    }
    return '';
  }

  function taskRole(entry) {
    const { current, task, raw } = taskContext(entry);
    return lower(current.task_role) || lower(current.role) || lower(task.role)
      || lower(raw.task_role) || lower(entry.task_role);
  }

  function canonicalRoom(room, layout) {
    const value = lower(room) || 'lobby';
    const rooms = layout && Array.isArray(layout.rooms) ? layout.rooms : [];
    const byId = rooms.find((item) => item && lower(item.id) === value);
    if (byId) return lower(byId.id) || value;
    const byDepartment = rooms.find((item) => item && lower(item.department) === value);
    if (byDepartment) return lower(byDepartment.id) || value;
    return value;
  }

  /* Deterministic, free-text-free room assignment. entry.room is
   * authoritative when the backend supplied it. */
  function roomFor(entry, layout) {
    const raw = unwrap(entry) || {};
    const explicit = lower(entry && entry.room) || lower(raw.room);
    if (explicit && explicit !== 'unassigned') return canonicalRoom(explicit, layout);
    const taskRoom = explicitTaskRoom(entry);
    if (taskRoom) return canonicalRoom(taskRoom, layout);
    const agentRoom = explicitAgentRoom(entry);
    if (agentRoom) return canonicalRoom(agentRoom, layout);
    const role = taskRole(entry);
    if (ROLE_ROOMS[role]) return canonicalRoom(ROLE_ROOMS[role], layout);
    return canonicalRoom('lobby', layout);
  }

  function hashKey(value) {
    const input = String(value == null ? '' : value);
    let hash = 0x811c9dc5;
    for (let index = 0; index < input.length; index += 1) {
      hash ^= input.charCodeAt(index);
      hash = Math.imul(hash, 0x01000193);
    }
    return hash >>> 0;
  }

  function deskCandidates(room, layout) {
    const desks = layout && Array.isArray(layout.desks) ? layout.desks : [];
    const rooms = layout && Array.isArray(layout.rooms) ? layout.rooms : [];
    const record = rooms.find((item) => item && lower(item.id) === lower(room))
      || rooms.find((item) => item && lower(item.department) === lower(room));
    const aliases = new Set([lower(room), lower(record && record.id), lower(record && record.department)]
      .filter(Boolean));
    return desks.filter((desk) => desk && (
      aliases.has(lower(desk.room)) || aliases.has(lower(desk.department))
    ));
  }

  function deskFor(entry, layout, previous) {
    const room = roomFor(entry, layout);
    const candidates = deskCandidates(room, layout);
    if (!candidates.length) return null;
    const previousId = text(previous && previous.deskId);
    if (previousId && lower(previous.room) === lower(room)) {
      const kept = candidates.find((desk) => text(desk.id) === previousId);
      if (kept) return kept;
    }
    const index = hashKey(sessionKey(entry)) % candidates.length;
    return candidates[index];
  }

  function evidenceOpen(entry) {
    if (entry && (entry.kind === 'managed' || entry.managed)) {
      const status = normalizedStatus(entry.managed?.status || entry.status);
      return status === 'starting' || status === 'running';
    }
    const raw = unwrap(entry) || {};
    const evidence = raw.evidence && typeof raw.evidence === 'object' ? raw.evidence : {};
    const current = raw.current && typeof raw.current === 'object' ? raw.current : {};
    return bool(entry && entry.open) || bool(raw.open)
      || bool(evidence.open_span) || bool(current.open_span);
  }

  function evidenceStale(entry) {
    const raw = unwrap(entry) || {};
    const evidence = raw.evidence && typeof raw.evidence === 'object' ? raw.evidence : {};
    const current = raw.current && typeof raw.current === 'object' ? raw.current : {};
    return bool(entry && entry.stale) || bool(raw.stale)
      || bool(evidence.stale) || bool(current.stale);
  }

  function evidenceSource(entry) {
    const raw = unwrap(entry) || {};
    const evidence = raw.evidence && typeof raw.evidence === 'object' ? raw.evidence : {};
    const source = lower(evidence.source);
    if (source) return source;
    if (isSessionEntry(entry) || isSessionEntry(raw)) return 'native-hook';
    const binding = bindingObject(entry);
    return bindingHasIdentity(binding) ? 'binding' : 'none';
  }

  function taskState(entry) {
    const { current, task, raw } = taskContext(entry);
    return normalizedStatus(current.task_state || task.state || raw.task_state || entry.task_state);
  }

  function explicitActivity(entry) {
    const { current, task, raw } = taskContext(entry);
    const activity = normalizedStatus(current.activity || task.activity || raw.activity || entry.activity);
    if (activity === 'read' || activity === 'reading' || activity === 'docs') return 'reading';
    if (activity === 'test' || activity === 'testing' || activity === 'running-tests'
        || activity === 'tests' || activity === 'check') return 'testing';
    if (activity === 'review' || activity === 'reviewing' || activity === 'audit') return 'reviewing';
    if (activity === 'working' || activity === 'implementing' || activity === 'coding') return 'working';

    const lastEvent = normalizedStatus(current.last_event || raw.last_event || entry.last_event);
    if (lastEvent === 'check') return 'testing';
    if (lastEvent === 'agent-start') return 'working';
    return null;
  }

  function observedSource(entry) {
    const source = evidenceSource(entry);
    return source !== 'none' && source !== 'binding';
  }

  function statusSupportsAwaiting(entry) {
    const { current, raw } = taskContext(entry);
    const state = taskState(entry);
    return state === 'awaiting-approval' || state === 'awaiting'
      || bool(raw.awaiting_approval) || bool(entry.awaiting_approval)
      || normalizedStatus(current.approval_state || raw.approval_state || entry.approval_state) === 'pending'
      || normalizedStatus(current.approval || raw.approval || entry.approval) === 'pending'
      || observedSource(entry)
      || evidenceOpen(entry);
  }

  function statusSupportsBlocked(entry) {
    const { current, raw } = taskContext(entry);
    return taskState(entry) === 'blocked'
      || bool(raw.blocked) || bool(entry.blocked)
      || bool(raw.evidence && raw.evidence.blocked)
      || bool(current.blocked)
      || observedSource(entry)
      || evidenceOpen(entry);
  }

  /* Map backend status onto a character pose without inventing activity.
   * A live open span is required before any working-ish pose is allowed. */
  function visualState(entry, previous) { // eslint-disable-line no-unused-vars
    if (!canSpawn(entry)) return 'unknown';
    const rawEntry = unwrap(entry) || {};
    const raw = normalizedStatus(rawEntry.status != null ? rawEntry.status : entry.status);
    const open = evidenceOpen(entry);
    const stale = evidenceStale(entry);
    const state = taskState(entry);

    if (raw === 'completed' || raw === 'success' || raw === 'succeeded' || raw === 'done' || raw === 'ok') {
      return 'completed';
    }
    if (raw === 'failed' || raw === 'failure' || raw === 'error'
        || raw === 'cancelled' || raw === 'canceled') {
      return 'failed';
    }
    if (raw === 'blocked' || state === 'blocked') {
      return statusSupportsBlocked(entry) ? 'blocked' : 'unknown';
    }
    if (raw === 'awaiting-approval' || raw === 'awaiting'
        || state === 'awaiting-approval') {
      return statusSupportsAwaiting(entry) ? 'awaiting' : 'unknown';
    }
    if (raw === 'reviewing' || raw === 'review') {
      return open && !stale ? 'reviewing' : 'unknown';
    }
    if (raw === 'running-tests' || raw === 'testing' || raw === 'tests') {
      return open && !stale ? 'testing' : 'unknown';
    }
    if (raw === 'reading') {
      return open && !stale ? 'reading' : 'unknown';
    }
    if (raw === 'idle') {
      return evidenceSource(entry) === 'none' ? 'unknown' : 'idle';
    }
    if (raw === 'working' || raw === 'running' || raw === 'active' || raw === 'open') {
      if (!open || stale) return 'unknown';
      return explicitActivity(entry) || 'working';
    }
    /* dispatching / pending / unknown: no native start, no working pose. */
    return 'unknown';
  }

  function warningFor(entry, state) {
    if (!canSpawn(entry)) return 'no-binding';
    const rawEntry = unwrap(entry) || {};
    const raw = normalizedStatus(rawEntry.status != null ? rawEntry.status : entry.status);
    if (state === 'working' || state === 'reading' || state === 'testing' || state === 'reviewing') {
      const evidence = rawEntry.evidence && typeof rawEntry.evidence === 'object'
        ? rawEntry.evidence : {};
      if (!bool(evidence.native_stop_observed) && evidenceOpen(entry)) {
        return 'start-only';
      }
      return null;
    }
    if (evidenceStale(entry)) return 'stale-open';
    if (raw === 'working' || raw === 'running' || raw === 'active') {
      return evidenceOpen(entry) ? null : 'stale-open';
    }
    if (raw === 'dispatching' || raw === 'pending') return 'native-start-missing';
    return null;
  }

  function evidenceFor(entry, state) {
    const raw = unwrap(entry) || {};
    const evidence = raw.evidence && typeof raw.evidence === 'object' ? raw.evidence : {};
    const source = evidenceSource(entry);
    const warning = warningFor(entry, state);
    return {
      source,
      confidence: lower(evidence.confidence) || (source === 'none' ? 'unknown' : 'observed'),
      detail: text(evidence.detail),
      open_span: evidenceOpen(entry),
      native_start_observed: bool(evidence.native_start_observed)
        || bool(raw.current && raw.current.native_start_observed)
        || evidenceOpen(entry),
      native_stop_observed: bool(evidence.native_stop_observed),
      stale: evidenceStale(entry),
      warning,
      room_source: lower(raw.room_source) || lower(entry.room_source) || 'unassigned',
    };
  }

  function derive(entry, previous, layout) {
    const spawnableEntry = canSpawn(entry);
    const key = sessionKey(entry);
    const room = roomFor(entry, layout);
    const state = visualState(entry, previous);
    const desk = spawnableEntry
      ? deskFor(entry, layout, previous && lower(previous.room) === lower(room) ? previous : null)
      : null;
    return {
      key,
      room,
      deskId: desk ? text(desk.id) : null,
      state,
      evidence: evidenceFor(entry, state),
      spawnable: spawnableEntry,
      warning: warningFor(entry, state),
    };
  }

  const api = {
    ROOM_IDS,
    STATES,
    ROLE_ROOMS,
    canSpawn,
    visualState,
    derive,
    sessionKey,
    dedupeKey,
    dedupe,
    spawnable,
    roomFor,
    canonicalRoom,
    deskFor,
    deskCandidates,
    statusSupportsAwaiting,
    statusSupportsBlocked,
    isSessionEntry,
  };

  if (typeof window !== 'undefined') window.AOAgentStateMachine = api;
  if (typeof globalThis !== 'undefined') globalThis.AOAgentStateMachine = api;
})();
