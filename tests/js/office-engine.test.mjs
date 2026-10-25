/* V7.2 office engine tests — tilemap, A*, conservative agent mapping, sprites.
 *
 * These tests load the plain browser scripts in a vm sandbox. No browser, DOM,
 * network or external asset is used. The final smoke test supplies tiny fake
 * canvas objects only to prove the scene controller can be constructed and fed
 * agents without throwing.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import vm from 'node:vm';

const root = process.cwd();
const officeDir = join(root, 'tools', 'dashboard_assets', 'office');
const files = [
  'tilemap.js',
  'pathfinding.js',
  'agent-state-machine.js',
  'agent-sprites.js',
  'office-scene.js',
];

function loadScripts(names = files, extra = {}) {
  const sandbox = { console, ...extra };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const name of names) {
    const code = readFileSync(join(officeDir, name), 'utf8');
    vm.runInContext(code, sandbox, { filename: name });
  }
  return sandbox;
}

const sandbox = loadScripts();
const Tilemap = sandbox.AOTilemap;
const Pathfinding = sandbox.AOPathfinding;
const StateMachine = sandbox.AOAgentStateMachine;
const Sprites = sandbox.AOAgentSprites;

/* ── shared fixtures ───────────────────────────────────────────────────── */

function layout(overrides = {}) {
  return {
    version: 1,
    id: 'test-lab',
    theme: 'ai',
    width: 20,
    height: 14,
    tile: 16,
    spawn: { x: 9, y: 12 },
    rooms: [
      {
        id: 'research', name: 'Research Lab', department: 'research',
        x: 2, y: 2, w: 6, h: 5, door: { x: 7, y: 4 },
      },
      {
        id: 'qa', name: 'QA Bench', department: 'qa',
        x: 12, y: 2, w: 6, h: 5, door: { x: 12, y: 4 },
      },
    ],
    furniture: [
      { id: 'plant-research', type: 'plant', x: 3, y: 3, w: 1, h: 1, blocked: true },
      { id: 'shelf-corridor', type: 'shelf', x: 9, y: 8, w: 1, h: 1, blocked: true },
      { id: 'block-a', type: 'crate', x: 9, y: 4, w: 1, h: 1, blocked: true },
    ],
    desks: [
      {
        id: 'research-1', room: 'research', department: 'research',
        x: 4, y: 4, seat: { x: 4, y: 5 }, facing: 'up',
      },
      {
        id: 'qa-1', room: 'qa', department: 'qa',
        x: 14, y: 4, seat: { x: 14, y: 5 }, facing: 'up',
      },
    ],
    decor: [{ type: 'rug', x: 8, y: 3, w: 3, h: 2 }],
    ...overrides,
  };
}

function grid(overrides) {
  return Tilemap.build(layout(overrides));
}

const BINDING = {
  agent_definition_id: 'a_000000000001',
  native_hash: 'a'.repeat(64),
  run_id: 'run-1',
  session_id: 'ses-1',
};

function agentEntry(overrides = {}) {
  const base = {
    agent_definition_id: 'a_000000000001',
    name: 'Ada',
    role: 'Engineer',
    platform: 'opencode',
    model: null,
    color: '#7dd3fc',
    status: 'working',
    status_label: 'Working',
    session_key: 'a_000000000001:run-1:aaaaaaaaaaaa',
    room: 'backend',
    room_source: 'task.department',
    binding: { ...BINDING },
    evidence: {
      source: 'native-hook',
      confidence: 'observed',
      detail: 'Native start observed; stop not observed.',
      open_span: true,
      native_stop_observed: false,
    },
    current: {
      run_id: 'run-1',
      task_id: 'api',
      task_role: 'implementer',
      task_state: 'running',
      department: 'backend',
      last_event: 'agent-start',
    },
  };
  return { ...base, ...overrides };
}

function sessionEntry(overrides = {}) {
  return {
    run_id: 'run-9',
    agent_id: 'deadbeef',
    native_hash: 'b'.repeat(64),
    platform: 'opencode',
    status: 'working',
    open: true,
    session_key: 's:run-9:deadbeef',
    room: 'lobby',
    room_source: 'unlinked',
    ...overrides,
  };
}

/* ── tilemap ───────────────────────────────────────────────────────────── */

test('tilemap derives walls, interiors, doors, desks, seats and furniture', () => {
  const map = grid();
  assert.equal(map.width, 20);
  assert.equal(map.height, 14);
  assert.equal(map.tile, 16);
  assert.equal(map.walkable.length, 20 * 14);

  // Room boundary is wall; interior is floor.
  assert.equal(map.wallAt(2, 2), true);
  assert.equal(map.isWalkable(2, 2), false);
  assert.equal(map.isWalkable(4, 3), true);
  assert.equal(map.roomAt(4, 3), 'research');

  // Door tile cuts the wall and is walkable on both sides.
  assert.equal(map.doorAt(7, 4), true);
  assert.equal(map.wallAt(7, 4), false);
  assert.equal(map.isWalkable(7, 4), true);
  assert.equal(map.isWalkable(8, 4), true);
  assert.equal(map.roomAt(7, 4), 'research');

  // Furniture marked blocked blocks; blocked desk tile blocks; seat walks.
  assert.equal(map.isWalkable(3, 3), false);
  assert.equal(map.isWalkable(9, 8), false);
  assert.equal(map.isWalkable(4, 4), false);
  assert.equal(map.isWalkable(4, 5), true);
  assert.equal(map.seatAt(4, 5), true);
  assert.equal(map.deskAt(4, 4).id, 'research-1');

  // Corridors are floor.
  assert.equal(map.isWalkable(9, 3), true);
  assert.equal(map.roomAt(9, 3), null);
  assert.equal(map.isWalkable(0, 0), true);
});

test('tilemap coordinate helpers use tile multiples', () => {
  const tile = Tilemap.worldToTile(33, 47, 16);
  assert.equal(tile.x, 2);
  assert.equal(tile.y, 2);
  const world = Tilemap.tileToWorld(2, 2, 16);
  assert.equal(world.x, 32);
  assert.equal(world.y, 32);
  const center = Tilemap.worldCenter(2, 2, 16);
  assert.equal(center.x, 40);
  assert.equal(center.y, 40);
});

test('tilemap rejects invalid coordinates, overlaps, corner doors and blocked doors', () => {
  assert.throws(() => grid({
    rooms: [{ id: 'bad', x: 18, y: 2, w: 5, h: 5, door: { x: 18, y: 3 } }],
  }), /outside|overlap/i);

  assert.throws(() => grid({
    rooms: [
      { id: 'a', x: 2, y: 2, w: 6, h: 5, door: { x: 7, y: 4 } },
      { id: 'b', x: 5, y: 3, w: 6, h: 5, door: { x: 5, y: 4 } },
    ],
  }), /overlap/i);

  assert.throws(() => grid({
    rooms: [{ id: 'corner', x: 2, y: 2, w: 6, h: 5, door: { x: 2, y: 2 } }],
  }), /corner/i);

  assert.throws(() => grid({
    rooms: [
      { id: 'left', x: 2, y: 2, w: 6, h: 5, door: { x: 7, y: 4 } },
      { id: 'right', x: 8, y: 2, w: 6, h: 5, door: { x: 8, y: 4 } },
    ],
  }), /overlap or touch/i);

  assert.throws(() => grid({
    rooms: [{ id: 'inner', x: 2, y: 2, w: 6, h: 5, door: { x: 4, y: 4 } }],
  }), /wall/i);

  assert.throws(() => grid({
    furniture: [{ id: 'door-block', type: 'plant', x: 7, y: 4, blocked: true }],
  }), /door/i);

  assert.throws(() => grid({
    furniture: [{ id: 'spawn-block', type: 'plant', x: 9, y: 12, blocked: true }],
  }), /spawn/i);
});

/* ── pathfinding ───────────────────────────────────────────────────────── */

test('A* crosses rooms through both door tiles and never steps diagonally', () => {
  const map = grid();
  const path = Pathfinding.findPath(map, { x: 5, y: 5 }, { x: 15, y: 5 });
  assert.ok(path, 'a route across rooms must exist');
  assert.equal(path[0].x, 5);
  assert.equal(path[0].y, 5);
  assert.equal(path[path.length - 1].x, 15);
  assert.equal(path[path.length - 1].y, 5);
  assert.ok(path.some((tile) => tile.x === 7 && tile.y === 4), 'research door used');
  assert.ok(path.some((tile) => tile.x === 12 && tile.y === 4), 'qa door used');
  assert.ok(path.length > 10);
  for (let index = 0; index < path.length; index += 1) {
    const tile = path[index];
    assert.equal(map.isWalkable(tile.x, tile.y), true, `step ${index} must be walkable`);
    if (index) {
      const previous = path[index - 1];
      assert.equal(Math.abs(previous.x - tile.x) + Math.abs(previous.y - tile.y), 1,
        'each step is cardinal');
    }
  }
});

test('A* is deterministic and avoids blocked furniture', () => {
  const map = grid();
  const first = Pathfinding.findPath(map, { x: 5, y: 5 }, { x: 15, y: 5 });
  const second = Pathfinding.findPath(map, { x: 5, y: 5 }, { x: 15, y: 5 });
  assert.deepEqual(first, second);
  assert.equal(first.some((tile) => tile.x === 9 && tile.y === 4), false,
    'blocked crate at 9,4 must not be used');

  const blockedGoal = Pathfinding.findPath(map, { x: 5, y: 5 }, { x: 9, y: 4 });
  assert.equal(blockedGoal, null, 'a blocked goal is not a path');
  const blockedStart = Pathfinding.findPath(map, { x: 9, y: 4 }, { x: 5, y: 5 });
  assert.equal(blockedStart, null, 'a blocked start is not a path');
});

test('A* returns start for a same-tile goal and null for a sealed target', () => {
  const map = grid();
  const same = Pathfinding.findPath(map, { x: 5, y: 5 }, { x: 5, y: 5 });
  assert.equal(same.length, 1);
  assert.equal(same[0].x, 5);
  assert.equal(same[0].y, 5);

  const sealed = grid({
    furniture: [
      { id: 'n', type: 'crate', x: 10, y: 9, blocked: true },
      { id: 'e', type: 'crate', x: 11, y: 10, blocked: true },
      { id: 's', type: 'crate', x: 10, y: 11, blocked: true },
      { id: 'w', type: 'crate', x: 9, y: 10, blocked: true },
    ],
  });
  assert.equal(sealed.isWalkable(10, 10), true);
  assert.equal(Pathfinding.findPath(sealed, { x: 5, y: 5 }, { x: 10, y: 10 }), null);
});

test('nearestWalkable returns the deterministic closest floor tile', () => {
  const map = grid();
  const near = Pathfinding.nearestWalkable(map, { x: 9, y: 8 });
  assert.ok(near);
  assert.equal(map.isWalkable(near.x, near.y), true);
  assert.notDeepEqual(near, { x: 9, y: 8 });
  assert.equal(Pathfinding.nearestWalkable(map, { x: 5, y: 5 }).x, 5);
  assert.equal(Pathfinding.nearestWalkable(map, { x: -10, y: -10 }).x, 0);
  assert.equal(Pathfinding.nearestWalkable(map, { x: -10, y: -10 }).y, 0);
});

/* ── conservative agent state machine ──────────────────────────────────── */

test('canSpawn requires a real binding or unlinked native session', () => {
  assert.equal(StateMachine.canSpawn(agentEntry()), true);
  assert.equal(StateMachine.canSpawn(sessionEntry()), true);
  assert.equal(StateMachine.canSpawn({
    agent_definition_id: 'a_000000000001',
    name: 'Unbound',
    status: 'unknown',
    binding: null,
  }), false);
  assert.equal(StateMachine.canSpawn({
    agent_definition_id: 'a_000000000001',
    session_key: 'a:unbound',
    status: 'unknown',
  }), false);

  // office.js wrapper shape
  assert.equal(StateMachine.canSpawn({
    kind: 'agent', key: 'a:000000000001', agent: agentEntry(), spawnable: true,
  }), true);
  assert.equal(StateMachine.canSpawn({
    kind: 'agent', key: 'a:unbound', spawnable: true,
    agent: { agent_definition_id: 'a_000000000001', status: 'unknown', binding: null },
  }), false, 'a wrapper hint must not override missing binding evidence');
  assert.equal(StateMachine.canSpawn({
    kind: 'session', key: 's:run-9:deadbeef', session: sessionEntry(), spawnable: true,
  }), true);
});

test('live open span is working, stale and missing starts are unknown', () => {
  assert.equal(StateMachine.visualState(agentEntry()), 'working');
  assert.equal(StateMachine.visualState(agentEntry({
    status: 'working',
    evidence: {
      source: 'native-hook', confidence: 'observed',
      open_span: false, native_stop_observed: false,
    },
  })), 'unknown', 'a status alone cannot invent an open span');

  assert.equal(StateMachine.visualState(agentEntry({
    status: 'unknown',
    evidence: {
      source: 'native-hook', confidence: 'unknown', stale: true,
      open_span: false, native_stop_observed: false,
    },
  })), 'unknown');

  assert.equal(StateMachine.visualState(agentEntry({
    status: 'dispatching',
    evidence: { source: 'supervisor-sqlite', confidence: 'inferred', open_span: false },
    current: { task_state: 'running' },
  })), 'unknown', 'a pending task without a native start is not working');

  assert.equal(StateMachine.visualState(agentEntry({
    status: 'pending',
    evidence: { source: 'supervisor-sqlite', confidence: 'observed', open_span: false },
    current: { task_state: 'pending' },
  })), 'unknown');

  assert.equal(StateMachine.visualState(sessionEntry({ status: 'working', open: true })), 'working');
  assert.equal(StateMachine.visualState(sessionEntry({ status: 'working', open: false })), 'unknown');
});

test('terminal states survive and awaiting/blocked require observed support', () => {
  assert.equal(StateMachine.visualState(agentEntry({
    status: 'completed',
    evidence: { source: 'native-hook', confidence: 'observed', native_stop_observed: true, open_span: false },
  })), 'completed');
  assert.equal(StateMachine.visualState(agentEntry({
    status: 'failed',
    evidence: { source: 'native-hook', confidence: 'observed', native_stop_observed: true, open_span: false },
  })), 'failed');
  assert.equal(StateMachine.visualState(sessionEntry({ status: 'completed', open: false })), 'completed');
  assert.equal(StateMachine.visualState(sessionEntry({ status: 'failed', open: false })), 'failed');

  assert.equal(StateMachine.visualState(agentEntry({
    status: 'awaiting-approval',
    evidence: { source: 'none', confidence: 'unknown', open_span: false },
  })), 'unknown', 'awaiting without supporting evidence is unknown');
  assert.equal(StateMachine.visualState(agentEntry({
    status: 'awaiting-approval',
    evidence: { source: 'supervisor-sqlite', confidence: 'observed', open_span: false },
  })), 'awaiting');

  assert.equal(StateMachine.visualState(agentEntry({
    status: 'blocked',
    evidence: { source: 'none', confidence: 'unknown', open_span: false },
  })), 'unknown');
  assert.equal(StateMachine.visualState(agentEntry({
    status: 'unknown',
    evidence: { source: 'supervisor-sqlite', confidence: 'observed', open_span: false },
    current: { task_state: 'blocked' },
  })), 'blocked');

  // Previous visual state never overrides terminal evidence.
  assert.equal(StateMachine.visualState(agentEntry({
    status: 'completed',
    evidence: { source: 'native-hook', confidence: 'observed', native_stop_observed: true, open_span: false },
  }), { state: 'walking' }), 'completed');
});

test('explicit activity is used only when the native evidence supports it', () => {
  assert.equal(StateMachine.visualState(agentEntry({
    current: { task_state: 'running', last_event: 'check' },
    evidence: { source: 'native-hook', confidence: 'observed', open_span: true },
  })), 'testing');
  assert.equal(StateMachine.visualState(agentEntry({
    status: 'running-tests',
    evidence: { source: 'native-hook', confidence: 'observed', open_span: true },
  })), 'testing');
  assert.equal(StateMachine.visualState(agentEntry({
    status: 'reviewing',
    evidence: { source: 'native-hook', confidence: 'observed', open_span: true },
  })), 'reviewing');
  assert.equal(StateMachine.visualState(agentEntry({
    status: 'working',
    evidence: { source: 'native-hook', confidence: 'observed', open_span: true },
    current: { task_state: 'running' },
  })), 'working');
});

test('room assignment is backend-first, explicit, then task role, then lobby', () => {
  const mapLayout = layout();
  assert.equal(StateMachine.roomFor(agentEntry({ room: 'frontend', current: { task_role: 'tester' } }), mapLayout),
    'frontend', 'backend room is authoritative');
  assert.equal(StateMachine.roomFor(agentEntry({
    room: undefined, current: { task_role: 'tester', department: undefined },
  }), mapLayout), 'qa');
  assert.equal(StateMachine.roomFor(agentEntry({
    room: undefined, default_room: 'frontend', current: { task_role: 'tester' },
  }), mapLayout), 'frontend', 'explicit agent default beats role inference');
  assert.equal(StateMachine.roomFor(agentEntry({
    room: undefined, default_room: 'frontend', current: { department: 'research', task_role: 'tester' },
  }), mapLayout), 'research', 'task department beats agent default');
  assert.equal(StateMachine.roomFor(agentEntry({
    room: undefined, default_room: undefined, current: { task_role: 'implementer', department: undefined },
  }), mapLayout), 'lobby', 'implementer needs an explicit room');
  assert.equal(StateMachine.roomFor(agentEntry({
    name: 'tester reviewer backend exploratory', role: 'tester',
    room: undefined, default_room: undefined, current: {},
  }), mapLayout), 'lobby', 'display name and display role never assign a room');

  const wrapper = {
    kind: 'agent', key: 'a:000000000001', agent: agentEntry({ room: 'qa' }), room: 'qa', spawnable: true,
  };
  assert.equal(StateMachine.roomFor(wrapper, mapLayout), 'qa');
});

test('desk selection is stable, room-scoped and survives repeated derive calls', () => {
  const mapLayout = layout();
  const entry = agentEntry({ room: 'research' });
  const first = StateMachine.derive(entry, null, mapLayout);
  const second = StateMachine.derive(entry, first, mapLayout);
  assert.equal(first.room, 'research');
  assert.equal(first.deskId, 'research-1');
  assert.equal(second.deskId, first.deskId);
  assert.equal(first.key, 'a_000000000001:run-1:aaaaaaaaaaaa');
  assert.equal(first.evidence.warning, 'start-only');

  const qa = StateMachine.derive(agentEntry({
    room: 'qa', session_key: 'a_000000000001:run-2:aaaaaaaaaaaa',
  }), null, mapLayout);
  assert.equal(qa.deskId, 'qa-1');
});

test('identical display names in different sessions are never merged', () => {
  const first = agentEntry({
    name: 'Alex',
    agent_definition_id: 'a_000000000101',
    session_key: 'a_000000000101:project-a:111111111111',
    binding: { ...BINDING, agent_definition_id: 'a_000000000101', run_id: 'project-a' },
    room: 'backend',
  });
  const second = agentEntry({
    name: 'Alex',
    agent_definition_id: 'a_000000000202',
    session_key: 'a_000000000202:project-b:222222222222',
    binding: { ...BINDING, agent_definition_id: 'a_000000000202', run_id: 'project-b' },
    room: 'frontend',
  });
  assert.equal(StateMachine.dedupe([first, second]).length, 2,
    'display names are not identities; session keys are');
  const one = StateMachine.derive(first, null, layout());
  const two = StateMachine.derive(second, null, layout());
  assert.notEqual(one.key, two.key);
  assert.notEqual(one.room, two.room);
});

test('session keys dedupe duplicate spawns without merging different sessions', () => {
  const one = agentEntry();
  const duplicate = agentEntry({ status: 'completed' });
  const other = agentEntry({
    session_key: 'a_000000000001:run-2:bbbbbbbbbbbb',
    binding: { ...BINDING, run_id: 'run-2', native_hash: 'b'.repeat(64) },
    current: { run_id: 'run-2', task_state: 'running' },
  });
  const list = StateMachine.spawnable([one, duplicate, other]);
  assert.equal(list.length, 2);
  assert.equal(StateMachine.dedupe([one, duplicate]).length, 1);

  const fallbackA = {
    agent_definition_id: 'a_000000000001',
    binding: { run_id: 'run-3', native_hash: 'c'.repeat(64) },
  };
  const fallbackB = { ...fallbackA, binding: { ...fallbackA.binding } };
  assert.equal(StateMachine.sessionKey(fallbackA), StateMachine.sessionKey(fallbackB));
  assert.equal(StateMachine.dedupe([fallbackA, fallbackB]).length, 1);

  const wrapperOne = { kind: 'session', key: 's:run-9:deadbeef', session: sessionEntry() };
  const wrapperTwo = { kind: 'session', key: 's:run-9:deadbeef', session: sessionEntry() };
  assert.equal(StateMachine.dedupe([wrapperOne, wrapperTwo]).length, 1);
});

/* ── sprites ───────────────────────────────────────────────────────────── */

function fakeCanvasContext() {
  const calls = { fillRect: 0, fillText: 0 };
  const ctx = {
    calls,
    fillStyle: '',
    strokeStyle: '',
    globalAlpha: 1,
    font: '',
    textAlign: '',
    textBaseline: '',
    imageSmoothingEnabled: false,
    save() {},
    restore() {},
    translate() {},
    scale() {},
    setTransform() {},
    fillRect() { calls.fillRect += 1; },
    clearRect() {},
    drawImage() {},
    fillText() { calls.fillText += 1; },
    createRadialGradient() { return { addColorStop() {} }; },
    measureText(value) { return { width: String(value).length * 4 }; },
  };
  return ctx;
}

test('pixel sprites draw every state with rects and no external assets', () => {
  const ctx = fakeCanvasContext();
  const states = ['spawn', 'idle', 'walking', 'working', 'reading', 'testing', 'reviewing',
    'awaiting', 'blocked', 'completed', 'failed', 'unknown'];
  for (const state of states) {
    const before = ctx.calls.fillRect;
    Sprites.draw(ctx, {
      x: 100, y: 200, scale: 2, skin: { skin: 2, hair: 3, accessory: 'glasses', hairStyle: 1 },
      state, frame: 3, accent: '#e8a87c', facing: 'down', label: 'Ada', warning: state === 'working',
    });
    assert.ok(ctx.calls.fillRect > before, `${state} must draw pixels`);
  }
  for (const facing of ['up', 'down', 'left', 'right']) {
    Sprites.draw(ctx, { x: 0, y: 0, scale: 1, state: 'walking', frame: 1, facing });
  }
});

test('sprite variants are deterministic and identity is not name-derived', () => {
  const first = Sprites.variantFor('s:run-1:deadbeef');
  const second = Sprites.variantFor('s:run-1:deadbeef');
  assert.deepEqual(first, second);
  assert.notDeepEqual(first, Sprites.variantFor('s:run-2:deadbeef'));
  const box = Sprites.hitBox({ x: 10, y: 20, scale: 2 });
  assert.equal(box.w, Sprites.metrics.hitWidth * 2);
  assert.equal(box.h, Sprites.metrics.hitHeight * 2);
});

/* ── scene controller contract / stub-canvas smoke ─────────────────────── */

function fakeCanvas(width = 800, height = 600) {
  const ctx = fakeCanvasContext();
  const listeners = new Map();
  const canvas = {
    width: 0,
    height: 0,
    clientWidth: width,
    clientHeight: height,
    style: {},
    getContext() { return ctx; },
    getBoundingClientRect() { return { left: 0, top: 0, right: width, bottom: height, width, height }; },
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(callback);
    },
    removeEventListener(type, callback) {
      if (listeners.has(type)) listeners.get(type).delete(callback);
    },
    dispatch(type, event) {
      for (const callback of listeners.get(type) || []) callback(event);
    },
    setPointerCapture() {},
    releasePointerCapture() {},
  };
  return canvas;
}

test('scene controller exposes the contract and runs headlessly with stub canvas', () => {
  assert.equal(typeof sandbox.AOOfficeScene.create, 'function');
  const canvas = fakeCanvas();
  const documentStub = {
    hidden: false,
    addEventListener() {},
    removeEventListener() {},
    createElement() { return fakeCanvas(); },
  };
  canvas.ownerDocument = documentStub;
  let rafCallback = null;
  const sceneSandbox = loadScripts(files, {
    document: documentStub,
    performance: { now: () => 0, memory: null },
    devicePixelRatio: 2,
    requestAnimationFrame(callback) { rafCallback = callback; return 1; },
    cancelAnimationFrame() { rafCallback = null; },
    addEventListener() {},
    removeEventListener() {},
  });
  const hovered = [];
  const selected = [];
  const selectedRooms = [];
  const scene = sceneSandbox.AOOfficeScene.create({
    canvas,
    layout: layout(),
    reducedMotion: false,
    onSelectAgent(entry, info) { selected.push({ entry, info }); },
    onSelectRoom(room, info) { selectedRooms.push({ room, info }); },
    onHover(hit, point) { hovered.push({ hit, point }); },
  });
  for (const method of ['setLayout', 'syncAgents', 'setReducedMotion', 'resize', 'zoomIn', 'zoomOut',
    'resetView', 'setRoomFilter', 'editMode', 'saveLayout', 'resetLayout', 'destroy',
    'getCamera', 'getAgentPositions', 'getStats']) {
    assert.equal(typeof scene[method], 'function', `${method} must be exposed`);
  }
  assert.equal(scene.setReducedMotion(true), true);

  const wrapper = {
    kind: 'agent',
    key: 'a_000000000001:run-1:aaaaaaaaaaaa',
    agent: agentEntry({ room: 'research' }),
    room: 'research',
    spawnable: true,
  };
  scene.syncAgents([wrapper, wrapper]);
  let positions = scene.getAgentPositions();
  assert.equal(positions.length, 1, 'duplicate session key must not spawn twice');
  assert.equal(scene.getStats().agents, 1);
  assert.ok(scene.getStats().drawnFrames > 0);

  const camera = scene.getCamera();
  assert.ok(camera.scale >= 1);
  assert.ok(camera.baseScale >= 1);
  assert.ok(camera.tilePx > 0);

  const pointerEvent = (clientX, clientY, type) => ({
    pointerId: 1, pointerType: 'mouse', button: 0, clientX, clientY, type, preventDefault() {},
  });
  const agentScreenX = positions[0].x * camera.scale + camera.x;
  const agentScreenY = positions[0].y * camera.scale + camera.y - 8;
  canvas.dispatch('pointermove', pointerEvent(agentScreenX, agentScreenY, 'pointermove'));
  assert.equal(hovered.length, 1, 'hover callback fires for an agent');
  assert.equal(hovered[0].hit.type, 'agent');
  assert.equal(hovered[0].hit.name, 'Ada');
  assert.equal(hovered[0].hit.status, 'working');
  assert.ok(hovered[0].point && Number.isFinite(hovered[0].point.x));

  canvas.dispatch('pointerdown', pointerEvent(agentScreenX, agentScreenY, 'pointerdown'));
  canvas.dispatch('pointerup', pointerEvent(agentScreenX, agentScreenY, 'pointerup'));
  assert.equal(selected.length, 1, 'clicking a character selects its wrapper entry');
  assert.equal(selected[0].entry, wrapper);
  assert.equal(selected[0].info.agentId, 'a_000000000001');
  assert.equal(selected[0].info.room, 'research');

  const roomTileX = camera.x + 5.5 * 16 * camera.scale;
  const roomTileY = camera.y + 3.5 * 16 * camera.scale;
  canvas.dispatch('pointermove', pointerEvent(roomTileX, roomTileY, 'pointermove'));
  canvas.dispatch('pointerdown', pointerEvent(roomTileX, roomTileY, 'pointerdown'));
  canvas.dispatch('pointerup', pointerEvent(roomTileX, roomTileY, 'pointerup'));
  assert.equal(selectedRooms.length, 1);
  assert.equal(selectedRooms[0].room, 'research');
  assert.equal(selectedRooms[0].info.id, 'research');
  assert.equal(scene.screenToTile(roomTileX, roomTileY).x, 5);
  assert.equal(scene.screenToTile(roomTileX, roomTileY).y, 3);
  assert.equal(scene.getCamera().tilePx * scene.getCamera().zoom,
    camera.scale * 16, 'layout editor projection stays in sync');

  // Fake rAF ticks move the agent from the spawn hub without teleporting.
  const before = { ...positions[0] };
  let clock = 1000;
  for (let index = 0; index < 8 && rafCallback; index += 1) {
    const callback = rafCallback;
    rafCallback = null;
    clock += 100;
    callback(clock);
  }
  positions = scene.getAgentPositions();
  assert.equal(positions.length, 1);
  assert.ok(positions[0].x !== before.x || positions[0].y !== before.y,
    'agent should walk along its A* route');

  // A new task department reroutes the same character instead of teleporting.
  const qaWrapper = {
    kind: 'agent',
    key: wrapper.key,
    agent: agentEntry({
      room: 'qa',
      current: {
        run_id: 'run-1',
        task_id: 'verify-api',
        task_role: 'tester',
        task_state: 'running',
        department: 'qa',
        last_event: 'agent-start',
      },
    }),
    room: 'qa',
    spawnable: true,
  };
  scene.syncAgents([qaWrapper]);
  let rerouted = scene.getAgentPositions()[0];
  assert.equal(rerouted.room, 'qa', 'backend-authoritative department updates the room');
  assert.equal(rerouted.walking, true, 'room change starts a walk, not a teleport');
  for (let index = 0; index < 300 && rafCallback; index += 1) {
    const callback = rafCallback;
    rafCallback = null;
    clock += 100;
    callback(clock);
    if (!scene.getAgentPositions()[0].walking) break;
  }
  rerouted = scene.getAgentPositions()[0];
  assert.equal(rerouted.walking, false, 'the character reaches the new room');
  assert.equal(rerouted.room, 'qa');
  assert.equal(rerouted.deskId, 'qa-1');

  // Reduced motion still applies state and positions; it only freezes frames.
  scene.setReducedMotion(true);
  assert.equal(scene.getStats().reducedMotion, true);
  scene.setRoomFilter('research');
  assert.equal(scene.getStats().roomFilter, 'research');
  scene.setRoomFilter(null);
  assert.equal(scene.getStats().roomFilter, null);

  // Editing freezes the render loop for the layout editor overlay and does not
  // leak pointer panning into the scene.
  scene.editMode(true);
  assert.equal(scene.getStats().editing, true);
  scene.editMode(false);
  assert.equal(scene.getStats().editing, false);

  scene.destroy();
  assert.equal(scene.getStats().agents, 0);
});

test('scene rejects invalid layouts instead of rendering a corrupt map', () => {
  const canvas = fakeCanvas();
  const documentStub = {
    hidden: false,
    addEventListener() {},
    removeEventListener() {},
    createElement() { return fakeCanvas(); },
  };
  canvas.ownerDocument = documentStub;
  const sceneSandbox = loadScripts(files, {
    document: documentStub,
    performance: { now: () => 0 },
    devicePixelRatio: 1,
    requestAnimationFrame() { return 1; },
    cancelAnimationFrame() {},
    addEventListener() {},
    removeEventListener() {},
  });
  assert.throws(() => sceneSandbox.AOOfficeScene.create({
    canvas,
    layout: layout({ rooms: [{ id: 'broken', x: 30, y: 30, w: 6, h: 5, door: { x: 35, y: 32 } }] }),
  }), /invalid layout/i);
});
