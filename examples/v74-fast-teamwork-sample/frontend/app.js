// Task manager frontend. No build step: this file runs as-is in the browser.
(function () {
  "use strict";

  var MAX_TITLE_LENGTH = 80;

  var API_BASE = String(
    typeof window.AO_API_BASE === "string" ? window.AO_API_BASE : ""
  ).replace(/\/+$/, "");

  var form = document.querySelector('[data-testid="task-form"]');
  var input = document.querySelector('[data-testid="task-input"]');
  var submitButton = document.querySelector('[data-testid="task-submit"]');
  var errorBox = document.querySelector('[data-testid="error"]');
  var statusLine = document.querySelector('[data-testid="status"]');
  var list = document.querySelector('[data-testid="task-list"]');
  var emptyState = document.querySelector('[data-testid="empty"]');

  var tasks = [];
  var rows = new Map();
  var pendingIds = new Set();
  var loading = true;
  var loadFailed = false;
  var submitting = false;

  function ApiError(message, status) {
    this.name = "ApiError";
    this.message = message;
    this.status = status || 0;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  /* Data access ----------------------------------------------------------- */

  async function request(path, options) {
    var opts = options || {};
    var headers = { Accept: "application/json" };
    var body;

    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }

    var response;
    try {
      response = await fetch(API_BASE + path, {
        method: opts.method || "GET",
        headers: headers,
        body: body,
      });
    } catch (cause) {
      throw new ApiError(
        "Could not reach the server. Check your connection and try again.",
        0
      );
    }

    var text = await response.text();
    var payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (cause) {
        payload = null;
      }
    }

    if (!response.ok) {
      var message =
        payload && typeof payload.error === "string" && payload.error.trim()
          ? payload.error
          : "Something went wrong (" + response.status + ").";
      throw new ApiError(message, response.status);
    }

    if (!payload || typeof payload !== "object") {
      throw new ApiError("The server returned an unexpected response.", response.status);
    }

    return payload;
  }

  function normalizeTask(raw) {
    return {
      id: raw && raw.id,
      title: raw && typeof raw.title === "string" ? raw.title : "",
      done: Boolean(raw && raw.done),
    };
  }

  async function loadTasks() {
    loading = true;
    loadFailed = false;
    setListBusy(true);
    render();
    try {
      var data = await request("/api/tasks");
      tasks = Array.isArray(data.tasks) ? data.tasks.map(normalizeTask) : [];
      clearError();
    } catch (error) {
      tasks = [];
      loadFailed = true;
      showError(error.message);
    } finally {
      loading = false;
      setListBusy(false);
      render();
    }
  }

  async function createTask(title) {
    submitting = true;
    setSubmitting(true);
    clearError();
    try {
      var data = await request("/api/tasks", {
        method: "POST",
        body: { title: title },
      });
      if (!data.task) {
        throw new ApiError("The server returned an unexpected response.", 0);
      }
      tasks.push(normalizeTask(data.task));
      input.value = "";
      setFieldInvalid(false);
      render();
    } catch (error) {
      showError(error.message);
    } finally {
      submitting = false;
      setSubmitting(false);
    }
  }

  async function toggleTask(task, checkbox) {
    if (pendingIds.has(task.id)) {
      return;
    }

    var previousDone = task.done;
    var nextDone = checkbox.checked;

    pendingIds.add(task.id);
    task.done = nextDone;
    updateRow(task);
    setItemPending(task.id, true);

    try {
      var data = await request("/api/tasks/" + encodeURIComponent(task.id), {
        method: "PATCH",
        body: { done: nextDone },
      });
      if (data.task) {
        task.done = Boolean(data.task.done);
        if (typeof data.task.title === "string") {
          task.title = data.task.title;
        }
      }
    } catch (error) {
      task.done = previousDone;
      showError(error.message);
      if (error.status === 404) {
        await loadTasks();
        return;
      }
    } finally {
      pendingIds.delete(task.id);
      setItemPending(task.id, false);
      updateRow(task);
      renderStatus();
    }
  }

  /* Rendering -------------------------------------------------------------- */

  function render() {
    rows.clear();
    list.textContent = "";

    var fragment = document.createDocumentFragment();
    tasks.forEach(function (task) {
      fragment.appendChild(createRow(task));
    });
    list.appendChild(fragment);

    renderStatus();
    emptyState.hidden = loading || loadFailed || tasks.length > 0;
  }

  function createRow(task) {
    var row = {
      element: document.createElement("li"),
      checkbox: document.createElement("input"),
      title: document.createElement("span"),
    };

    row.element.className = "task";
    row.element.setAttribute("data-testid", "task-item");
    row.element.dataset.id = String(task.id);
    row.element.dataset.done = String(task.done);

    var label = document.createElement("label");
    label.className = "task__label";

    row.checkbox.type = "checkbox";
    row.checkbox.className = "task__checkbox";
    row.checkbox.setAttribute("data-testid", "task-checkbox");
    row.checkbox.checked = task.done;
    row.checkbox.addEventListener("change", function () {
      toggleTask(task, row.checkbox);
    });

    row.title.className = "task__title";
    row.title.setAttribute("data-testid", "task-title");
    row.title.textContent = task.title;

    label.appendChild(row.checkbox);
    label.appendChild(row.title);
    row.element.appendChild(label);

    rows.set(task.id, row);
    updateRow(task);
    return row.element;
  }

  function updateRow(task) {
    var row = rows.get(task.id);
    if (!row) {
      return;
    }
    row.element.dataset.done = String(task.done);
    row.checkbox.checked = task.done;
    row.title.textContent = task.title;
    row.checkbox.setAttribute(
      "aria-label",
      (task.done ? "Mark as not done: " : "Mark as done: ") + task.title
    );
  }

  function setItemPending(id, isPending) {
    var row = rows.get(id);
    if (!row) {
      return;
    }
    row.element.dataset.pending = String(isPending);
    row.checkbox.disabled = isPending;
  }

  function renderStatus() {
    if (loading) {
      statusLine.textContent = "Loading tasks…";
      return;
    }

    if (loadFailed) {
      statusLine.textContent = "Could not load tasks";
      return;
    }

    var total = tasks.length;
    var done = tasks.filter(function (task) {
      return task.done;
    }).length;

    if (total === 0) {
      statusLine.textContent = "No tasks yet";
      return;
    }

    var text = total + (total === 1 ? " task" : " tasks");
    statusLine.textContent = done > 0 ? text + " · " + done + " done" : text;
  }

  function setListBusy(isBusy) {
    list.setAttribute("aria-busy", String(isBusy));
  }

  function setSubmitting(isSubmitting) {
    submitButton.disabled = isSubmitting;
    input.setAttribute("aria-busy", String(isSubmitting));
  }

  function setFieldInvalid(isInvalid) {
    if (isInvalid) {
      input.setAttribute("aria-invalid", "true");
    } else {
      input.removeAttribute("aria-invalid");
    }
  }

  /* Feedback --------------------------------------------------------------- */

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  function clearError() {
    errorBox.textContent = "";
    errorBox.hidden = true;
  }

  /* Events ----------------------------------------------------------------- */

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (submitting) {
      return;
    }

    var title = input.value.trim();

    if (!title) {
      setFieldInvalid(true);
      showError("Enter a task title.");
      input.focus();
      return;
    }

    if (title.length > MAX_TITLE_LENGTH) {
      setFieldInvalid(true);
      showError("Keep the title to " + MAX_TITLE_LENGTH + " characters or fewer.");
      input.focus();
      return;
    }

    setFieldInvalid(false);
    createTask(title);
  });

  input.addEventListener("input", function () {
    if (!errorBox.hidden) {
      clearError();
    }
    if (input.getAttribute("aria-invalid") === "true" && input.value.trim()) {
      setFieldInvalid(false);
    }
  });

  /* Start ------------------------------------------------------------------ */

  loadTasks();
})();
