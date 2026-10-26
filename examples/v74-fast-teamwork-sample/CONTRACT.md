# Teamwork E2E sample contract

The orchestrator builds this project with managed sessions. Do not rewrite this
file or README.md. The product is a small in-memory task manager.

## Backend (Python 3.11 stdlib)

- `GET /api/tasks` -> `200 {"tasks": [{"id": 1, "title": "...", "done": false}]}`
- `POST /api/tasks` body `{"title": "..."}`:
  - 1..80 non-whitespace characters -> `201 {"task": {...}}`
  - invalid -> `400 {"error": "..."}`
- `PATCH /api/tasks/<id>` body `{"done": true|false}` -> `200 {"task": {...}}` or 404.
- `GET /`, `/app.js`, `/styles.css`, `/config.js` serve files from `frontend/`.
- `python3.11 backend/app.py --port 0 --print-port` prints the bound port and
  serves until interrupted.
- No third-party packages. Data is in memory.

## Frontend (no build step)

- `frontend/index.html`, `frontend/styles.css`, `frontend/app.js`,
  `frontend/config.js`.
- `frontend/config.js` sets `window.AO_API_BASE = ""` by default.
- List tasks, create a task, show validation errors, toggle done.
- Requests are relative to `window.AO_API_BASE`.

## Tests

- Backend: `python3.11 -m unittest discover -s tests -v`.
- Browser E2E: CommonJS `e2e/check.cjs`, uses `require("playwright")` supplied
  through `NODE_PATH`, starts the backend, creates a task, rejects an empty
  title, toggles done, asserts the DOM.
