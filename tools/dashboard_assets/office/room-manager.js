/* Auto-Orchestrator — deterministic room and desk assignment helpers.
 *
 * This mirrors the backend room order for UI fallback only. It never guesses
 * from display names or fuzzy strings: only explicit lowercase room ids are
 * accepted. Unknown explicit values are ignored, not rewritten.
 *
 * Order (same as the backend contract):
 *   1. task.department / task.assigned_room  -> source "task.department"
 *   2. agent.default_room / agent.assigned_room -> source "agent.default_room"
 *   3. task role tester/reviewer/explorer    -> source "task.role"
 *   4. lobby                                 -> source "unassigned"
 */
(function (root) {
  'use strict';

  var ROOM_IDS = ['frontend', 'backend', 'qa', 'review', 'research', 'meeting', 'lobby'];
  var ROLE_ROOMS = { tester: 'qa', reviewer: 'review', explorer: 'research' };

  function toText(value) {
    return typeof value === 'string' ? value.trim().toLowerCase() : '';
  }

  function normalizeRoom(value) {
    var candidate = toText(value);
    return ROOM_IDS.indexOf(candidate) >= 0 ? candidate : null;
  }

  function roleRoom(role) {
    var candidate = toText(role);
    return ROLE_ROOMS[candidate] || null;
  }

  function assignRoom(input) {
    var data = input && typeof input === 'object' ? input : {};
    var task = data.task || {};
    var agent = data.agent || {};

    var explicit = normalizeRoom(task.department);
    if (explicit) return { room: explicit, source: 'task.department' };
    explicit = normalizeRoom(task.assigned_room);
    if (explicit) return { room: explicit, source: 'task.department' };

    var agentRoom = normalizeRoom(agent.default_room);
    if (agentRoom) return { room: agentRoom, source: 'agent.default_room' };
    agentRoom = normalizeRoom(agent.assigned_room);
    if (agentRoom) return { room: agentRoom, source: 'agent.default_room' };

    var mapped = roleRoom(task.role);
    if (mapped) return { room: mapped, source: 'task.role' };

    return { room: 'lobby', source: 'unassigned' };
  }

  /* Resolve a full office payload entry. A backend-provided, known room id is
   * authoritative; otherwise fall back to the same order as the backend. */
  function roomFor(entry) {
    if (!entry || typeof entry !== 'object') return { room: 'lobby', source: 'unassigned' };
    if (entry.room_source === 'unlinked') return { room: 'lobby', source: 'unlinked' };
    var direct = normalizeRoom(entry.room);
    if (direct) {
      return { room: direct, source: entry.room_source ? String(entry.room_source) : 'backend' };
    }
    var agent = entry.agent && typeof entry.agent === 'object' ? entry.agent : entry;
    var current = entry.current && typeof entry.current === 'object'
      ? entry.current
      : (agent.current && typeof agent.current === 'object' ? agent.current : {});
    var task = entry.task && typeof entry.task === 'object' ? entry.task : {};
    return assignRoom({
      task: {
        department: current.department != null ? current.department : task.department,
        assigned_room: task.assigned_room,
        role: task.role != null ? task.role : current.task_role
      },
      agent: {
        default_room: agent.default_room,
        assigned_room: agent.assigned_room
      }
    });
  }

  function compareDesks(a, b) {
    var left = a && a.id != null ? String(a.id) : '';
    var right = b && b.id != null ? String(b.id) : '';
    if (left < right) return -1;
    if (left > right) return 1;
    var ax = a && typeof a.x === 'number' ? a.x : 0;
    var bx = b && typeof b.x === 'number' ? b.x : 0;
    if (ax !== bx) return ax - bx;
    var ay = a && typeof a.y === 'number' ? a.y : 0;
    var by = b && typeof b.y === 'number' ? b.y : 0;
    return ay - by;
  }

  function desksForRoom(layout, roomId) {
    var room = normalizeRoom(roomId);
    if (!room || !layout || !Array.isArray(layout.desks)) return [];
    return layout.desks
      .filter(function (desk) { return desk && desk.room === room; })
      .slice()
      .sort(compareDesks);
  }

  function entryKey(entry, index) {
    if (entry && typeof entry === 'object') {
      var value = entry.session_key || entry.key || entry.agent_definition_id || entry.id;
      if (typeof value === 'string' && value.trim()) return value.trim();
    }
    return 'agent-' + index;
  }

  function assignDesks(layout, entries, options) {
    var list = Array.isArray(entries) ? entries : [];
    var allowShared = Boolean(options && options.allowShared);
    var prepared = list.map(function (entry, index) {
      return { entry: entry, index: index, key: entryKey(entry, index) };
    });
    prepared.sort(function (a, b) {
      if (a.key < b.key) return -1;
      if (a.key > b.key) return 1;
      return a.index - b.index;
    });

    var usedDesks = Object.create(null);
    var byDesk = Object.create(null);
    var assignments = Object.create(null);
    var unassigned = [];
    var order = [];
    var seenKeys = Object.create(null);

    prepared.forEach(function (item) {
      var key = item.key;
      if (seenKeys[key]) {
        seenKeys[key] += 1;
        key = key + '#' + seenKeys[key];
      } else {
        seenKeys[key] = 1;
      }
      order.push(key);

      var entry = item.entry && typeof item.entry === 'object' ? item.entry : {};
      var room = normalizeRoom(entry.room) || roomFor(entry).room;
      var desks = desksForRoom(layout, room);
      var chosen = null;

      for (var i = 0; i < desks.length; i += 1) {
        if (!usedDesks[desks[i].id]) {
          chosen = desks[i];
          break;
        }
      }
      if (!chosen) {
        for (var j = 0; j < desks.length; j += 1) {
          var desk = desks[j];
          var shareable = allowShared || desk.share === true || desk.shared === true;
          if (shareable) {
            chosen = desk;
            break;
          }
        }
      }

      if (chosen) {
        usedDesks[chosen.id] = true;
        assignments[key] = chosen.id;
        if (!byDesk[chosen.id]) byDesk[chosen.id] = key;
      } else {
        assignments[key] = null;
        unassigned.push(key);
      }
    });

    return {
      assignments: assignments,
      byDesk: byDesk,
      unassigned: unassigned,
      order: order
    };
  }

  var api = {
    rooms: ROOM_IDS,
    roleRooms: ROLE_ROOMS,
    normalizeRoom: normalizeRoom,
    roleRoom: roleRoom,
    assignRoom: assignRoom,
    /* Compatibility alias used by office.js: takes a raw payload entry. */
    assign: roomFor,
    roomFor: roomFor,
    desksForRoom: desksForRoom,
    assignDesks: assignDesks
  };

  root.AORoomManager = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
