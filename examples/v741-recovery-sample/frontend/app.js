(function () {
  "use strict";

  var MAX_TITLE_LENGTH = 80;

  var form = document.getElementById("task-form");
  var input = document.getElementById("task-title");
  var submitButton = form ? form.querySelector('button[type="submit"]') : null;
  var errorBox = document.getElementById("task-error");
  var list = document.getElementById("task-list");
  var emptyState = document.getElementById("task-empty");

  var tasks = [];
  var creating = false;

  function apiBase() {
    var base = window.AO_API_BASE;
    return typeof base === "string" ? base.replace(/\/+$/, "") : "";
  }

  function apiUrl(path) {
    return apiBase() + path;
  }

  function showError(message) {
    if (!errorBox) {
      return;
    }
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  function clearError() {
    if (!errorBox) {
      return;
    }
    errorBox.textContent = "";
    errorBox.hidden = true;
  }

  async function request(path, options) {
    var response;
    try {
      response = await fetch(apiUrl(path), options);
    } catch (cause) {
      throw new Error("Could not reach the server. Is it running?");
    }

    var payload = null;
    try {
      payload = await response.json();
    } catch (cause) {
      payload = null;
    }

    if (!response.ok) {
      var message =
        payload && typeof payload.error === "string" && payload.error
          ? payload.error
          : "Request failed with status " + response.status + ".";
      throw new Error(message);
    }

    return payload;
  }

  function fetchTasks() {
    return request("/api/tasks", { headers: { Accept: "application/json" } }).then(
      function (payload) {
        return Array.isArray(payload && payload.tasks) ? payload.tasks : [];
      }
    );
  }

  function createTask(title) {
    return request("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ title: title }),
    }).then(function (payload) {
      if (!payload || !payload.task) {
        throw new Error("The server did not return the created task.");
      }
      return payload.task;
    });
  }

  function updateTaskDone(id, done) {
    return request("/api/tasks/" + encodeURIComponent(id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ done: done }),
    }).then(function (payload) {
      if (!payload || !payload.task) {
        throw new Error("The server did not return the updated task.");
      }
      return payload.task;
    });
  }

  function renderTasks() {
    if (!list) {
      return;
    }

    list.textContent = "";
    tasks.forEach(function (task) {
      list.appendChild(renderTask(task));
    });

    var isEmpty = tasks.length === 0;
    list.hidden = isEmpty;
    if (emptyState) {
      emptyState.hidden = !isEmpty;
    }
  }

  function renderTask(task) {
    var item = document.createElement("li");
    item.className = "task" + (task.done ? " task--done" : "");
    item.dataset.taskId = String(task.id);

    var checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "task__checkbox";
    checkbox.checked = Boolean(task.done);
    checkbox.setAttribute(
      "aria-label",
      'Mark "' + String(task.title) + '" as done'
    );
    checkbox.addEventListener("change", function () {
      toggleTask(task, checkbox, item);
    });

    var title = document.createElement("span");
    title.className = "task__title";
    title.textContent = String(task.title);

    item.appendChild(checkbox);
    item.appendChild(title);
    return item;
  }

  function toggleTask(task, checkbox, item) {
    var nextDone = checkbox.checked;
    checkbox.disabled = true;

    updateTaskDone(task.id, nextDone)
      .then(function (updated) {
        task.done = Boolean(updated.done);
        checkbox.checked = task.done;
        item.classList.toggle("task--done", task.done);
        clearError();
      })
      .catch(function (error) {
        checkbox.checked = !nextDone;
        showError(error.message);
      })
      .finally(function () {
        checkbox.disabled = false;
      });
  }

  function validate(title) {
    if (!title) {
      return "Please enter a task title.";
    }
    if (title.length > MAX_TITLE_LENGTH) {
      return "A task title can be at most " + MAX_TITLE_LENGTH + " characters.";
    }
    return "";
  }

  function onSubmit(event) {
    event.preventDefault();
    if (creating) {
      return;
    }

    var title = input ? input.value.trim() : "";
    var validationError = validate(title);
    if (validationError) {
      showError(validationError);
      if (input) {
        input.focus();
      }
      return;
    }

    creating = true;
    if (submitButton) {
      submitButton.disabled = true;
    }
    clearError();

    createTask(title)
      .then(function (task) {
        tasks.push(task);
        renderTasks();
        if (input) {
          input.value = "";
          input.focus();
        }
      })
      .catch(function (error) {
        showError(error.message);
      })
      .finally(function () {
        creating = false;
        if (submitButton) {
          submitButton.disabled = false;
        }
      });
  }

  function init() {
    if (!form || !input || !list) {
      return;
    }

    form.addEventListener("submit", onSubmit);

    fetchTasks()
      .then(function (loaded) {
        tasks = loaded;
        renderTasks();
      })
      .catch(function (error) {
        renderTasks();
        showError(error.message);
      });
  }

  init();
})();
