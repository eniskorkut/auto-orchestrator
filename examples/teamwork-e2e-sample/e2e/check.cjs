#!/usr/bin/env node
"use strict";

// Browser end-to-end check for the task app.
//
// Playwright is supplied through NODE_PATH (it is not a project dependency):
//
//   NODE_PATH=/path/to/node_modules node e2e/check.cjs
//
// The script starts the backend on an ephemeral port, drives the real UI in
// Chromium, and verifies both the DOM and the API state. Environment overrides:
//   AO_PYTHON             Python executable (default: python3.11)
//   AO_BACKEND            Backend script path (default: backend/app.py)
//   AO_HEADLESS           "0" to watch the browser (default: headless)
//   AO_CHROMIUM_EXECUTABLE  Use a specific Chromium binary instead of the one
//                           bundled with the installed Playwright version

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");

let chromium;
try {
  ({ chromium } = require("playwright"));
} catch (error) {
  console.error(
    "Failed to load playwright. Expose it through NODE_PATH, for example:\n" +
      "  NODE_PATH=/path/to/node_modules node e2e/check.cjs"
  );
  console.error(error.message);
  process.exit(2);
}

const ROOT = path.resolve(__dirname, "..");
const PYTHON = process.env.AO_PYTHON || "python3.11";
const BACKEND = process.env.AO_BACKEND || path.join(ROOT, "backend", "app.py");
const HEADLESS = process.env.AO_HEADLESS !== "0";
const START_TIMEOUT_MS = 15000;

function startBackend() {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, [BACKEND, "--port", "0", "--print-port"], {
      cwd: ROOT,
      stdio: ["ignore", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";
    let settled = false;

    const timer = setTimeout(() => {
      finish(
        new Error(
          `Backend did not print a port within ${START_TIMEOUT_MS}ms.\n` +
            `stdout:\n${stdout}\nstderr:\n${stderr}`
        )
      );
    }, START_TIMEOUT_MS);

    function finish(error, value) {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);

      if (error) {
        child.kill("SIGKILL");
        reject(error);
        return;
      }

      resolve({ child, port: value, stderr: () => stderr });
    }

    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");

    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      const match = stdout.match(/^\s*(?:PORT\s*[=:]\s*)?(\d{1,5})\s*$/im);
      if (match) {
        finish(null, Number(match[1]));
      }
    });

    child.stderr.on("data", (chunk) => {
      stderr = (stderr + chunk).slice(-4000);
    });

    child.on("error", (error) => {
      finish(new Error(`Could not start ${PYTHON} ${BACKEND}: ${error.message}`));
    });

    child.on("exit", (code, signal) => {
      finish(
        new Error(
          `Backend exited before reporting a port (code ${code}, signal ${signal}).\n` +
            `stdout:\n${stdout}\nstderr:\n${stderr}`
        )
      );
    });
  });
}

async function stopBackend(backend) {
  if (!backend || !backend.child || backend.child.exitCode !== null) {
    return;
  }

  const exited = new Promise((resolve) => backend.child.once("exit", resolve));
  backend.child.kill("SIGTERM");

  const forceKill = setTimeout(() => backend.child.kill("SIGKILL"), 5000);
  await exited;
  clearTimeout(forceKill);
}

async function fetchTasks(origin) {
  const response = await fetch(origin + "/api/tasks", {
    headers: { Accept: "application/json" },
  });
  assert.equal(response.status, 200, "GET /api/tasks should return 200");
  const payload = await response.json();
  assert.ok(Array.isArray(payload.tasks), "GET /api/tasks should return a tasks array");
  return payload.tasks;
}

async function run() {
  const backend = await startBackend();
  const origin = "http://127.0.0.1:" + backend.port;
  let browser;

  try {
    const launchOptions = { headless: HEADLESS };
    if (process.env.AO_CHROMIUM_EXECUTABLE) {
      launchOptions.executablePath = process.env.AO_CHROMIUM_EXECUTABLE;
    }
    browser = await chromium.launch(launchOptions);
    const page = await browser.newPage();
    const pageErrors = [];

    page.on("pageerror", (error) => pageErrors.push(error.message));

    await page.goto(origin + "/", { waitUntil: "load" });

    assert.equal(
      await page.evaluate(() => window.AO_API_BASE),
      "",
      "config.js should expose an empty default AO_API_BASE"
    );
    console.log("ok - config.js exposes window.AO_API_BASE");

    await page.locator("#empty-state").waitFor({ state: "visible" });
    assert.equal(
      await page.locator("#task-list .task").count(),
      0,
      "the task list should start empty"
    );
    console.log("ok - empty task list renders");

    const title = "E2E task " + Date.now();
    await page.fill("#task-title", title);
    await page.click('#task-form button[type="submit"]');

    const task = page.locator("#task-list .task", { hasText: title });
    await task.waitFor({ state: "visible" });
    assert.equal(
      (await task.locator(".task-title-text").innerText()).trim(),
      title,
      "the created task should be listed"
    );
    assert.equal(
      await page.inputValue("#task-title"),
      "",
      "the input should clear after a successful create"
    );
    await page.locator("#empty-state").waitFor({ state: "hidden" });

    let tasks = await fetchTasks(origin);
    assert.equal(tasks.length, 1, "the created task should be persisted by the API");
    assert.equal(tasks[0].title, title);
    assert.equal(tasks[0].done, false);
    console.log("ok - creates a task through the UI and API");

    await page.fill("#task-title", "   ");
    await page.click('#task-form button[type="submit"]');

    const formError = page.locator("#form-error");
    await formError.waitFor({ state: "visible" });
    assert.ok(
      (await formError.innerText()).trim().length > 0,
      "an empty title should show a validation error"
    );
    assert.equal(
      await page.locator("#task-list .task").count(),
      1,
      "a rejected submit must not add a task"
    );
    assert.equal(
      (await fetchTasks(origin)).length,
      1,
      "a rejected submit must not create a task in the API"
    );
    console.log("ok - rejects an empty title and shows the error");

    const toggle = task.locator(".task-toggle");
    assert.equal(await toggle.isChecked(), false, "new tasks start unchecked");
    await toggle.check();
    await page
      .locator("#task-list .task.is-done", { hasText: title })
      .waitFor({ state: "visible" });
    assert.equal(await toggle.isChecked(), true, "the checkbox should be checked");

    tasks = await fetchTasks(origin);
    assert.equal(tasks[0].done, true, "PATCH should mark the task done");
    console.log("ok - toggles a task to done");

    await page.reload({ waitUntil: "load" });
    const reloadedTask = page.locator("#task-list .task", { hasText: title });
    await reloadedTask.waitFor({ state: "visible" });
    assert.equal(
      await reloadedTask.locator(".task-toggle").isChecked(),
      true,
      "a reload should render the persisted done state"
    );
    assert.ok(
      (await reloadedTask.getAttribute("class")).split(/\s+/).includes("is-done"),
      "a done task should carry the is-done class"
    );
    console.log("ok - reload renders the done task from the API");

    await reloadedTask.locator(".task-toggle").uncheck();
    await page
      .locator("#task-list .task:not(.is-done)", { hasText: title })
      .waitFor({ state: "visible" });
    assert.equal(
      await reloadedTask.locator(".task-toggle").isChecked(),
      false,
      "the checkbox should be unchecked again"
    );
    tasks = await fetchTasks(origin);
    assert.equal(tasks[0].done, false, "PATCH should clear the done flag");
    console.log("ok - toggles a task back to open");

    assert.deepEqual(pageErrors, [], "the page should not raise uncaught errors");
  } finally {
    if (browser) {
      await browser.close();
    }
    await stopBackend(backend);
  }
}

run().then(
  () => {
    console.log("browser E2E passed");
  },
  (error) => {
    console.error("browser E2E failed");
    console.error(error && error.stack ? error.stack : error);
    process.exitCode = 1;
  }
);
