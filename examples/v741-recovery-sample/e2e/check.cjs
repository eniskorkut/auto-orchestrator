#!/usr/bin/env node
"use strict";

/*
 * Browser end-to-end check for the task manager.
 *
 * Usage:
 *   NODE_PATH=/path/to/node_modules node e2e/check.cjs
 *
 * Optional environment:
 *   AO_REPO_ROOT        repository root that contains backend/ and frontend/
 *                       (defaults to the parent of this script's directory)
 *   AO_BACKEND_ENTRY    backend entrypoint relative to AO_REPO_ROOT
 *                       (defaults to backend/app.py)
 *   AO_PYTHON           Python executable (defaults to python3.11)
 *
 * The script starts the backend on an ephemeral port, drives the real page in
 * Chromium, and asserts the DOM after create / invalid submit / toggle.
 */

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");

const REPO_ROOT = process.env.AO_REPO_ROOT
  ? path.resolve(process.env.AO_REPO_ROOT)
  : path.resolve(__dirname, "..");
const PYTHON = process.env.AO_PYTHON || "python3.11";
const BACKEND_ENTRY = path.resolve(
  REPO_ROOT,
  process.env.AO_BACKEND_ENTRY || path.join("backend", "app.py")
);
const STARTUP_TIMEOUT_MS = 15000;
const TASK_TITLE = "Write the browser E2E";

let backendProcess = null;
let browser = null;

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function loadChromium() {
  try {
    const playwright = require("playwright");
    return playwright.chromium;
  } catch (error) {
    throw new Error(
      "Cannot require('playwright'). Install it or point NODE_PATH at a " +
        "node_modules directory that provides it.\n" +
        "Underlying error: " +
        error.message
    );
  }
}

function parsePort(stdout) {
  const bare = stdout.match(/^\s*(\d{1,5})\s*$/m);
  if (bare) {
    return Number(bare[1]);
  }
  const labeled = stdout.match(/\bport\b\W{0,3}(\d{1,5})\b/i);
  return labeled ? Number(labeled[1]) : null;
}

function startBackend() {
  if (!fs.existsSync(BACKEND_ENTRY)) {
    return Promise.reject(
      new Error("Backend entry not found: " + BACKEND_ENTRY)
    );
  }

  const child = spawn(
    PYTHON,
    ["-u", BACKEND_ENTRY, "--port", "0", "--print-port"],
    {
      cwd: REPO_ROOT,
      stdio: ["ignore", "pipe", "pipe"],
    }
  );

  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });

  return new Promise((resolve, reject) => {
    let stdout = "";
    let settled = false;

    const timer = setTimeout(() => {
      fail(
        new Error(
          "Backend did not print a port within " +
            STARTUP_TIMEOUT_MS +
            "ms.\nstdout: " +
            stdout.trim() +
            "\nstderr: " +
            stderr.trim()
        )
      );
    }, STARTUP_TIMEOUT_MS);

    function cleanup() {
      settled = true;
      clearTimeout(timer);
      child.stdout.removeListener("data", onData);
      child.removeListener("exit", onExit);
      child.removeListener("error", onError);
    }

    function fail(error) {
      if (settled) {
        return;
      }
      cleanup();
      if (child.exitCode === null) {
        child.kill("SIGKILL");
      }
      reject(error);
    }

    function onData(chunk) {
      stdout += chunk.toString();
      const port = parsePort(stdout);
      if (!port || !Number.isInteger(port) || port < 1 || port > 65535) {
        return;
      }
      cleanup();
      resolve({ child, port });
    }

    function onExit(code, signal) {
      fail(
        new Error(
          "Backend exited before printing a port (code " +
            code +
            ", signal " +
            signal +
            ").\nstderr: " +
            stderr.trim()
        )
      );
    }

    function onError(error) {
      fail(
        new Error(
          "Could not start " + PYTHON + ": " + error.message
        )
      );
    }

    child.stdout.on("data", onData);
    child.once("exit", onExit);
    child.once("error", onError);
  });
}

async function waitForServer(port) {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  const candidates = [
    "http://127.0.0.1:" + port,
    "http://localhost:" + port,
    "http://[::1]:" + port,
  ];
  let lastError = null;

  while (Date.now() < deadline) {
    for (const baseUrl of candidates) {
      try {
        const response = await fetch(baseUrl + "/api/tasks", {
          headers: { accept: "application/json" },
          signal: AbortSignal.timeout(1500),
        });
        if (response.ok) {
          return baseUrl;
        }
        lastError = new Error("GET /api/tasks returned " + response.status);
      } catch (error) {
        lastError = error;
      }
    }
    await delay(100);
  }

  throw new Error(
    "Backend on port " +
      port +
      " never became ready: " +
      (lastError ? lastError.message : "unknown error")
  );
}

async function waitForTaskListIdle(page) {
  // The list can legitimately be empty (zero size), so wait for the
  // aria-busy attribute rather than for visibility.
  await page.waitForFunction(() => {
    const list = document.getElementById("task-list");
    return Boolean(list) && list.getAttribute("aria-busy") === "false";
  });
}

async function stopBackend() {
  const child = backendProcess;
  backendProcess = null;
  if (!child || child.exitCode !== null || child.signalCode !== null) {
    return;
  }

  child.kill("SIGTERM");
  await Promise.race([once(child, "exit"), delay(2000)]);
  if (child.exitCode === null && child.signalCode === null) {
    child.kill("SIGKILL");
  }
}

async function main() {
  const chromium = loadChromium();

  console.log("Starting backend: " + BACKEND_ENTRY);
  const started = await startBackend();
  backendProcess = started.child;
  const baseUrl = await waitForServer(started.port);
  console.log("Backend ready on " + baseUrl);

  browser = await chromium.launch();
  const page = await browser.newPage();
  page.setDefaultTimeout(10000);

  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  const taskItems = page.getByTestId("task-item");
  const errorBox = page.getByTestId("error");
  const countBadge = page.getByTestId("task-count");
  const titleInput = page.getByLabel("New task");
  const addButton = page.getByRole("button", { name: "Add task" });

  // 1. Initial load.
  console.log("1. Loading the app");
  await page.goto(baseUrl + "/", { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { name: "Tasks", level: 1 }).waitFor();
  await waitForTaskListIdle(page);
  assert.equal(await taskItems.count(), 0, "a fresh backend has no tasks");
  assert.equal((await countBadge.innerText()).trim(), "0");
  await page.getByTestId("empty-state").waitFor({ state: "visible" });

  // 2. Create a task through the form.
  console.log("2. Creating a task");
  await titleInput.fill(TASK_TITLE);
  await addButton.click();
  await taskItems.first().waitFor();
  assert.equal(await taskItems.count(), 1, "one task after create");
  assert.equal((await taskItems.first().innerText()).trim(), TASK_TITLE);
  assert.equal((await countBadge.innerText()).trim(), "1");
  await page.getByTestId("empty-state").waitFor({ state: "hidden" });
  assert.equal(await errorBox.isVisible(), false, "no error after create");
  assert.equal(
    (await titleInput.inputValue()).trim(),
    "",
    "input is cleared after create"
  );

  // 3. Empty title is rejected without creating anything.
  console.log("3. Rejecting an empty title");
  await titleInput.fill("");
  await addButton.click();
  await errorBox.waitFor({ state: "visible" });
  assert.match(
    (await errorBox.innerText()).trim(),
    /title/i,
    "validation error mentions the title"
  );
  assert.equal(await taskItems.count(), 1, "invalid submit creates nothing");
  assert.equal((await countBadge.innerText()).trim(), "1");

  // 4. Whitespace-only title is also rejected.
  console.log("4. Rejecting a whitespace-only title");
  await titleInput.fill("   ");
  await addButton.click();
  await errorBox.waitFor({ state: "visible" });
  assert.match((await errorBox.innerText()).trim(), /title/i);
  assert.equal(await taskItems.count(), 1, "whitespace submit creates nothing");

  // 5. Toggle done.
  console.log("5. Toggling the task done");
  const firstItem = taskItems.first();
  const toggle = firstItem.getByTestId("task-toggle");
  assert.equal(await toggle.isChecked(), false, "new task starts undone");
  assert.equal(await firstItem.getAttribute("data-done"), "false");

  await toggle.check();
  await page
    .locator('[data-testid="task-item"][data-done="true"]')
    .waitFor();
  assert.equal(await toggle.isChecked(), true);
  assert.equal(await firstItem.getAttribute("data-done"), "true");
  assert.equal(await errorBox.isVisible(), false, "no error after toggle");
  assert.match(
    (await firstItem.getAttribute("class")) || "",
    /task--done/,
    "done task gets the done class"
  );

  // 6. State survives a reload (PATCH reached the backend).
  console.log("6. Reloading to confirm the toggle persisted");
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitForTaskListIdle(page);
  const reloadedItem = page.getByTestId("task-item").first();
  assert.equal((await reloadedItem.innerText()).trim(), TASK_TITLE);
  assert.equal(await reloadedItem.getAttribute("data-done"), "true");
  assert.equal(
    await reloadedItem.getByTestId("task-toggle").isChecked(),
    true,
    "done state persists across reload"
  );

  assert.deepEqual(
    pageErrors,
    [],
    "page threw JavaScript errors: " + pageErrors.join("; ")
  );

  console.log("\nE2E passed: load, create, empty-title rejection, toggle, reload.");
}

async function run() {
  try {
    await main();
    return 0;
  } catch (error) {
    console.error("\nE2E failed: " + (error && error.stack ? error.stack : error));
    return 1;
  } finally {
    if (browser) {
      try {
        await browser.close();
      } catch (error) {
        // Closing a crashed browser is not interesting.
      }
      browser = null;
    }
    await stopBackend();
  }
}

process.once("SIGINT", () => {
  stopBackend().finally(() => process.exit(130));
});
process.once("SIGTERM", () => {
  stopBackend().finally(() => process.exit(143));
});
process.once("exit", () => {
  if (
    backendProcess &&
    backendProcess.exitCode === null &&
    backendProcess.signalCode === null
  ) {
    backendProcess.kill("SIGKILL");
  }
});

run().then((code) => {
  process.exitCode = code;
});
