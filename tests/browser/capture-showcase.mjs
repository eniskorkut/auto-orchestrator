// Capture the README showcase screenshots from the real dashboard demo server.
// Usage: python3.11 tools/dashboard.py --demo --port 8791 &
//        node tests/browser/capture-showcase.mjs
import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const BASE = process.env.AO_SHOWCASE_URL || 'http://127.0.0.1:8791';
const OUT = fileURLToPath(new URL('../../docs/assets/', import.meta.url));
const browser = await chromium.launch({
  channel: process.env.AO_BROWSER_CHANNEL || 'chrome',
  headless: true,
});
const page = await browser.newPage({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  reducedMotion: 'reduce',
});

async function capture(name, path, prepare, options = {}) {
  await page.goto(`${BASE}/${path}`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.view:not(.hidden)');
  await prepare();
  await page.screenshot({ path: `${OUT}${name}`, type: 'png',
    fullPage: Boolean(options.fullPage) });
}

await mkdir(OUT, { recursive: true });

await capture('dashboard-overview.png', '#/overview', async () => {
  await page.getByText('3 projects loaded').waitFor();
});

const projects = await (await page.request.get(`${BASE}/api/projects`)).json();
const project = projects.projects.find((item) => item.name === 'AI Research Lab');
if (!project) throw new Error('demo project AI Research Lab missing');
const base = `#/p/${project.project_id}`;

await capture('teamwork-workflow.png', `${base}/board`, async () => {
  await page.getByText('TEAMWORK / MILESTONES').waitFor();
}, { fullPage: true });

await capture('verified-completion.png', `${base}/runs`, async () => {
  await page.getByRole('button', { name: /demo-verified/ }).click();
  await page.getByText('Evidence chain').waitFor();
});

await browser.close();
console.log('showcase screenshots written to', OUT);
