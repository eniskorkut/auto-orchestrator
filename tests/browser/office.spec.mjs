import { expect, test } from '@playwright/test';
import {
  DEMO,
  agentPoint,
  clickAgent,
  demoProjectId,
  officeDebug,
  openOffice,
  roomDrag,
  selectProject,
  trackPageErrors,
  waitForSettled,
} from './helpers.mjs';

let errors = [];

test.beforeEach(async ({ page }) => {
  errors = trackPageErrors(page);
});

test.afterEach(async () => {
  expect(errors, 'browser console, page and HTTP errors').toEqual([]);
});

test('renders one continuous office canvas instead of repeated workstation cards', async ({ page }) => {
  await openOffice(page);
  await waitForSettled(page);
  await expect(page.locator('.office-canvas')).toHaveCount(1);
  await expect(page.locator('.office-shell')).toHaveCount(1);
  await expect(page.locator('.desk-card')).toHaveCount(0);
  const debug = await officeDebug(page);
  expect(debug.stats.agents).toBe(12);
  expect(debug.stats.drawnFrames).toBeGreaterThan(3);
  expect(debug.stats.layoutError).toBeNull();
  expect(debug.layout.rooms.map((room) => room.id).sort()).toEqual([
    'backend', 'frontend', 'lobby', 'meeting', 'qa', 'research', 'review',
  ]);
  const canvas = await page.locator('.office-canvas').evaluate((node) => ({
    width: node.width,
    height: node.height,
    cssWidth: node.clientWidth,
    cssHeight: node.clientHeight,
  }));
  expect(canvas.width).toBeGreaterThan(300);
  expect(canvas.height).toBeGreaterThan(300);
});

test('registry-only agents stay in the roster and never become working characters', async ({ page }) => {
  await openOffice(page, { project: DEMO.web });
  await waitForSettled(page);
  const debug = await officeDebug(page);
  const registryAgents = debug.entries.filter((entry) => entry.kind === 'agent');
  expect(registryAgents.map((entry) => entry.name)).toEqual(['Noor', 'Ravi', 'Ines']);
  const unbound = registryAgents.filter((entry) => !entry.spawnable);
  expect(unbound.map((entry) => entry.name)).toEqual(['Ravi', 'Ines']);
  expect(debug.stats.agents).toBe(debug.entries.filter((entry) => entry.spawnable).length);
  const drawnKeys = debug.positions.map((position) => position.key);
  for (const entry of unbound) {
    expect(drawnKeys, `${entry.name} must not be drawn without a session`).not.toContain(entry.key);
  }
  expect(drawnKeys).toContain(registryAgents.find((entry) => entry.name === 'Noor').key);
  await expect(page.locator('.office-agent-row')).toHaveCount(10);
  await expect(page.locator('.office-agent-row strong', { hasText: 'Ravi' })).toHaveCount(1);
});

test('rooms follow explicit task departments and the room filter scopes the roster', async ({ page }) => {
  await openOffice(page);
  await waitForSettled(page);
  const debug = await officeDebug(page);
  const byName = Object.fromEntries(debug.entries
    .filter((entry) => entry.kind === 'agent')
    .map((entry) => [entry.name, entry]));
  expect(byName.Ada.room).toBe('backend');
  expect(byName.Ada.roomSource).toBe('task.department');
  expect(byName.Grace.room).toBe('qa');
  expect(byName.Linus.room).toBe('research');
  expect(byName.Margaret.room).toBe('review');
  expect(byName.Sofia.room).toBe('frontend');

  await page.locator('.room-chip', { hasText: 'Backend Room' }).click();
  await expect(page.locator('.room-chip[aria-pressed="true"]')).toHaveText('Backend Room');
  await expect(page.locator('.office-agent-row')).toHaveCount(1);
  await expect(page.locator('.office-agent-row strong')).toHaveText('Ada');
  expect((await officeDebug(page)).stats.roomFilter).toBe('backend');

  await page.locator('.room-chip', { hasText: 'QA / Test Lab' }).click();
  await expect(page.locator('.office-agent-row strong')).toHaveText('Grace');
  expect((await officeDebug(page)).stats.roomFilter).toBe('qa');

  await page.locator('.room-chip', { hasText: 'All rooms' }).click();
  await expect(page.locator('.office-agent-row')).toHaveCount(12);
  expect((await officeDebug(page)).stats.roomFilter).toBeNull();
});

test('clicking a character opens the evidence inspector and resizes the map', async ({ page }) => {
  await openOffice(page);
  await waitForSettled(page);
  const before = await page.locator('.office-stage').evaluate((node) => node.getBoundingClientRect().width);
  await clickAgent(page, 'Ada');
  await expect(page.locator('#inspector-shell')).toBeVisible();
  await expect(page.locator('.inspector-name')).toHaveText('Ada');
  await expect(page.locator('.inspector-role')).toContainText('RAG Engineer');
  await expect(page.locator('.inspector-body')).toContainText('AI Research Lab');
  await expect(page.locator('.inspector-body')).toContainText('opencode');
  await expect(page.locator('.inspector-body')).toContainText('not attested');
  await expect(page.locator('.terminal-panel')).toContainText('not available');
  await expect(page.locator('.terminal-panel')).toContainText('never renders a simulated command stream');
  await expect(page.locator('.office-roster')).toBeHidden();
  await page.waitForFunction((initial) => {
    const stage = document.querySelector('.office-stage');
    return stage && stage.getBoundingClientRect().width < initial - 100;
  }, before);
  const after = await page.locator('.office-stage').evaluate((node) => node.getBoundingClientRect().width);
  expect(after).toBeLessThan(before - 100);

  await page.locator('#inspector-close').click();
  await expect(page.locator('#inspector-shell')).toBeHidden();
  await page.waitForFunction((initial) => {
    const stage = document.querySelector('.office-stage');
    return stage && Math.abs(stage.getBoundingClientRect().width - initial) < 3;
  }, before);
  await expect(page.locator('.office-roster')).toBeVisible();

  await clickAgent(page, 'Ada');
  await page.keyboard.press('Escape');
  await expect(page.locator('#inspector-shell')).toBeHidden();
});

test('project switching isolates layout, theme, roster and character identities', async ({ page }) => {
  await openOffice(page);
  await waitForSettled(page);
  const ai = await officeDebug(page);
  expect(ai.theme).toBe('ai');
  expect(ai.layout.id).toBe('ai-lab');
  expect(ai.layout.rooms.find((room) => room.id === 'research').x).toBe(2);
  expect(ai.entries.filter((entry) => entry.kind === 'agent').map((entry) => entry.name))
    .toEqual(['Ada', 'Grace', 'Linus', 'Margaret', 'Sofia']);

  await selectProject(page, DEMO.security);
  await page.waitForFunction(() => window.AOOffice.officeDebugState().theme === 'security');
  const security = await officeDebug(page);
  expect(security.projectId).not.toBe(ai.projectId);
  expect(security.theme).toBe('security');
  expect(security.layout.id).toBe('security-soc');
  expect(security.layout.rooms.find((room) => room.id === 'research').x).toBe(16);
  const securityNames = await page.locator('.office-agent-row strong').allTextContents();
  expect(securityNames).toEqual(expect.arrayContaining(['Mira', 'Kai', 'Tomas']));
  for (const name of ['Ada', 'Grace', 'Linus', 'Margaret', 'Sofia']) {
    expect(securityNames).not.toContain(name);
  }
  expect(security.stats.agents).toBeLessThan(ai.stats.agents);

  await selectProject(page, DEMO.ai);
  await page.waitForFunction(() => window.AOOffice.officeDebugState().theme === 'ai');
  const back = await officeDebug(page);
  expect(back.layout.id).toBe('ai-lab');
  const names = await page.locator('.office-agent-row strong').allTextContents();
  expect(names).toEqual(expect.arrayContaining(['Ada', 'Grace', 'Linus', 'Margaret', 'Sofia']));
  expect(names).not.toContain('Mira');
});

test('layout editor moves rooms and desks, saves per project, survives reload and resets', async ({ page }) => {
  const projectId = await openOffice(page, { clearLayout: false });
  await waitForSettled(page);
  await page.getByRole('button', { name: 'Edit layout' }).click();
  await expect(page.locator('.office-editbar')).toBeVisible();
  await roomDrag(page, 'research', 2, 0);
  let debug = await officeDebug(page);
  let research = debug.layout.rooms.find((room) => room.id === 'research');
  expect(research.x).toBe(4);
  expect(research.door.x).toBe(12);
  expect(debug.stats.editing).toBe(true);

  // Doors slide along the room perimeter. Drag the current door to the top wall.
  const doorTarget = { x: research.x + 5, y: research.y + 2 };
  const doorGeometry = await page.evaluate(() => {
    const debugState = window.AOOffice.officeDebugState();
    const room = debugState.layout.rooms.find((item) => item.id === 'research');
    const canvas = document.querySelector('.office-canvas');
    const rect = canvas.getBoundingClientRect();
    const camera = debugState.scene.getCamera();
    return { room, rect: { x: rect.x, y: rect.y }, camera };
  });
  const tile = doorGeometry.camera.scale * 16;
  const point = (x, y) => ({
    x: doorGeometry.rect.x + (x + 0.5) * tile + doorGeometry.camera.x,
    y: doorGeometry.rect.y + (y + 0.5) * tile + doorGeometry.camera.y,
  });
  const doorStart = point(doorGeometry.room.door.x, doorGeometry.room.door.y);
  const doorEnd = point(doorTarget.x, doorTarget.y);
  await page.mouse.move(doorStart.x, doorStart.y);
  await page.mouse.down();
  await page.mouse.move(doorEnd.x, doorEnd.y, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(250);
  debug = await officeDebug(page);
  research = debug.layout.rooms.find((room) => room.id === 'research');
  expect(research.door.y).toBe(research.y);
  expect(research.door.x).toBe(doorTarget.x);
  expect(await page.evaluate(() => window.AOOffice.officeDebugState().editor.getError())).toBeNull();

  await page.getByRole('button', { name: 'Save layout' }).click();
  const stored = await page.evaluate((id) => JSON.parse(localStorage.getItem(`ao-office-layout:${id}`)), projectId);
  expect(stored.rooms.find((room) => room.id === 'research').x).toBe(4);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.office-canvas');
  await page.waitForFunction(() => window.AOOffice?.officeDebugState?.()?.scene?.getStats().drawnFrames > 1);
  await waitForSettled(page);
  debug = await officeDebug(page);
  research = debug.layout.rooms.find((room) => room.id === 'research');
  expect(research.x).toBe(4);
  expect(research.door.y).toBe(research.y);
  expect(research.door.x).toBe(doorTarget.x);

  await page.getByRole('button', { name: 'Edit layout' }).click();
  await page.getByRole('button', { name: 'Reset layout' }).click();
  await page.waitForTimeout(250);
  const afterReset = await page.evaluate((id) => localStorage.getItem(`ao-office-layout:${id}`), projectId);
  expect(afterReset).toBeNull();
  debug = await officeDebug(page);
  expect(debug.layout.rooms.find((room) => room.id === 'research').x).toBe(2);
});

test('reduced motion is honoured and the keyboard agent list opens the inspector', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await openOffice(page, { reducedMotion: true });
  await waitForSettled(page);
  await expect(page.locator('html')).toHaveAttribute('data-motion', 'reduced');
  let debug = await officeDebug(page);
  expect(debug.stats.reducedMotion).toBe(true);
  const frames = debug.stats.drawnFrames;
  await page.waitForTimeout(500);
  debug = await officeDebug(page);
  expect(debug.stats.drawnFrames).toBeGreaterThanOrEqual(frames);

  const row = page.locator('.office-agent-row').first();
  await row.focus();
  await expect(row).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#inspector-shell')).toBeVisible();
  await expect(page.locator('.inspector-name')).toHaveText('Ada');
  await page.keyboard.press('Escape');
  await expect(page.locator('#inspector-shell')).toBeHidden();
  await expect(row).toBeFocused();

  await page.getByRole('button', { name: 'Reduce motion' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-motion', 'full');
  expect((await officeDebug(page)).stats.reducedMotion).toBe(false);
});

test('mobile map pans and zooms without horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openOffice(page);
  await waitForSettled(page);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  const before = (await officeDebug(page)).camera;
  await page.locator('[data-office-zoom="in"]').click();
  await page.waitForTimeout(200);
  const zoomed = (await officeDebug(page)).camera;
  expect(zoomed.zoom).toBeGreaterThan(before.zoom);
  expect(zoomed.scale).toBeGreaterThan(before.scale);

  const canvas = page.locator('.office-canvas');
  const box = await canvas.boundingBox();
  const x = box.x + box.width / 2;
  const y = box.y + Math.min(box.height - 40, 150);
  const panBefore = (await officeDebug(page)).camera;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x - 90, y - 30, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(200);
  const panAfter = (await officeDebug(page)).camera;
  expect(Math.abs(panAfter.offsetX - panBefore.offsetX) + Math.abs(panAfter.offsetY - panBefore.offsetY))
    .toBeGreaterThan(20);

  await page.locator('[data-office-zoom="reset"]').click();
  await page.waitForTimeout(200);
  expect((await officeDebug(page)).camera.zoom).toBe(1);
  await expect(page.locator('.office-roster')).toBeVisible();
});

test('resizing the window keeps world positions and refits the camera', async ({ page }) => {
  await openOffice(page);
  await waitForSettled(page);
  const before = await officeDebug(page);
  const positions = Object.fromEntries(before.positions.map((item) => [item.key, { x: item.x, y: item.y }]));
  await page.setViewportSize({ width: 1100, height: 760 });
  await page.waitForTimeout(500);
  const after = await officeDebug(page);
  for (const item of after.positions) {
    const original = positions[item.key];
    expect(original, `${item.key} still exists after resize`).toBeTruthy();
    expect(Math.abs(item.x - original.x)).toBeLessThan(0.01);
    expect(Math.abs(item.y - original.y)).toBeLessThan(0.01);
  }
  expect(after.camera.viewWidth).toBeLessThan(before.camera.viewWidth);
  expect(after.stats.layoutError).toBeNull();
});

test('canvas hover shows an agent tooltip with room and status', async ({ page }) => {
  await openOffice(page);
  await waitForSettled(page);
  const point = await agentPoint(page, 'Ada');
  await page.mouse.move(point.x, point.y);
  const tooltip = page.locator('.office-map-tooltip');
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText('Ada');
  await expect(tooltip).toContainText('Backend Room');
  await expect(tooltip).toContainText('Completed');
});

test('inspector stays readable at tablet and mobile widths', async ({ page }) => {
  const widths = [
    { name: '1024x768', width: 1024, height: 768 },
    { name: '840x900', width: 840, height: 900 },
    { name: '768x1024', width: 768, height: 1024 },
    { name: '390x844', width: 390, height: 844 },
  ];
  for (const viewport of widths) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await openOffice(page, { clearLayout: true, reducedMotion: true });
    await waitForSettled(page);

    await page.locator('.office-agent-row').first().click();
    await expect(page.locator('#inspector-shell')).toBeVisible();
    await expect(page.locator('.inspector-name')).not.toHaveText('');
    await expect(page.locator('.inspector-field dd').first()).toBeVisible();
    const name = (await page.locator('.inspector-name').textContent())?.trim() ?? '';
    expect(name.length, `${viewport.name}: inspector name is non-empty`).toBeGreaterThan(0);

    const fields = page.locator('.inspector-field dd');
    const fieldCount = await fields.count();
    expect(fieldCount, `${viewport.name}: inspector renders fields`).toBeGreaterThan(0);
    let measured = 0;
    for (let index = 0; index < fieldCount; index += 1) {
      const field = fields.nth(index);
      if (!(await field.isVisible())) continue;
      const box = await field.boundingBox();
      expect(box, `${viewport.name}: field ${index} has a box`).toBeTruthy();
      expect(box.width, `${viewport.name}: field ${index} value width`).toBeGreaterThanOrEqual(80);
      measured += 1;
    }
    expect(measured, `${viewport.name}: inspected visible fields`).toBeGreaterThan(0);

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, `${viewport.name}: horizontal overflow`).toBeLessThanOrEqual(1);

    if (viewport.width <= 840) {
      await expect(page.locator('.inspector-pinned')).toBeHidden();
      const content = await page.locator('.inspector-content').boundingBox();
      expect(content, `${viewport.name}: content box`).toBeTruthy();
      expect(content.width, `${viewport.name}: drawer uses full width`).toBeGreaterThanOrEqual(viewport.width - 40);
    }

    await page.locator('#inspector-close').click();
    await expect(page.locator('#inspector-shell')).toBeHidden();
  }
});

const VIEWPORTS = [
  { name: '1440x900', width: 1440, height: 900 },
  { name: '1280x800', width: 1280, height: 800 },
  { name: '768x1024', width: 768, height: 1024 },
  { name: '390x844', width: 390, height: 844 },
];

for (const viewport of VIEWPORTS) {
  test(`visual office snapshot at ${viewport.name}`, async ({ page }) => {
    test.skip(process.env.AO_SKIP_VISUALS === '1', 'visual baselines are macOS Chrome references; CI runs functional E2E');
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await openOffice(page, { reducedMotion: true });
    await waitForSettled(page);
    const canvas = page.locator('.office-canvas');
    await expect(canvas).toHaveScreenshot(`canvas-${viewport.name}.png`, {
      animations: 'disabled',
      maxDiffPixelRatio: 0.02,
      threshold: 0.2,
    });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(120);
    await page.screenshot({
      path: `tests/browser/screenshots/office-${viewport.name}.png`,
      fullPage: viewport.width <= 768,
      animations: 'disabled',
    });
  });
}
