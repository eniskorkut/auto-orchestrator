import { expect } from '@playwright/test';

export const DEMO = {
  ai: 'AI Research Lab',
  security: 'Security Lab',
  web: 'Web Studio',
};

/** Console/page errors are test failures unless a test opts out. */
export function trackPageErrors(page) {
  const errors = [];
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`);
  });
  page.on('response', (response) => {
    if (response.status() >= 400) errors.push(`http ${response.status()}: ${response.url()}`);
  });
  return errors;
}

export async function demoProjectId(page, name = DEMO.ai) {
  const response = await page.request.get('/api/projects');
  expect(response.ok()).toBeTruthy();
  const payload = await response.json();
  const project = (payload.projects || []).find((item) => item.name === name);
  expect(project, `demo project ${name}`).toBeTruthy();
  return project.project_id;
}

export async function openOffice(page, { project = DEMO.ai, reducedMotion = false, clearLayout = true } = {}) {
  await page.addInitScript(({ motion, clear }) => {
    try {
      if (clear) {
        for (const key of Object.keys(localStorage)) {
          if (key.startsWith('ao-office-layout:') || key.startsWith('ao-office-theme:') || key.startsWith('ao-desk-order:')) {
            localStorage.removeItem(key);
          }
        }
      }
      localStorage.setItem('ao-reduced-motion', motion ? '1' : '0');
    } catch (error) {
      /* about:blank and restricted documents have no usable storage. */
    }
  }, { motion: reducedMotion, clear: clearLayout });
  const projectId = await demoProjectId(page, project);
  await page.goto(`/#/p/${projectId}/office`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.office-canvas', { timeout: 20_000 });
  await page.waitForFunction(() => {
    const debug = window.AOOffice?.officeDebugState?.();
    return Boolean(debug?.scene) && debug.scene.getStats().drawnFrames > 1;
  });
  return projectId;
}

export async function officeDebug(page) {
  return page.evaluate(() => {
    const debug = window.AOOffice.officeDebugState();
    const scene = debug.scene;
    return {
      projectId: debug.projectId,
      theme: debug.theme,
      activeRoom: debug.activeRoom,
      layout: debug.layout,
      entries: debug.entries.map((entry) => ({
        key: entry.key,
        kind: entry.kind,
        room: entry.room,
        spawnable: entry.spawnable,
        name: entry.kind === 'agent' ? entry.agent?.name : entry.session?.name,
        status: entry.kind === 'agent' ? entry.agent?.status : entry.session?.status,
        roomSource: entry.kind === 'agent' ? entry.agent?.room_source : null,
        sessionKey: entry.kind === 'agent' ? entry.agent?.session_key : entry.session?.session_key,
      })),
      camera: scene.getCamera(),
      stats: scene.getStats(),
      positions: scene.getAgentPositions(),
    };
  });
}

/** Wait until every spawn is visible and every walk has finished. */
export async function waitForSettled(page, timeout = 20_000) {
  await page.waitForFunction(() => {
    const scene = window.AOOffice?.officeDebugState?.()?.scene;
    if (!scene) return false;
    const positions = scene.getAgentPositions();
    return positions.length > 0 && positions.every((position) => !position.walking);
  }, undefined, { timeout });
  await page.waitForTimeout(350);
}

/** Screen point (relative to the viewport) for a named agent. */
export async function agentPoint(page, name) {
  const point = await page.evaluate((agentName) => {
    const debug = window.AOOffice.officeDebugState();
    const scene = debug.scene;
    const canvas = document.querySelector('.office-canvas');
    const rect = canvas.getBoundingClientRect();
    const camera = scene.getCamera();
    const positions = scene.getAgentPositions();
    const key = debug.entries.find((entry) => entry.kind === 'agent' && entry.agent?.name === agentName)?.key;
    if (!key) return null;
    const position = positions.find((item) => item.key === key);
    if (!position) return null;
    return {
      x: rect.x + position.x * camera.scale + camera.x,
      y: rect.y + position.y * camera.scale + camera.y - 9 * camera.scale,
      room: position.room,
      key,
    };
  }, name);
  expect(point, `agent ${name} is drawn on the map`).toBeTruthy();
  return point;
}

export async function clickAgent(page, name) {
  const point = await agentPoint(page, name);
  await page.mouse.click(point.x, point.y);
  await page.waitForSelector('#inspector-shell:not([hidden])');
  return point;
}

export async function selectProject(page, name) {
  await page.selectOption('#project-select', { label: name });
  await page.waitForFunction((label) => {
    const select = document.querySelector('#project-select');
    return select?.selectedOptions?.[0]?.textContent?.trim() === label;
  }, name);
  await page.waitForFunction(() => {
    const debug = window.AOOffice?.officeDebugState?.();
    return Boolean(debug?.scene) && debug.scene.getStats().drawnFrames > 1;
  });
}

export async function roomDrag(page, roomId, dxTiles, dyTiles = 0) {
  // Select a room tile that is not a desk, desk seat or door, then move with the
  // editor's documented arrow-key path. This is deterministic across Chromium
  // builds; pointer dragging is covered by the door editor assertion.
  const point = await page.evaluate((id) => {
    const debug = window.AOOffice.officeDebugState();
    const canvas = document.querySelector('.office-canvas');
    const rect = canvas.getBoundingClientRect();
    const camera = debug.scene.getCamera();
    const layout = debug.layout;
    const room = layout.rooms.find((item) => item.id === id);
    const occupied = new Set();
    for (const desk of layout.desks || []) {
      if (desk.room !== id) continue;
      occupied.add(`${desk.x},${desk.y}`);
      if (desk.seat) occupied.add(`${desk.seat.x},${desk.seat.y}`);
    }
    let tile = null;
    for (let y = room.y + 1; y < room.y + room.h - 1 && !tile; y += 1) {
      for (let x = room.x + 1; x < room.x + room.w - 1; x += 1) {
        if (room.door && room.door.x === x && room.door.y === y) continue;
        if (occupied.has(`${x},${y}`)) continue;
        tile = { x, y };
        break;
      }
    }
    const tilePx = camera.scale * 16;
    return {
      x: rect.x + (tile.x + 0.5) * tilePx + camera.x,
      y: rect.y + (tile.y + 0.5) * tilePx + camera.y,
    };
  }, roomId);
  await page.mouse.click(point.x, point.y);
  const xKey = dxTiles >= 0 ? 'ArrowRight' : 'ArrowLeft';
  const yKey = dyTiles >= 0 ? 'ArrowDown' : 'ArrowUp';
  for (let step = 0; step < Math.abs(dxTiles); step += 1) await page.keyboard.press(xKey);
  for (let step = 0; step < Math.abs(dyTiles); step += 1) await page.keyboard.press(yKey);
  await page.waitForTimeout(250);
}
