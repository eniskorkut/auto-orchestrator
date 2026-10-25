/* Auto-Orchestrator V7.2 — deterministic 4-direction A* for the office map.
 *
 * Movement is cardinal only, so a path can never clip a diagonal wall.
 * The open-set comparator is (f, h, y, x, insertion): ties are stable and
 * therefore reproducible for tests, screenshots and agent choreography.
 */
(() => {
  'use strict';

  const DIRECTIONS = [
    { x: 0, y: -1 }, // north
    { x: 1, y: 0 },  // east
    { x: 0, y: 1 },  // south
    { x: -1, y: 0 }, // west
  ];

  function validGrid(grid) {
    return Boolean(grid) && Number.isInteger(grid.width) && Number.isInteger(grid.height)
      && grid.width > 0 && grid.height > 0
      && typeof grid.isWalkable === 'function';
  }

  function tileValue(tile) {
    if (!tile || typeof tile !== 'object') return null;
    if (!Number.isInteger(tile.x) || !Number.isInteger(tile.y)) return null;
    return { x: tile.x, y: tile.y };
  }

  function heuristic(ax, ay, bx, by) {
    return Math.abs(ax - bx) + Math.abs(ay - by);
  }

  /* Binary min-heap keyed by (f, h, y, x, serial). */
  function createHeap() {
    const items = [];
    let serial = 0;

    function less(a, b) {
      if (a.f !== b.f) return a.f < b.f;
      if (a.h !== b.h) return a.h < b.h;
      if (a.y !== b.y) return a.y < b.y;
      if (a.x !== b.x) return a.x < b.x;
      return a.serial < b.serial;
    }

    function swap(i, j) {
      const tmp = items[i];
      items[i] = items[j];
      items[j] = tmp;
    }

    return {
      get size() { return items.length; },
      push(node) {
        node.serial = serial;
        serial += 1;
        items.push(node);
        let index = items.length - 1;
        while (index > 0) {
          const parent = (index - 1) >> 1;
          if (!less(items[index], items[parent])) break;
          swap(index, parent);
          index = parent;
        }
      },
      pop() {
        if (!items.length) return null;
        const top = items[0];
        const last = items.pop();
        if (items.length && last) {
          items[0] = last;
          let index = 0;
          for (;;) {
            const left = index * 2 + 1;
            const right = left + 1;
            let smallest = index;
            if (left < items.length && less(items[left], items[smallest])) smallest = left;
            if (right < items.length && less(items[right], items[smallest])) smallest = right;
            if (smallest === index) break;
            swap(index, smallest);
            index = smallest;
          }
        }
        return top;
      },
    };
  }

  function findPath(grid, startTile, goalTile) {
    if (!validGrid(grid)) return null;
    const start = tileValue(startTile);
    const goal = tileValue(goalTile);
    if (!start || !goal) return null;
    const { width, height } = grid;
    if (start.x < 0 || start.y < 0 || start.x >= width || start.y >= height) return null;
    if (goal.x < 0 || goal.y < 0 || goal.x >= width || goal.y >= height) return null;
    if (!grid.isWalkable(start.x, start.y) || !grid.isWalkable(goal.x, goal.y)) return null;
    if (start.x === goal.x && start.y === goal.y) return [{ x: start.x, y: start.y }];

    const total = width * height;
    const startIndex = start.y * width + start.x;
    const goalIndex = goal.y * width + goal.x;
    const cost = new Float64Array(total);
    cost.fill(Infinity);
    const cameFrom = new Int32Array(total);
    cameFrom.fill(-1);
    const closed = new Uint8Array(total);
    const heap = createHeap();

    cost[startIndex] = 0;
    heap.push({
      x: start.x,
      y: start.y,
      index: startIndex,
      g: 0,
      f: heuristic(start.x, start.y, goal.x, goal.y),
      h: heuristic(start.x, start.y, goal.x, goal.y),
    });

    while (heap.size) {
      const current = heap.pop();
      if (!current || closed[current.index]) continue;
      if (current.index === goalIndex) {
        const path = [];
        let cursor = goalIndex;
        while (cursor !== -1) {
          path.push({ x: cursor % width, y: Math.floor(cursor / width) });
          cursor = cameFrom[cursor];
        }
        path.reverse();
        return path;
      }
      closed[current.index] = 1;
      for (const dir of DIRECTIONS) {
        const nx = current.x + dir.x;
        const ny = current.y + dir.y;
        if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue;
        const neighbour = ny * width + nx;
        if (closed[neighbour] || !grid.isWalkable(nx, ny)) continue;
        const tentative = cost[current.index] + 1;
        if (tentative >= cost[neighbour]) continue;
        cost[neighbour] = tentative;
        cameFrom[neighbour] = current.index;
        const h = heuristic(nx, ny, goal.x, goal.y);
        heap.push({
          x: nx,
          y: ny,
          index: neighbour,
          g: tentative,
          f: tentative + h,
          h,
        });
      }
    }
    return null;
  }

  function nearestWalkable(grid, tile) {
    if (!validGrid(grid)) return null;
    const target = tileValue(tile) || { x: 0, y: 0 };
    let best = null;
    for (let y = 0; y < grid.height; y += 1) {
      for (let x = 0; x < grid.width; x += 1) {
        if (!grid.isWalkable(x, y)) continue;
        const distance = Math.abs(x - target.x) + Math.abs(y - target.y);
        if (!best || distance < best.distance
            || (distance === best.distance && (y < best.y || (y === best.y && x < best.x)))) {
          best = { x, y, distance };
        }
      }
    }
    return best ? { x: best.x, y: best.y } : null;
  }

  function pathLength(path) {
    return Array.isArray(path) ? Math.max(0, path.length - 1) : 0;
  }

  function pathCost(path) {
    return pathLength(path);
  }

  const api = {
    findPath,
    nearestWalkable,
    pathLength,
    pathCost,
  };

  if (typeof window !== 'undefined') window.AOPathfinding = api;
  if (typeof globalThis !== 'undefined') globalThis.AOPathfinding = api;
})();
