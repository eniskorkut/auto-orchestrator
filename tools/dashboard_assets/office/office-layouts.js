/* Auto-Orchestrator — virtual office layout schema, themes and local overrides.
 *
 * Plain browser script (no bundler, no external assets). The layout is pure data:
 * rooms are walled floor rectangles, a door sits on exactly one wall tile and
 * opens onto a corridor. Desks are 1x1 blocking tiles with one walkable seat.
 *
 * Seat/facing convention: `seat` is the tile the agent occupies and `facing` is
 * the direction the agent looks (towards the desk). So facing "up" means the
 * desk is one tile above the seat.
 *
 * Schemas:
 *   version 1, width/height in tiles, tile in CSS pixels.
 *   room   {id,name,department,x,y,w,h,door:{x,y}}
 *   desk   {id,room,department,x,y,seat:{x,y},facing}
 *   furn   {id,type,x,y,w,h,blocked}
 *   decor  {type,x,y,w,h}                    (never blocks walking)
 *
 * Storage key: "ao-office-layout:<project_id>". A stored layout is only used
 * when validate() accepts it; storage failures never break rendering.
 */
(function (root) {
  'use strict';

  var VERSION = 1;
  var THEMES = ['ai', 'security', 'web', 'custom'];
  var ROOM_IDS = ['frontend', 'backend', 'qa', 'review', 'research', 'meeting', 'lobby'];
  var FACINGS = ['up', 'down', 'left', 'right'];

  /* ------------------------------------------------------------------ */
  /* Small utilities                                                     */
  /* ------------------------------------------------------------------ */

  function isInt(value) {
    return typeof value === 'number' && isFinite(value) && Math.floor(value) === value;
  }

  function isObject(value) {
    return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
  }

  function text(value) {
    return typeof value === 'string' && value.trim().length > 0;
  }

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }

  function serialize(layout) {
    return JSON.stringify(layout);
  }

  function normalizeTheme(value) {
    return THEMES.indexOf(value) >= 0 ? value : 'ai';
  }

  function storageKey(projectId) {
    return 'ao-office-layout:' + String(projectId == null || projectId === '' ? 'default' : projectId);
  }

  function seatFor(x, y, facing) {
    if (facing === 'up') return { x: x, y: y + 1 };
    if (facing === 'down') return { x: x, y: y - 1 };
    if (facing === 'left') return { x: x + 1, y: y };
    if (facing === 'right') return { x: x - 1, y: y };
    return null;
  }

  function rectHas(rect, x, y) {
    return x >= rect.x && x < rect.x + rect.w && y >= rect.y && y < rect.y + rect.h;
  }

  function rectsOverlap(a, b) {
    return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  }

  /* Two rooms need at least one corridor tile between them so the derived
   * wall rings never eat another room's floor. */
  function roomsSeparated(a, b) {
    return a.x + a.w + 1 <= b.x ||
      b.x + b.w + 1 <= a.x ||
      a.y + a.h + 1 <= b.y ||
      b.y + b.h + 1 <= a.y;
  }

  function onRoomBorder(room, x, y) {
    if (!rectHas(room, x, y)) return false;
    return x === room.x || x === room.x + room.w - 1 ||
      y === room.y || y === room.y + room.h - 1;
  }

  function inRoomInterior(room, x, y) {
    return x > room.x && x < room.x + room.w - 1 &&
      y > room.y && y < room.y + room.h - 1;
  }

  function doorDirection(room, door) {
    if (door.x === room.x) return { x: -1, y: 0 };
    if (door.x === room.x + room.w - 1) return { x: 1, y: 0 };
    if (door.y === room.y) return { x: 0, y: -1 };
    if (door.y === room.y + room.h - 1) return { x: 0, y: 1 };
    return null;
  }

  /* ------------------------------------------------------------------ */
  /* Layout construction                                                 */
  /* ------------------------------------------------------------------ */

  function makeLayout(def) {
    var rooms = def.rooms.map(function (room) {
      return {
        id: room.id,
        name: room.name,
        department: room.department,
        x: room.x,
        y: room.y,
        w: room.w,
        h: room.h,
        door: { x: room.door.x, y: room.door.y }
      };
    });
    var byId = {};
    rooms.forEach(function (room) { byId[room.id] = room; });

    var desks = [];
    (def.desks || []).forEach(function (group) {
      var room = byId[group.room];
      group.tiles.forEach(function (tile, index) {
        var facing = tile[2] || 'up';
        desks.push({
          id: group.room + '-' + (index + 1),
          room: group.room,
          department: room ? room.department : group.room,
          x: tile[0],
          y: tile[1],
          seat: seatFor(tile[0], tile[1], facing),
          facing: facing
        });
      });
    });

    var furnitureSeq = {};
    var furniture = (def.furniture || []).map(function (item) {
      furnitureSeq[item.type] = (furnitureSeq[item.type] || 0) + 1;
      return {
        id: item.type + '-' + furnitureSeq[item.type],
        type: item.type,
        x: item.x,
        y: item.y,
        w: item.w == null ? 1 : item.w,
        h: item.h == null ? 1 : item.h,
        blocked: item.blocked !== false
      };
    });

    return {
      version: VERSION,
      id: def.id,
      theme: def.theme,
      width: def.width,
      height: def.height,
      tile: def.tile == null ? 16 : def.tile,
      spawn: { x: def.spawn.x, y: def.spawn.y },
      rooms: rooms,
      furniture: furniture,
      desks: desks,
      decor: (def.decor || []).map(function (item) {
        return {
          type: item.type,
          x: item.x,
          y: item.y,
          w: item.w == null ? 1 : item.w,
          h: item.h == null ? 1 : item.h
        };
      })
    };
  }

  function buildAi() {
    return makeLayout({
      id: 'ai-lab',
      theme: 'ai',
      width: 44,
      height: 28,
      spawn: { x: 30, y: 24 },
      rooms: [
        { id: 'research', name: 'Research Lab', department: 'research', x: 2, y: 2, w: 18, h: 8, door: { x: 10, y: 9 } },
        { id: 'backend', name: 'Model Platform', department: 'backend', x: 25, y: 2, w: 17, h: 8, door: { x: 33, y: 9 } },
        { id: 'frontend', name: 'Interface Lab', department: 'frontend', x: 2, y: 12, w: 15, h: 8, door: { x: 8, y: 12 } },
        { id: 'meeting', name: 'Seminar Room', department: 'meeting', x: 19, y: 12, w: 10, h: 8, door: { x: 24, y: 19 } },
        { id: 'qa', name: 'Evaluation Bay', department: 'qa', x: 31, y: 12, w: 11, h: 8, door: { x: 31, y: 16 } },
        { id: 'review', name: 'Paper Review', department: 'review', x: 2, y: 21, w: 17, h: 6, door: { x: 18, y: 24 } },
        { id: 'lobby', name: 'Atrium', department: 'lobby', x: 22, y: 21, w: 20, h: 6, door: { x: 30, y: 21 } }
      ],
      desks: [
        { room: 'research', tiles: [[5, 4, 'up'], [8, 4, 'up'], [11, 4, 'up'], [14, 4, 'up'], [5, 7, 'down'], [8, 7, 'down'], [11, 7, 'down'], [14, 7, 'down']] },
        { room: 'backend', tiles: [[28, 4, 'up'], [31, 4, 'up'], [34, 4, 'up'], [37, 4, 'up'], [28, 7, 'down'], [31, 7, 'down'], [34, 7, 'down'], [37, 7, 'down']] },
        { room: 'frontend', tiles: [[4, 14, 'up'], [7, 14, 'up'], [10, 14, 'up'], [13, 14, 'up'], [4, 17, 'down'], [7, 17, 'down'], [10, 17, 'down'], [13, 17, 'down']] },
        { room: 'qa', tiles: [[33, 14, 'up'], [36, 14, 'up'], [39, 14, 'up'], [33, 17, 'down'], [36, 17, 'down'], [39, 17, 'down']] },
        { room: 'review', tiles: [[5, 23, 'up'], [8, 23, 'up'], [11, 23, 'up'], [14, 23, 'up']] }
      ],
      furniture: [
        { type: 'shelf', x: 3, y: 3, w: 1, h: 2 },
        { type: 'plant', x: 17, y: 3 },
        { type: 'whiteboard', x: 3, y: 8, w: 3, h: 1 },
        { type: 'cabinet', x: 17, y: 8 },
        { type: 'cabinet', x: 26, y: 3, w: 1, h: 2 },
        { type: 'whiteboard', x: 29, y: 3, w: 4, h: 1 },
        { type: 'cabinet', x: 40, y: 3, w: 1, h: 2 },
        { type: 'cabinet', x: 26, y: 8, w: 2, h: 1 },
        { type: 'plant', x: 40, y: 8 },
        { type: 'plant', x: 3, y: 13 },
        { type: 'shelf', x: 15, y: 13, w: 1, h: 2 },
        { type: 'whiteboard', x: 3, y: 18, w: 3, h: 1 },
        { type: 'sofa', x: 14, y: 18, w: 2, h: 1 },
        { type: 'whiteboard', x: 21, y: 13, w: 5, h: 1 },
        { type: 'plant', x: 20, y: 13 },
        { type: 'plant', x: 27, y: 13 },
        { type: 'table', x: 21, y: 15, w: 6, h: 2 },
        { type: 'chair', x: 20, y: 15 },
        { type: 'chair', x: 27, y: 15 },
        { type: 'chair', x: 20, y: 16 },
        { type: 'chair', x: 27, y: 16 },
        { type: 'sofa', x: 26, y: 18, w: 2, h: 1 },
        { type: 'shelf', x: 32, y: 13, w: 1, h: 2 },
        { type: 'whiteboard', x: 35, y: 13, w: 4, h: 1 },
        { type: 'plant', x: 40, y: 13 },
        { type: 'cabinet', x: 34, y: 18, w: 2, h: 1 },
        { type: 'whiteboard', x: 4, y: 22, w: 4, h: 1 },
        { type: 'plant', x: 3, y: 22 },
        { type: 'shelf', x: 17, y: 22, w: 1, h: 2 },
        { type: 'plant', x: 3, y: 25 },
        { type: 'sofa', x: 15, y: 25, w: 3, h: 1 },
        { type: 'sofa', x: 24, y: 24, w: 3, h: 1 },
        { type: 'sofa', x: 35, y: 24, w: 3, h: 1 },
        { type: 'table', x: 28, y: 23 },
        { type: 'plant', x: 23, y: 22 },
        { type: 'plant', x: 40, y: 22 },
        { type: 'plant', x: 23, y: 25 },
        { type: 'plant', x: 40, y: 25 },
        { type: 'shelf', x: 38, y: 22, w: 2, h: 1 }
      ],
      decor: [
        { type: 'rug', x: 27, y: 22, w: 8, h: 2 },
        { type: 'rug', x: 20, y: 17, w: 8, h: 1 },
        { type: 'rug', x: 5, y: 25, w: 8, h: 1 }
      ]
    });
  }

  function buildSecurity() {
    return makeLayout({
      id: 'security-soc',
      theme: 'security',
      width: 44,
      height: 28,
      spawn: { x: 21, y: 23 },
      rooms: [
        { id: 'meeting', name: 'War Room', department: 'meeting', x: 2, y: 2, w: 12, h: 7, door: { x: 13, y: 5 } },
        { id: 'research', name: 'Threat Research', department: 'research', x: 16, y: 2, w: 14, h: 7, door: { x: 22, y: 8 } },
        { id: 'backend', name: 'Telemetry Core', department: 'backend', x: 32, y: 2, w: 10, h: 7, door: { x: 36, y: 8 } },
        { id: 'qa', name: 'Red Team Range', department: 'qa', x: 2, y: 11, w: 16, h: 7, door: { x: 6, y: 11 } },
        { id: 'review', name: 'SOC Review', department: 'review', x: 21, y: 11, w: 12, h: 7, door: { x: 26, y: 11 } },
        { id: 'frontend', name: 'Console Studio', department: 'frontend', x: 35, y: 11, w: 7, h: 7, door: { x: 35, y: 14 } },
        { id: 'lobby', name: 'Ops Floor', department: 'lobby', x: 2, y: 20, w: 40, h: 6, door: { x: 21, y: 20 } }
      ],
      desks: [
        { room: 'research', tiles: [[18, 4, 'up'], [21, 4, 'up'], [24, 4, 'up'], [27, 4, 'up'], [18, 7, 'down'], [21, 7, 'down'], [24, 7, 'down'], [27, 7, 'down']] },
        { room: 'backend', tiles: [[34, 4, 'up'], [37, 4, 'up'], [34, 7, 'down'], [37, 7, 'down']] },
        { room: 'qa', tiles: [[4, 13, 'up'], [7, 13, 'up'], [10, 13, 'up'], [13, 13, 'up'], [4, 16, 'down'], [7, 16, 'down'], [10, 16, 'down'], [13, 16, 'down']] },
        { room: 'review', tiles: [[23, 13, 'up'], [26, 13, 'up'], [29, 13, 'up'], [23, 16, 'down'], [26, 16, 'down'], [29, 16, 'down']] },
        { room: 'frontend', tiles: [[37, 13, 'up'], [39, 13, 'up'], [37, 16, 'down'], [39, 16, 'down']] }
      ],
      furniture: [
        { type: 'table', x: 5, y: 5, w: 5, h: 2 },
        { type: 'chair', x: 4, y: 5 },
        { type: 'chair', x: 10, y: 5 },
        { type: 'chair', x: 4, y: 6 },
        { type: 'chair', x: 10, y: 6 },
        { type: 'whiteboard', x: 5, y: 3, w: 4, h: 1 },
        { type: 'sofa', x: 10, y: 7, w: 2, h: 1 },
        { type: 'plant', x: 3, y: 7 },
        { type: 'shelf', x: 17, y: 3, w: 1, h: 2 },
        { type: 'whiteboard', x: 18, y: 3, w: 4, h: 1 },
        { type: 'cabinet', x: 28, y: 3, w: 1, h: 2 },
        { type: 'plant', x: 28, y: 7 },
        { type: 'cabinet', x: 33, y: 3, w: 1, h: 2 },
        { type: 'cabinet', x: 40, y: 3, w: 1, h: 2 },
        { type: 'shelf', x: 40, y: 6, w: 1, h: 2 },
        { type: 'plant', x: 39, y: 7 },
        { type: 'plant', x: 36, y: 12 },
        { type: 'shelf', x: 40, y: 13, w: 1, h: 2 },
        { type: 'plant', x: 40, y: 16 },
        { type: 'shelf', x: 3, y: 12, w: 1, h: 3 },
        { type: 'cabinet', x: 8, y: 12, w: 2, h: 1 },
        { type: 'table', x: 11, y: 12, w: 3, h: 1 },
        { type: 'plant', x: 16, y: 12 },
        { type: 'plant', x: 16, y: 16 },
        { type: 'whiteboard', x: 28, y: 12, w: 4, h: 1 },
        { type: 'plant', x: 22, y: 16 },
        { type: 'sofa', x: 30, y: 16, w: 2, h: 1 },
        { type: 'sofa', x: 4, y: 23, w: 4, h: 1 },
        { type: 'table', x: 9, y: 23, w: 2, h: 1 },
        { type: 'sofa', x: 34, y: 23, w: 4, h: 1 },
        { type: 'shelf', x: 28, y: 21, w: 3, h: 1 },
        { type: 'table', x: 24, y: 24, w: 2, h: 1 },
        { type: 'plant', x: 3, y: 21 },
        { type: 'plant', x: 40, y: 21 },
        { type: 'plant', x: 3, y: 24 },
        { type: 'plant', x: 40, y: 24 }
      ],
      decor: [
        { type: 'rug', x: 15, y: 22, w: 10, h: 2 },
        { type: 'rug', x: 5, y: 4, w: 6, h: 4 }
      ]
    });
  }

  function buildWeb() {
    return makeLayout({
      id: 'web-studio',
      theme: 'web',
      width: 44,
      height: 28,
      spawn: { x: 30, y: 24 },
      rooms: [
        { id: 'frontend', name: 'Design Studio', department: 'frontend', x: 2, y: 2, w: 18, h: 8, door: { x: 6, y: 9 } },
        { id: 'backend', name: 'API Workshop', department: 'backend', x: 24, y: 2, w: 18, h: 8, door: { x: 34, y: 9 } },
        { id: 'review', name: 'Crit Room', department: 'review', x: 2, y: 12, w: 10, h: 7, door: { x: 11, y: 15 } },
        { id: 'research', name: 'User Research', department: 'research', x: 15, y: 12, w: 11, h: 7, door: { x: 25, y: 15 } },
        { id: 'qa', name: 'QA Studio', department: 'qa', x: 29, y: 12, w: 13, h: 7, door: { x: 29, y: 15 } },
        { id: 'meeting', name: 'Project Room', department: 'meeting', x: 2, y: 21, w: 14, h: 6, door: { x: 6, y: 21 } },
        { id: 'lobby', name: 'Lounge', department: 'lobby', x: 19, y: 21, w: 23, h: 6, door: { x: 30, y: 21 } }
      ],
      desks: [
        { room: 'frontend', tiles: [[4, 4, 'up'], [7, 4, 'up'], [10, 4, 'up'], [13, 4, 'up'], [16, 4, 'up'], [4, 8, 'down'], [7, 8, 'down'], [10, 8, 'down']] },
        { room: 'backend', tiles: [[26, 4, 'up'], [29, 4, 'up'], [32, 4, 'up'], [35, 4, 'up'], [38, 4, 'up'], [26, 8, 'down'], [29, 8, 'down'], [32, 8, 'down']] },
        { room: 'review', tiles: [[4, 14, 'up'], [7, 14, 'up'], [9, 14, 'up'], [4, 17, 'down'], [7, 17, 'down']] },
        { room: 'research', tiles: [[17, 14, 'up'], [20, 14, 'up'], [23, 14, 'up'], [17, 17, 'down'], [20, 17, 'down']] },
        { room: 'qa', tiles: [[31, 14, 'up'], [34, 14, 'up'], [37, 14, 'up'], [40, 14, 'up'], [31, 17, 'down'], [34, 17, 'down'], [37, 17, 'down']] }
      ],
      furniture: [
        { type: 'whiteboard', x: 13, y: 3, w: 4, h: 1 },
        { type: 'shelf', x: 18, y: 3, w: 1, h: 3 },
        { type: 'plant', x: 3, y: 3 },
        { type: 'cabinet', x: 13, y: 8, w: 2, h: 1 },
        { type: 'plant', x: 18, y: 8 },
        { type: 'cabinet', x: 25, y: 3, w: 1, h: 2 },
        { type: 'whiteboard', x: 29, y: 3, w: 4, h: 1 },
        { type: 'cabinet', x: 40, y: 3, w: 1, h: 2 },
        { type: 'plant', x: 25, y: 8 },
        { type: 'shelf', x: 40, y: 7, w: 1, h: 2 },
        { type: 'whiteboard', x: 4, y: 13, w: 4, h: 1 },
        { type: 'sofa', x: 9, y: 17, w: 2, h: 1 },
        { type: 'plant', x: 3, y: 17 },
        { type: 'shelf', x: 16, y: 13, w: 1, h: 3 },
        { type: 'whiteboard', x: 19, y: 13, w: 3, h: 1 },
        { type: 'plant', x: 24, y: 17 },
        { type: 'shelf', x: 30, y: 13, w: 1, h: 2 },
        { type: 'plant', x: 40, y: 13 },
        { type: 'plant', x: 30, y: 17 },
        { type: 'cabinet', x: 39, y: 17, w: 2, h: 1 },
        { type: 'table', x: 5, y: 23, w: 5, h: 2 },
        { type: 'chair', x: 4, y: 23 },
        { type: 'chair', x: 10, y: 23 },
        { type: 'chair', x: 4, y: 24 },
        { type: 'chair', x: 10, y: 24 },
        { type: 'whiteboard', x: 10, y: 22, w: 4, h: 1 },
        { type: 'plant', x: 3, y: 25 },
        { type: 'sofa', x: 12, y: 25, w: 2, h: 1 },
        { type: 'sofa', x: 21, y: 25, w: 4, h: 1 },
        { type: 'table', x: 26, y: 25, w: 2, h: 1 },
        { type: 'sofa', x: 36, y: 25, w: 3, h: 1 },
        { type: 'shelf', x: 38, y: 22, w: 3, h: 1 },
        { type: 'plant', x: 20, y: 22 },
        { type: 'plant', x: 40, y: 25 },
        { type: 'plant', x: 20, y: 25 }
      ],
      decor: [
        { type: 'rug', x: 27, y: 22, w: 8, h: 2 },
        { type: 'rug', x: 5, y: 25, w: 6, h: 1 }
      ]
    });
  }

  function buildCustom() {
    return makeLayout({
      id: 'custom-loft',
      theme: 'custom',
      width: 42,
      height: 26,
      spawn: { x: 14, y: 21 },
      rooms: [
        { id: 'research', name: 'Quiet Lab', department: 'research', x: 2, y: 2, w: 12, h: 7, door: { x: 13, y: 6 } },
        { id: 'frontend', name: 'Open Studio', department: 'frontend', x: 16, y: 2, w: 12, h: 7, door: { x: 17, y: 8 } },
        { id: 'backend', name: 'Systems Nook', department: 'backend', x: 30, y: 2, w: 10, h: 7, door: { x: 34, y: 8 } },
        { id: 'meeting', name: 'Round Table', department: 'meeting', x: 2, y: 11, w: 10, h: 6, door: { x: 11, y: 13 } },
        { id: 'qa', name: 'Test Corner', department: 'qa', x: 14, y: 11, w: 12, h: 6, door: { x: 14, y: 13 } },
        { id: 'review', name: 'Reading Room', department: 'review', x: 28, y: 11, w: 12, h: 9, door: { x: 28, y: 15 } },
        { id: 'lobby', name: 'Entry Loft', department: 'lobby', x: 2, y: 19, w: 24, h: 5, door: { x: 25, y: 21 } }
      ],
      desks: [
        { room: 'research', tiles: [[4, 4, 'up'], [7, 4, 'up'], [10, 4, 'up'], [4, 7, 'down']] },
        { room: 'frontend', tiles: [[18, 4, 'up'], [21, 4, 'up'], [24, 4, 'up'], [18, 7, 'down'], [21, 7, 'down'], [24, 7, 'down']] },
        { room: 'backend', tiles: [[32, 4, 'up'], [35, 4, 'up'], [32, 7, 'down'], [35, 7, 'down']] },
        { room: 'qa', tiles: [[16, 13, 'up'], [19, 13, 'up'], [22, 13, 'up'], [17, 15, 'down'], [20, 15, 'down']] },
        { room: 'review', tiles: [[30, 13, 'up'], [33, 13, 'up'], [36, 13, 'up'], [30, 17, 'down'], [33, 17, 'down']] }
      ],
      furniture: [
        { type: 'whiteboard', x: 4, y: 3, w: 3, h: 1 },
        { type: 'shelf', x: 12, y: 3, w: 1, h: 3 },
        { type: 'plant', x: 8, y: 7 },
        { type: 'whiteboard', x: 19, y: 3, w: 4, h: 1 },
        { type: 'plant', x: 26, y: 3 },
        { type: 'shelf', x: 26, y: 5, w: 1, h: 2 },
        { type: 'cabinet', x: 25, y: 7, w: 2, h: 1 },
        { type: 'shelf', x: 31, y: 3, w: 1, h: 2 },
        { type: 'cabinet', x: 38, y: 3, w: 1, h: 2 },
        { type: 'plant', x: 31, y: 7 },
        { type: 'table', x: 4, y: 13, w: 4, h: 2 },
        { type: 'chair', x: 3, y: 13 },
        { type: 'chair', x: 8, y: 13 },
        { type: 'chair', x: 3, y: 14 },
        { type: 'chair', x: 8, y: 14 },
        { type: 'whiteboard', x: 5, y: 12, w: 3, h: 1 },
        { type: 'plant', x: 10, y: 12 },
        { type: 'sofa', x: 9, y: 15, w: 2, h: 1 },
        { type: 'shelf', x: 15, y: 14, w: 1, h: 2 },
        { type: 'plant', x: 24, y: 12 },
        { type: 'cabinet', x: 21, y: 15, w: 2, h: 1 },
        { type: 'whiteboard', x: 29, y: 12, w: 5, h: 1 },
        { type: 'plant', x: 38, y: 12 },
        { type: 'sofa', x: 30, y: 18, w: 3, h: 1 },
        { type: 'shelf', x: 38, y: 17, w: 1, h: 2 },
        { type: 'shelf', x: 3, y: 20, w: 2, h: 1 },
        { type: 'sofa', x: 4, y: 22, w: 4, h: 1 },
        { type: 'table', x: 9, y: 22, w: 2, h: 1 },
        { type: 'sofa', x: 19, y: 22, w: 3, h: 1 },
        { type: 'plant', x: 12, y: 20 },
        { type: 'plant', x: 16, y: 20 },
        { type: 'plant', x: 3, y: 22 }
      ],
      decor: [
        { type: 'rug', x: 11, y: 20, w: 6, h: 2 }
      ]
    });
  }

  var LAYOUTS = {
    ai: buildAi(),
    security: buildSecurity(),
    web: buildWeb(),
    custom: buildCustom()
  };

  /* Corridor tiles used for the deterministic per-project plant variation.
   * They are deliberately away from doors and room walls. */
  var VARIANT_ANCHORS = {
    ai: [{ x: 21, y: 3 }, { x: 17, y: 13 }, { x: 20, y: 23 }],
    security: [{ x: 14, y: 3 }, { x: 19, y: 12 }, { x: 33, y: 5 }],
    web: [{ x: 20, y: 3 }, { x: 12, y: 14 }, { x: 17, y: 22 }],
    custom: [{ x: 14, y: 5 }, { x: 26, y: 13 }, { x: 26, y: 22 }]
  };

  function hashSeed(value) {
    var hash = 2166136261;
    var str = String(value);
    for (var i = 0; i < str.length; i += 1) {
      hash ^= str.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  }

  function mulberry32(seed) {
    var state = seed >>> 0;
    return function next() {
      state = (state + 0x6D2B79F5) >>> 0;
      var t = state;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /* ------------------------------------------------------------------ */
  /* Validation                                                          */
  /* ------------------------------------------------------------------ */

  function validate(layout) {
    var errors = [];
    function add(path, message) {
      errors.push(path + ': ' + message);
    }

    if (!isObject(layout)) {
      return { ok: false, errors: ['layout: must be an object'] };
    }
    if (layout.version !== VERSION) add('version', 'must be ' + VERSION);
    if (!text(layout.id)) add('id', 'must be a non-empty string');
    if (THEMES.indexOf(layout.theme) < 0) add('theme', 'must be one of ' + THEMES.join(', '));
    if (!isInt(layout.width) || layout.width < 12 || layout.width > 200) add('width', 'must be an integer between 12 and 200');
    if (!isInt(layout.height) || layout.height < 12 || layout.height > 200) add('height', 'must be an integer between 12 and 200');
    if (!isInt(layout.tile) || layout.tile < 4 || layout.tile > 64) add('tile', 'must be an integer between 4 and 64');

    var width = layout.width;
    var height = layout.height;
    var widthOk = isInt(width) && width > 0;
    var heightOk = isInt(height) && height > 0;

    if (!Array.isArray(layout.rooms) || layout.rooms.length === 0) {
      add('rooms', 'must be a non-empty array');
      return { ok: false, errors: errors };
    }
    if (layout.desks != null && !Array.isArray(layout.desks)) {
      add('desks', 'must be an array when present');
      layout = clone(layout);
      layout.desks = [];
    } else if (layout.desks == null) {
      layout = clone(layout);
      layout.desks = [];
    }
    if (layout.furniture != null && !Array.isArray(layout.furniture)) {
      add('furniture', 'must be an array when present');
      layout = clone(layout);
      layout.furniture = [];
    } else if (layout.furniture == null) {
      layout = clone(layout);
      layout.furniture = [];
    }
    if (layout.decor != null && !Array.isArray(layout.decor)) {
      add('decor', 'must be an array when present');
    }

    var rooms = layout.rooms;
    var roomById = Object.create(null);
    var roomIds = Object.create(null);

    rooms.forEach(function (room, index) {
      var path = 'rooms[' + index + ']';
      if (!isObject(room)) {
        add(path, 'must be an object');
        return;
      }
      if (!text(room.id)) add(path + '.id', 'must be a non-empty string');
      else {
        if (roomIds[room.id]) add(path + '.id', 'duplicate room id "' + room.id + '"');
        roomIds[room.id] = true;
        roomById[room.id] = room;
      }
      if (!text(room.name)) add(path + '.name', 'must be a non-empty string');
      if (!text(room.department)) add(path + '.department', 'must be a non-empty string');
      if (!isInt(room.x) || !isInt(room.y) || !isInt(room.w) || !isInt(room.h)) {
        add(path, 'x, y, w and h must be integers');
        return;
      }
      if (room.w < 4 || room.h < 4) add(path, 'must be at least 4x4 tiles');
      if (room.x < 0 || room.y < 0 || (widthOk && room.x + room.w > width) || (heightOk && room.y + room.h > height)) {
        add(path, 'is outside the ' + layout.width + 'x' + layout.height + ' floor');
      }
      if (!isObject(room.door) || !isInt(room.door.x) || !isInt(room.door.y)) {
        add(path + '.door', 'must have integer x and y');
      }
    });

    ROOM_IDS.forEach(function (id) {
      if (!roomIds[id]) add('rooms', 'missing required room "' + id + '"');
    });

    rooms.forEach(function (room, index) {
      var path = 'rooms[' + index + ']';
      if (!isObject(room) || !isInt(room.x) || !isInt(room.y) || !isInt(room.w) || !isInt(room.h)) return;
      rooms.forEach(function (other, otherIndex) {
        if (otherIndex <= index || !isObject(other) || !isInt(other.x) || !isInt(other.y) || !isInt(other.w) || !isInt(other.h)) return;
        if (!roomsSeparated(room, other)) {
          add(path, 'overlaps or shares a wall with rooms[' + otherIndex + '] ("' + other.id + '") without a corridor');
        }
      });
    });

    var doorTiles = Object.create(null);
    rooms.forEach(function (room, index) {
      var path = 'rooms[' + index + ']';
      if (!isObject(room) || !isObject(room.door) || !isInt(room.door.x) || !isInt(room.door.y)) return;
      if (!isInt(room.x) || !isInt(room.y) || !isInt(room.w) || !isInt(room.h)) return;
      if (!rectHas(room, room.door.x, room.door.y)) {
        add(path + '.door', 'is not inside the room rectangle');
        return;
      }
      var onX = room.door.x === room.x || room.door.x === room.x + room.w - 1;
      var onY = room.door.y === room.y || room.door.y === room.y + room.h - 1;
      if (onX === onY) {
        add(path + '.door', onX ? 'is on a corner' : 'is not on a wall');
        return;
      }
      var doorKey = room.door.x + ',' + room.door.y;
      if (doorTiles[doorKey]) add(path + '.door', 'duplicate door tile ' + doorKey);
      doorTiles[doorKey] = true;
      var direction = doorDirection(room, room.door);
      var outside = { x: room.door.x + direction.x, y: room.door.y + direction.y };
      if (outside.x < 0 || outside.y < 0 || (widthOk && outside.x >= width) || (heightOk && outside.y >= height)) {
        add(path + '.door', 'opens outside the floor');
      } else {
        var insideAnotherRoom = rooms.some(function (other) {
          return other !== room && isObject(other) && rectHas(other, outside.x, outside.y);
        });
        if (insideAnotherRoom) add(path + '.door', 'does not open onto a corridor');
      }
    });

    var blockedFurniture = [];
    var furnitureIds = Object.create(null);
    (layout.furniture || []).forEach(function (item, index) {
      var path = 'furniture[' + index + ']';
      if (!isObject(item)) {
        add(path, 'must be an object');
        return;
      }
      if (!text(item.id)) add(path + '.id', 'must be a non-empty string');
      else if (furnitureIds[item.id]) add(path + '.id', 'duplicate furniture id "' + item.id + '"');
      else furnitureIds[item.id] = true;
      if (!text(item.type)) add(path + '.type', 'must be a non-empty string');
      if (!isInt(item.x) || !isInt(item.y) || !isInt(item.w) || !isInt(item.h) || item.w < 1 || item.h < 1) {
        add(path, 'x, y, w and h must be positive integers');
        return;
      }
      if (item.x < 0 || item.y < 0 || (widthOk && item.x + item.w > width) || (heightOk && item.y + item.h > height)) {
        add(path, 'is outside the floor');
        return;
      }
      /* Matches tilemap: only an explicit blocked:true is non-walkable. */
      var blocked = item.blocked === true;
      if (blocked) blockedFurniture.push(item);
      if (roomIds && blocked) {
        rooms.forEach(function (room) {
          if (!isObject(room) || !isInt(room.x)) return;
          for (var tx = item.x; tx < item.x + item.w; tx += 1) {
            for (var ty = item.y; ty < item.y + item.h; ty += 1) {
              if (onRoomBorder(room, tx, ty)) {
                add(path, 'blocked furniture sits on the wall of room "' + room.id + '"');
              }
            }
          }
        });
      }
      for (var key in doorTiles) {
        if (!Object.prototype.hasOwnProperty.call(doorTiles, key)) continue;
        var parts = key.split(',');
        var dx = Number(parts[0]);
        var dy = Number(parts[1]);
        if (rectHas(item, dx, dy)) add(path, 'covers the door at ' + key);
      }
    });

    (Array.isArray(layout.decor) ? layout.decor : []).forEach(function (item, index) {
      var path = 'decor[' + index + ']';
      if (!isObject(item)) {
        add(path, 'must be an object');
        return;
      }
      if (!text(item.type)) add(path + '.type', 'must be a non-empty string');
      if (!isInt(item.x) || !isInt(item.y) || !isInt(item.w) || !isInt(item.h) || item.w < 1 || item.h < 1) {
        add(path, 'x, y, w and h must be positive integers');
        return;
      }
      if (item.x < 0 || item.y < 0 || (widthOk && item.x + item.w > width) || (heightOk && item.y + item.h > height)) {
        add(path, 'is outside the floor');
      }
    });

    var deskIds = Object.create(null);
    var deskTiles = Object.create(null);
    var seatTiles = Object.create(null);
    var desks = layout.desks || [];

    desks.forEach(function (desk, index) {
      var path = 'desks[' + index + ']';
      if (!isObject(desk)) {
        add(path, 'must be an object');
        return;
      }
      if (!text(desk.id)) add(path + '.id', 'must be a non-empty string');
      else if (deskIds[desk.id]) add(path + '.id', 'duplicate desk id "' + desk.id + '"');
      else deskIds[desk.id] = true;
      var room = roomById[desk.room];
      if (!room) {
        add(path + '.room', 'unknown room "' + desk.room + '"');
        return;
      }
      if (!text(desk.department)) add(path + '.department', 'must be a non-empty string');
      if (!isInt(desk.x) || !isInt(desk.y)) {
        add(path, 'x and y must be integers');
        return;
      }
      if (!inRoomInterior(room, desk.x, desk.y)) {
        add(path, 'desk is outside the interior of room "' + room.id + '"');
      }
      var deskKey = desk.x + ',' + desk.y;
      if (deskTiles[deskKey]) add(path, 'two desks share tile ' + deskKey);
      deskTiles[deskKey] = true;

      var facing = desk.facing;
      if (FACINGS.indexOf(facing) < 0) {
        add(path + '.facing', 'must be one of ' + FACINGS.join(', '));
        return;
      }
      if (!isObject(desk.seat) || !isInt(desk.seat.x) || !isInt(desk.seat.y)) {
        add(path + '.seat', 'must have integer x and y');
        return;
      }
      var expected = seatFor(desk.x, desk.y, facing);
      if (desk.seat.x !== expected.x || desk.seat.y !== expected.y) {
        add(path + '.seat', 'must be the adjacent seat for facing "' + facing + '"');
      }
      if (!inRoomInterior(room, desk.seat.x, desk.seat.y)) {
        add(path + '.seat', 'is outside the interior of room "' + room.id + '"');
      }
      var seatKey = desk.seat.x + ',' + desk.seat.y;
      if (seatTiles[seatKey]) add(path + '.seat', 'two desks share seat ' + seatKey);
      seatTiles[seatKey] = true;
      if (deskTiles[seatKey]) add(path + '.seat', 'overlaps a desk tile');
      if (desk.x === desk.seat.x && desk.y === desk.seat.y) add(path + '.seat', 'overlaps its own desk');
    });

    blockedFurniture.forEach(function (item) {
      var path = 'furniture "' + item.id + '"';
      for (var key in deskTiles) {
        if (!Object.prototype.hasOwnProperty.call(deskTiles, key)) continue;
        var parts = key.split(',');
        if (rectHas(item, Number(parts[0]), Number(parts[1]))) add(path, 'covers desk tile ' + key);
      }
      for (var seat in seatTiles) {
        if (!Object.prototype.hasOwnProperty.call(seatTiles, seat)) continue;
        var seatParts = seat.split(',');
        if (rectHas(item, Number(seatParts[0]), Number(seatParts[1]))) add(path, 'covers seat ' + seat);
      }
    });

    for (var i = 0; i < blockedFurniture.length; i += 1) {
      for (var j = i + 1; j < blockedFurniture.length; j += 1) {
        if (rectsOverlap(blockedFurniture[i], blockedFurniture[j])) {
          add('furniture "' + blockedFurniture[i].id + '"', 'overlaps furniture "' + blockedFurniture[j].id + '"');
        }
      }
    }

    if (!isObject(layout.spawn) || !isInt(layout.spawn.x) || !isInt(layout.spawn.y)) {
      add('spawn', 'must have integer x and y');
    } else if (
      layout.spawn.x < 0 || layout.spawn.y < 0 ||
      (widthOk && layout.spawn.x >= width) || (heightOk && layout.spawn.y >= height)
    ) {
      add('spawn', 'is outside the floor');
    } else {
      var spawn = layout.spawn;
      rooms.forEach(function (room) {
        if (isObject(room) && isInt(room.x) && onRoomBorder(room, spawn.x, spawn.y)) {
          add('spawn', 'sits on the wall of room "' + room.id + '"');
        }
      });
      blockedFurniture.forEach(function (item) {
        if (rectHas(item, spawn.x, spawn.y)) add('spawn', 'is blocked by furniture "' + item.id + '"');
      });
      for (var deskKey in deskTiles) {
        if (!Object.prototype.hasOwnProperty.call(deskTiles, deskKey)) continue;
        var dp = deskKey.split(',');
        if (Number(dp[0]) === spawn.x && Number(dp[1]) === spawn.y) add('spawn', 'sits on a desk');
      }
    }

    /* Connectivity: spawn -> corridor -> every door -> every seat. */
    if (widthOk && heightOk && errors.length === 0) {
      var total = width * height;
      var walkable = new Uint8Array(total);
      for (var cell = 0; cell < total; cell += 1) walkable[cell] = 1;

      rooms.forEach(function (room) {
        if (!isObject(room) || !isInt(room.x) || !isInt(room.y) || !isInt(room.w) || !isInt(room.h)) return;
        for (var rv = 0; rv < room.h; rv += 1) {
          for (var rh = 0; rh < room.w; rh += 1) {
            var tx = room.x + rh;
            var ty = room.y + rv;
            if (tx < 0 || ty < 0 || tx >= width || ty >= height) continue;
            if (onRoomBorder(room, tx, ty)) walkable[ty * width + tx] = 0;
          }
        }
        if (isObject(room.door) && isInt(room.door.x) && isInt(room.door.y) &&
          room.door.x >= 0 && room.door.y >= 0 && room.door.x < width && room.door.y < height) {
          walkable[room.door.y * width + room.door.x] = 1;
        }
      });

      blockedFurniture.forEach(function (item) {
        for (var fy = item.y; fy < item.y + item.h; fy += 1) {
          for (var fx = item.x; fx < item.x + item.w; fx += 1) {
            if (fx >= 0 && fy >= 0 && fx < width && fy < height) walkable[fy * width + fx] = 0;
          }
        }
      });

      desks.forEach(function (desk) {
        if (!isObject(desk) || !isInt(desk.x) || !isInt(desk.y)) return;
        if (desk.x >= 0 && desk.y >= 0 && desk.x < width && desk.y < height) walkable[desk.y * width + desk.x] = 0;
        if (isObject(desk.seat) && isInt(desk.seat.x) && isInt(desk.seat.y) &&
          desk.seat.x >= 0 && desk.seat.y >= 0 && desk.seat.x < width && desk.seat.y < height) {
          walkable[desk.seat.y * width + desk.seat.x] = 1;
        }
      });

      var spawnOk = isObject(layout.spawn) && isInt(layout.spawn.x) && isInt(layout.spawn.y) &&
        layout.spawn.x >= 0 && layout.spawn.y >= 0 && layout.spawn.x < width && layout.spawn.y < height &&
        walkable[layout.spawn.y * width + layout.spawn.x] === 1;

      if (spawnOk) {
        var queue = [layout.spawn.y * width + layout.spawn.x];
        var seen = new Uint8Array(total);
        seen[queue[0]] = 1;
        var head = 0;
        while (head < queue.length) {
          var current = queue[head];
          head += 1;
          var cx = current % width;
          var cy = (current - cx) / width;
          [[1, 0], [-1, 0], [0, 1], [0, -1]].forEach(function (delta) {
            var nx = cx + delta[0];
            var ny = cy + delta[1];
            if (nx < 0 || ny < 0 || nx >= width || ny >= height) return;
            var next = ny * width + nx;
            if (!seen[next] && walkable[next] === 1) {
              seen[next] = 1;
              queue.push(next);
            }
          });
        }

        rooms.forEach(function (room) {
          if (!isObject(room) || !isObject(room.door) || !isInt(room.door.x) || !isInt(room.door.y)) return;
          var direction = doorDirection(room, room.door);
          if (!direction) return;
          var inward = { x: room.door.x - direction.x, y: room.door.y - direction.y };
          if (inward.x < 0 || inward.y < 0 || inward.x >= width || inward.y >= height ||
            seen[inward.y * width + inward.x] !== 1) {
            add('rooms', 'door of "' + room.id + '" is not reachable from the spawn');
          }
          var outside = { x: room.door.x + direction.x, y: room.door.y + direction.y };
          if (outside.x >= 0 && outside.y >= 0 && outside.x < width && outside.y < height &&
            walkable[outside.y * width + outside.x] !== 1) {
            add('rooms', 'door of "' + room.id + '" is blocked on the corridor side');
          }
        });

        desks.forEach(function (desk) {
          if (!isObject(desk) || !isObject(desk.seat) || !isInt(desk.seat.x) || !isInt(desk.seat.y)) return;
          if (desk.seat.x < 0 || desk.seat.y < 0 || desk.seat.x >= width || desk.seat.y >= height ||
            seen[desk.seat.y * width + desk.seat.x] !== 1) {
            add('desks', 'seat of "' + desk.id + '" is not reachable from the spawn');
          }
        });
      }
    }

    return { ok: errors.length === 0, errors: errors };
  }

  /* ------------------------------------------------------------------ */
  /* Read-only helpers                                                   */
  /* ------------------------------------------------------------------ */

  function roomAt(layout, x, y) {
    if (!isObject(layout) || !Array.isArray(layout.rooms)) return null;
    if (isObject(x)) { y = x.y; x = x.x; }
    for (var i = 0; i < layout.rooms.length; i += 1) {
      var room = layout.rooms[i];
      if (isObject(room) && rectHas(room, x, y)) return room;
    }
    return null;
  }

  function deskFor(layout, x, y) {
    if (!isObject(layout) || !Array.isArray(layout.desks)) return null;
    if (isObject(x)) { y = x.y; x = x.x; }
    for (var i = 0; i < layout.desks.length; i += 1) {
      var desk = layout.desks[i];
      if (!isObject(desk)) continue;
      if (isObject(desk.seat) && desk.seat.x === x && desk.seat.y === y) return desk;
    }
    for (var j = 0; j < layout.desks.length; j += 1) {
      var other = layout.desks[j];
      if (isObject(other) && isInt(other.x) && isInt(other.y) &&
        (other.w == null ? 1 : other.w) >= 1 && (other.h == null ? 1 : other.h) >= 1 &&
        rectHas({ x: other.x, y: other.y, w: other.w == null ? 1 : other.w, h: other.h == null ? 1 : other.h }, x, y)) {
        return other;
      }
    }
    return null;
  }

  function deskById(layout, id) {
    if (!isObject(layout) || !Array.isArray(layout.desks)) return null;
    for (var i = 0; i < layout.desks.length; i += 1) {
      if (layout.desks[i] && layout.desks[i].id === id) return layout.desks[i];
    }
    return null;
  }

  function failed(errors) {
    return { ok: false, layout: null, errors: errors };
  }

  function moveRoom(layout, roomId, dx, dy) {
    if (!isObject(layout)) return failed(['layout: must be an object']);
    if (!isInt(dx) || !isInt(dy)) return failed(['delta: dx and dy must be integers']);
    var next = clone(layout);
    var room = null;
    for (var i = 0; i < (next.rooms || []).length; i += 1) {
      if (next.rooms[i] && next.rooms[i].id === roomId) {
        room = next.rooms[i];
        break;
      }
    }
    if (!room) return failed(['room "' + roomId + '" not found']);
    var oldRect = { x: room.x, y: room.y, w: room.w, h: room.h };
    function insideOldRoom(item) {
      return isObject(item) && isInt(item.x) && isInt(item.y) && isInt(item.w) && isInt(item.h) &&
        item.x >= oldRect.x && item.y >= oldRect.y &&
        item.x + item.w <= oldRect.x + oldRect.w &&
        item.y + item.h <= oldRect.y + oldRect.h;
    }
    room.x += dx;
    room.y += dy;
    if (room.door) {
      room.door.x += dx;
      room.door.y += dy;
    }
    /* Furniture and rugs live inside their room, so they travel with it. The
     * contract only requires the door and desks; carrying the room contents is
     * what keeps a dragged room looking like a room instead of leaving props
     * behind in the corridor. */
    (Array.isArray(next.furniture) ? next.furniture : []).forEach(function (item) {
      if (!insideOldRoom(item)) return;
      item.x += dx;
      item.y += dy;
    });
    (Array.isArray(next.decor) ? next.decor : []).forEach(function (item) {
      if (!insideOldRoom(item)) return;
      item.x += dx;
      item.y += dy;
    });
    (Array.isArray(next.desks) ? next.desks : []).forEach(function (desk) {
      if (!desk || desk.room !== roomId) return;
      desk.x += dx;
      desk.y += dy;
      if (desk.seat) {
        desk.seat.x += dx;
        desk.seat.y += dy;
      }
    });
    var result = validate(next);
    if (!result.ok) return failed(result.errors);
    return { ok: true, layout: next, errors: [] };
  }

  function moveDoor(layout, roomId, x, y) {
    if (!isObject(layout)) return failed(['layout: must be an object']);
    if (!isInt(x) || !isInt(y)) return failed(['door: x and y must be integers']);
    var next = clone(layout);
    var room = null;
    for (var i = 0; i < (next.rooms || []).length; i += 1) {
      if (next.rooms[i] && next.rooms[i].id === roomId) {
        room = next.rooms[i];
        break;
      }
    }
    if (!room) return failed(['room "' + roomId + '" not found']);
    if (!isInt(room.x) || !isInt(room.y) || !isInt(room.w) || !isInt(room.h)) {
      return failed(['room "' + roomId + '" has invalid coordinates']);
    }

    /* Snap to the nearest non-corner wall tile. Doors stay on the perimeter;
     * validation then decides whether that side opens onto a corridor. */
    var candidates = [];
    function clamp(value, low, high) {
      return Math.max(low, Math.min(high, value));
    }
    if (room.w >= 4) {
      var insetX = clamp(x, room.x + 1, room.x + room.w - 2);
      candidates.push({ x: insetX, y: room.y });
      candidates.push({ x: insetX, y: room.y + room.h - 1 });
    }
    if (room.h >= 4) {
      var insetY = clamp(y, room.y + 1, room.y + room.h - 2);
      candidates.push({ x: room.x, y: insetY });
      candidates.push({ x: room.x + room.w - 1, y: insetY });
    }
    if (!candidates.length) return failed(['room "' + roomId + '" is too small for a door']);
    candidates.sort(function (a, b) {
      var da = Math.abs(a.x - x) + Math.abs(a.y - y);
      var db = Math.abs(b.x - x) + Math.abs(b.y - y);
      return da - db || a.y - b.y || a.x - b.x;
    });
    var firstErrors = null;
    for (var c = 0; c < candidates.length; c += 1) {
      room.door = candidates[c];
      var result = validate(next);
      if (result.ok) return { ok: true, layout: next, errors: [] };
      if (!firstErrors) firstErrors = result.errors;
    }
    return failed(firstErrors || ['room "' + roomId + '" has no valid door wall']);
  }

  function moveDesk(layout, deskId, dx, dy) {
    if (!isObject(layout)) return failed(['layout: must be an object']);
    if (!isInt(dx) || !isInt(dy)) return failed(['delta: dx and dy must be integers']);
    var next = clone(layout);
    var desk = null;
    for (var i = 0; i < (next.desks || []).length; i += 1) {
      if (next.desks[i] && next.desks[i].id === deskId) {
        desk = next.desks[i];
        break;
      }
    }
    if (!desk) return failed(['desk "' + deskId + '" not found']);
    if (!isInt(desk.x) || !isInt(desk.y) || !isObject(desk.seat)) {
      return failed(['desk "' + deskId + '" has invalid coordinates']);
    }
    desk.x += dx;
    desk.y += dy;
    if (desk.seat) {
      desk.seat.x += dx;
      desk.seat.y += dy;
    }
    var result = validate(next);
    if (!result.ok) return failed(result.errors);
    return { ok: true, layout: next, errors: [] };
  }

  /* ------------------------------------------------------------------ */
  /* Theme + project variations                                          */
  /* ------------------------------------------------------------------ */

  function variantLayout(theme, projectId) {
    var base = LAYOUTS[theme];
    var layout = clone(base);
    var random = mulberry32(hashSeed(String(projectId == null ? '' : projectId) + '|' + theme));

    var anchors = VARIANT_ANCHORS[theme] || [];
    if (anchors.length) {
      var anchor = anchors[Math.floor(random() * anchors.length) % anchors.length];
      layout.furniture.push({
        id: 'plant-variant',
        type: 'plant',
        x: anchor.x,
        y: anchor.y,
        w: 1,
        h: 1,
        blocked: true
      });
    }

    var lobby = null;
    layout.rooms.forEach(function (room) {
      if (room.id === 'lobby') lobby = room;
    });
    if (lobby && lobby.w >= 8 && lobby.h >= 6) {
      var rugW = Math.min(6, lobby.w - 4);
      var rugH = Math.min(2, lobby.h - 4);
      var maxX = lobby.x + 1 + Math.max(0, lobby.w - 2 - rugW - 1);
      var minX = lobby.x + 1;
      var rugX = minX + Math.floor(random() * Math.max(1, maxX - minX + 1));
      var rugY = lobby.y + 1 + Math.floor(random() * Math.max(1, lobby.h - 2 - rugH));
      layout.decor.push({ type: 'rug', x: rugX, y: rugY, w: rugW, h: rugH });
    }

    var result = validate(layout);
    if (result.ok) return layout;
    /* Variation must never make the office unrenderable. */
    return clone(base);
  }

  function withProject(layout, projectId) {
    if (projectId == null || projectId === '') return layout;
    var copy = clone(layout);
    copy.project_id = String(projectId);
    return copy;
  }

  function get(theme, projectId) {
    return withProject(variantLayout(normalizeTheme(theme), projectId), projectId);
  }

  /* ------------------------------------------------------------------ */
  /* localStorage override                                               */
  /* ------------------------------------------------------------------ */

  function resolveStorage() {
    var candidates = [];
    try {
      if (root && root.localStorage) candidates.push(root.localStorage);
    } catch (error) { /* access can throw in hardened browsers */ }
    try {
      if (typeof globalThis !== 'undefined' && globalThis.localStorage) candidates.push(globalThis.localStorage);
    } catch (error) { /* ignore */ }
    return candidates.length ? candidates[0] : null;
  }

  function rawRead(projectId) {
    var store = resolveStorage();
    if (!store || typeof store.getItem !== 'function') return null;
    try {
      return store.getItem(storageKey(projectId));
    } catch (error) {
      return null;
    }
  }

  function load(projectId, theme) {
    var raw = rawRead(projectId);
    if (typeof raw === 'string' && raw.length) {
      var parsed = null;
      try {
        parsed = JSON.parse(raw);
      } catch (error) {
        parsed = null;
      }
      if (parsed && validate(parsed).ok) return withProject(clone(parsed), projectId);
    }
    return get(theme, projectId);
  }

  function save(projectId, layout) {
    if (!validate(layout).ok) return false;
    var store = resolveStorage();
    if (!store || typeof store.setItem !== 'function') return false;
    try {
      store.setItem(storageKey(projectId), serialize(layout));
      return true;
    } catch (error) {
      return false;
    }
  }

  function reset(projectId) {
    var store = resolveStorage();
    if (!store || typeof store.removeItem !== 'function') return false;
    try {
      store.removeItem(storageKey(projectId));
      return true;
    } catch (error) {
      return false;
    }
  }

  var api = {
    version: VERSION,
    themes: THEMES,
    rooms: ROOM_IDS,
    facings: FACINGS,
    seatFor: seatFor,
    normalizeTheme: normalizeTheme,
    storageKey: storageKey,
    get: get,
    load: load,
    save: save,
    reset: reset,
    validate: validate,
    clone: clone,
    serialize: serialize,
    moveRoom: moveRoom,
    moveDoor: moveDoor,
    moveDesk: moveDesk,
    roomAt: roomAt,
    deskFor: deskFor,
    deskById: deskById
  };

  root.AOOfficeLayouts = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
