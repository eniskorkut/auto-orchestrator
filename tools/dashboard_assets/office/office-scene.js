/* Auto-Orchestrator V7.2 — continuous Canvas 2D pixel-art office scene.
 *
 * One map, one camera. Rooms, corridors, walls, doors, furniture and desks are
 * static scenery; agents are z-sorted characters that walk A* routes instead of
 * teleporting. The scene never invents bindings: syncAgents() only renders
 * entries accepted by AOAgentStateMachine.canSpawn().
 */
(() => {
  'use strict';

  const SPEED_TILES = 4;
  const SPAWN_SECONDS = 0.5;
  const SETTLE_SECONDS = 0.22;
  const MAX_ZOOM = 6;
  const CAMERA_MARGIN = 48;

  const THEMES = {
    ai: {
      outer: '#070a10',
      edge: '#121a26',
      corridorA: '#111824',
      corridorB: '#141d2b',
      seam: '#0a0f17',
      room: '#182235',
      wall: '#2b3852',
      wallTop: '#465570',
      wallShadow: '#121a27',
      wallLine: '#090d14',
      rug: '#1a2b46',
      desk: '#4e3a2d',
      deskTop: '#6e5442',
      metal: '#333e50',
      metalTop: '#48586f',
      text: '#dbe5f2',
      label: '#cbd9ea',
      dim: '#05070b',
    },
    security: {
      outer: '#060c09',
      edge: '#101e17',
      corridorA: '#0c1611',
      corridorB: '#0f1b15',
      seam: '#070e0b',
      room: '#12211a',
      wall: '#263d30',
      wallTop: '#3d604a',
      wallShadow: '#0e1b14',
      wallLine: '#060d09',
      rug: '#16291e',
      desk: '#44392b',
      deskTop: '#61523d',
      metal: '#2b3d32',
      metalTop: '#3d5344',
      text: '#dcebe1',
      label: '#c6ddcd',
      dim: '#040805',
    },
    web: {
      outer: '#0d0907',
      edge: '#211813',
      corridorA: '#19120f',
      corridorB: '#1d1512',
      seam: '#0e0907',
      room: '#221814',
      wall: '#403027',
      wallTop: '#62483a',
      wallShadow: '#201612',
      wallLine: '#0d0907',
      rug: '#32251e',
      desk: '#523a2d',
      deskTop: '#765440',
      metal: '#40332c',
      metalTop: '#58463b',
      text: '#efe2d8',
      label: '#e0cec0',
      dim: '#080604',
    },
    custom: {
      outer: '#0a0811',
      edge: '#1a1530',
      corridorA: '#141122',
      corridorB: '#18142a',
      seam: '#0b0915',
      room: '#1d1931',
      wall: '#322c57',
      wallTop: '#4d467c',
      wallShadow: '#171331',
      wallLine: '#090711',
      rug: '#262144',
      desk: '#463c57',
      deskTop: '#625675',
      metal: '#352f4d',
      metalTop: '#4a4368',
      text: '#e8e2f6',
      label: '#d8cff0',
      dim: '#07050c',
    },
  };

  const ROOM_TINTS = {
    frontend: '#4f8cff',
    backend: '#e8a87c',
    qa: '#70e0a0',
    review: '#c4b5fd',
    research: '#7dd3fc',
    meeting: '#f2c96a',
    lobby: '#9aa7b8',
  };
  const CUSTOM_TINTS = ['#8fb4ff', '#e8a87c', '#70e0a0', '#c4b5fd', '#7dd3fc', '#f2c96a', '#f08a8a'];
  const AGENT_ACCENTS = ['#7dd3fc', '#f2a65a', '#70e0a0', '#c4b5fd', '#82c8ff', '#f2b45f', '#e8a87c', '#8fe0a4'];
  const STATUS_TONE = {
    completed: '#70e0a0',
    failed: '#f47d7d',
    blocked: '#f2b45f',
    awaiting: '#f2b45f',
    unknown: '#9aa0aa',
  };

  function parseHex(value) {
    if (typeof value !== 'string') return null;
    const raw = value.trim();
    if (/^#[0-9a-f]{3}$/i.test(raw)) {
      return {
        r: parseInt(raw[1] + raw[1], 16),
        g: parseInt(raw[2] + raw[2], 16),
        b: parseInt(raw[3] + raw[3], 16),
      };
    }
    if (/^#[0-9a-f]{6}$/i.test(raw)) {
      return {
        r: parseInt(raw.slice(1, 3), 16),
        g: parseInt(raw.slice(3, 5), 16),
        b: parseInt(raw.slice(5, 7), 16),
      };
    }
    return null;
  }

  function toHex(rgb) {
    const clamp = (value) => Math.max(0, Math.min(255, Math.round(value)));
    return `#${clamp(rgb.r).toString(16).padStart(2, '0')}${clamp(rgb.g).toString(16).padStart(2, '0')}${clamp(rgb.b).toString(16).padStart(2, '0')}`;
  }

  function mix(a, b, amount) {
    const first = parseHex(a);
    const second = parseHex(b);
    if (!first || !second) return a;
    const t = Math.max(0, Math.min(1, amount));
    return toHex({
      r: first.r + (second.r - first.r) * t,
      g: first.g + (second.g - first.g) * t,
      b: first.b + (second.b - first.b) * t,
    });
  }

  function rgba(color, alpha) {
    const rgb = parseHex(color);
    if (!rgb) return `rgba(255,255,255,${alpha})`;
    return `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${alpha})`;
  }

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function hash(value) {
    const input = String(value == null ? '' : value);
    let result = 0x811c9dc5;
    for (let index = 0; index < input.length; index += 1) {
      result ^= input.charCodeAt(index);
      result = Math.imul(result, 0x01000193);
    }
    return result >>> 0;
  }

  function tileHash(x, y) {
    let value = (x * 374761393 + y * 668265263) >>> 0;
    value = Math.imul(value ^ (value >>> 13), 1274126177) >>> 0;
    return (value ^ (value >>> 16)) >>> 0;
  }

  function safeCall(callback, ...args) {
    if (typeof callback !== 'function') return;
    try {
      callback(...args);
    } catch (error) {
      if (typeof console !== 'undefined' && console.error) {
        console.error('AOOfficeScene callback failed', error);
      }
    }
  }

  function create(options = {}) {
    const canvas = options.canvas;
    if (!canvas || typeof canvas.getContext !== 'function') {
      throw new TypeError('AOOfficeScene.create: a canvas element is required');
    }
    const root = typeof globalThis !== 'undefined' ? globalThis : {};
    const Tilemap = root.AOTilemap;
    const Pathfinding = root.AOPathfinding;
    const Sprites = root.AOAgentSprites;
    const StateMachine = root.AOAgentStateMachine;
    const doc = canvas.ownerDocument || (typeof document !== 'undefined' ? document : null);
    if (!Tilemap || !Pathfinding || !Sprites || !StateMachine) {
      throw new Error('AOOfficeScene: load tilemap, pathfinding, agent-sprites and agent-state-machine first');
    }
    if (!doc) throw new Error('AOOfficeScene: a document context is required');

    let gl = null;
    try {
      gl = canvas.getContext('2d', { alpha: false });
    } catch (error) {
      gl = null;
    }
    if (!gl) gl = canvas.getContext('2d');
    if (!gl) throw new Error('AOOfficeScene: Canvas 2D is unavailable');

    const state = {
      layout: null,
      grid: null,
      entries: [],
      agents: new Map(),
      reducedMotion: Boolean(options.reducedMotion),
      roomFilter: null,
      editing: false,
      selectedKey: null,
      hoverKey: null,
      destroyed: false,
      frame: 0,
      drawnFrames: 0,
      fps: 0,
      fpsAccum: 0,
      fpsFrames: 0,
      clock: 0,
      visible: true,
      raf: 0,
      lastNow: 0,
      lastError: null,
      view: { width: 0, height: 0, dpr: 1 },
      baseScale: 1,
      scale: 1,
      maxZoom: MAX_ZOOM,
      zoom: 1,
      centerX: 0,
      centerY: 0,
      offsetX: 0,
      offsetY: 0,
      hasFit: false,
      pendingEntries: [],
      needsReplan: false,
    };
    let layout = null;
    let grid = null;
    let theme = THEMES.ai;
    let staticCanvas = null;
    let staticKey = '';
    let drag = null;
    let pinch = null;
    const pointers = new Map();

    const now = () => (root.performance && typeof root.performance.now === 'function'
      ? root.performance.now()
      : Date.now());

    const moment = () => (state.clock > 0 ? state.clock : now());

    function themeFor(value) {
      return THEMES[value] || THEMES.custom;
    }

    function tintFor(room) {
      if (!room) return THEMES.ai.edge;
      const key = String(room.department || room.id || '').toLowerCase();
      if (ROOM_TINTS[key]) return ROOM_TINTS[key];
      if (ROOM_TINTS[String(room.id || '').toLowerCase()]) {
        return ROOM_TINTS[String(room.id).toLowerCase()];
      }
      return CUSTOM_TINTS[hash(key) % CUSTOM_TINTS.length];
    }

    function roomForId(id) {
      if (!grid || !id) return null;
      return grid.roomById(id);
    }

    function roomNameFor(id) {
      const room = roomForId(id);
      return room ? room.name : (id || null);
    }

    function accentFor(entry, key) {
      const raw = rawEntry(entry);
      if (raw && parseHex(raw.color)) return raw.color.trim();
      return AGENT_ACCENTS[hash(key) % AGENT_ACCENTS.length];
    }

    /* ── static scenery ─────────────────────────────────────────────────── */

    function buildStatic() {
      if (!grid) return;
      const width = grid.width * grid.tile;
      const height = grid.height * grid.tile;
      const key = `${width}x${height}:${state.roomFilter || 'all'}:${grid.tile}`;
      if (key === staticKey && staticCanvas) return;
      if (!staticCanvas) staticCanvas = doc.createElement('canvas');
      staticCanvas.width = width;
      staticCanvas.height = height;
      const ctx = staticCanvas.getContext('2d');
      if (!ctx) return;
      ctx.imageSmoothingEnabled = false;
      const tile = grid.tile;
      const filter = state.roomFilter;
      const dim = (color, roomId) => (filter && roomId !== filter
        ? mix(color, theme.dim, 0.64)
        : color);

      ctx.fillStyle = theme.corridorA;
      ctx.fillRect(0, 0, width, height);
      ctx.fillStyle = theme.dim;
      ctx.fillRect(0, 0, width, height);

      /* floors: room interiors and corridors */
      for (let y = 0; y < grid.height; y += 1) {
        for (let x = 0; x < grid.width; x += 1) {
          if (grid.wallAt(x, y) || grid.doorAt(x, y)) continue;
          const info = grid.roomInfo(x, y);
          const roomId = info ? info.id : null;
          const checker = (Math.floor(x / 2) + Math.floor(y / 2)) % 2 === 0;
          let color;
          if (info) {
            const tint = tintFor(info);
            color = checker ? theme.room : mix(theme.room, '#ffffff', 0.022);
            color = mix(color, tint, 0.13);
          } else {
            color = checker ? theme.corridorA : theme.corridorB;
          }
          ctx.fillStyle = dim(color, roomId);
          ctx.fillRect(x * tile, y * tile, tile, tile);

          /* One-tile seam, very low contrast: floor, not a card grid. */
          ctx.fillStyle = roomId
            ? rgba('#000000', 0.075)
            : rgba(theme.seam, 0.24);
          ctx.fillRect(x * tile, y * tile + tile - 1, tile, 1);
          ctx.fillRect(x * tile + tile - 1, y * tile, 1, tile);
          ctx.fillStyle = rgba('#ffffff', 0.025);
          ctx.fillRect(x * tile, y * tile, tile - 1, 1);

          const speck = tileHash(x, y);
          if (speck % 17 === 0) {
            ctx.fillStyle = rgba('#ffffff', 0.035);
            ctx.fillRect(x * tile + 2 + (speck % 9), y * tile + 3 + ((speck >>> 4) % 9), 1, 1);
          } else if (speck % 31 === 0) {
            ctx.fillStyle = rgba('#000000', 0.22);
            ctx.fillRect(x * tile + 3 + (speck % 7), y * tile + 4 + ((speck >>> 5) % 8), 1, 1);
          }
        }
      }

      /* rugs and floor decor, below walls */
      const rugItems = [
        ...(Array.isArray(grid.decor) ? grid.decor : []),
        ...(Array.isArray(grid.furniture) ? grid.furniture : [])
          .filter((item) => /^(rug|carpet|mat|floor)$/i.test(item.type)),
      ];
      for (const item of rugItems) drawRug(ctx, item, dim, filter);

      /* walls: derived rings around every room */
      for (let y = 0; y < grid.height; y += 1) {
        for (let x = 0; x < grid.width; x += 1) {
          if (!grid.wallAt(x, y)) continue;
          const info = grid.roomInfo(x, y);
          const roomId = info ? info.id : null;
          const tint = info ? tintFor(info) : theme.edge;
          const wall = mix(theme.wall, tint, 0.075);
          const top = mix(theme.wallTop, tint, 0.06);
          ctx.fillStyle = dim(wall, roomId);
          ctx.fillRect(x * tile, y * tile, tile, tile);
          ctx.fillStyle = dim(top, roomId);
          ctx.fillRect(x * tile, y * tile, tile, Math.max(2, Math.round(tile * 0.19)));
          ctx.fillStyle = dim(theme.wallShadow, roomId);
          ctx.fillRect(x * tile, y * tile + tile - Math.max(2, Math.round(tile * 0.13)), tile,
            Math.max(2, Math.round(tile * 0.13)));
          ctx.fillStyle = rgba(theme.wallLine, 0.65);
          ctx.fillRect(x * tile, y * tile, 1, tile);
          ctx.fillRect(x * tile + tile - 1, y * tile, 1, tile);
          ctx.fillStyle = rgba('#ffffff', 0.035);
          ctx.fillRect(x * tile + 2, y * tile + 4, tile - 4, 1);
          if ((tileHash(x, y) & 7) === 0) {
            ctx.fillStyle = rgba('#000000', 0.24);
            ctx.fillRect(x * tile + Math.floor(tile * 0.55), y * tile + Math.floor(tile * 0.55),
              1, 1);
          }
        }
      }

      /* doors cut the wall ring and stay visibly walkable */
      for (const door of grid.doors) drawDoor(ctx, door, dim, filter);

      /* spawn hub pad */
      if (grid.spawn) {
        const info = grid.roomInfo(grid.spawn.x, grid.spawn.y);
        const roomId = info ? info.id : null;
        const tint = info ? tintFor(info) : theme.edge;
        const sx = grid.spawn.x * tile;
        const sy = grid.spawn.y * tile;
        ctx.fillStyle = rgba(dim(tint, roomId), 0.14);
        ctx.fillRect(sx + 3, sy + 3, tile - 6, tile - 6);
        ctx.fillStyle = rgba(dim(tint, roomId), 0.35);
        for (let step = 0; step < 3; step += 1) {
          ctx.fillRect(sx + 4 + step * 3, sy + 4, 2, 1);
          ctx.fillRect(sx + 4 + step * 3, sy + tile - 5, 2, 1);
          ctx.fillRect(sx + 4, sy + 4 + step * 3, 1, 2);
          ctx.fillRect(sx + tile - 5, sy + 4 + step * 3, 1, 2);
        }
        ctx.fillStyle = rgba(dim(tint, roomId), 0.55);
        ctx.fillRect(sx + Math.floor(tile / 2) - 1, sy + Math.floor(tile / 2) - 1, 2, 2);
      }

      /* room edge shadow across the interior floor */
      for (const room of grid.rooms) {
        const roomId = room.id;
        const roomDim = filter && filter !== roomId;
        ctx.fillStyle = roomDim ? rgba('#000000', 0.12) : rgba('#000000', 0.18);
        const inset = tile;
        ctx.fillRect(room.x * tile + inset, room.y * tile + inset, room.w * tile - inset * 2, 1);
        ctx.fillRect(room.x * tile + inset, room.y * tile + room.h * tile - inset - 1,
          room.w * tile - inset * 2, 1);
      }

      /* map trim */
      ctx.fillStyle = rgba(theme.edge, 0.95);
      ctx.fillRect(0, 0, width, 2);
      ctx.fillRect(0, height - 2, width, 2);
      ctx.fillRect(0, 0, 2, height);
      ctx.fillRect(width - 2, 0, 2, height);
      staticKey = key;
    }

    function drawRug(ctx, item, dim, filter) {
      const tile = grid.tile;
      const x = item.x * tile;
      const y = item.y * tile;
      const w = item.w * tile;
      const h = item.h * tile;
      const info = grid.roomInfo(item.x, item.y);
      const roomId = info ? info.id : null;
      const tint = info ? tintFor(info) : theme.edge;
      const base = dim(mix(theme.rug, tint, 0.22), roomId);
      const border = dim(mix(theme.rug, '#ffffff', 0.11), roomId);
      ctx.fillStyle = base;
      ctx.fillRect(x + 2, y + 2, w - 4, h - 4);
      ctx.fillStyle = border;
      ctx.fillRect(x + 2, y + 2, w - 4, 1);
      ctx.fillRect(x + 2, y + h - 3, w - 4, 1);
      ctx.fillRect(x + 2, y + 2, 1, h - 4);
      ctx.fillRect(x + w - 3, y + 2, 1, h - 4);
      ctx.fillStyle = rgba(mix(theme.rug, '#ffffff', 0.16), 0.38);
      for (let stripe = x + 6; stripe < x + w - 5; stripe += 6) {
        ctx.fillRect(stripe, y + 5, 1, h - 10);
      }
      if (filter && filter !== roomId) {
        ctx.fillStyle = rgba(theme.dim, 0.32);
        ctx.fillRect(x, y, w, h);
      }
    }

    function drawDoor(ctx, door, dim, filter) {
      const tile = grid.tile;
      const x = door.x * tile;
      const y = door.y * tile;
      const info = grid.roomInfo(door.x, door.y);
      const roomId = info ? info.id : null;
      const roomDim = filter && filter !== roomId;
      const tint = info ? tintFor(info) : theme.edge;
      const floor = dim(mix(theme.corridorB, tint, 0.07), roomId);
      ctx.fillStyle = floor;
      ctx.fillRect(x, y, tile, tile);

      const frame = dim(theme.wallTop, roomId);
      const glow = roomDim ? rgba(tint, 0.12) : rgba(tint, 0.42);
      if (door.side === 'up' || door.side === 'down') {
        ctx.fillStyle = frame;
        ctx.fillRect(x, y, 2, tile);
        ctx.fillRect(x + tile - 2, y, 2, tile);
        ctx.fillStyle = rgba('#000000', 0.35);
        ctx.fillRect(x + 2, y, 1, tile);
        ctx.fillRect(x + tile - 3, y, 1, tile);
        ctx.fillStyle = glow;
        ctx.fillRect(x + 3, y, 1, tile);
        ctx.fillRect(x + tile - 4, y, 1, tile);
        ctx.fillStyle = dim(mix(theme.wallShadow, tint, 0.18), roomId);
        ctx.fillRect(x, door.side === 'down' ? y : y + tile - 2, tile, 2);
      } else {
        ctx.fillStyle = frame;
        ctx.fillRect(x, y, tile, 2);
        ctx.fillRect(x, y + tile - 2, tile, 2);
        ctx.fillStyle = rgba('#000000', 0.35);
        ctx.fillRect(x, y + 2, tile, 1);
        ctx.fillRect(x, y + tile - 3, tile, 1);
        ctx.fillStyle = glow;
        ctx.fillRect(x, y + 3, tile, 1);
        ctx.fillRect(x, y + tile - 4, tile, 1);
        ctx.fillStyle = dim(mix(theme.wallShadow, tint, 0.18), roomId);
        ctx.fillRect(door.side === 'right' ? x : x + tile - 2, y, 2, tile);
      }
      /* threshold dots */
      ctx.fillStyle = rgba('#ffffff', roomDim ? 0.05 : 0.14);
      if (door.side === 'up' || door.side === 'down') {
        ctx.fillRect(x + Math.floor(tile / 2) - 1, y + 2, 2, 1);
        ctx.fillRect(x + Math.floor(tile / 2) - 1, y + tile - 3, 2, 1);
      } else {
        ctx.fillRect(x + 2, y + Math.floor(tile / 2) - 1, 1, 2);
        ctx.fillRect(x + tile - 3, y + Math.floor(tile / 2) - 1, 1, 2);
      }
    }

    function drawFurniture(ctx, item, alpha) {
      const tile = grid.tile;
      const u = tile / 16;
      const x = item.x * tile;
      const y = item.y * tile;
      const w = item.w * tile;
      const h = item.h * tile;
      const info = grid.roomInfo(item.x, item.y);
      const tint = info ? tintFor(info) : theme.edge;
      const type = String(item.type || '').toLowerCase();
      ctx.save();
      ctx.globalAlpha = alpha;
      if (/^(rug|carpet|mat|floor)$/.test(type)) {
        ctx.restore();
        return;
      }
      if (/^(plant|plants|tree)$/.test(type)) drawPlant(ctx, x, y, w, h, u);
      else if (/^(table|meeting|desk)$/.test(type)) drawTable(ctx, x, y, w, h, u, tint);
      else if (/^(whiteboard|board|screen)$/.test(type)) drawWhiteboard(ctx, x, y, w, h, u, tint);
      else if (/^(bookshelf|shelf|bookcase)$/.test(type)) drawShelf(ctx, x, y, w, h, u);
      else if (/^(sofa|couch|bench|seat)$/.test(type)) drawSofa(ctx, x, y, w, h, u, tint);
      else if (/^(server|rack|machine)$/.test(type)) drawRack(ctx, x, y, w, h, u, tint);
      else if (/^(cooler|water|watercooler)$/.test(type)) drawCooler(ctx, x, y, w, h, u);
      else if (/^(coffee|kitchen|counter)$/.test(type)) drawCounter(ctx, x, y, w, h, u);
      else if (/^(cabinet|locker|drawer)$/.test(type)) drawCabinet(ctx, x, y, w, h, u, tint);
      else if (/^(lamp|light)$/.test(type)) drawLamp(ctx, x, y, w, h, u);
      else if (/^(chair|stool)$/.test(type)) drawChairShape(ctx, x + w / 2, y + h, u, 'up', tint);
      else drawCrate(ctx, x, y, w, h, u, tint);
      ctx.restore();
    }

    function drawPlant(ctx, x, y, w, h, u) {
      const cx = x + w / 2;
      const by = y + h - 2 * u;
      const potW = Math.min(w * 0.46, 9 * u);
      const potH = 5 * u;
      ctx.fillStyle = '#70452f';
      ctx.fillRect(cx - potW / 2, by - potH, potW, potH);
      ctx.fillStyle = '#8f5b3c';
      ctx.fillRect(cx - potW / 2, by - potH, potW, 1.5 * u);
      ctx.fillStyle = '#3f6f4e';
      ctx.fillRect(cx - u, by - potH - 4 * u, 2 * u, 4 * u);
      ctx.fillStyle = '#4d8a5e';
      ctx.fillRect(cx - 4 * u, by - potH - 7 * u, 3 * u, 3 * u);
      ctx.fillRect(cx + u, by - potH - 8 * u, 3 * u, 3 * u);
      ctx.fillRect(cx - 6 * u, by - potH - 4 * u, 3 * u, 3 * u);
      ctx.fillStyle = '#65a873';
      ctx.fillRect(cx - 3 * u, by - potH - 9 * u, 2 * u, 2 * u);
      ctx.fillRect(cx + 3 * u, by - potH - 5 * u, 2 * u, 2 * u);
    }

    function drawTable(ctx, x, y, w, h, u, tint) {
      ctx.fillStyle = rgba('#000000', 0.28);
      ctx.fillRect(x + 2 * u, y + h - 2 * u, w - 4 * u, 2 * u);
      ctx.fillStyle = theme.desk;
      ctx.fillRect(x + 2 * u, y + 3 * u, w - 4 * u, h - 5 * u);
      ctx.fillStyle = mix(theme.deskTop, tint, 0.12);
      ctx.fillRect(x + 2 * u, y + 3 * u, w - 4 * u, 2 * u);
      ctx.fillStyle = rgba('#ffffff', 0.08);
      ctx.fillRect(x + 3 * u, y + 4 * u, w - 6 * u, 1);
      ctx.fillStyle = theme.desk;
      ctx.fillRect(x + 4 * u, y + h - 2 * u, 2 * u, 2 * u);
      ctx.fillRect(x + w - 6 * u, y + h - 2 * u, 2 * u, 2 * u);
      ctx.fillStyle = '#e7edf3';
      ctx.fillRect(x + w * 0.3, y + 5 * u, 4 * u, 3 * u);
      ctx.fillRect(x + w * 0.68, y + 6 * u, 3 * u, 3 * u);
      ctx.fillStyle = tint;
      ctx.fillRect(x + w * 0.3 + u, y + 6 * u, 2 * u, 1);
      ctx.fillStyle = '#d9d2c4';
      ctx.fillRect(x + w * 0.52, y + 4 * u, 2 * u, 2 * u);
    }

    function drawWhiteboard(ctx, x, y, w, h, u, tint) {
      /* Muted slate board with a visible frame. A pure white slab reads as a
       * rendering glitch at small zoom, so keep the surface mid-tone. */
      ctx.fillStyle = '#0d131c';
      ctx.fillRect(x + 2 * u, y + 2 * u, w - 4 * u, h - 4 * u);
      ctx.fillStyle = '#39465a';
      ctx.fillRect(x + 3 * u, y + 3 * u, w - 6 * u, h - 6 * u);
      ctx.fillStyle = '#67757f';
      ctx.fillRect(x + 4 * u, y + 4 * u, w - 8 * u, h - 9 * u);
      /* Paper notes and a marker line make it read as a board, not a stray bar. */
      const note = Math.max(2 * u, Math.min(4 * u, w * 0.12));
      ctx.fillStyle = '#e8c66d';
      ctx.fillRect(x + 6 * u, y + 5 * u, note, note);
      ctx.fillStyle = '#8fd3b0';
      ctx.fillRect(x + 6 * u + note + u, y + 5 * u, note, note);
      ctx.fillStyle = 'rgba(15,23,34,0.26)';
      ctx.fillRect(x + 6 * u, y + 6 * u + note, Math.max(3 * u, w * 0.5), u);
      ctx.fillRect(x + 6 * u, y + 8 * u + note, Math.max(2 * u, w * 0.34), u);
      ctx.fillStyle = tint;
      ctx.fillRect(x + 6 * u, y + 9 * u + note, Math.max(2 * u, w * 0.22), u);
      ctx.fillStyle = '#4b5866';
      ctx.fillRect(x + w - 9 * u, y + h - 4 * u, 5 * u, u);
      ctx.fillStyle = '#aebbc7';
      ctx.fillRect(x + w - 8 * u, y + h - 3 * u, 3 * u, u);
    }

    function drawShelf(ctx, x, y, w, h, u) {
      ctx.fillStyle = '#3b2a20';
      ctx.fillRect(x + 2 * u, y + 1 * u, w - 4 * u, h - 2 * u);
      ctx.fillStyle = '#241811';
      ctx.fillRect(x + 3 * u, y + 3 * u, w - 6 * u, h - 6 * u);
      const shelves = Math.max(2, Math.floor(h / (7 * u)));
      for (let index = 0; index < shelves; index += 1) {
        const sy = y + 4 * u + index * ((h - 7 * u) / shelves);
        ctx.fillStyle = '#6b4c37';
        ctx.fillRect(x + 3 * u, sy + 3 * u, w - 6 * u, 1);
        for (let book = 0; book < 5; book += 1) {
          const bx = x + 4 * u + book * ((w - 8 * u) / 5);
          ctx.fillStyle = CUSTOM_TINTS[(index * 5 + book) % CUSTOM_TINTS.length];
          ctx.fillRect(bx, sy + u, Math.max(1, (w - 10 * u) / 7), 2 * u);
        }
      }
    }

    function drawSofa(ctx, x, y, w, h, u, tint) {
      ctx.fillStyle = rgba('#000000', 0.24);
      ctx.fillRect(x + 2 * u, y + h - 2 * u, w - 4 * u, 2 * u);
      ctx.fillStyle = mix('#394352', tint, 0.12);
      ctx.fillRect(x + 2 * u, y + 4 * u, w - 4 * u, h - 6 * u);
      ctx.fillStyle = mix('#4a5668', tint, 0.1);
      ctx.fillRect(x + 3 * u, y + 3 * u, w - 6 * u, 4 * u);
      ctx.fillStyle = mix('#2c3441', tint, 0.08);
      ctx.fillRect(x + 1 * u, y + 6 * u, 3 * u, h - 8 * u);
      ctx.fillRect(x + w - 4 * u, y + 6 * u, 3 * u, h - 8 * u);
      ctx.fillStyle = rgba('#ffffff', 0.06);
      ctx.fillRect(x + 5 * u, y + 8 * u, Math.max(2 * u, w * 0.3), u);
    }

    function drawRack(ctx, x, y, w, h, u, tint) {
      ctx.fillStyle = '#141922';
      ctx.fillRect(x + 2 * u, y + 1 * u, w - 4 * u, h - 2 * u);
      ctx.fillStyle = '#222b37';
      ctx.fillRect(x + 3 * u, y + 2 * u, w - 6 * u, h - 4 * u);
      for (let row = 0; row < 4; row += 1) {
        const ry = y + 3 * u + row * ((h - 6 * u) / 4);
        ctx.fillStyle = '#0d1118';
        ctx.fillRect(x + 4 * u, ry, w - 8 * u, 2 * u);
        ctx.fillStyle = row % 2 === 0 ? tint : '#70e0a0';
        ctx.fillRect(x + w - 6 * u, ry + u / 2, 1, 1);
      }
    }

    function drawCooler(ctx, x, y, w, h, u) {
      const cx = x + w / 2;
      ctx.fillStyle = '#c9d3de';
      ctx.fillRect(cx - 4 * u, y + 5 * u, 8 * u, h - 6 * u);
      ctx.fillStyle = '#e8eef4';
      ctx.fillRect(cx - 4 * u, y + 5 * u, 8 * u, u);
      ctx.fillStyle = '#5fa8cf';
      ctx.fillRect(cx - 3 * u, y + 1 * u, 6 * u, 4 * u);
      ctx.fillStyle = '#8fd5f2';
      ctx.fillRect(cx - 2 * u, y + 1 * u, 2 * u, 3 * u);
      ctx.fillStyle = '#2e6c8a';
      ctx.fillRect(cx - 1 * u, y + 6 * u, 2 * u, 1);
    }

    function drawCounter(ctx, x, y, w, h, u) {
      ctx.fillStyle = '#3a2f27';
      ctx.fillRect(x + 1 * u, y + 4 * u, w - 2 * u, h - 5 * u);
      ctx.fillStyle = '#5f4a3a';
      ctx.fillRect(x + 1 * u, y + 4 * u, w - 2 * u, 2 * u);
      ctx.fillStyle = '#232b36';
      ctx.fillRect(x + 3 * u, y + 1 * u, 6 * u, 4 * u);
      ctx.fillStyle = '#e8b06f';
      ctx.fillRect(x + 4 * u, y + 2 * u, 1, 2 * u);
      ctx.fillRect(x + 7 * u, y + 2 * u, 1, 2 * u);
      ctx.fillStyle = '#8e9aa8';
      ctx.fillRect(x + w - 6 * u, y + 5 * u, 3 * u, 1);
    }

    function drawCabinet(ctx, x, y, w, h, u, tint) {
      ctx.fillStyle = mix('#2b3544', tint, 0.05);
      ctx.fillRect(x + 2 * u, y + 1 * u, w - 4 * u, h - 2 * u);
      ctx.fillStyle = '#1b222d';
      ctx.fillRect(x + 3 * u, y + 2 * u, w - 6 * u, h - 4 * u);
      ctx.fillStyle = '#3c4859';
      ctx.fillRect(x + w / 2, y + 2 * u, 1, h - 4 * u);
      ctx.fillStyle = tint;
      ctx.fillRect(x + w / 2 - 2 * u, y + h / 2, 1, 1);
      ctx.fillRect(x + w / 2 + 2 * u, y + h / 2, 1, 1);
    }

    function drawLamp(ctx, x, y, w, h, u) {
      const cx = x + w / 2;
      ctx.fillStyle = '#2b3444';
      ctx.fillRect(cx - u, y + h - 2 * u, 2 * u, h - 4 * u);
      ctx.fillStyle = '#f2c96a';
      ctx.fillRect(cx - 3 * u, y + 1 * u, 6 * u, 4 * u);
      ctx.fillStyle = rgba('#f2c96a', 0.18);
      ctx.fillRect(cx - 5 * u, y + 4 * u, 10 * u, 4 * u);
    }

    function drawCrate(ctx, x, y, w, h, u, tint) {
      ctx.fillStyle = '#4a3a2c';
      ctx.fillRect(x + 2 * u, y + 2 * u, w - 4 * u, h - 4 * u);
      ctx.fillStyle = '#6a523d';
      ctx.fillRect(x + 2 * u, y + 2 * u, w - 4 * u, 1);
      ctx.fillStyle = rgba(tint, 0.25);
      ctx.fillRect(x + 4 * u, y + 4 * u, w - 8 * u, h - 8 * u);
    }

    /* ── desks and agents ───────────────────────────────────────────────── */

    function seatDirection(desk) {
      const cx = desk.x + desk.w / 2;
      const cy = desk.y + desk.h / 2;
      const dx = desk.seat.x + 0.5 - cx;
      const dy = desk.seat.y + 0.5 - cy;
      if (Math.abs(dx) > Math.abs(dy)) return dx > 0 ? 'right' : 'left';
      return dy > 0 ? 'down' : 'up';
    }

    function deskFacing(desk) {
      const declared = String(desk.facing || '').toLowerCase();
      if (['up', 'down', 'left', 'right'].includes(declared)) return declared;
      const direction = seatDirection(desk);
      return direction === 'down' ? 'up'
        : direction === 'up' ? 'down'
          : direction === 'right' ? 'left' : 'right';
    }

    function stateColor(agentState, accent) {
      if (agentState === 'working' || agentState === 'reading'
          || agentState === 'testing' || agentState === 'reviewing') {
        return accent;
      }
      if (agentState === 'completed') return STATUS_TONE.completed;
      if (agentState === 'failed') return STATUS_TONE.failed;
      if (agentState === 'awaiting' || agentState === 'blocked' || agentState === 'walking') {
        return STATUS_TONE.awaiting;
      }
      if (agentState === 'unknown') return STATUS_TONE.unknown;
      return mix(accent, '#ffffff', 0.2);
    }

    function drawChairShape(ctx, cx, baseY, u, facing, tint) {
      const side = facing === 'left' ? 1 : facing === 'right' ? -1 : 0;
      ctx.fillStyle = rgba('#000000', 0.28);
      ctx.fillRect(cx - 5 * u, baseY - 2 * u, 10 * u, 2 * u);
      ctx.fillStyle = '#394556';
      ctx.fillRect(cx - 4 * u, baseY - 5 * u, 8 * u, 3 * u);
      ctx.fillStyle = mix('#55647b', tint, 0.08);
      ctx.fillRect(cx - 4 * u, baseY - 6 * u, 8 * u, u);
      if (side) {
        ctx.fillStyle = '#465466';
        ctx.fillRect(cx + side * 3 * u, baseY - 8 * u, 2 * u, 6 * u);
      } else if (facing === 'down') {
        ctx.fillStyle = '#465466';
        ctx.fillRect(cx - 4 * u, baseY - 9 * u, 8 * u, 2 * u);
      } else {
        ctx.fillStyle = '#465466';
        ctx.fillRect(cx - 4 * u, baseY - 2 * u, 8 * u, 2 * u);
      }
    }

    function drawDesk(ctx, desk, alpha) {
      const tile = grid.tile;
      const u = tile / 16;
      const x = desk.x * tile;
      const y = desk.y * tile;
      const w = desk.w * tile;
      const h = desk.h * tile;
      const info = grid.roomInfo(desk.x, desk.y);
      const tint = info ? tintFor(info) : theme.edge;
      const facing = deskFacing(desk);
      const direction = seatDirection(desk);
      const occupant = [...state.agents.values()].find((agent) => agent.deskId === desk.id);
      const active = occupant ? occupantState(occupant) : null;
      const screenColor = active
        ? stateColor(active, occupant ? occupant.accent : tint)
        : mix(theme.metal, '#000000', 0.2);

      ctx.save();
      ctx.globalAlpha = alpha;
      ctx.fillStyle = rgba('#000000', 0.3);
      ctx.fillRect(x + 2 * u, y + h - 2 * u, w - 4 * u, 2 * u);

      /* chair behind the agent */
      drawChairShape(ctx, (desk.seat.x + 0.5) * tile, (desk.seat.y + 1) * tile - u, u,
        facing, tint);

      /* desk top */
      ctx.fillStyle = theme.desk;
      ctx.fillRect(x + 1 * u, y + 3 * u, w - 2 * u, h - 3 * u);
      ctx.fillStyle = mix(theme.deskTop, tint, 0.08);
      ctx.fillRect(x + 1 * u, y + 3 * u, w - 2 * u, Math.max(2, 3 * u));
      ctx.fillStyle = rgba('#ffffff', 0.075);
      ctx.fillRect(x + 2 * u, y + 4 * u, w - 4 * u, 1);
      ctx.fillStyle = mix(theme.desk, '#000000', 0.3);
      ctx.fillRect(x + 4 * u, y + h - 2 * u, 2 * u, 2 * u);
      ctx.fillRect(x + w - 6 * u, y + h - 2 * u, 2 * u, 2 * u);

      /* monitor sits on the far side of the desk from the seat */
      const horizontal = direction === 'left' || direction === 'right';
      const screenW = horizontal ? 4 * u : 7 * u;
      const screenH = horizontal ? 7 * u : 4 * u;
      const monitorX = x + w / 2 - screenW / 2 + (direction === 'right' ? -w * 0.17
        : direction === 'left' ? w * 0.17 : 0);
      const monitorY = y + h / 2 - screenH / 2 + (direction === 'down' ? -h * 0.16
        : direction === 'up' ? h * 0.16 : 0);
      ctx.fillStyle = '#151a23';
      ctx.fillRect(monitorX - u, monitorY - u, screenW + 2 * u, screenH + 2 * u);
      ctx.fillStyle = '#0a0f17';
      ctx.fillRect(monitorX, monitorY, screenW, screenH);
      if (active) {
        ctx.fillStyle = rgba(screenColor, 0.28);
        ctx.fillRect(monitorX + u, monitorY + u, screenW - 2 * u, screenH - 2 * u);
        ctx.fillStyle = screenColor;
        ctx.fillRect(monitorX + 1.5 * u, monitorY + 1.5 * u,
          Math.max(1, screenW * 0.4), Math.max(1, u));
        ctx.fillStyle = rgba(screenColor, 0.72);
        ctx.fillRect(monitorX + 1.5 * u, monitorY + 2.5 * u,
          Math.max(1, screenW * 0.58), Math.max(1, u));
        ctx.fillRect(monitorX + 1.5 * u, monitorY + 3.5 * u,
          Math.max(1, screenW * 0.28), Math.max(1, u));
      } else {
        ctx.fillStyle = rgba(tint, 0.12);
        ctx.fillRect(monitorX + u, monitorY + u, screenW - 2 * u, screenH - 2 * u);
      }
      ctx.fillStyle = theme.metal;
      ctx.fillRect(x + w / 2 - u, monitorY + screenH + u, 2 * u, 2 * u);

      /* keyboard + mug + paper */
      ctx.fillStyle = '#242c38';
      ctx.fillRect(x + w / 2 - 3 * u, y + h / 2 + (direction === 'up' ? -4 * u : 2 * u),
        6 * u, 2 * u);
      ctx.fillStyle = '#d8dee6';
      ctx.fillRect(x + w - 5 * u, y + 5 * u, 2 * u, 2 * u);
      ctx.fillStyle = '#c98d63';
      ctx.fillRect(x + 3 * u, y + 6 * u, 2 * u, 2 * u);
      ctx.fillStyle = tint;
      ctx.fillRect(x + w - 4 * u, y + 7 * u, 1, 1);
      ctx.restore();
    }

    function occupantState(agent) {
      if (!agent) return null;
      if (agent.path && agent.path.length) return 'walking';
      return agent.derived ? agent.derived.state : 'unknown';
    }

    function drawAgent(ctx, agent, nowMs) {
      const tile = grid.tile;
      const sc = tile / 16;
      const display = displayState(agent, nowMs);
      const spawn = agent.spawnUntil > nowMs
        ? clamp(1 - (agent.spawnUntil - nowMs) / (SPAWN_SECONDS * 1000), 0, 1) : 1;
      const settle = agent.settleAt
        ? clamp((nowMs - agent.settleAt) / 1000 / SETTLE_SECONDS, 0, 1) : 1;
      const yOffset = (state.reducedMotion ? 0 : -(1 - easeOut(settle)) * 2);
      const filt = state.roomFilter;
      const dim = filt && agent.room !== filt;
      let alpha = dim ? 0.24 : 1;
      if (state.reducedMotion) alpha *= spawn >= 1 ? 1 : 0.65 + spawn * 0.35;
      else alpha *= spawn;

      ctx.save();
      ctx.globalAlpha = alpha;
      if (agent.path && agent.path.length && !state.reducedMotion) {
        ctx.fillStyle = rgba('#000000', 0.28);
        ctx.fillRect(agent.x - 5 * sc, agent.y - 1.5 * sc, 10 * sc, 2 * sc);
      }
      if (agent.key === state.selectedKey || agent.key === state.hoverKey) {
        const color = agent.key === state.selectedKey ? agent.accent : '#d9e6f5';
        ctx.fillStyle = color;
        const ring = 7 * sc;
        ctx.fillRect(agent.x - ring, agent.y - 1.5 * sc, 3 * sc, sc);
        ctx.fillRect(agent.x + ring - 3 * sc, agent.y - 1.5 * sc, 3 * sc, sc);
        ctx.fillRect(agent.x - ring - sc, agent.y - 4 * sc, sc, 3 * sc);
        ctx.fillRect(agent.x + ring, agent.y - 4 * sc, sc, 3 * sc);
      }
      const scale = state.reducedMotion ? sc : sc * (0.82 + 0.18 * spawn);
      Sprites.draw(ctx, {
        x: agent.x,
        y: agent.y + yOffset + (state.reducedMotion ? 0 : -(1 - easeOut(spawn)) * 4),
        scale,
        skin: agent.variant,
        state: display,
        frame: state.reducedMotion ? 0 : agent.frameTick,
        accent: agent.accent,
        facing: agent.facing,
        seated: Boolean(agent.desk),
        warning: agent.derived && agent.derived.warning === 'start-only',
      });
      ctx.restore();
    }

    function displayState(agent, nowMs) {
      if (agent.spawnUntil > nowMs) return 'spawn';
      if (agent.path && agent.path.length) return 'walking';
      return agent.derived ? agent.derived.state : 'unknown';
    }

    function easeOut(value) {
      const t = clamp(value, 0, 1);
      return 1 - (1 - t) * (1 - t);
    }

    /* ── layout and agents ──────────────────────────────────────────────── */

    function setLayout(nextLayout) {
      if (!nextLayout || typeof nextLayout !== 'object') return false;
      let nextGrid;
      try {
        nextGrid = Tilemap.build(nextLayout);
      } catch (error) {
        state.lastError = error instanceof Error ? error.message : String(error);
        return false;
      }
      layout = nextLayout;
      grid = nextGrid;
      state.layout = nextLayout;
      state.grid = nextGrid;
      state.lastError = null;
      theme = themeFor(nextLayout.theme || nextLayout.roomTheme || 'ai');
      staticKey = '';
      buildStatic();
      updateCameraMetrics();
      if (!state.hasFit) {
        resetView();
        state.hasFit = true;
      } else {
        clampCamera();
      }
      if (state.agents.size) {
        for (const agent of state.agents.values()) {
          const tileX = Math.floor(agent.x / grid.tile);
          const tileY = Math.floor(agent.y / grid.tile);
          if (!grid.isWalkable(tileX, tileY)) {
            const near = Pathfinding.nearestWalkable(grid, { x: tileX, y: tileY });
            if (near) {
              const center = Tilemap.worldCenter(near.x, near.y, grid.tile);
              agent.x = center.x;
              agent.y = center.y;
              agent.tile = near;
            }
          }
        }
        applyAgents(state.entries, true);
      } else if (state.pendingEntries.length) {
        applyAgents(state.pendingEntries, false);
      }
      if (state.editing) renderNow();
      return true;
    }

    function roomSpot(roomId, reserved) {
      if (!grid) return null;
      const room = roomForId(roomId);
      if (!room) return null;
      const centerX = room.x + room.w / 2;
      const centerY = room.y + room.h / 2;
      const cells = [];
      for (let y = room.y + 1; y < room.y + room.h - 1; y += 1) {
        for (let x = room.x + 1; x < room.x + room.w - 1; x += 1) {
          if (!grid.isWalkable(x, y)) continue;
          if (grid.seatAt(x, y)) continue;
          cells.push({
            x,
            y,
            distance: Math.abs(x + 0.5 - centerX) + Math.abs(y + 0.5 - centerY),
            reserved: Boolean(reserved && reserved.has(`${x},${y}`)),
          });
        }
      }
      if (!cells.length) return null;
      const free = cells.filter((cell) => !cell.reserved);
      const candidates = free.length ? free : cells;
      const claimed = [];
      for (const cell of cells) {
        if (cell.reserved) claimed.push(cell);
      }
      if (claimed.length) {
        for (const cell of candidates) {
          let spread = Infinity;
          for (const used of claimed) {
            spread = Math.min(spread, Math.abs(cell.x - used.x) + Math.abs(cell.y - used.y));
          }
          cell.spread = spread;
        }
        candidates.sort((a, b) => (b.spread - a.spread)
          || (a.distance - b.distance)
          || (a.y - b.y)
          || (a.x - b.x));
      } else {
        candidates.sort((a, b) => (a.distance - b.distance) || (a.y - b.y) || (a.x - b.x));
      }
      const picked = candidates[0];
      return picked ? { x: picked.x, y: picked.y } : null;
    }

    function chooseDesk(roomId, desiredId, occupied) {
      if (!grid) return null;
      const candidates = StateMachine.deskCandidates(roomId, layout);
      if (!candidates.length) return null;
      let start = 0;
      if (desiredId) {
        const index = candidates.findIndex((desk) => desk.id === desiredId);
        if (index >= 0) start = index;
      }
      for (let offset = 0; offset < candidates.length; offset += 1) {
        const desk = candidates[(start + offset) % candidates.length];
        if (!occupied.has(desk.id)) return desk;
      }
      return null;
    }

    function targetTileFor(agent, desk, roomId, reserved) {
      if (desk) {
        const seat = desk.seat;
        if (grid.isWalkable(seat.x, seat.y)) return { x: seat.x, y: seat.y };
        const near = Pathfinding.nearestWalkable(grid, seat);
        if (near) return near;
      }
      const spot = roomSpot(roomId, reserved);
      if (spot) return spot;
      if (grid.spawn) return { ...grid.spawn };
      return Pathfinding.nearestWalkable(grid, {
        x: Math.floor(agent.x / grid.tile),
        y: Math.floor(agent.y / grid.tile),
      });
    }

    function planRoute(agent, goal, force, avoid) {
      if (!grid || !goal) return;
      const same = agent.goalTile && agent.goalTile.x === goal.x && agent.goalTile.y === goal.y;
      if (!force && same && agent.path && agent.path.length) {
        agent.goalTile = { x: goal.x, y: goal.y };
        return;
      }
      if (!force && same && !agent.path) {
        const target = Tilemap.worldCenter(goal.x, goal.y, grid.tile);
        if (Math.abs(agent.x - target.x) < 0.6 && Math.abs(agent.y - target.y) < 0.6) {
          agent.goalTile = { x: goal.x, y: goal.y };
          return;
        }
      }
      const tile = {
        x: clamp(Math.floor(agent.x / grid.tile), 0, grid.width - 1),
        y: clamp(Math.floor(agent.y / grid.tile), 0, grid.height - 1),
      };
      const start = grid.isWalkable(tile.x, tile.y)
        ? tile : Pathfinding.nearestWalkable(grid, tile);
      if (!start) return;
      const routeGrid = avoid && avoid.size
        ? {
          width: grid.width,
          height: grid.height,
          isWalkable(x, y) {
            if (!grid.isWalkable(x, y)) return false;
            if (x === start.x && y === start.y) return true;
            if (x === goal.x && y === goal.y) return true;
            return !avoid.has(`${x},${y}`);
          },
        }
        : grid;
      let path = Pathfinding.findPath(routeGrid, start, goal);
      if (!path) {
        /* A reserved seat may seal the only short corridor. Never abandon a
         * reachable target because of a reservation: retry on the raw grid and
         * let the local separation rule handle the encounter. */
        const near = Pathfinding.nearestWalkable(grid, goal);
        path = Pathfinding.findPath(grid, start, near || goal);
      }
      if (!path) {
        agent.path = null;
        agent.pathIndex = 0;
        agent.goalTile = { x: goal.x, y: goal.y };
        agent.facing = agent.facing || 'down';
        return;
      }
      agent.path = path;
      agent.pathIndex = 0;
      agent.goalTile = { x: goal.x, y: goal.y };
      agent.waiting = false;
    }

    function applyAgents(entries, force) {
      if (!grid) {
        state.pendingEntries = Array.isArray(entries) ? entries.slice() : [];
        return [];
      }
      const list = Array.isArray(entries) ? entries
        : (entries && Array.isArray(entries.agents)
          ? [...entries.agents, ...(Array.isArray(entries.unlinked_sessions) ? entries.unlinked_sessions : [])]
          : []);
      state.entries = list.slice();
      const spawnable = StateMachine.spawnable(list);
      const incoming = new Map();
      for (const entry of spawnable) incoming.set(StateMachine.dedupeKey(entry), entry);

      let hoverCleared = false;
      for (const key of [...state.agents.keys()]) {
        if (!incoming.has(key)) {
          state.agents.delete(key);
          if (state.selectedKey === key) state.selectedKey = null;
          if (state.hoverKey === key) {
            state.hoverKey = null;
            hoverCleared = true;
          }
        }
      }
      if (hoverCleared) {
        updateCursor();
        safeCall(options.onHover, null, null);
      }

      const nowMs = moment();
      const occupied = new Set();
      const reserved = new Set();
      const orderedKeys = [
        ...[...state.agents.keys()].filter((key) => incoming.has(key)),
        ...[...incoming.keys()].filter((key) => !state.agents.has(key)),
      ];

      for (const key of orderedKeys) {
        const entry = incoming.get(key);
        if (!entry) continue;
        let agent = state.agents.get(key);
        const previous = agent ? agent.derived : null;
        const previousRoom = agent ? agent.room : null;
        const derived = StateMachine.derive(entry, previous, layout);
        if (!agent) {
          const spawnTile = grid.spawn || Pathfinding.nearestWalkable(grid, {
            x: Math.floor(grid.width / 2), y: Math.floor(grid.height / 2),
          }) || { x: 1, y: 1 };
          const center = Tilemap.worldCenter(spawnTile.x, spawnTile.y, grid.tile);
          const jitter = hash(key);
          agent = {
            key,
            entry,
            derived,
            x: center.x + (((jitter % 7) - 3) / 10) * grid.tile,
            y: center.y + ((((jitter >>> 3) % 7) - 3) / 10) * grid.tile,
            tile: { x: spawnTile.x, y: spawnTile.y },
            room: derived.room,
            deskId: null,
            desk: null,
            goalTile: null,
            path: null,
            pathIndex: 0,
            facing: 'down',
            accent: accentFor(entry, key),
            variant: Sprites.variantFor(key),
            name: entryName(entry),
            bornAt: nowMs,
            spawnUntil: state.reducedMotion ? nowMs
              : nowMs + SPAWN_SECONDS * 1000 + (hash(key) % 6) * 110,
            settleAt: 0,
            frameTick: 0,
            anim: (hash(key) % 100) / 40,
            waiting: false,
            statusLabel: rawEntry(entry).status_label || rawEntry(entry).status || 'unknown',
          };
          state.agents.set(key, agent);
        } else {
          agent.entry = entry;
          agent.derived = derived;
          agent.room = derived.room;
          agent.accent = accentFor(entry, key);
          agent.name = entryName(entry);
          agent.statusLabel = rawEntry(entry).status_label || rawEntry(entry).status || 'unknown';
        }

        let desk = null;
        if (derived.deskId) {
          const desired = StateMachine.deskCandidates(derived.room, layout)
            .find((item) => item.id === derived.deskId) || null;
          if (desired && !occupied.has(desired.id)) desk = desired;
          else desk = chooseDesk(derived.room, derived.deskId, occupied);
        } else {
          desk = chooseDesk(derived.room, null, occupied);
        }
        if (desk) occupied.add(desk.id);
        agent.desk = desk || null;
        agent.deskId = desk ? desk.id : null;

        const goal = targetTileFor(agent, desk, derived.room, reserved);
        if (goal) reserved.add(`${goal.x},${goal.y}`);
        agent.nextGoal = goal ? { x: goal.x, y: goal.y } : null;

        const previousGoal = agent.goalTile;
        const changed = force || !previousGoal || (goal
          && (previousGoal.x !== goal.x || previousGoal.y !== goal.y));
        const roomChanged = previousRoom !== derived.room;
        agent.planNeeded = Boolean(goal) && Boolean(changed || roomChanged || !agent.path);
        if (agent.desk) {
          agent.facing = deskFacing(agent.desk);
        }
      }

      /* Reserve every goal before routing, then let each A* route avoid the
       * other agents' reserved tiles. This stops a late walker from choosing a
       * short-cut through a seat that is already occupied. */
      const avoid = new Set();
      for (const agent of state.agents.values()) {
        if (agent.nextGoal) avoid.add(`${agent.nextGoal.x},${agent.nextGoal.y}`);
        if (!agent.path) {
          const tx = clamp(Math.floor(agent.x / grid.tile), 0, grid.width - 1);
          const ty = clamp(Math.floor(agent.y / grid.tile), 0, grid.height - 1);
          avoid.add(`${tx},${ty}`);
        }
      }
      for (const agent of state.agents.values()) {
        if (agent.nextGoal) {
          if (agent.planNeeded) {
            planRoute(agent, agent.nextGoal, force, avoid);
          } else {
            agent.goalTile = { x: agent.nextGoal.x, y: agent.nextGoal.y };
          }
        } else {
          agent.path = null;
          agent.pathIndex = 0;
          agent.goalTile = null;
        }
        agent.planNeeded = false;
        agent.nextGoal = null;
      }

      /* remove agents that were not ordered (defensive) */
      for (const key of [...state.agents.keys()]) {
        if (!incoming.has(key)) state.agents.delete(key);
      }
      return [...state.agents.values()].map((agent) => ({
        key: agent.key,
        room: agent.room,
        deskId: agent.deskId,
        state: agent.derived ? agent.derived.state : 'unknown',
      }));
    }

    function syncAgents(entries) {
      return applyAgents(entries, false);
    }

    function rawEntry(entry) {
      if (!entry || typeof entry !== 'object') return {};
      if (entry.kind === 'agent' && entry.agent && typeof entry.agent === 'object') return entry.agent;
      if (entry.kind === 'session' && entry.session && typeof entry.session === 'object') return entry.session;
      if (entry.kind === 'managed' && entry.managed && typeof entry.managed === 'object') return entry.managed;
      return entry;
    }

    function entryName(entry) {
      const raw = rawEntry(entry);
      return raw.name || raw.agent_id || raw.agent_definition_id || 'session';
    }

    /* ── simulation ─────────────────────────────────────────────────────── */

    function occupancyByTile() {
      const map = new Map();
      for (const agent of state.agents.values()) {
        const tx = clamp(Math.floor(agent.x / grid.tile), 0, grid.width - 1);
        const ty = clamp(Math.floor(agent.y / grid.tile), 0, grid.height - 1);
        const key = `${tx},${ty}`;
        if (!map.has(key)) map.set(key, []);
        map.get(key).push(agent);
      }
      return map;
    }

    function shouldWait(agent, nextTile, occupancy) {
      const key = `${nextTile.x},${nextTile.y}`;
      const others = occupancy.get(key);
      if (!others) return false;
      for (const other of others) {
        if (other === agent) continue;
        const otherMoving = Boolean(other.path && other.path.length);
        if (!otherMoving) return true;
        /* Two movers: the lexicographically smaller key has priority. */
        if (agent.key > other.key) return true;
      }
      return false;
    }

    function updateAgent(agent, dt, nowMs, occupancy) {
      if (!state.reducedMotion) {
        agent.anim += dt;
        agent.frameTick = Math.floor(agent.anim * 8);
      } else {
        agent.frameTick = 0;
      }
      if (nowMs < agent.spawnUntil) return;
      if (!agent.path || !agent.path.length) {
        if (agent.path && !agent.path.length) agent.path = null;
        return;
      }
      if (!grid) return;
      let remaining = SPEED_TILES * grid.tile * dt;
      let guard = 0;
      while (remaining > 0.0001 && agent.path && guard < 64) {
        guard += 1;
        const tile = agent.path[agent.pathIndex];
        if (!tile) {
          agent.path = null;
          agent.pathIndex = 0;
          break;
        }
        const target = Tilemap.worldCenter(tile.x, tile.y, grid.tile);
        const dx = target.x - agent.x;
        const dy = target.y - agent.y;
        const distance = Math.abs(dx) + Math.abs(dy);
        if (distance < 0.01) {
          agent.pathIndex += 1;
          if (agent.pathIndex >= agent.path.length) {
            finishWalk(agent, nowMs);
            break;
          }
          continue;
        }
        const currentTile = {
          x: clamp(Math.floor(agent.x / grid.tile), 0, grid.width - 1),
          y: clamp(Math.floor(agent.y / grid.tile), 0, grid.height - 1),
        };
        const entering = currentTile.x !== tile.x || currentTile.y !== tile.y;
        if (entering && shouldWait(agent, tile, occupancy)) {
          agent.waiting = true;
          break;
        }
        agent.waiting = false;
        const step = Math.min(remaining, distance);
        if (Math.abs(dx) > 0.0001) {
          const move = Math.min(step, Math.abs(dx));
          agent.x += Math.sign(dx) * move;
          remaining -= move;
          agent.facing = dx > 0 ? 'right' : 'left';
        } else {
          const move = Math.min(step, Math.abs(dy));
          agent.y += Math.sign(dy) * move;
          remaining -= move;
          agent.facing = dy > 0 ? 'down' : 'up';
        }
        if (Math.abs(target.x - agent.x) < 0.01 && Math.abs(target.y - agent.y) < 0.01) {
          agent.x = target.x;
          agent.y = target.y;
          agent.pathIndex += 1;
          if (agent.pathIndex >= agent.path.length) {
            finishWalk(agent, nowMs);
            break;
          }
        }
      }
      agent.tile = {
        x: clamp(Math.floor(agent.x / grid.tile), 0, grid.width - 1),
        y: clamp(Math.floor(agent.y / grid.tile), 0, grid.height - 1),
      };
    }

    function finishWalk(agent, nowMs) {
      agent.path = null;
      agent.pathIndex = 0;
      agent.waiting = false;
      agent.settleAt = nowMs;
      if (agent.desk) agent.facing = deskFacing(agent.desk);
      else if (agent.goalTile) {
        const current = {
          x: Math.floor(agent.x / grid.tile),
          y: Math.floor(agent.y / grid.tile),
        };
        if (current.x !== agent.goalTile.x) agent.facing = current.x < agent.goalTile.x ? 'right' : 'left';
        else if (current.y !== agent.goalTile.y) agent.facing = current.y < agent.goalTile.y ? 'down' : 'up';
      }
      agent.tile = {
        x: clamp(Math.floor(agent.x / grid.tile), 0, grid.width - 1),
        y: clamp(Math.floor(agent.y / grid.tile), 0, grid.height - 1),
      };
    }

    function update(dt, nowMs) {
      if (!grid) return;
      const occupancy = occupancyByTile();
      for (const agent of state.agents.values()) updateAgent(agent, dt, nowMs, occupancy);
    }

    /* ── camera ─────────────────────────────────────────────────────────── */

    function mapSize() {
      return grid
        ? { width: grid.width * grid.tile, height: grid.height * grid.tile }
        : { width: 0, height: 0 };
    }

    function updateCameraMetrics() {
      const map = mapSize();
      const view = state.view;
      const usableWidth = Math.max(1, view.width - CAMERA_MARGIN);
      const usableHeight = Math.max(1, view.height - CAMERA_MARGIN);
      const fit = map.width > 0 && map.height > 0
        ? Math.min(usableWidth / map.width, usableHeight / map.height)
        : 1;
      /* Fit the whole floor. Fractional scales are intentional: all scenery is
       * drawn as vector rectangles, so filling the stage stays crisp while an
       * integer-only scale would leave most of the stage empty. */
      state.baseScale = Math.max(1, Math.min(2.5, Math.round(fit * 100) / 100));
      state.maxZoom = Math.max(1, Math.min(MAX_ZOOM, Math.floor(12 / state.baseScale)));
      state.zoom = clamp(state.zoom, 1, state.maxZoom);
      state.scale = state.baseScale * state.zoom;
      updateOffsets();
    }

    function updateOffsets() {
      const view = state.view;
      state.offsetX = view.width / 2 - state.centerX * state.scale;
      state.offsetY = view.height / 2 - state.centerY * state.scale;
    }

    function clampCamera() {
      if (!grid || state.view.width < 1 || state.view.height < 1) {
        const map = mapSize();
        state.centerX = map.width / 2;
        state.centerY = map.height / 2;
        state.scale = state.baseScale * state.zoom;
        updateOffsets();
        return;
      }
      const map = mapSize();
      const view = state.view;
      const scale = state.baseScale * state.zoom;
      const halfW = view.width / (2 * scale);
      const halfH = view.height / (2 * scale);
      const marginW = CAMERA_MARGIN / scale;
      const marginH = CAMERA_MARGIN / scale;
      if (map.width * scale <= view.width) {
        state.centerX = map.width / 2;
      } else {
        state.centerX = clamp(state.centerX, halfW - marginW, map.width - halfW + marginW);
      }
      if (map.height * scale <= view.height) {
        state.centerY = map.height / 2;
      } else {
        state.centerY = clamp(state.centerY, halfH - marginH, map.height - halfH + marginH);
      }
      state.scale = scale;
      updateOffsets();
    }

    function resetView() {
      const map = mapSize();
      state.zoom = 1;
      state.centerX = map.width / 2;
      state.centerY = map.height / 2;
      updateCameraMetrics();
      clampCamera();
    }

    function zoomAt(nextZoom, screenX, screenY) {
      if (!grid) return;
      const before = screenToWorld(screenX, screenY);
      const zoom = clamp(Math.round(nextZoom), 1, state.maxZoom);
      if (zoom === state.zoom) return;
      state.zoom = zoom;
      state.scale = state.baseScale * state.zoom;
      state.centerX = before.x - (screenX - state.view.width / 2) / state.scale;
      state.centerY = before.y - (screenY - state.view.height / 2) / state.scale;
      clampCamera();
      if (state.editing) renderNow();
    }

    function zoomBy(delta, screenX = state.view.width / 2, screenY = state.view.height / 2) {
      zoomAt(state.zoom + delta, screenX, screenY);
    }

    function panBy(dx, dy) {
      if (!grid) return;
      state.centerX -= dx / state.scale;
      state.centerY -= dy / state.scale;
      clampCamera();
    }

    function screenToWorld(sx, sy) {
      return {
        x: (sx - state.offsetX) / state.scale,
        y: (sy - state.offsetY) / state.scale,
      };
    }

    function worldToScreen(wx, wy) {
      return {
        x: wx * state.scale + state.offsetX,
        y: wy * state.scale + state.offsetY,
      };
    }

    function measure() {
      let width = 0;
      let height = 0;
      if (typeof canvas.getBoundingClientRect === 'function') {
        const rect = canvas.getBoundingClientRect();
        width = rect.width;
        height = rect.height;
      }
      if (!width) width = canvas.clientWidth || 0;
      if (!height) height = canvas.clientHeight || 0;
      const dpr = Math.max(1, Math.min(4, Number(root.devicePixelRatio) || 1));
      const cssWidth = Math.max(0, Math.round(width));
      const cssHeight = Math.max(0, Math.round(height));
      state.view.width = cssWidth;
      state.view.height = cssHeight;
      state.view.dpr = dpr;
      if (cssWidth > 0 && cssHeight > 0) {
        const pixelWidth = Math.max(1, Math.round(cssWidth * dpr));
        const pixelHeight = Math.max(1, Math.round(cssHeight * dpr));
        if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
        if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
      }
    }

    function resize() {
      const previous = { ...state.view };
      measure();
      if (state.hasFit
          && previous.width === state.view.width
          && previous.height === state.view.height
          && previous.dpr === state.view.dpr) {
        return;
      }
      updateCameraMetrics();
      if (!state.hasFit) {
        resetView();
        state.hasFit = true;
      } else {
        clampCamera();
      }
      if (state.destroyed) return;
      if (state.view.width > 0) {
        renderNow();
        startLoop();
      }
    }

    function renderNow() {
      if (state.destroyed || !grid || state.view.width < 1 || state.view.height < 1) return;
      draw(now());
    }

    /* ── rendering ──────────────────────────────────────────────────────── */

    function draw(nowMs) {
      if (state.destroyed || !grid || state.view.width < 1 || state.view.height < 1) return;
      const view = state.view;
      const dpr = view.dpr;
      const scale = state.scale;
      const offsetX = state.offsetX;
      const offsetY = state.offsetY;
      const map = mapSize();
      const glc = gl;

      glc.setTransform(dpr, 0, 0, dpr, 0, 0);
      glc.fillStyle = theme.outer;
      glc.fillRect(0, 0, view.width, view.height);
      const gradient = glc.createRadialGradient(
        view.width / 2, view.height * 0.35, 0,
        view.width / 2, view.height * 0.35, Math.max(view.width, view.height) * 0.75,
      );
      gradient.addColorStop(0, rgba(mix(theme.outer, theme.edge, 0.55), 0.85));
      gradient.addColorStop(1, rgba(theme.outer, 0));
      glc.fillStyle = gradient;
      glc.fillRect(0, 0, view.width, view.height);

      /* map frame */
      glc.fillStyle = rgba(theme.edge, 0.8);
      glc.fillRect(offsetX - 3, offsetY - 3, map.width * scale + 6, map.height * scale + 6);
      glc.fillStyle = theme.outer;
      glc.fillRect(offsetX - 1, offsetY - 1, map.width * scale + 2, map.height * scale + 2);

      glc.setTransform(dpr * scale, 0, 0, dpr * scale, dpr * offsetX, dpr * offsetY);
      glc.imageSmoothingEnabled = false;
      if (staticCanvas) glc.drawImage(staticCanvas, 0, 0);

      if (state.roomFilter) drawRoomFilterOutline(glc);

      const items = [];
      for (const furniture of grid.furniture) {
        if (/^(rug|carpet|mat|floor)$/i.test(furniture.type)) continue;
        items.push({
          base: (furniture.y + furniture.h) * grid.tile,
          order: 0,
          kind: 'furniture',
          item: furniture,
        });
      }
      for (const desk of grid.desks) {
        items.push({
          base: (desk.y + desk.h) * grid.tile - 0.2,
          order: 1,
          kind: 'desk',
          item: desk,
        });
      }
      for (const agent of state.agents.values()) {
        items.push({ base: agent.y, order: 2, kind: 'agent', item: agent });
      }
      items.sort((a, b) => (a.base - b.base) || (a.order - b.order)
        || String(a.item.id || a.item.key).localeCompare(String(b.item.id || b.item.key)));

      for (const item of items) {
        if (item.kind === 'agent') {
          drawAgent(glc, item.item, nowMs);
        } else if (item.kind === 'desk') {
          const desk = item.item;
          const dim = state.roomFilter && state.roomFilter !== (desk.room || roomAtPx(desk.x, desk.y));
          drawDesk(glc, desk, dim ? 0.28 : 1);
        } else {
          const furniture = item.item;
          const info = grid.roomInfo(furniture.x, furniture.y);
          const dim = state.roomFilter && info && info.id !== state.roomFilter;
          drawFurniture(glc, furniture, dim || (state.roomFilter && !info) ? 0.26 : 1);
        }
      }

      if (state.editing) drawEditOverlay(glc);

      glc.setTransform(dpr, 0, 0, dpr, 0, 0);
      drawRoomLabels(glc);
      state.drawnFrames += 1;
    }

    function roomAtPx(x, y) {
      if (!grid) return null;
      return grid.roomAt(x, y);
    }

    function drawRoomFilterOutline(ctx) {
      const room = roomForId(state.roomFilter);
      if (!room) return;
      const tile = grid.tile;
      const tint = tintFor(room);
      ctx.fillStyle = rgba(tint, 0.9);
      const x = room.x * tile;
      const y = room.y * tile;
      const w = room.w * tile;
      const h = room.h * tile;
      ctx.fillRect(x, y, w, 2);
      ctx.fillRect(x, y + h - 2, w, 2);
      ctx.fillRect(x, y, 2, h);
      ctx.fillRect(x + w - 2, y, 2, h);
    }

    function drawEditOverlay(ctx) {
      const tile = grid.tile;
      const width = grid.width * tile;
      const height = grid.height * tile;
      ctx.fillStyle = rgba(theme.text, 0.032);
      for (let x = 1; x < grid.width; x += 1) {
        ctx.fillRect(x * tile, 0, 1, height);
      }
      for (let y = 1; y < grid.height; y += 1) {
        ctx.fillRect(0, y * tile, width, 1);
      }
      for (const room of grid.rooms) {
        const tint = tintFor(room);
        ctx.fillStyle = rgba(tint, 0.3);
        const x = room.x * tile;
        const y = room.y * tile;
        const w = room.w * tile;
        const h = room.h * tile;
        ctx.fillRect(x, y, w, 1);
        ctx.fillRect(x, y + h - 1, w, 1);
        ctx.fillRect(x, y, 1, h);
        ctx.fillRect(x + w - 1, y, 1, h);
      }
      for (const desk of grid.desks) {
        const tint = tintFor(roomForId(desk.room) || grid.roomInfo(desk.x, desk.y));
        ctx.fillStyle = rgba(tint, 0.35);
        ctx.fillRect(desk.x * tile + Math.floor(tile / 2) - 1,
          desk.y * tile + Math.floor(tile / 2) - 1, 2, 2);
        ctx.fillStyle = rgba(theme.text, 0.4);
        ctx.fillRect(desk.seat.x * tile + Math.floor(tile / 2) - 1,
          desk.seat.y * tile + Math.floor(tile / 2) - 1, 2, 2);
      }
    }

    function drawRoomLabels(ctx) {
      if (!grid) return;
      const filter = state.roomFilter;
      for (const room of grid.rooms) {
        const labelX = (room.x + 1) * grid.tile + 6;
        const labelY = (room.y + 1) * grid.tile + 6;
        const screen = worldToScreen(labelX, labelY);
        const roomWidth = room.w * grid.tile * state.scale;
        if (roomWidth < 56) continue;
        if (screen.x < -180 || screen.y < -40
            || screen.x > state.view.width + 40 || screen.y > state.view.height + 40) continue;
        const dim = filter && filter !== room.id;
        const tint = tintFor(room);
        const name = String(room.name || room.id).toUpperCase();
        ctx.font = '600 9px ui-monospace, SFMono-Regular, Menlo, monospace';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        const measured = ctx.measureText(name).width;
        /* A slightly wider plate reads as a room header and covers furniture
         * that happens to sit next to the label anchor. */
        const barWidth = Math.min(Math.max(measured + 56, 92), Math.max(92, roomWidth - 8));
        ctx.fillStyle = rgba(theme.outer, dim ? 0.24 : 0.66);
        ctx.fillRect(screen.x - 4, screen.y - 3, barWidth, 13);
        ctx.fillStyle = rgba(tint, dim ? 0.18 : 0.42);
        ctx.fillRect(screen.x - 4, screen.y - 3, 2, 13);
        ctx.fillStyle = dim ? rgba(theme.label, 0.24) : rgba(theme.label, 0.9);
        ctx.fillText(name, screen.x, screen.y);
      }
    }

    /* ── interaction ────────────────────────────────────────────────────── */

    function pointerPosition(event) {
      const rect = canvas.getBoundingClientRect();
      return {
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
        inside: event.clientX >= rect.left && event.clientX <= rect.right
          && event.clientY >= rect.top && event.clientY <= rect.bottom,
      };
    }

    function hitAgent(screenX, screenY) {
      const world = screenToWorld(screenX, screenY);
      const sc = grid ? grid.tile / 16 : 1;
      const timestamp = moment();
      const candidates = [...state.agents.values()].sort((a, b) => b.y - a.y);
      for (const agent of candidates) {
        if (agent.spawnUntil > timestamp) continue;
        const box = Sprites.hitBox({ x: agent.x, y: agent.y, scale: sc });
        if (world.x >= box.x && world.x <= box.x + box.w
            && world.y >= box.y && world.y <= box.y + box.h) {
          return agent;
        }
      }
      return null;
    }

    function hitRoom(screenX, screenY) {
      if (!grid) return { id: null, room: null };
      const world = screenToWorld(screenX, screenY);
      const x = Math.floor(world.x / grid.tile);
      const y = Math.floor(world.y / grid.tile);
      if (!grid.inBounds(x, y)) return { id: null, room: null };
      const room = grid.roomInfo(x, y);
      return { id: room ? room.id : null, room };
    }

    function agentInfo(agent, screenX, screenY, event) {
      const wrapper = agent.entry || {};
      const entry = rawEntry(wrapper);
      const sc = grid ? grid.tile / 16 : 1;
      const head = worldToScreen(agent.x, agent.y - 22 * sc);
      return {
        type: 'agent',
        key: agent.key,
        name: agent.name,
        status: entry.status || wrapper.status || (agent.derived ? agent.derived.state : 'unknown'),
        statusLabel: entry.status_label || wrapper.statusLabel || agent.statusLabel || 'unknown',
        state: displayState(agent, moment()),
        room: agent.room,
        roomName: roomNameFor(agent.room),
        deskId: agent.deskId,
        warning: agent.derived ? agent.derived.warning : null,
        agentId: entry.agent_definition_id || entry.agent_id || null,
        runId: entry.run_id || (entry.current && entry.current.run_id) || null,
        nativeHash: entry.native_hash || (entry.current && entry.current.native_hash) || null,
        entry: wrapper,
        x: head.x,
        y: head.y,
        pointerX: screenX,
        pointerY: screenY,
        clientX: event ? event.clientX : undefined,
        clientY: event ? event.clientY : undefined,
      };
    }

    function stagePoint(event) {
      if (!event) return null;
      let rect = canvas.getBoundingClientRect();
      const parent = canvas.parentElement;
      if (parent && typeof parent.getBoundingClientRect === 'function') {
        const parentRect = parent.getBoundingClientRect();
        if (parentRect.width > 0 && parentRect.height > 0) rect = parentRect;
      }
      return {
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
        clientX: event.clientX,
        clientY: event.clientY,
      };
    }

    function emitHover(agent, screenX, screenY, event) {
      if (!agent) {
        if (state.hoverKey !== null) {
          state.hoverKey = null;
          updateCursor();
          safeCall(options.onHover, null, null);
        }
        return;
      }
      const info = agentInfo(agent, screenX, screenY, event);
      const changed = state.hoverKey !== agent.key
        || !state.hoverLast
        || Math.abs(state.hoverLast.x - info.x) > 2
        || Math.abs(state.hoverLast.y - info.y) > 2;
      state.hoverKey = agent.key;
      state.hoverLast = { x: info.x, y: info.y };
      updateCursor();
      if (changed) safeCall(options.onHover, info, stagePoint(event));
    }

    function updateCursor() {
      if (!canvas.style) return;
      if (state.editing) canvas.style.cursor = 'crosshair';
      else if (drag && drag.moved) canvas.style.cursor = 'grabbing';
      else if (state.hoverKey) canvas.style.cursor = 'pointer';
      else canvas.style.cursor = 'grab';
    }

    function handleClick(event) {
      const point = pointerPosition(event);
      if (!point.inside) return;
      const agent = hitAgent(point.x, point.y);
      if (agent) {
        state.selectedKey = agent.key;
        updateCursor();
        safeCall(options.onSelectAgent, agent.entry, agentInfo(agent, point.x, point.y, event));
        return;
      }
      state.selectedKey = null;
      const hit = hitRoom(point.x, point.y);
      safeCall(options.onSelectRoom, hit.id, hit.room);
    }

    function onPointerDown(event) {
      if (state.destroyed || state.editing) return;
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      if (typeof canvas.setPointerCapture === 'function') {
        try { canvas.setPointerCapture(event.pointerId); } catch (error) { /* optional */ }
      }
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const distance = Math.hypot(a.x - b.x, a.y - b.y);
        pinch = { distance: Math.max(1, distance), zoom: state.zoom, id: event.pointerId };
        drag = null;
        return;
      }
      if (pointers.size === 1) {
        drag = {
          id: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          lastX: event.clientX,
          lastY: event.clientY,
          moved: false,
        };
        updateCursor();
      }
    }

    function onPointerMove(event) {
      if (state.destroyed || state.editing) return;
      if (pointers.has(event.pointerId)) {
        pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      }
      if (pointers.size >= 2 && pinch) {
        const [a, b] = [...pointers.values()];
        const distance = Math.max(1, Math.hypot(a.x - b.x, a.y - b.y));
        const rect = canvas.getBoundingClientRect();
        const midX = ((a.x + b.x) / 2) - rect.left;
        const midY = ((a.y + b.y) / 2) - rect.top;
        const next = clamp(Math.round(pinch.zoom * (distance / pinch.distance)), 1, state.maxZoom);
        if (next !== state.zoom) zoomAt(next, midX, midY);
        return;
      }
      if (drag && drag.id === event.pointerId) {
        const dx = event.clientX - drag.lastX;
        const dy = event.clientY - drag.lastY;
        if (!drag.moved && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) > 4) {
          drag.moved = true;
          updateCursor();
        }
        if (drag.moved) {
          panBy(dx, dy);
          drag.lastX = event.clientX;
          drag.lastY = event.clientY;
          if (state.hoverKey !== null) {
            state.hoverKey = null;
            safeCall(options.onHover, null);
          }
          return;
        }
      }
      const point = pointerPosition(event);
      if (!point.inside) {
        if (state.hoverKey !== null) emitHover(null, point.x, point.y, event);
        return;
      }
      emitHover(hitAgent(point.x, point.y), point.x, point.y, event);
    }

    function onPointerUp(event) {
      if (state.destroyed || state.editing) return;
      const wasDrag = drag && drag.id === event.pointerId ? drag : null;
      pointers.delete(event.pointerId);
      if (pointers.size < 2) pinch = null;
      if (wasDrag) {
        if (!wasDrag.moved && event.type !== 'pointercancel') handleClick(event);
        drag = null;
        updateCursor();
      }
    }

    function onPointerLeave(event) {
      if (state.destroyed) return;
      if (!drag && !pointers.size) {
        emitHover(null, 0, 0, event);
        updateCursor();
      }
    }

    function onWheel(event) {
      if (state.destroyed || !grid) return;
      event.preventDefault();
      const point = pointerPosition(event);
      const direction = event.deltaY < 0 ? 1 : -1;
      zoomBy(direction, point.x, point.y);
    }

    function onDoubleClick(event) {
      if (state.destroyed) return;
      event.preventDefault();
      resetView();
    }

    function onKeyDown(event) {
      if (state.destroyed || !grid) return;
      if (event.key === '+' || event.key === '=') {
        zoomBy(1);
        event.preventDefault();
      } else if (event.key === '-' || event.key === '_') {
        zoomBy(-1);
        event.preventDefault();
      } else if (event.key === '0') {
        resetView();
        event.preventDefault();
      }
    }

    function onVisibilityChange() {
      if (state.destroyed || !doc) return;
      state.visible = !doc.hidden;
      if (!state.visible) {
        if (state.raf && typeof root.cancelAnimationFrame === 'function') {
          root.cancelAnimationFrame(state.raf);
        }
        state.raf = 0;
        state.fps = 0;
      } else {
        state.lastNow = now();
        if (state.editing) renderNow();
        else startLoop();
      }
    }

    let resizeObserver = null;

    function startLoop() {
      if (state.destroyed || state.editing || state.raf) return;
      if (doc && doc.hidden) return;
      if (typeof root.requestAnimationFrame !== 'function') return;
      state.lastNow = now();
      state.raf = root.requestAnimationFrame(tick);
    }

    function tick(timestamp) {
      state.raf = 0;
      if (state.destroyed) return;
      if (state.editing) return;
      if (doc && doc.hidden) {
        state.visible = false;
        state.fps = 0;
        return;
      }
      state.visible = true;
      const current = Number.isFinite(timestamp) ? timestamp : now();
      state.clock = current;
      const raw = Math.max(0, (current - state.lastNow) / 1000);
      state.lastNow = current;
      const dt = Math.min(0.1, raw);
      if (state.view.width > 0 && state.view.height > 0 && grid) {
        update(dt, current);
        draw(current);
      }
      state.frame += 1;
      state.fpsAccum += raw;
      state.fpsFrames += 1;
      if (state.fpsAccum >= 0.5) {
        state.fps = state.fpsFrames / Math.max(0.001, state.fpsAccum);
        state.fpsAccum = 0;
        state.fpsFrames = 0;
      }
      if (!state.destroyed) state.raf = root.requestAnimationFrame(tick);
    }

    function addListeners() {
      canvas.addEventListener('pointerdown', onPointerDown);
      canvas.addEventListener('pointermove', onPointerMove);
      canvas.addEventListener('pointerup', onPointerUp);
      canvas.addEventListener('pointercancel', onPointerUp);
      canvas.addEventListener('pointerleave', onPointerLeave);
      canvas.addEventListener('wheel', onWheel, { passive: false });
      canvas.addEventListener('dblclick', onDoubleClick);
      canvas.addEventListener('keydown', onKeyDown);
      if (canvas.style) canvas.style.touchAction = 'none';
      if (doc && typeof doc.addEventListener === 'function') {
        doc.addEventListener('visibilitychange', onVisibilityChange);
      }
      const ResizeObserverCtor = root.ResizeObserver;
      if (typeof ResizeObserverCtor === 'function') {
        resizeObserver = new ResizeObserverCtor(() => resize());
        try { resizeObserver.observe(canvas); } catch (error) { resizeObserver = null; }
      }
      if (typeof root.addEventListener === 'function') {
        root.addEventListener('resize', resize);
      }
    }

    function removeListeners() {
      canvas.removeEventListener('pointerdown', onPointerDown);
      canvas.removeEventListener('pointermove', onPointerMove);
      canvas.removeEventListener('pointerup', onPointerUp);
      canvas.removeEventListener('pointercancel', onPointerUp);
      canvas.removeEventListener('pointerleave', onPointerLeave);
      canvas.removeEventListener('wheel', onWheel);
      canvas.removeEventListener('dblclick', onDoubleClick);
      canvas.removeEventListener('keydown', onKeyDown);
      if (canvas.style) {
        canvas.style.touchAction = '';
        canvas.style.cursor = '';
      }
      if (doc && typeof doc.removeEventListener === 'function') {
        doc.removeEventListener('visibilitychange', onVisibilityChange);
      }
      if (resizeObserver) {
        try { resizeObserver.disconnect(); } catch (error) { /* optional */ }
        resizeObserver = null;
      }
      if (typeof root.removeEventListener === 'function') {
        root.removeEventListener('resize', resize);
      }
    }

    function getCamera() {
      return {
        zoom: state.zoom,
        baseScale: state.baseScale,
        scale: state.scale,
        x: state.offsetX,
        y: state.offsetY,
        offsetX: state.offsetX,
        offsetY: state.offsetY,
        centerX: state.centerX,
        centerY: state.centerY,
        tilePx: grid ? grid.tile * state.baseScale : 0,
        tileSize: grid ? grid.tile * state.baseScale : 0,
        viewWidth: state.view.width,
        viewHeight: state.view.height,
        dpr: state.view.dpr,
      };
    }

    function getAgentPositions() {
      return [...state.agents.values()].map((agent) => ({
        key: agent.key,
        agentId: agent.entry
          ? (agent.entry.agent_definition_id || agent.entry.agent_id || null) : null,
        room: agent.room,
        deskId: agent.deskId,
        state: displayState(agent, moment()),
        evidenceState: agent.derived ? agent.derived.state : 'unknown',
        x: agent.x,
        y: agent.y,
        tileX: Math.floor(agent.x / (grid ? grid.tile : 16)),
        tileY: Math.floor(agent.y / (grid ? grid.tile : 16)),
        walking: Boolean(agent.path && agent.path.length),
      }));
    }

    function getStats() {
      const memory = root.performance && root.performance.memory
        && Number.isFinite(root.performance.memory.usedJSHeapSize)
        ? root.performance.memory.usedJSHeapSize : null;
      return {
        fps: Math.round(state.fps * 10) / 10,
        agents: state.agents.size,
        drawnFrames: state.drawnFrames,
        frames: state.frame,
        memory: memory,
        memoryMB: memory ? Math.round((memory / 1048576) * 10) / 10 : null,
        visible: state.visible,
        reducedMotion: state.reducedMotion,
        roomFilter: state.roomFilter,
        editing: state.editing,
        zoom: state.zoom,
        scale: state.scale,
        width: state.view.width,
        height: state.view.height,
        dpr: state.view.dpr,
        layoutError: state.lastError,
      };
    }

    function setRoomFilter(room) {
      let next = typeof room === 'string' && room.trim() && room !== 'all'
        ? room.trim().toLowerCase() : null;
      if (next && grid) {
        const match = grid.rooms.find((item) => item.id.toLowerCase() === next
          || String(item.department || '').toLowerCase() === next);
        next = match ? match.id : null;
      }
      state.roomFilter = next;
      staticKey = '';
      buildStatic();
      if (state.view.width > 0) draw(now());
      return state.roomFilter;
    }

    function editMode(enabled) {
      const next = Boolean(enabled);
      if (next === state.editing) {
        if (next) renderNow();
        return state.editing;
      }
      state.editing = next;
      if (state.editing) {
        if (state.raf && typeof root.cancelAnimationFrame === 'function') {
          root.cancelAnimationFrame(state.raf);
        }
        state.raf = 0;
        renderNow();
      } else {
        state.lastNow = now();
        renderNow();
        startLoop();
      }
      updateCursor();
      return state.editing;
    }

    function saveLayout() {
      const projectId = state.layout && (state.layout.project_id || state.layout.projectId
        || options.projectId);
      const layouts = root.AOOfficeLayouts;
      if (layouts && projectId && typeof layouts.save === 'function') {
        try {
          return Boolean(layouts.save(projectId, layout));
        } catch (error) {
          state.lastError = error instanceof Error ? error.message : String(error);
          return false;
        }
      }
      if (typeof options.onLayoutChange === 'function') {
        safeCall(options.onLayoutChange, layout, { reason: 'save' });
        return true;
      }
      return false;
    }

    function resetLayout() {
      const projectId = state.layout && (state.layout.project_id || state.layout.projectId
        || options.projectId);
      const layouts = root.AOOfficeLayouts;
      if (layouts && projectId) {
        let next = null;
        try {
          if (typeof layouts.reset === 'function') layouts.reset(projectId);
          if (typeof layouts.load === 'function') next = layouts.load(projectId, layout && layout.theme);
        } catch (error) {
          state.lastError = error instanceof Error ? error.message : String(error);
          return false;
        }
        if (next && setLayout(next)) {
          safeCall(options.onLayoutChange, layout, { reason: 'reset' });
          return true;
        }
      }
      if (typeof options.onLayoutChange === 'function') {
        safeCall(options.onLayoutChange, layout, { reason: 'reset' });
        return true;
      }
      return false;
    }

    function destroy() {
      if (state.destroyed) return;
      state.destroyed = true;
      if (state.raf && typeof root.cancelAnimationFrame === 'function') {
        root.cancelAnimationFrame(state.raf);
      }
      state.raf = 0;
      removeListeners();
      state.agents.clear();
      pointers.clear();
      drag = null;
      pinch = null;
      staticCanvas = null;
      staticKey = '';
    }

    const controller = {
      setLayout,
      syncAgents,
      setReducedMotion(value) {
        const next = Boolean(value);
        if (next === state.reducedMotion) return state.reducedMotion;
        state.reducedMotion = next;
        if (state.reducedMotion) {
          for (const agent of state.agents.values()) {
            agent.frameTick = 0;
            if (agent.spawnUntil > now()) agent.spawnUntil = now();
          }
        }
        if (state.editing) renderNow();
        return state.reducedMotion;
      },
      resize,
      zoomIn() { zoomBy(1); return state.zoom; },
      zoomOut() { zoomBy(-1); return state.zoom; },
      resetView() { resetView(); if (state.editing) renderNow(); return getCamera(); },
      setRoomFilter,
      editMode,
      saveLayout,
      resetLayout,
      destroy,
      getCamera,
      getAgentPositions,
      getStats,
      /* Compose-friendly extras; the layout editor uses these. */
      getLayout() { return layout; },
      getGrid() { return grid; },
      /* CSS pixels relative to the canvas, matching AOLayoutEditor. */
      screenToTile(px, py) {
        if (!grid) return null;
        const world = screenToWorld(Number(px) || 0, Number(py) || 0);
        return {
          x: Math.floor(world.x / grid.tile),
          y: Math.floor(world.y / grid.tile),
          world,
        };
      },
      clientToTile(clientX, clientY) {
        const rect = canvas.getBoundingClientRect();
        return controller.screenToTile(clientX - rect.left, clientY - rect.top);
      },
    };

    addListeners();
    if (!setLayout(options.layout)) {
      removeListeners();
      throw new TypeError(`AOOfficeScene: invalid layout${state.lastError ? ` (${state.lastError})` : ''}`);
    }
    state.pendingEntries = [];
    resize();
    if (state.view.width > 0) draw(now());
    startLoop();
    return controller;
  }

  const api = {
    create,
    THEMES,
    ROOM_TINTS,
  };

  if (typeof window !== 'undefined') window.AOOfficeScene = api;
  if (typeof globalThis !== 'undefined') globalThis.AOOfficeScene = api;
})();
