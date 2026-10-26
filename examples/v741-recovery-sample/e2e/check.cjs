#!/usr/bin/env node
"use strict";

// Browser E2E check for the task manager sample.
//
// Starts the stdlib backend on an ephemeral port, drives the page with
// Playwright, and asserts the DOM plus the API state behind it.
//
// Playwright is supplied through NODE_PATH (no local install needed).

const assert = require("node:assert/strict");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { once } = require("node:events");

const { chromium } = require("playwright");

const ROOT = path.join(__dirname, "..");
const PYTHON = process.env.AO_PYTHON || "python3.11";
const START_TIMEOUT_MS = 15000;
const READY_TIMEOUT_MS = 10000;

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function startBackend() {
  const entry = path.join(ROOT, "backend", "app.py");
  const child = spawn(PYTHON, [entry, "--port", "0", "--print-port"], {
    cwd: ROOT,
    stdio: ["ignore", "pipe", "pipe"],
  });

  return new Promise((resolve, reject) => {
    let stdout = "";
    let stderr = "";
    let settled = false;

    const fail = (message) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      child.kill("SIGKILL");
      reject(new Error(message + "\nbackend stderr:\n" + stderr.trim()));
    };

    const timer = setTimeout(() => {
      fail("Backend did not report a port within " + START_TIMEOUT_MS + "ms.");
    }, START_TIMEOUT_MS);

    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");

    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      if (settled) {
        return;
      }
      const match = stdout.match(/^\s*(\d{1,5})\s*$/m);
      if (!match) {
        return;
      }
      const port = Number(match[1]);
      if (!Number.isInteger(port) || port <= 0) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      resolve({
        child,
        port,
        logs: () => stdout + stderr,
      });
    });

    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });

    child.on("error", (error) => {
      fail("Could not start the backend with " + PYTHON + ": " + error.message);
    });

    child.on("exit", (code, signal) => {
      if (!settled) {
        fail(
          "Backend exited before reporting a port (code=" +
            code +
            ", signal=" +
            signal +
            ")."
        );
      }
    });
  });
}

async function stopBackend(backend) {
  const child = backend && backend.child;
  if (!child || child.exitCode !== null || child.signalCode !== null) {
    return;
  }
  const exited = once(child, "exit");
  child.kill("SIGTERM");
  await Promise.race([exited, delay(3000)]);
  if (child.exitCode === null && child.signalCode === null) {
    child.kill("SIGKILL");
    await Promise.race([exited, delay(2000)]);
  }
}

function step(message) {
  console.log("  ok - " + message);
}

async function waitForTasksLoaded(page) {
  await page.waitForFunction(
    () => {
      const list = document.querySelector("#task-list");
      const empty = document.querySelector("#task-empty");
      const error = document.querySelector("#task-error");
      const listSettled = list && list.children.length > 0;
      const emptySettled = empty && !empty.hidden;
      const failed = error && !error.hidden;
      return Boolean(listSettled || emptySettled || failed);
    },
    null,
    { timeout: READY_TIMEOUT_MS }
  );
}

async function elementState(page, selector) {
  return page.evaluate((target) => {
    const element = document.querySelector(target);
    if (!element) {
      return null;
    }
    return {
      hidden: Boolean(element.hidden),
      text: (element.textContent || "").trim(),
    };
  }, selector);
}

async function taskItems(page) {
  return page.evaluate(() => {
    return Array.from(document.querySelectorAll("#task-list li")).map((item) => ({
      id: item.dataset.taskId,
      title: (item.querySelector(".task__title") || {}).textContent || "",
      done: item.classList.contains("task--done"),
      checked: Boolean(
        item.querySelector('input[type="checkbox"]') &&
          item.querySelector('input[type="checkbox"]').checked
      ),
    }));
  });
}

async function apiTasks(page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/tasks");
    const payload = await response.json();
    return { status: response.status, tasks: payload.tasks };
  });
}

async function waitForError(page) {
  await page.waitForFunction(
    () => {
      const element = document.querySelector("#task-error");
      return Boolean(element && !element.hidden && element.textContent.trim());
    },
    null,
    { timeout: READY_TIMEOUT_MS }
  );
  return (await elementState(page, "#task-error")).text;
}

async function run() {
  const backend = await startBackend();
  const baseUrl = "http://127.0.0.1:" + backend.port;

  let browser = null;
  try {
    browser = await chromium.launch({ headless: process.env.AO_HEADLESS !== "0" });
    const page = await browser.newPage();
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error));

    const response = await page.goto(baseUrl + "/", {
      waitUntil: "load",
      timeout: READY_TIMEOUT_MS,
    });
    assert.equal(response && response.status(), 200, "GET / should return 200");
    step("page loads from the backend (GET / -> 200)");

    const apiBase = await page.evaluate(() => window.AO_API_BASE);
    assert.equal(apiBase, "", "window.AO_API_BASE should default to an empty string");
    step('window.AO_API_BASE is ""');

    await waitForTasksLoaded(page);
    assert.equal((await taskItems(page)).length, 0, "starting list should be empty");
    step("initial task list renders empty");

    // Empty title is rejected client side: error shown, nothing created.
    await page.click('#task-form button[type="submit"]');
    const emptyError = await waitForError(page);
    assert.match(emptyError, /title/i, "empty-title error should mention the title");
    assert.equal((await taskItems(page)).length, 0, "no task should be created");
    step("empty title shows a validation error and creates nothing");

    // Whitespace-only title is rejected too.
    await page.fill("#task-title", "   ");
    await page.click('#task-form button[type="submit"]');
    await waitForError(page);
    assert.equal((await apiTasks(page)).tasks.length, 0, "no task should reach the API");
    step("whitespace-only title is rejected");

    // Create a task.
    const title = "Write the browser E2E check";
    await page.fill("#task-title", title);
    await page.click('#task-form button[type="submit"]');
    await page.waitForFunction(
      () => document.querySelectorAll("#task-list li").length === 1,
      null,
      { timeout: READY_TIMEOUT_MS }
    );

    let items = await taskItems(page);
    assert.equal(items.length, 1, "one task row should render");
    assert.equal(items[0].title, title, "row should show the created title");
    assert.equal(items[0].done, false, "new task starts not done");

    const errorAfterCreate = await elementState(page, "#task-error");
    assert.equal(errorAfterCreate.hidden, true, "validation error should clear");
    assert.equal(
      await page.inputValue("#task-title"),
      "",
      "the input should be cleared after a successful create"
    );

    let api = await apiTasks(page);
    assert.equal(api.status, 200, "GET /api/tasks should return 200");
    assert.deepEqual(
      api.tasks.map((task) => [task.title, task.done]),
      [[title, false]],
      "API should contain exactly the created task"
    );
    step("creates a task, clears the input, and persists it through the API");

    // Toggle done via the checkbox.
    const checkbox = page.locator('#task-list li input[type="checkbox"]').first();
    await checkbox.check();
    await page.waitForFunction(
      () => {
        const item = document.querySelector("#task-list li");
        return Boolean(item && item.classList.contains("task--done"));
      },
      null,
      { timeout: READY_TIMEOUT_MS }
    );

    items = await taskItems(page);
    assert.equal(items[0].done, true, "row should be marked done");
    assert.equal(items[0].checked, true, "checkbox should be checked");

    api = await apiTasks(page);
    assert.equal(api.tasks[0].done, true, "API should persist done=true");
    step("toggles done through the API and reflects it in the DOM");

    // Reload: state comes back from the backend.
    await page.reload({ waitUntil: "load", timeout: READY_TIMEOUT_MS });
    await page.waitForSelector("#task-list li", { timeout: READY_TIMEOUT_MS });

    items = await taskItems(page);
    assert.equal(items.length, 1, "task should survive a reload");
    assert.equal(items[0].title, title, "title should survive a reload");
    assert.equal(items[0].done, true, "done state should survive a reload");
    assert.equal(items[0].checked, true, "checkbox should stay checked after reload");
    step("done state survives a page reload");

    // Toggle back off.
    const reloadedCheckbox = page
      .locator('#task-list li input[type="checkbox"]')
      .first();
    await reloadedCheckbox.uncheck();
    await page.waitForFunction(
      () => {
        const item = document.querySelector("#task-list li");
        return Boolean(item && !item.classList.contains("task--done"));
      },
      null,
      { timeout: READY_TIMEOUT_MS }
    );

    items = await taskItems(page);
    assert.equal(items[0].done, false, "row should return to not done");
    api = await apiTasks(page);
    assert.equal(api.tasks[0].done, false, "API should persist done=false");
    step("untoggles done and reflects it in the DOM and API");

    assert.deepEqual(
      pageErrors.map((error) => error.message),
      [],
      "the page should not throw uncaught errors"
    );
    step("no uncaught page errors");
  } finally {
    if (browser) {
      await browser.close().catch(() => {});
    }
    await stopBackend(backend);
  }
}

run().then(
  () => {
    console.log("\nE2E check passed.");
  },
  (error) => {
    console.error("\nE2E check failed:");
    console.error(error && error.stack ? error.stack : error);
    process.exitCode = 1;
  }
);
