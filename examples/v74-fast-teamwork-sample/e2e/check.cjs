#!/usr/bin/env node
/**
 * Browser end-to-end check for the task manager.
 *
 * Starts the Python backend on an ephemeral port, drives the page with
 * Playwright, and asserts the contract behaviour:
 *   - the task list loads
 *   - a task can be created
 *   - an empty / whitespace-only title is rejected in the UI
 *   - a task can be toggled done, and the change survives a reload
 *
 * Playwright is provided through NODE_PATH, so run it like:
 *   NODE_PATH=/path/to/node_modules node e2e/check.cjs
 *
 * Optional environment:
 *   AO_PYTHON  interpreter to use (default: python3.11)
 *   AO_BACKEND absolute path to the backend entrypoint
 *              (default: <repo>/backend/app.py)
 */
"use strict";

const path = require("node:path");
const fs = require("node:fs");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..");
const BACKEND = process.env.AO_BACKEND || path.join(ROOT, "backend", "app.py");
const PYTHON = process.env.AO_PYTHON || "python3.11";
const BASE_TITLE = "Write the report";
const PAGE_TIMEOUT = 8000;
const START_TIMEOUT = 10000;

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitFor(check, description, timeout = PAGE_TIMEOUT) {
  const deadline = Date.now() + timeout;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const result = await check();
      if (result) {
        return result;
      }
    } catch (error) {
      lastError = error;
    }
    await wait(50);
  }
  const suffix = lastError ? ` (last error: ${lastError.message})` : "";
  throw new Error(`Timed out waiting for ${description}${suffix}`);
}

function startBackend() {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(PYTHON, [BACKEND, "--port", "0", "--print-port"], {
        cwd: ROOT,
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (error) {
      reject(new Error(`Could not start ${PYTHON}: ${error.message}`));
      return;
    }

    let stdout = "";
    let stderr = "";
    let settled = false;
    const timer = setTimeout(() => {
      finish(
        new Error(
          `Backend did not print a port within ${START_TIMEOUT}ms. ` +
            `stdout: ${JSON.stringify(stdout)} stderr: ${JSON.stringify(stderr)}`
        )
      );
    }, START_TIMEOUT);

    function finish(error, port) {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      if (error) {
        child.kill("SIGKILL");
        reject(error);
      } else {
        resolve({ child, port, getStderr: () => stderr });
      }
    }

    child.on("error", (error) => {
      finish(new Error(`Could not start ${PYTHON}: ${error.message}`));
    });

    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      const lines = stdout.split(/\r?\n/);
      for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line) {
          continue;
        }
        let match = line.match(/^\s*(\d{1,5})\s*$/);
        if (!match) {
          match = line.match(/\bport\s+(\d{1,5})\b/i);
        }
        if (match) {
          finish(null, Number(match[1]));
          return;
        }
      }
    });

    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
      if (stderr.length > 4000) {
        stderr = stderr.slice(-4000);
      }
    });

    child.on("exit", (code, signal) => {
      finish(
        new Error(
          `Backend exited before reporting a port (code=${code}, signal=${signal}). ` +
            `stderr: ${JSON.stringify(stderr)}`
        )
      );
    });
  });
}

async function waitForBackend(port) {
  const url = `http://127.0.0.1:${port}/api/tasks`;
  await waitFor(async () => {
    const response = await fetch(url);
    return response.ok;
  }, `backend readiness at ${url}`, START_TIMEOUT);
}

function stopBackend(child) {
  if (!child || child.exitCode !== null || child.signalCode) {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const killTimer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve();
    }, 3000);
    child.once("exit", () => {
      clearTimeout(killTimer);
      resolve();
    });
    child.kill("SIGTERM");
  });
}

async function taskTitles(page) {
  return page
    .locator('[data-testid="task-item"] [data-testid="task-title"]')
    .allTextContents();
}

async function main() {
  assert(
    fs.existsSync(BACKEND),
    `Backend entrypoint not found at ${BACKEND}. ` +
      "Run the backend task first or set AO_BACKEND."
  );

  const backend = await startBackend();
  const baseUrl = `http://127.0.0.1:${backend.port}`;
  const pageErrors = [];
  let browser;

  try {
    await waitForBackend(backend.port);
    browser = await chromium.launch();
    const context = await browser.newContext();
    const page = await context.newPage();
    page.setDefaultTimeout(PAGE_TIMEOUT);
    page.on("pageerror", (error) => pageErrors.push(error));

    // 1. The app boots against the configured (same-origin) API base.
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('[data-testid="empty"]', { state: "visible" });

    const apiBase = await page.evaluate(() => window.AO_API_BASE);
    assert(apiBase === "", `expected window.AO_API_BASE to be "", got ${JSON.stringify(apiBase)}`);

    assert(
      (await taskTitles(page)).length === 0,
      "expected no tasks on a fresh backend"
    );
    console.log("ok - empty state renders and AO_API_BASE defaults to same origin");

    // 2. Create a task.
    const input = page.locator('[data-testid="task-input"]');
    const submit = page.locator('[data-testid="task-submit"]');
    const items = page.locator('[data-testid="task-item"]');
    const error = page.locator('[data-testid="error"]');

    await input.fill(BASE_TITLE);
    await submit.click();
    await page.waitForSelector('[data-testid="task-item"]', { state: "visible" });

    let titles = await taskTitles(page);
    assert(titles.length === 1, `expected 1 task after create, got ${titles.length}`);
    assert(titles[0] === BASE_TITLE, `expected "${BASE_TITLE}", got "${titles[0]}"`);

    const checkbox = page.locator('[data-testid="task-checkbox"]').first();
    assert(!(await checkbox.isChecked()), "new task should start unchecked");
    assert(await error.isHidden(), "error banner should stay hidden after a valid create");
    console.log("ok - creating a task adds it to the list");

    // 3. Empty and whitespace-only titles are rejected without adding a task.
    await input.fill("");
    await submit.click();
    await error.waitFor({ state: "visible" });
    const emptyMessage = (await error.textContent()).trim();
    assert(emptyMessage.length > 0, "empty title should show a validation message");
    assert(
      await input.getAttribute("aria-invalid") === "true",
      "empty title should mark the input invalid"
    );
    assert((await items.count()) === 1, "empty title must not create a task");

    await input.fill("   ");
    await submit.click();
    await error.waitFor({ state: "visible" });
    const whitespaceMessage = (await error.textContent()).trim();
    assert(
      whitespaceMessage.length > 0,
      "whitespace-only title should show a validation message"
    );
    assert(
      (await items.count()) === 1,
      "whitespace-only title must not create a task"
    );
    console.log("ok - empty and whitespace-only titles are rejected in the UI");

    // Typing clears the previous validation error.
    await input.fill(BASE_TITLE);
    await waitFor(async () => await error.isHidden(), "validation error to clear");

    // 4. Toggle done, wait for the server round-trip, then reload.
    await checkbox.click();
    await page.waitForFunction(
      () => document.querySelector('[data-testid="task-item"]')?.dataset.done === "true",
      undefined,
      { timeout: PAGE_TIMEOUT }
    );
    await page.waitForFunction(
      () => document.querySelector('[data-testid="task-item"]')?.dataset.pending === "false",
      undefined,
      { timeout: PAGE_TIMEOUT }
    );

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector('[data-testid="task-item"]', { state: "visible" });

    const reloadedCheckbox = page.locator('[data-testid="task-checkbox"]').first();
    assert(
      await reloadedCheckbox.isChecked(),
      "toggled task should still be checked after reload"
    );
    assert(
      (await page.locator('[data-testid="task-item"]').first().getAttribute("data-done")) === "true",
      "task item should expose data-done=true after reload"
    );
    assert(
      (await taskTitles(page))[0] === BASE_TITLE,
      "task title should survive a reload"
    );
    const status = (await page.locator('[data-testid="status"]').textContent()).trim();
    assert(/1 task/.test(status) && /1 done/.test(status), `unexpected status text: "${status}"`);
    console.log("ok - toggling done persists across a reload");

    // 5. The page raised no uncaught errors along the way.
    assert(pageErrors.length === 0, `page errors: ${pageErrors.map((e) => e.message).join("; ")}`);
    console.log("ok - no uncaught page errors");

    console.log("\nPASS - browser E2E");
  } finally {
    if (browser) {
      await browser.close().catch(() => {});
    }
    await stopBackend(backend.child);
  }
}

main().catch((error) => {
  console.error(`\nFAIL - browser E2E\n${error.stack || error.message}`);
  process.exitCode = 1;
});
