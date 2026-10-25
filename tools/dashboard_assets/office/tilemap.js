/* Auto-Orchestrator V7.2 — virtual office tilemap.
 *
 * Turns the versioned office layout (rooms, furniture, desks, decor) into a
 * deterministic walkability grid. Rooms are represented by their bounding
 * rectangle: the outer ring is wall, the interior is floor, and an explicit
 * door tile cuts one opening in that ring. Everything outside a room is a
 * corridor. Furniture marked blocked, desk tiles and walls are non-walkable;
 * seat tiles are always walkable.
 *
 * No DOM, no external assets, no state beyond the object returned by build().
 */
(() => {
  'use strict';

  const DEFAULT_TILE = 16;
  const CELL_FLOOR = 0;
  const CELL_WALL = 1;
  const CELL_DOOR = 2;

  function invalid(message) {
    throw new TypeError(`AOTilemap: ${message}`);
  }

  function integer(value, name, minimum = 0) {
    if (!Number.isInteger(value) || value < minimum) {
      invalid(`${name} must be an integer >= ${minimum} (got ${JSON.stringify(value)})`);
    }
    return value;
  }

  function optionalInteger(value, name, fallback, minimum = 0) {
    if (value == null) return fallback;
    return integer(value, name, minimum);
  }

  function text(value) {
    return typeof value === 'string' ? value.trim() : '';
  }

  function rectsOverlap(a, b) {
    return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  }

  /* Rooms need at least one corridor tile between their wall rings. */
  function roomsSeparated(a, b) {
    return a.x + a.w + 1 <= b.x
      || b.x + b.w + 1 <= a.x
      || a.y + a.h + 1 <= b.y
      || b.y + b.h + 1 <= a.y;
  }

  function build(layout) {
    if (!layout || typeof layout !== 'object' || Array.isArray(layout)) {
      invalid('layout object is required');
    }
    const width = integer(layout.width, 'width', 1);
    const height = integer(layout.height, 'height', 1);
    const tile = optionalInteger(layout.tile, 'tile', DEFAULT_TILE, 1);
    const size = width * height;
    const walkable = new Uint8Array(size);
    const wall = new Uint8Array(size);
    const door = new Uint8Array(size);
    const roomIndex = new Int16Array(size);
    const blocked = new Uint8Array(size);
    const cellKind = new Uint8Array(size);
    roomIndex.fill(-1);
    walkable.fill(1);
    cellKind.fill(CELL_FLOOR);

    const indexOf = (x, y) => y * width + x;
    const inBounds = (x, y) => Number.isInteger(x) && Number.isInteger(y)
      && x >= 0 && y >= 0 && x < width && y < height;

    const rooms = [];
    const roomById = new Map();
    const roomRects = [];
    const doors = [];
    const doorIds = new Map();
    const roomNames = new Set();

    /* ── rooms ─────────────────────────────────────────────────────────── */
    const rawRooms = layout.rooms == null ? [] : layout.rooms;
    if (!Array.isArray(rawRooms)) invalid('rooms must be an array');
    rawRooms.forEach((raw, position) => {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
        invalid(`room[${position}] must be an object`);
      }
      const id = text(raw.id);
      if (!id) invalid(`room[${position}] needs a non-empty id`);
      if (roomById.has(id)) invalid(`duplicate room id "${id}"`);
      if (roomNames.has(id)) invalid(`duplicate room id "${id}"`);
      roomNames.add(id);
      const x = integer(raw.x, `room[${id}].x`, 0);
      const y = integer(raw.y, `room[${id}].y`, 0);
      const w = integer(raw.w, `room[${id}].w`, 3);
      const h = integer(raw.h, `room[${id}].h`, 3);
      if (x + w > width || y + h > height) {
        invalid(`room "${id}" (${x},${y},${w}x${h}) is outside the ${width}x${height} map`);
      }
      const rect = { x, y, w, h };
      for (const other of rooms) {
        if (rectsOverlap(rect, other.rect) || !roomsSeparated(rect, other.rect)) {
          invalid(`rooms "${id}" and "${other.id}" overlap or touch`);
        }
      }
      const room = {
        id,
        name: text(raw.name) || id,
        department: text(raw.department) || id,
        x,
        y,
        w,
        h,
        rect,
        interior: { x: x + 1, y: y + 1, w: w - 2, h: h - 2 },
        door: null,
      };
      roomRects.push(rect);
      rooms.push(room);
      roomById.set(id, room);

      for (let ty = y; ty < y + h; ty += 1) {
        for (let tx = x; tx < x + w; tx += 1) {
          const index = indexOf(tx, ty);
          if (roomIndex[index] !== -1) {
            invalid(`room "${id}" overlaps room "${rooms[roomIndex[index]].id}" at ${tx},${ty}`);
          }
          roomIndex[index] = rooms.length - 1;
          const edge = tx === x || ty === y || tx === x + w - 1 || ty === y + h - 1;
          if (edge) {
            wall[index] = 1;
            cellKind[index] = CELL_WALL;
            walkable[index] = 0;
          }
        }
      }

      if (raw.door != null) {
        const rawDoor = raw.door;
        if (!rawDoor || typeof rawDoor !== 'object' || Array.isArray(rawDoor)) {
          invalid(`room "${id}" door must be an object`);
        }
        const dx = integer(rawDoor.x, `room[${id}].door.x`, 0);
        const dy = integer(rawDoor.y, `room[${id}].door.y`, 0);
        const key = `${dx},${dy}`;
        if (doorIds.has(key)) {
          invalid(`duplicate door tile ${dx},${dy} (rooms "${doorIds.get(key)}" and "${id}")`);
        }
        if (!inBounds(dx, dy)) invalid(`room "${id}" door is outside the map`);
        const onEdgeX = dx === x || dx === x + w - 1;
        const onEdgeY = dy === y || dy === y + h - 1;
        if (!onEdgeX && !onEdgeY) {
          invalid(`room "${id}" door ${dx},${dy} is not on the room wall`);
        }
        if (onEdgeX && onEdgeY) {
          invalid(`room "${id}" door ${dx},${dy} is on a corner`);
        }
        const side = dy === y ? 'up'
          : dy === y + h - 1 ? 'down'
            : dx === x ? 'left' : 'right';
        const outside = side === 'up' ? { x: dx, y: dy - 1 }
          : side === 'down' ? { x: dx, y: dy + 1 }
            : side === 'left' ? { x: dx - 1, y: dy }
              : { x: dx + 1, y: dy };
        const inside = side === 'up' ? { x: dx, y: dy + 1 }
          : side === 'down' ? { x: dx, y: dy - 1 }
            : side === 'left' ? { x: dx + 1, y: dy }
              : { x: dx - 1, y: dy };
        if (!inBounds(outside.x, outside.y)) {
          invalid(`room "${id}" door ${dx},${dy} opens outside the map`);
        }
        if (roomIndex[indexOf(inside.x, inside.y)] !== rooms.length - 1) {
          invalid(`room "${id}" door ${dx},${dy} does not open into its interior`);
        }
        const doorEntry = {
          x: dx,
          y: dy,
          roomId: id,
          side,
          outside,
          inside,
          key,
        };
        room.door = doorEntry;
        doors.push(doorEntry);
        doorIds.set(key, id);
        wall[indexOf(dx, dy)] = 0;
        door[indexOf(dx, dy)] = 1;
        cellKind[indexOf(dx, dy)] = CELL_DOOR;
        walkable[indexOf(dx, dy)] = 1;
      }
    });

    /* ── furniture ─────────────────────────────────────────────────────── */
    const furniture = [];
    const rawFurniture = layout.furniture == null ? [] : layout.furniture;
    if (!Array.isArray(rawFurniture)) invalid('furniture must be an array');
    rawFurniture.forEach((raw, position) => {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
        invalid(`furniture[${position}] must be an object`);
      }
      const x = integer(raw.x, `furniture[${position}].x`, 0);
      const y = integer(raw.y, `furniture[${position}].y`, 0);
      const w = optionalInteger(raw.w, `furniture[${position}].w`, 1, 1);
      const h = optionalInteger(raw.h, `furniture[${position}].h`, 1, 1);
      if (x + w > width || y + h > height) {
        invalid(`furniture[${position}] (${x},${y},${w}x${h}) is outside the map`);
      }
      const item = {
        id: text(raw.id) || `furniture-${position}`,
        type: text(raw.type) || 'crate',
        x,
        y,
        w,
        h,
        blocked: raw.blocked === true,
      };
      furniture.push(item);
      if (item.blocked) {
        for (let ty = y; ty < y + h; ty += 1) {
          for (let tx = x; tx < x + w; tx += 1) {
            const index = indexOf(tx, ty);
            if (door[index]) {
              invalid(`blocked furniture "${item.id}" covers door tile ${tx},${ty}`);
            }
            blocked[index] = 1;
            walkable[index] = 0;
          }
        }
      }
    });

    /* ── desks ─────────────────────────────────────────────────────────── */
    const desks = [];
    const deskIds = new Set();
    const occupiedDeskTiles = new Uint8Array(size);
    const seatTiles = new Uint8Array(size);
    const rawDesks = layout.desks == null ? [] : layout.desks;
    if (!Array.isArray(rawDesks)) invalid('desks must be an array');
    rawDesks.forEach((raw, position) => {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
        invalid(`desks[${position}] must be an object`);
      }
      const id = text(raw.id);
      if (!id) invalid(`desks[${position}] needs a non-empty id`);
      if (deskIds.has(id)) invalid(`duplicate desk id "${id}"`);
      deskIds.add(id);
      const x = integer(raw.x, `desk[${id}].x`, 0);
      const y = integer(raw.y, `desk[${id}].y`, 0);
      const w = optionalInteger(raw.w, `desk[${id}].w`, 1, 1);
      const h = optionalInteger(raw.h, `desk[${id}].h`, 1, 1);
      if (x + w > width || y + h > height) {
        invalid(`desk "${id}" (${x},${y},${w}x${h}) is outside the map`);
      }
      if (!raw.seat || typeof raw.seat !== 'object' || Array.isArray(raw.seat)) {
        invalid(`desk "${id}" needs a seat tile`);
      }
      const sx = integer(raw.seat.x, `desk[${id}].seat.x`, 0);
      const sy = integer(raw.seat.y, `desk[${id}].seat.y`, 0);
      if (sx >= width || sy >= height) invalid(`desk "${id}" seat is outside the map`);
      if (wall[indexOf(sx, sy)]) invalid(`desk "${id}" seat sits on a wall`);
      if (door[indexOf(sx, sy)]) invalid(`desk "${id}" seat sits on a door`);
      if (sx >= x && sx < x + w && sy >= y && sy < y + h) {
        invalid(`desk "${id}" seat overlaps the desk footprint`);
      }
      for (let ty = y; ty < y + h; ty += 1) {
        for (let tx = x; tx < x + w; tx += 1) {
          const index = indexOf(tx, ty);
          if (wall[index] || door[index]) {
            invalid(`desk "${id}" sits on a wall or door at ${tx},${ty}`);
          }
          if (occupiedDeskTiles[index]) invalid(`desk "${id}" overlaps another desk at ${tx},${ty}`);
          occupiedDeskTiles[index] = 1;
          blocked[index] = 1;
          walkable[index] = 0;
        }
      }
      const seatIndex = indexOf(sx, sy);
      if (seatTiles[seatIndex]) invalid(`desk "${id}" reuses a seat tile`);
      seatTiles[seatIndex] = 1;
      walkable[seatIndex] = 1;
      desks.push({
        id,
        room: text(raw.room),
        department: text(raw.department) || text(raw.room),
        x,
        y,
        w,
        h,
        seat: { x: sx, y: sy },
        facing: ['up', 'down', 'left', 'right'].includes(raw.facing) ? raw.facing : null,
      });
    });

    /* ── decor (visual only) ───────────────────────────────────────────── */
    const decor = [];
    const rawDecor = layout.decor == null ? [] : layout.decor;
    if (!Array.isArray(rawDecor)) invalid('decor must be an array');
    rawDecor.forEach((raw, position) => {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
        invalid(`decor[${position}] must be an object`);
      }
      const x = integer(raw.x, `decor[${position}].x`, 0);
      const y = integer(raw.y, `decor[${position}].y`, 0);
      const w = optionalInteger(raw.w, `decor[${position}].w`, 1, 1);
      const h = optionalInteger(raw.h, `decor[${position}].h`, 1, 1);
      if (x + w > width || y + h > height) {
        invalid(`decor[${position}] (${x},${y},${w}x${h}) is outside the map`);
      }
      decor.push({
        id: text(raw.id) || `decor-${position}`,
        type: text(raw.type) || 'rug',
        x,
        y,
        w,
        h,
      });
    });

    /* ── doors must connect to usable floor on both sides ──────────────── */
    for (const entry of doors) {
      const outside = indexOf(entry.outside.x, entry.outside.y);
      const inside = indexOf(entry.inside.x, entry.inside.y);
      if (wall[outside]) {
        invalid(`room "${entry.roomId}" door ${entry.x},${entry.y} opens into a wall`);
      }
      if (!walkable[outside] || blocked[outside]) {
        invalid(`room "${entry.roomId}" door ${entry.x},${entry.y} is blocked from the corridor`);
      }
      if (!walkable[inside] || blocked[inside]) {
        invalid(`room "${entry.roomId}" door ${entry.x},${entry.y} is blocked from the room`);
      }
      if (door[outside]) {
        invalid(`room "${entry.roomId}" door ${entry.x},${entry.y} touches another door`);
      }
    }

    /* ── spawn ─────────────────────────────────────────────────────────── */
    let spawn = null;
    if (layout.spawn != null) {
      if (!layout.spawn || typeof layout.spawn !== 'object' || Array.isArray(layout.spawn)) {
        invalid('spawn must be a tile object');
      }
      const x = integer(layout.spawn.x, 'spawn.x', 0);
      const y = integer(layout.spawn.y, 'spawn.y', 0);
      if (!inBounds(x, y)) invalid('spawn is outside the map');
      if (!walkable[indexOf(x, y)]) {
        invalid('spawn must be on walkable floor');
      }
      spawn = { x, y };
    }

    /* ── public surface ────────────────────────────────────────────────── */
    const roomLookup = (tx, ty) => {
      if (!inBounds(tx, ty)) return null;
      const room = rooms[roomIndex[indexOf(tx, ty)]];
      return room || null;
    };

    const api = {
      width,
      height,
      tile,
      walkable,
      wall,
      door,
      blocked,
      roomIndex,
      cellKind,
      rooms,
      doors,
      desks,
      furniture,
      decor,
      spawn,
      size,
      indexOf,
      inBounds,
      isWalkable(tx, ty) {
        return inBounds(tx, ty) && walkable[indexOf(tx, ty)] === 1;
      },
      isBlocked(tx, ty) {
        return inBounds(tx, ty) && blocked[indexOf(tx, ty)] === 1;
      },
      roomAt(tx, ty) {
        const room = roomLookup(tx, ty);
        return room ? room.id : null;
      },
      roomInfo(tx, ty) {
        return roomLookup(tx, ty);
      },
      roomById(id) {
        return roomById.get(text(id)) || null;
      },
      wallAt(tx, ty) {
        return inBounds(tx, ty) && wall[indexOf(tx, ty)] === 1;
      },
      doorAt(tx, ty) {
        return inBounds(tx, ty) && door[indexOf(tx, ty)] === 1;
      },
      doorInfo(tx, ty) {
        if (!inBounds(tx, ty) || !door[indexOf(tx, ty)]) return null;
        return doors.find((item) => item.x === tx && item.y === ty) || null;
      },
      seatAt(tx, ty) {
        return inBounds(tx, ty) && seatTiles[indexOf(tx, ty)] === 1;
      },
      deskAt(tx, ty) {
        if (!inBounds(tx, ty)) return null;
        return desks.find((desk) => tx >= desk.x && tx < desk.x + desk.w
          && ty >= desk.y && ty < desk.y + desk.h) || null;
      },
    };

    return api;
  }

  function worldToTile(x, y, tile = DEFAULT_TILE) {
    const size = Number(tile) > 0 ? Number(tile) : DEFAULT_TILE;
    if (!Number.isFinite(Number(x)) || !Number.isFinite(Number(y))) {
      return { x: 0, y: 0 };
    }
    return { x: Math.floor(Number(x) / size), y: Math.floor(Number(y) / size) };
  }

  function tileToWorld(tx, ty, tile = DEFAULT_TILE) {
    const size = Number(tile) > 0 ? Number(tile) : DEFAULT_TILE;
    return { x: Number(tx) * size, y: Number(ty) * size };
  }

  function worldCenter(tx, ty, tile = DEFAULT_TILE) {
    const size = Number(tile) > 0 ? Number(tile) : DEFAULT_TILE;
    return { x: (Number(tx) + 0.5) * size, y: (Number(ty) + 0.5) * size };
  }

  const api = {
    build,
    worldToTile,
    tileToWorld,
    worldCenter,
    DEFAULT_TILE,
  };

  if (typeof window !== 'undefined') window.AOTilemap = api;
  if (typeof globalThis !== 'undefined') globalThis.AOTilemap = api;
})();
