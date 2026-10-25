import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import vm from 'node:vm';

const root = process.cwd();
const officeDir = join(root, 'tools', 'dashboard_assets', 'office');
const sources = {
  layouts: readFileSync(join(officeDir, 'office-layouts.js'), 'utf8'),
  rooms: readFileSync(join(officeDir, 'room-manager.js'), 'utf8'),
  editor: readFileSync(join(officeDir, 'layout-editor.js'), 'utf8'),
};

function createStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  const state = { throwGet: false, throwSet: false, throwRemove: false };
  return {
    data,
    state,
    getItem(key) {
      if (state.throwGet) throw new Error('storage read denied');
      return data.has(key) ? data.get(key) : null;
    },
    setItem(key, value) {
      if (state.throwSet) throw new Error('storage write denied');
      data.set(String(key), String(value));
    },
    removeItem(key) {
      if (state.throwRemove) throw new Error('storage remove denied');
      data.delete(String(key));
    },
  };
}

function boot(storage = null) {
  const sandbox = { console, window: {} };
  if (storage) {
    sandbox.window.localStorage = storage;
    sandbox.localStorage = storage;
  }
  vm.createContext(sandbox);
  vm.runInContext(sources.layouts, sandbox, { filename: 'office-layouts.js' });
  vm.runInContext(sources.rooms, sandbox, { filename: 'room-manager.js' });
  vm.runInContext(sources.editor, sandbox, { filename: 'layout-editor.js' });
  return {
    sandbox,
    L: sandbox.window.AOOfficeLayouts,
    R: sandbox.window.AORoomManager,
    E: sandbox.window.AOLayoutEditor,
  };
}

const { L, R } = boot();

function roomOf(layout, id) {
  return layout.rooms.find((room) => room.id === id);
}

function deskOf(layout, id) {
  return layout.desks.find((desk) => desk.id === id);
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function expectInvalid(layout, pattern) {
  const result = L.validate(layout);
  assert.equal(result.ok, false, 'expected layout to be invalid');
  assert.match(result.errors.join('\n'), pattern);
}

test('all four themes expose a complete, valid, walkable office', () => {
  assert.deepEqual(plain(L.themes), ['ai', 'security', 'web', 'custom']);
  assert.deepEqual(plain(L.rooms), ['frontend', 'backend', 'qa', 'review', 'research', 'meeting', 'lobby']);
  const signatures = new Set();
  const requiredFurniture = ['plant', 'shelf', 'table', 'sofa', 'chair'];

  for (const theme of L.themes) {
    const layout = L.get(theme, 'theme-check');
    const result = L.validate(layout);
    assert.ok(result.ok, `${theme}: ${result.errors.join('; ')}`);
    assert.equal(layout.theme, theme);
    assert.equal(layout.version, 1);
    assert.ok(layout.width >= 40 && layout.width <= 44, `${theme} width is 44-ish`);
    assert.ok(layout.height >= 26 && layout.height <= 28, `${theme} height is 28-ish`);
    assert.equal(layout.rooms.length >= 7, true);
    assert.deepEqual(
      plain(layout.rooms.map((room) => room.id).sort()),
      [...L.rooms].sort(),
      `${theme} must contain every default room exactly once`,
    );

    const types = new Set(layout.furniture.map((item) => item.type));
    for (const type of requiredFurniture) {
      assert.ok(types.has(type), `${theme} needs ${type} furniture`);
    }

    for (const room of layout.rooms) {
      assert.equal(room.door.x >= room.x && room.door.x < room.x + room.w, true);
      assert.equal(room.door.y >= room.y && room.door.y < room.y + room.h, true);
      const onVerticalWall = room.door.x === room.x || room.door.x === room.x + room.w - 1;
      const onHorizontalWall = room.door.y === room.y || room.door.y === room.y + room.h - 1;
      assert.notEqual(onVerticalWall, onHorizontalWall, `${theme}/${room.id} door must be on one wall`);
    }

    for (const roomId of ['frontend', 'backend', 'qa', 'review', 'research']) {
      assert.ok(layout.desks.some((desk) => desk.room === roomId), `${theme}/${roomId} needs at least one desk`);
    }
    assert.ok(layout.desks.length >= 20, `${theme} should be a real office, got ${layout.desks.length} desks`);

    signatures.add(layout.rooms.map((room) => `${room.id}:${room.x},${room.y},${room.w},${room.h}`).join('|'));
  }
  assert.equal(signatures.size, 4, 'room sizes and positions must differ per theme, not just colours');
});

test('validate rejects malformed versions, dimensions, rooms and doors', () => {
  const base = L.get('ai', 'invalid');

  const wrongVersion = L.clone(base);
  wrongVersion.version = 2;
  expectInvalid(wrongVersion, /version/);

  const wrongWidth = L.clone(base);
  wrongWidth.width = 8;
  expectInvalid(wrongWidth, /width/);

  const duplicateRooms = L.clone(base);
  duplicateRooms.rooms.find((room) => room.id === 'backend').id = 'research';
  expectInvalid(duplicateRooms, /duplicate room id/);

  const missingRoom = L.clone(base);
  missingRoom.rooms = missingRoom.rooms.filter((room) => room.id !== 'lobby');
  expectInvalid(missingRoom, /missing required room "lobby"/);

  const overlap = L.clone(base);
  roomOf(overlap, 'frontend').x = 19;
  expectInvalid(overlap, /overlaps or shares a wall/);

  const outside = L.clone(base);
  roomOf(outside, 'qa').x = outside.width - 2;
  expectInvalid(outside, /outside the/);

  const cornerDoor = L.clone(base);
  const research = roomOf(cornerDoor, 'research');
  research.door = { x: research.x, y: research.y };
  expectInvalid(cornerDoor, /corner/);

  const innerDoor = L.clone(base);
  roomOf(innerDoor, 'research').door = { x: 5, y: 4 };
  expectInvalid(innerDoor, /not on a wall/);

  const doorOnFurniture = L.clone(base);
  doorOnFurniture.furniture.push({ id: 'door-blocker', type: 'plant', x: 10, y: 9, w: 1, h: 1, blocked: true });
  expectInvalid(doorOnFurniture, /door/);

  const floorEdgeDoor = L.clone(base);
  const edgeRoom = roomOf(floorEdgeDoor, 'research');
  edgeRoom.x = 0;
  edgeRoom.door = { x: 0, y: 5 };
  expectInvalid(floorEdgeDoor, /opens outside the floor/);
});

test('validate rejects bad desks, seats, furniture and spawn points', () => {
  const base = L.get('ai', 'invalid-desks');

  const deskOnWall = L.clone(base);
  const researchDesk = deskOnWall.desks.find((desk) => desk.room === 'research');
  const researchRoom = roomOf(deskOnWall, 'research');
  researchDesk.x = researchRoom.x;
  expectInvalid(deskOnWall, /interior/);

  const seatOutside = L.clone(base);
  const outsideDesk = seatOutside.desks.find((desk) => desk.id === 'research-1');
  outsideDesk.seat = { x: outsideDesk.x, y: roomOf(seatOutside, 'research').y };
  expectInvalid(seatOutside, /seat/);

  const occupiedSeat = L.clone(base);
  const firstDesk = occupiedSeat.desks.find((desk) => desk.id === 'research-1');
  occupiedSeat.furniture.push({
    id: 'seat-blocker',
    type: 'plant',
    x: firstDesk.seat.x,
    y: firstDesk.seat.y,
    w: 1,
    h: 1,
    blocked: true,
  });
  expectInvalid(occupiedSeat, /covers seat|seat/);

  const duplicateSeat = L.clone(base);
  duplicateSeat.desks[1].seat = { x: duplicateSeat.desks[0].seat.x, y: duplicateSeat.desks[0].seat.y };
  expectInvalid(duplicateSeat, /seat/);

  const furnitureOutside = L.clone(base);
  furnitureOutside.furniture.push({ id: 'runaway', type: 'cabinet', x: furnitureOutside.width, y: 3, w: 1, h: 1, blocked: true });
  expectInvalid(furnitureOutside, /outside the floor/);

  const furnitureOverlap = L.clone(base);
  furnitureOverlap.furniture.push({ id: 'overlap-a', type: 'cabinet', x: 21, y: 10, w: 1, h: 1, blocked: true });
  furnitureOverlap.furniture.push({ id: 'overlap-b', type: 'shelf', x: 21, y: 10, w: 1, h: 1, blocked: true });
  expectInvalid(furnitureOverlap, /overlaps furniture/);

  const spawnOnWall = L.clone(base);
  spawnOnWall.spawn = { x: roomOf(base, 'research').x, y: roomOf(base, 'research').y + 1 };
  expectInvalid(spawnOnWall, /spawn/);

  const badDecor = L.clone(base);
  badDecor.decor.push({ type: 'rug', x: badDecor.width - 1, y: 0, w: 4, h: 2 });
  expectInvalid(badDecor, /decor.*outside the floor/);
});

test('get() is deterministic per project and valid across seeds', () => {
  for (const theme of L.themes) {
    const first = L.serialize(L.get(theme, 'project-alpha'));
    const second = L.serialize(L.get(theme, 'project-alpha'));
    assert.equal(first, second, `${theme} must be stable for one project`);

    for (let seed = 0; seed < 8; seed += 1) {
      const layout = L.get(theme, `seed-${seed}`);
      const result = L.validate(layout);
      assert.ok(result.ok, `${theme}/seed-${seed}: ${result.errors.join('; ')}`);
    }
  }

  const variants = new Set();
  for (let index = 0; index < 12; index += 1) variants.add(L.serialize(L.get('ai', `variant-${index}`)));
  assert.ok(variants.size > 1, 'the project seed must add a small safe variation');
});

test('load/save/reset use localStorage overrides and survive storage errors', () => {
  const storage = createStorage();
  const { L: layouts } = boot(storage);
  const layout = layouts.get('web', 'project-1');

  assert.equal(layouts.storageKey('project-1'), 'ao-office-layout:project-1');
  assert.equal(layouts.storageKey(''), 'ao-office-layout:default');
  assert.equal(layouts.save('project-1', layout), true);
  assert.ok(storage.data.has('ao-office-layout:project-1'));

  const loaded = layouts.load('project-1', 'ai');
  assert.equal(layouts.serialize(loaded), layouts.serialize(layout));
  assert.equal(loaded.theme, 'web', 'a valid project override wins over the theme default');

  const invalid = layouts.clone(layout);
  invalid.version = 99;
  assert.equal(layouts.save('project-1', invalid), false);
  assert.equal(layouts.serialize(JSON.parse(storage.data.get('ao-office-layout:project-1'))), layouts.serialize(layout));

  storage.data.set('ao-office-layout:project-1', '{not-json');
  const afterBrokenJson = layouts.load('project-1', 'security');
  assert.equal(afterBrokenJson.theme, 'security');
  assert.ok(layouts.validate(afterBrokenJson).ok);

  storage.data.set('ao-office-layout:project-1', JSON.stringify(invalid));
  assert.equal(layouts.load('project-1', 'security').theme, 'security');

  assert.equal(layouts.reset('project-1'), true);
  assert.equal(storage.data.has('ao-office-layout:project-1'), false);

  storage.state.throwSet = true;
  assert.equal(layouts.save('project-2', layout), false);
  storage.state.throwSet = false;

  storage.state.throwGet = true;
  assert.equal(layouts.load('project-2', 'ai').theme, 'ai');
  storage.state.throwGet = false;

  storage.state.throwRemove = true;
  assert.equal(layouts.reset('project-2'), false);
  storage.state.throwRemove = false;

  const memoryOnly = boot(null).L;
  assert.equal(memoryOnly.save('project-3', layout), false);
  assert.equal(memoryOnly.load('project-3', 'custom').theme, 'custom');
  assert.equal(memoryOnly.reset('project-3'), false);
});

test('moveRoom shifts the door, desks, seats and the room contents', () => {
  const base = L.get('ai', 'move-room');
  const original = L.serialize(base);
  const room = roomOf(base, 'research');
  const door = { ...room.door };
  const desk = base.desks.find((item) => item.room === 'research');
  const deskStart = { x: desk.x, y: desk.y };
  const seatStart = { ...desk.seat };
  const roomFurniture = base.furniture.find((item) => item.type === 'shelf');
  const furnitureStart = { x: roomFurniture.x, y: roomFurniture.y };

  const result = L.moveRoom(base, 'research', 1, 0);
  assert.ok(result.ok, result.errors.join('; '));
  assert.ok(L.validate(result.layout).ok);

  const movedRoom = roomOf(result.layout, 'research');
  assert.equal(movedRoom.x, room.x + 1);
  assert.equal(movedRoom.y, room.y);
  assert.deepEqual(plain(movedRoom.door), { x: door.x + 1, y: door.y });

  const movedDesk = deskOf(result.layout, desk.id);
  assert.equal(movedDesk.x, deskStart.x + 1);
  assert.equal(movedDesk.y, deskStart.y);
  assert.equal(movedDesk.seat.x, seatStart.x + 1);
  assert.equal(movedDesk.seat.y, seatStart.y);

  const movedShelf = result.layout.furniture.find((item) => item.id === roomFurniture.id);
  assert.equal(movedShelf.x, furnitureStart.x + 1);
  assert.equal(movedShelf.y, furnitureStart.y);
  assert.equal(L.serialize(base), original, 'moveRoom must not mutate its input');

  const overlap = L.moveRoom(base, 'research', 10, 0);
  assert.equal(overlap.ok, false);
  assert.equal(overlap.layout, null);
  assert.match(overlap.errors.join('\n'), /overlap|wall/);

  assert.equal(L.moveRoom(base, 'not-a-room', 1, 0).ok, false);
  assert.equal(L.moveRoom(base, 'research', 0.5, 0).ok, false);
});

test('moveDoor snaps to a non-corner wall tile and keeps the office valid', () => {
  const base = L.get('ai', 'door-test');
  const room = roomOf(base, 'research');
  const original = L.serialize(base);

  const top = L.moveDoor(base, 'research', room.x + 4, room.y + 2);
  assert.equal(top.ok, true, top.errors && top.errors.join('; '));
  assert.deepEqual(plain(roomOf(top.layout, 'research').door), {
    x: room.x + 4,
    y: room.y,
  });
  assert.equal(L.serialize(base), original, 'moveDoor must not mutate its input');

  const right = L.moveDoor(top.layout, 'research', room.x + room.w - 2, room.y + 3);
  assert.equal(right.ok, true, right.errors && right.errors.join('; '));
  assert.deepEqual(plain(roomOf(right.layout, 'research').door), {
    x: room.x + room.w - 1,
    y: room.y + 3,
  });

  const corner = L.moveDoor(base, 'research', room.x, room.y);
  assert.equal(corner.ok, true, 'a corner click still snaps to the nearest wall tile: ' + ((corner.errors || []).join('; ')));
  const cornerDoor = roomOf(corner.layout, 'research').door;
  const onOneWall = cornerDoor.x === room.x || cornerDoor.x === room.x + room.w - 1
    || cornerDoor.y === room.y || cornerDoor.y === room.y + room.h - 1;
  const onCorner = (cornerDoor.x === room.x || cornerDoor.x === room.x + room.w - 1)
    && (cornerDoor.y === room.y || cornerDoor.y === room.y + room.h - 1);
  assert.equal(onOneWall && !onCorner, true);

  assert.equal(L.moveDoor(base, 'missing-room', 2, 2).ok, false);
  assert.equal(L.moveDoor(base, 'research', 2.5, 2).ok, false);
});

test('moveDesk stays inside its room and on a free, adjacent seat', () => {
  const base = L.get('ai', 'move-desk');
  const original = L.serialize(base);
  const desk = deskOf(base, 'research-1');

  const result = L.moveDesk(base, 'research-1', 1, 0);
  assert.ok(result.ok, result.errors.join('; '));
  const moved = deskOf(result.layout, 'research-1');
  assert.equal(moved.x, desk.x + 1);
  assert.equal(moved.y, desk.y);
  assert.equal(moved.seat.x, desk.seat.x + 1);
  assert.equal(moved.seat.y, desk.seat.y);
  assert.equal(L.serialize(base), original, 'moveDesk must not mutate its input');

  const outside = L.moveDesk(base, 'research-1', 30, 0);
  assert.equal(outside.ok, false);
  assert.equal(outside.layout, null);

  const seatOnWall = L.moveDesk(base, 'research-1', 5, 4);
  assert.equal(seatOnWall.ok, false);
  assert.match(seatOnWall.errors.join('\n'), /seat|interior/);

  const occupied = L.clone(base);
  const target = { x: desk.x + 1, y: desk.y };
  occupied.furniture.push({ id: 'desk-blocker', type: 'plant', x: target.x, y: target.y, w: 1, h: 1, blocked: true });
  const blocked = L.moveDesk(occupied, 'research-1', 1, 0);
  assert.equal(blocked.ok, false);
  assert.match(blocked.errors.join('\n'), /desk|furniture|seat/);

  const ontoAnotherDesk = L.moveDesk(base, 'research-1', 3, 0);
  assert.equal(ontoAnotherDesk.ok, false);
  assert.match(ontoAnotherDesk.errors.join('\n'), /desk/);

  assert.equal(L.moveDesk(base, 'missing-desk', 1, 0).ok, false);
});

test('pure helpers expose room/desk lookup, cloning and serialization', () => {
  const base = L.get('security', 'helpers');
  const room = L.roomAt(base, roomOf(base, 'qa').x + 2, roomOf(base, 'qa').y + 2);
  assert.equal(room.id, 'qa');
  assert.equal(L.roomAt(base, 0, 0), null);

  const desk = base.desks.find((item) => item.room === 'research');
  assert.equal(L.deskFor(base, desk.seat.x, desk.seat.y).id, desk.id);
  assert.equal(L.deskFor(base, desk.x, desk.y).id, desk.id);
  assert.equal(L.deskById(base, desk.id).id, desk.id);

  const copy = L.clone(base);
  copy.rooms[0].x = 999;
  copy.desks[0].seat.x = 999;
  assert.notEqual(base.rooms[0].x, 999);
  assert.notEqual(base.desks[0].seat.x, 999);
  assert.equal(typeof L.serialize(base), 'string');
  assert.equal(JSON.parse(L.serialize(base)).theme, 'security');

  assert.deepEqual({ ...L.seatFor(5, 4, 'up') }, { x: 5, y: 5 });
  assert.deepEqual({ ...L.seatFor(5, 4, 'down') }, { x: 5, y: 3 });
  assert.deepEqual({ ...L.seatFor(5, 4, 'left') }, { x: 6, y: 4 });
  assert.deepEqual({ ...L.seatFor(5, 4, 'right') }, { x: 4, y: 4 });
});

test('room assignment follows the explicit backend order and never guesses names', () => {
  const cases = [
    [{ task: { department: 'backend' } }, { room: 'backend', source: 'task.department' }],
    [{ task: { assigned_room: 'frontend' } }, { room: 'frontend', source: 'task.department' }],
    [{ task: { department: 'qa' }, agent: { default_room: 'research' } }, { room: 'qa', source: 'task.department' }],
    [{ agent: { default_room: 'review' } }, { room: 'review', source: 'agent.default_room' }],
    [{ agent: { assigned_room: 'research' } }, { room: 'research', source: 'agent.default_room' }],
    [{ task: { role: 'tester' } }, { room: 'qa', source: 'task.role' }],
    [{ task: { role: 'reviewer' } }, { room: 'review', source: 'task.role' }],
    [{ task: { role: 'explorer' } }, { room: 'research', source: 'task.role' }],
    [{ task: { role: 'implementer' } }, { room: 'lobby', source: 'unassigned' }],
    [{ task: { role: 'implementer', department: 'backend' } }, { room: 'backend', source: 'task.department' }],
    [{ task: { department: 'data science' } }, { room: 'lobby', source: 'unassigned' }],
    [{ task: { department: 'front end' } }, { room: 'lobby', source: 'unassigned' }],
    [{}, { room: 'lobby', source: 'unassigned' }],
    [null, { room: 'lobby', source: 'unassigned' }],
  ];

  for (const [input, expected] of cases) {
    assert.deepEqual(plain(R.assignRoom(input)), expected);
  }

  assert.equal(R.roleRoom('tester'), 'qa');
  assert.equal(R.roleRoom('reviewer'), 'review');
  assert.equal(R.roleRoom('explorer'), 'research');
  assert.equal(R.roleRoom('implementer'), null);
  assert.equal(R.normalizeRoom('frontend'), 'frontend');
  assert.equal(R.normalizeRoom('FRONTEND'), 'frontend');
  assert.equal(R.normalizeRoom('front end'), null);
  assert.equal(R.normalizeRoom('lobby '), 'lobby');
});

test('roomFor honours an authoritative backend room and mirrors fallback order', () => {
  assert.deepEqual(plain(R.roomFor({ room: 'backend', room_source: 'task.department' })), {
    room: 'backend',
    source: 'task.department',
  });
  assert.deepEqual(plain(R.roomFor({ room: 'lobby', room_source: 'unlinked' })), {
    room: 'lobby',
    source: 'unlinked',
  });
  assert.deepEqual(plain(R.roomFor({ current: { task_role: 'tester' } })), {
    room: 'qa',
    source: 'task.role',
  });
  assert.deepEqual(plain(R.roomFor({ current: { department: 'frontend', task_role: 'tester' } })), {
    room: 'frontend',
    source: 'task.department',
  });
  assert.deepEqual(plain(R.roomFor({ agent: { default_room: 'review' } })), {
    room: 'review',
    source: 'agent.default_room',
  });
  assert.deepEqual(plain(R.roomFor({ room: 'made-up-room' })), {
    room: 'lobby',
    source: 'unassigned',
  });
  assert.deepEqual(plain(R.roomFor({
    room_source: 'unlinked',
    current: { task_role: 'tester' },
  })), { room: 'lobby', source: 'unlinked' });
  assert.deepEqual(plain(R.roomFor(null)), { room: 'lobby', source: 'unassigned' });
  /* office.js calls AORoomManager.assign(rawEntry) as its fallback. */
  assert.deepEqual(plain(R.assign({ current: { task_role: 'explorer' } })), {
    room: 'research',
    source: 'task.role',
  });
  assert.deepEqual(plain(R.assign({ room: 'qa', room_source: 'agent.default_room' })), {
    room: 'qa',
    source: 'agent.default_room',
  });
});

test('desk assignment is room-local, stable and unique unless sharing is explicit', () => {
  const layout = L.get('web', 'desks');
  const frontendDesks = R.desksForRoom(layout, 'frontend');
  assert.equal(frontendDesks.length, 8);
  assert.deepEqual(
    plain(frontendDesks.map((desk) => desk.id)),
    plain(frontendDesks.map((desk) => desk.id).slice().sort()),
    'desk order must be stable',
  );

  const entries = frontendDesks.map((desk, index) => ({
    id: `agent-${String(index).padStart(2, '0')}`,
    room: 'frontend',
  }));
  const result = R.assignDesks(layout, entries);
  const assigned = Object.values(result.assignments);
  assert.equal(assigned.length, 8);
  assert.equal(new Set(assigned).size, 8, 'no two agents may share a desk by default');
  assert.deepEqual(
    assigned.slice().sort(),
    plain(frontendDesks.map((desk) => desk.id).sort()),
  );
  for (const deskId of assigned) {
    assert.equal(R.desksForRoom(layout, 'frontend').some((desk) => desk.id === deskId), true);
  }
  assert.deepEqual(plain(result.order), plain(entries.map((entry) => entry.id).sort()));

  const overflowEntries = entries.concat([
    { id: 'agent-99', room: 'frontend' },
    { id: 'agent-98', room: 'frontend' },
  ]);
  const overflow = R.assignDesks(layout, overflowEntries);
  assert.equal(overflow.unassigned.length, 2);
  assert.equal(Object.values(overflow.assignments).filter((value) => value === null).length, 2);

  const reversed = R.assignDesks(layout, overflowEntries.slice().reverse());
  assert.deepEqual(plain(reversed.assignments), plain(overflow.assignments), 'input order must not change desks');

  const shared = R.assignDesks(layout, overflowEntries, { allowShared: true });
  assert.equal(shared.unassigned.length, 0);
  assert.equal(Object.values(shared.assignments).filter((value) => value !== null).length, 10);
  assert.ok(new Set(Object.values(shared.assignments)).size < 10, 'sharing must reuse a desk');

  const explicitShareLayout = L.clone(layout);
  deskOf(explicitShareLayout, frontendDesks[0].id).share = true;
  const explicitShare = R.assignDesks(explicitShareLayout, entries.concat([{ id: 'agent-extra', room: 'frontend' }]));
  assert.equal(explicitShare.assignments['agent-extra'], frontendDesks[0].id);

  const qaEntries = [
    { id: 'a', current: { task_role: 'tester' } },
    { id: 'b', current: { task_role: 'tester' } },
  ];
  const qaResult = R.assignDesks(layout, qaEntries);
  for (const deskId of Object.values(qaResult.assignments)) {
    assert.equal(R.desksForRoom(layout, 'qa').some((desk) => desk.id === deskId), true);
  }

  const lobby = R.assignDesks(layout, [{ id: 'ghost', room: 'lobby' }]);
  assert.equal(lobby.assignments.ghost, null);
  assert.deepEqual(plain(lobby.unassigned), ['ghost']);
  assert.equal(R.desksForRoom(layout, 'meeting').length, 0);
  assert.equal(R.desksForRoom(layout, 'not-a-room').length, 0);
});


test('layout editor snaps room/desk drags, rejects invalid moves and supports save/reset/escape', () => {
  const { sandbox, L: layouts, E: editorApi } = boot();
  assert.ok(editorApi, 'layout-editor.js must expose AOLayoutEditor');
  let layout = layouts.get('ai', 'editor');
  let changes = 0;
  let saved = null;
  let resetCalls = 0;
  const errors = [];
  const listeners = Object.create(null);
  const documentListeners = Object.create(null);
  const doc = {
    addEventListener(name, handler) {
      (documentListeners[name] = documentListeners[name] || []).push(handler);
    },
    removeEventListener(name, handler) {
      documentListeners[name] = (documentListeners[name] || []).filter((item) => item !== handler);
    },
  };
  sandbox.window.document = doc;

  const context = {
    save() {}, restore() {}, setTransform() {}, setLineDash() {}, strokeRect() {},
    globalAlpha: 1, strokeStyle: '', lineWidth: 0,
  };
  const canvas = {
    width: 704,
    height: 448,
    style: { cursor: '' },
    getContext() { return context; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 704, height: 448 }; },
    addEventListener(name, handler) {
      (listeners[name] = listeners[name] || []).push(handler);
    },
    removeEventListener(name, handler) {
      listeners[name] = (listeners[name] || []).filter((item) => item !== handler);
    },
    setPointerCapture() {},
    releasePointerCapture() {},
  };
  const scene = {
    modes: [],
    tileCalls: [],
    editMode(value) { this.modes.push(value); },
    getCamera() { return { offsetX: 0, offsetY: 0, zoom: 1, tileSize: 16 }; },
    clientToTile(clientX, clientY) {
      this.tileCalls.push([clientX, clientY]);
      return { x: Math.floor(clientX / 16), y: Math.floor(clientY / 16) };
    },
  };
  const editor = editorApi.attach({
    canvas,
    scene,
    getLayout: () => layout,
    onChange: (next) => { layout = next; changes += 1; },
    onSave: (next) => { saved = next; return true; },
    onReset: () => { resetCalls += 1; },
    onError: (message) => { errors.push(message); },
  });

  function fire(name, event) {
    for (const handler of listeners[name] || []) handler(event);
  }
  function pointer(tileX, tileY, extra = {}) {
    return {
      clientX: tileX * 16 + 8,
      clientY: tileY * 16 + 8,
      button: 0,
      pointerId: 1,
      pointerType: 'mouse',
      preventDefault() {},
      ...extra,
    };
  }
  function key(name) {
    for (const handler of documentListeners.keydown || []) {
      handler({ key: name, target: {}, preventDefault() {} });
    }
  }

  editor.enable(true);
  assert.equal(editor.isEnabled(), true);
  assert.deepEqual(scene.modes, [true]);

  const researchBefore = roomOf(layout, 'research');
  fire('pointerdown', pointer(7, 6));
  assert.ok(scene.tileCalls.some(([x, y]) => x === 7 * 16 + 8 && y === 6 * 16 + 8),
    'the editor passes client coordinates to scene.clientToTile');
  fire('pointermove', pointer(8, 6));
  fire('pointerup', pointer(8, 6));
  const researchAfter = roomOf(layout, 'research');
  assert.equal(researchAfter.x, researchBefore.x + 1);
  assert.equal(researchAfter.y, researchBefore.y);
  assert.equal(researchAfter.door.x, researchBefore.door.x + 1);
  assert.equal(changes, 1);
  assert.deepEqual({ ...editor.getSelection() }, { kind: 'room', id: 'research' });

  const deskBefore = deskOf(layout, 'research-1');
  fire('pointerdown', pointer(deskBefore.seat.x, deskBefore.seat.y));
  fire('pointermove', pointer(deskBefore.seat.x + 1, deskBefore.seat.y));
  fire('pointerup', pointer(deskBefore.seat.x + 1, deskBefore.seat.y));
  const deskAfter = deskOf(layout, 'research-1');
  assert.equal(deskAfter.x, deskBefore.x + 1);
  assert.equal(deskAfter.seat.x, deskBefore.seat.x + 1);
  assert.equal(changes, 2);

  assert.equal(editor.save(), true);
  assert.equal(saved.rooms.find((room) => room.id === 'research').x, researchAfter.x);
  assert.equal(editor.reset(), true);
  assert.equal(resetCalls, 1);

  const stable = layouts.serialize(layout);
  const researchNow = roomOf(layout, 'research');
  fire('pointerdown', pointer(researchNow.x + 4, researchNow.y + 3));
  fire('pointermove', pointer(researchNow.x + 4 + 20, researchNow.y + 3));
  fire('pointerup', pointer(researchNow.x + 4 + 20, researchNow.y + 3));
  assert.equal(layouts.serialize(layout), stable, 'out-of-bounds/colliding drags must be rejected');
  assert.ok(errors.length > 0, 'the editor reports rejected placements');

  const beforeEscape = layouts.serialize(layout);
  const roomForEscape = roomOf(layout, 'research');
  fire('pointerdown', pointer(roomForEscape.x + 4, roomForEscape.y + 3));
  fire('pointermove', pointer(roomForEscape.x + 5, roomForEscape.y + 3));
  key('Escape');
  assert.equal(layouts.serialize(layout), beforeEscape, 'Escape restores the pre-drag layout');

  editor.enable(false);
  assert.equal(scene.modes[0], true);
  assert.equal(scene.modes[scene.modes.length - 1], false);
  assert.equal(Object.values(listeners).reduce((total, handlers) => total + handlers.length, 0), 0);
  editor.destroy();
  assert.equal(canvas.style.cursor, '');
});
