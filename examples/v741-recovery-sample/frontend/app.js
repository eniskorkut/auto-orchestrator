"use strict";

/*
 * Static task manager frontend. No build step, no dependencies.
 *
 * Talks to the documented JSON API:
 *   GET    /api/tasks        -> { tasks: [...] }
 *   POST   /api/tasks        -> { task: {...} } (or 400 { error })
 *   PATCH  /api/tasks/<id>   -> { task: {...} } (or 404 { error })
 *
 * Every request is relative to window.AO_API_BASE (see config.js).
 */
(function () {
  const TASKS_PATH = "/api/tasks";
  const MAX_TITLE_LENGTH = 80;

  const apiBase = (
    typeof window.AO_API_BASE === "string" ? window.AO_API_BASE : ""
  ).replace(/\/+$/, "");

  const state = {
    tasks: [],
    loading: true,
    busy: false,
  };

  const els = {};

  function init() {
    els.form = document.getElementById("task-form");
    els.input = document.getElementById("task-title");
    els.submit = document.getElementById("task-submit");
    els.error = document.getElementById("form-error");
    els.list = document.getElementById("task-list");
    els.count = document.getElementById("task-count");
    els.empty = document.getElementById("task-empty");
    els.status = document.getElementById("status");

    if (!els.form || !els.input || !els.list) {
      return;
    }

    els.form.addEventListener("submit", handleSubmit);
    els.input.addEventListener("input", function () {
      if (!els.error.hidden) {
        clearError();
      }
    });

    render();
    loadTasks();
  }

  function url(path) {
    return apiBase + path;
  }

  async function request(path, options) {
    const init = Object.assign({}, options);
    const headers = { Accept: "application/json" };
    if (init.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    init.headers = Object.assign(headers, init.headers || {});

    let response;
    try {
      response = await fetch(url(path), init);
    } catch (cause) {
      throw new Error("Could not reach the server. Check your connection.");
    }

    const text = await response.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (cause) {
        payload = null;
      }
    }

    if (!response.ok) {
      const message =
        payload && typeof payload.error === "string" && payload.error.trim()
          ? payload.error.trim()
          : "Request failed with status " + response.status + ".";
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }

    return payload;
  }

  function validateTitle(raw) {
    const title = String(raw == null ? "" : raw).trim();
    if (!title) {
      return { ok: false, error: "Enter a task title." };
    }
    if (title.length > MAX_TITLE_LENGTH) {
      return {
        ok: false,
        error:
          "Keep the title to " + MAX_TITLE_LENGTH + " characters or fewer.",
      };
    }
    return { ok: true, title: title };
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (state.busy) {
      return;
    }

    const result = validateTitle(els.input.value);
    if (!result.ok) {
      showError(result.error);
      els.input.focus();
      return;
    }

    clearError();
    setBusy(true);
    try {
      const payload = await request(TASKS_PATH, {
        method: "POST",
        body: JSON.stringify({ title: result.title }),
      });
      const task = payload && payload.task;
      if (!task || typeof task !== "object") {
        throw new Error("The server returned an unexpected response.");
      }

      state.tasks.push(task);
      els.input.value = "";
      render();
      announce('Added "' + task.title + '".');
    } catch (error) {
      showError(messageOf(error, "Could not create the task."));
    } finally {
      setBusy(false);
    }

    els.input.focus();
  }

  async function loadTasks() {
    state.loading = true;
    render();
    try {
      const payload = await request(TASKS_PATH);
      if (!payload || !Array.isArray(payload.tasks)) {
        throw new Error("The server returned an unexpected response.");
      }
      state.tasks = payload.tasks;
    } catch (error) {
      state.tasks = [];
      showError(messageOf(error, "Could not load tasks."));
    } finally {
      state.loading = false;
      render();
    }
  }

  async function toggleTask(task, checkbox) {
    const nextDone = checkbox.checked;
    const previousDone = Boolean(task.done);

    clearError();
    checkbox.disabled = true;
    checkbox.setAttribute("aria-busy", "true");

    try {
      const payload = await request(
        TASKS_PATH + "/" + encodeURIComponent(String(task.id)),
        {
          method: "PATCH",
          body: JSON.stringify({ done: nextDone }),
        }
      );
      const updated =
        payload && payload.task && typeof payload.task === "object"
          ? payload.task
          : Object.assign({}, task, { done: nextDone });

      task.done = Boolean(updated.done);
      if (typeof updated.title === "string") {
        task.title = updated.title;
      }
      applyTaskDone(task, checkbox);
      announce(
        '"' + task.title + '" marked ' + (task.done ? "done" : "not done") + "."
      );
    } catch (error) {
      task.done = previousDone;
      applyTaskDone(task, checkbox);
      showError(messageOf(error, "Could not update the task."));
    } finally {
      checkbox.disabled = false;
      checkbox.removeAttribute("aria-busy");
    }
  }

  function applyTaskDone(task, checkbox) {
    const done = Boolean(task.done);
    checkbox.checked = done;
    checkbox.setAttribute("aria-label", toggleLabel(task));
    const item = checkbox.closest("li");
    if (item) {
      item.dataset.done = done ? "true" : "false";
      item.dataset.taskId = String(task.id);
      item.classList.toggle("task--done", done);
    }
  }

  function toggleLabel(task) {
    return (
      'Mark "' + task.title + '" as ' + (task.done ? "not done" : "done")
    );
  }

  function render() {
    if (!els.list) {
      return;
    }

    els.list.textContent = "";
    for (const task of state.tasks) {
      els.list.appendChild(renderTask(task));
    }

    if (els.count) {
      els.count.textContent = String(state.tasks.length);
    }
    if (els.empty) {
      els.empty.hidden = state.loading || state.tasks.length > 0;
    }
    syncBusyState();
  }

  function renderTask(task) {
    const item = document.createElement("li");
    item.className = "task";
    item.dataset.testid = "task-item";
    item.dataset.taskId = String(task.id);
    const done = Boolean(task.done);
    item.dataset.done = done ? "true" : "false";
    if (done) {
      item.classList.add("task--done");
    }

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "task__checkbox";
    checkbox.id = "task-" + task.id;
    checkbox.dataset.testid = "task-toggle";
    checkbox.checked = done;
    checkbox.setAttribute("aria-label", toggleLabel(task));
    checkbox.addEventListener("change", function () {
      toggleTask(task, checkbox);
    });

    const label = document.createElement("label");
    label.className = "task__title";
    label.htmlFor = checkbox.id;
    label.textContent = task.title;

    item.appendChild(checkbox);
    item.appendChild(label);
    return item;
  }

  function setBusy(busy) {
    state.busy = Boolean(busy);
    if (els.submit) {
      els.submit.disabled = state.busy;
    }
    if (els.form) {
      els.form.setAttribute("aria-busy", state.busy ? "true" : "false");
    }
    syncBusyState();
  }

  function syncBusyState() {
    if (!els.list) {
      return;
    }
    const busy = state.loading || state.busy;
    els.list.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function showError(message) {
    if (!els.error) {
      return;
    }
    els.error.textContent = message;
    els.error.hidden = false;
  }

  function clearError() {
    if (!els.error) {
      return;
    }
    els.error.textContent = "";
    els.error.hidden = true;
  }

  function announce(message) {
    if (els.status) {
      els.status.textContent = message;
    }
  }

  function messageOf(error, fallback) {
    return error && typeof error.message === "string" && error.message
      ? error.message
      : fallback;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
