import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const PORT = Number(process.env.AO_TEST_PORT || 8799);
const BASE_URL = `http://127.0.0.1:${PORT}`;
const ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PYTHON = process.env.AO_PYTHON || 'python3.11';
const BROWSER_CHANNEL = process.env.AO_BROWSER_CHANNEL === 'none'
  ? undefined
  : (process.env.AO_BROWSER_CHANNEL || 'chrome');

export default defineConfig({
  testDir: '.',
  testMatch: '**/*.spec.mjs',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 75_000,
  expect: { timeout: 15_000 },
  reporter: [['list']],
  snapshotPathTemplate: '{testDir}/screenshots/{arg}{ext}',
  use: {
    baseURL: BASE_URL,
    ...(BROWSER_CHANNEL ? { channel: BROWSER_CHANNEL } : {}),
    headless: true,
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chrome', use: { ...devices['Desktop Chrome'], ...(BROWSER_CHANNEL ? { channel: BROWSER_CHANNEL } : {}), viewport: { width: 1440, height: 900 } } },
  ],
  webServer: {
    command: `${PYTHON} tools/dashboard.py --demo --port ${PORT}`,
    url: `${BASE_URL}/`,
    cwd: ROOT,
    timeout: 30_000,
    reuseExistingServer: true,
    stdout: 'ignore',
    stderr: 'pipe',
  },
});
