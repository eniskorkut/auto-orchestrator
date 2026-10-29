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
  const xSteps = Math.abs(dxTiles);
  const ySteps = Math.abs(dyTiles);
  const stepX = Math.sign(dxTiles);
  const stepY = Math.sign(dyTiles);
  // Move one tile per drag. A single multi-tile pointer move can lose its last
  // event on a slow Chromium runner, leaving the room one tile short. Alt
  // makes the editor prefer the room over a desk at the same tile.
  await page.keyboard.down('Alt');
  try {
    for (let step = 0; step < xSteps + ySteps; step += 1) {
      const geometry = await page.evaluate((id) => {
        const debug = window.AOOffice.officeDebugState();
        const canvas = document.querySelector('.office-canvas');
        const rect = canvas.getBoundingClientRect();
        const camera = debug.scene.getCamera();
        const room = debug.layout.rooms.find((item) => item.id === id);
        return {
          room: { x: room.x, y: room.y, w: room.w, h: room.h },
          camera,
          rect: { x: rect.x, y: rect.y },
          scale: camera.scale,
        };
      }, roomId);
      const tile = geometry.scale * 16;
      const startX = geometry.rect.x + (geometry.room.x + geometry.room.w / 2 + 0.5) * tile + geometry.camera.x;
      const startY = geometry.rect.y + (geometry.room.y + geometry.room.h / 2 + 0.5) * tile + geometry.camera.y;
      const moveX = step < xSteps ? stepX * tile : 0;
      const moveY = step < xSteps ? 0 : stepY * tile;
      await page.mouse.move(startX, startY);
      await page.mouse.down();
      await page.waitForTimeout(80);
      await page.mouse.move(startX + moveX, startY + moveY, { steps: 4 });
      await page.waitForTimeout(120);
      await page.mouse.up();
      await page.waitForTimeout(250);
    }
  } finally {
    await page.keyboard.up('Alt');
  }
}
