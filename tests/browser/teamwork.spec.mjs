import { expect, test } from '@playwright/test';
import { DEMO, demoProjectId, trackPageErrors } from './helpers.mjs';

let errors = [];

test.beforeEach(async ({ page }) => {
  errors = trackPageErrors(page);
});

test.afterEach(() => {
  expect(errors, 'browser console, page and HTTP errors').toEqual([]);
});

test('milestone panel shows verified progress and filters the task board', async ({ page }) => {
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/board`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.milestone-card');
  await expect(page.locator('.milestone-card')).toHaveCount(3);
  await expect(page.locator('#board-milestones')).toContainText('DEMO DATA');
  await expect(page.locator('#board-milestones')).toContainText('1/3 verified');
  await expect(page.locator('#board-milestones')).toContainText('No estimated progress is shown');
  await expect(page.locator('#board-milestones .mode-chip', { hasText: 'BALANCED' })).toHaveCount(1);
  await expect(page.locator('#board-milestones .recovery-chip', { hasText: 'ACTIVE' })).toHaveCount(1);
  await expect(page.locator('#board-milestones .scheduler-chip')).toContainText('running');

  await page.locator('.milestone-card').filter({ hasText: 'Build and verify' }).click();
  await expect(page.locator('.milestone-card.selected')).toHaveCount(1);
  await expect(page.locator('.milestone-card.selected')).toContainText('M2');
  await expect(page.locator('.task-card')).toHaveCount(2);

  await page.getByRole('button', { name: 'Clear milestone filter' }).click();
  await expect(page.locator('.milestone-card.selected')).toHaveCount(0);
  await expect(page.locator('.task-card')).toHaveCount(5);
});

test('disconnected runner shows recovery and orphan state without working animation', async ({ page }) => {
  await page.route('**/api/projects/*/milestones', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const run = payload.runs[0];
    run.recovery_state = 'runner_disconnected';
    run.owner_connected = false;
    run.orphaned_managed_sessions = 1;
    run.scheduler = { ...(run.scheduler || {}), active_sessions: 0 };
    run.managed_sessions = [{
      task_id: 'backend', attempt: 1, provider: 'opencode', status: 'running',
      managed_status: 'orphaned', session_hash: 'orphan123', tool_count: 0,
    }];
    await route.fulfill({ response, json: payload });
  });
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/board`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.milestone-card');
  await expect(page.locator('#board-milestones .recovery-chip',
    { hasText: 'RUNNER DISCONNECTED' })).toHaveCount(1);
  await expect(page.locator('#board-milestones .orphan-chip',
    { hasText: '1 orphaned' })).toHaveCount(1);
  await expect(page.locator('#board-milestones .scheduler-chip')).toContainText('0 running');
});

test('office milestone strip opens the matching board milestone', async ({ page }) => {
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/office`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.office-milestone-strip');
  await expect(page.locator('.office-milestone-strip')).toContainText('M2');
  await expect(page.locator('.office-milestone-strip')).toContainText('1/3 verified');
  await page.locator('.office-milestone-open').click();
  await expect(page).toHaveURL(/#\/p\/.*\/board/);
  await expect(page.locator('.milestone-card.selected')).toContainText('M2');
});

test('milestone panel stays usable on a narrow screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const projectId = await demoProjectId(page, DEMO.ai);
  await page.goto(`/#/p/${projectId}/board`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.milestone-card');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await expect(page.locator('.milestone-card')).toHaveCount(3);
  await page.screenshot({ path: 'tests/browser/screenshots/office-milestones-390x844.png', fullPage: false });
});
