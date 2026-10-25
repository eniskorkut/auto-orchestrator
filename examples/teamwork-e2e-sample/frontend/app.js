(function () {
  "use strict";

  var configuredBase =
    typeof window.AO_API_BASE === "string" ? window.AO_API_BASE.trim() : "";
  var API_BASE = configuredBase.replace(/\/+$/, "");

  var form = document.getElementById("task-form");
  var input = document.getElementById("task-title");
  var submitButton = document.getElementById("task-submit");
  var errorElement = document.getElementById("form-error");
  var summaryElement = document.getElementById("task-summary");
  var listElement = document.getElementById("task-list");
  var loadingElement = document.getElementById("loading-state");
  var emptyElement = document.getElementById("empty-state");

  var tasks = [];
  var loading = true;
  var loadFailed = false;
  var submitting = false;

  function apiUrl(path) {
    return API_BASE + path;
  }

  async function request(path, options) {
    var method = (options && options.method) || "GET";
    var body = options && options.body;
    var headers = { Accept: "application/json" };

    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }

    var response;
    try {
      response = await fetch(apiUrl(path), { method: method, headers: headers, body: body });
    } catch (error) {
      throw new Error("Could not reach the API. Check the server and try again.");
    }

    var data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }

    if (!response.ok) {
      var message =
        data && typeof data.error === "string" && data.error.trim()
          ? data.error
          : "Request failed (HTTP " + response.status + ").";
      throw new Error(message);
    }

    return data;
  }

  function errorMessage(error) {
    return error && typeof error.message === "string" && error.message
      ? error.message
      : "Something went wrong. Please try again.";
  }

  function showError(message) {
    errorElement.textContent = message;
    errorElement.hidden = false;
  }

  function clearError() {
    errorElement.textContent = "";
    errorElement.hidden = true;
  }

  function normalizeTask(raw) {
    if (!raw || typeof raw !== "object") {
      return null;
    }

    var id = Number(raw.id);
    if (!Number.isFinite(id)) {
      return null;
    }

    return {
      id: id,
      title: typeof raw.title === "string" ? raw.title : String(raw.title == null ? "" : raw.title),
      done: Boolean(raw.done),
    };
  }

  function updateSummary() {
    if (loading) {
      summaryElement.textContent = "";
      return;
    }

    if (tasks.length === 0) {
      summaryElement.textContent = "";
      return;
    }

    var done = tasks.filter(function (task) {
      return task.done;
    }).length;
    summaryElement.textContent =
      done + " of " + tasks.length + " done · " + (tasks.length - done) + " open";
  }

  function createTaskRow(task) {
    var item = document.createElement("li");
    item.className = task.done ? "task is-done" : "task";
    item.dataset.id = String(task.id);

    var label = document.createElement("label");
    label.className = "task-label";

    var checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "task-toggle";
    checkbox.checked = Boolean(task.done);

    var title = document.createElement("span");
    title.className = "task-title-text";
    title.textContent = task.title;

    label.append(checkbox, title);
    item.append(label);

    checkbox.addEventListener("change", function () {
      void toggleTask(task, item, checkbox);
    });

    return item;
  }

  function render() {
    listElement.replaceChildren.apply(
      listElement,
      tasks.map(createTaskRow)
    );

    var hasTasks = tasks.length > 0;
    listElement.hidden = !hasTasks;
    emptyElement.hidden = hasTasks || loading || loadFailed;
    loadingElement.hidden = !loading;

    updateSummary();
  }

  async function loadTasks() {
    loading = true;
    loadFailed = false;
    clearError();
    render();

    try {
      var data = await request("/api/tasks");
      var rawTasks = data && Array.isArray(data.tasks) ? data.tasks : [];
      tasks = rawTasks
        .map(normalizeTask)
        .filter(function (task) {
          return task !== null;
        });
    } catch (error) {
      tasks = [];
      loadFailed = true;
      showError(errorMessage(error));
    } finally {
      loading = false;
      render();
    }
  }

  async function createTask(title) {
    var data = await request("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ title: title }),
    });

    var created = normalizeTask(data && data.task);
    if (!created) {
      throw new Error("Unexpected response from the server.");
    }

    tasks.push(created);
    loadFailed = false;
    render();
  }

  async function toggleTask(task, item, checkbox) {
    var nextDone = checkbox.checked;

    checkbox.disabled = true;
    item.classList.add("is-pending");

    try {
      var data = await request("/api/tasks/" + encodeURIComponent(task.id), {
        method: "PATCH",
        body: JSON.stringify({ done: nextDone }),
      });

      var updated = data && data.task ? normalizeTask(data.task) : null;
      task.done = updated ? updated.done : nextDone;
      task.title = updated ? updated.title : task.title;

      checkbox.checked = task.done;
      item.classList.toggle("is-done", task.done);
      clearError();
      updateSummary();
    } catch (error) {
      checkbox.checked = !nextDone;
      showError(errorMessage(error));
    } finally {
      checkbox.disabled = false;
      item.classList.remove("is-pending");
    }
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();

    if (submitting) {
      return;
    }

    submitting = true;
    submitButton.disabled = true;
    clearError();

    try {
      await createTask(input.value.trim());
      input.value = "";
      input.focus();
    } catch (error) {
      showError(errorMessage(error));
      input.focus();
    } finally {
      submitting = false;
      submitButton.disabled = false;
    }
  });

  input.addEventListener("input", clearError);

  void loadTasks();
})();
