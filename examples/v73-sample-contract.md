# Teamwork E2E sample contract

This disposable project is built by Auto-Orchestrator managed sessions. It is a
small task manager with a stdlib Python backend and a static frontend.

## API

- `GET /api/tasks` -> `200 {"tasks": [{"id": 1, "title": "...", "done": false}]}`
- `POST /api/tasks` with `{"title": "..."}`:
  - valid (1..80 non-whitespace characters) -> `201 {"task": {...}}`
  - invalid -> `400 {"error": "..."}`
- `PATCH /api/tasks/<id>` with `{"done": true|false}` -> `200 {"task": {...}}`
  or `404`.
- `GET /` serves `frontend/index.html`; `/app.js`, `/styles.css`,
  `/config.js` are static assets.
- The server binds `127.0.0.1` on port `0` in tests and supports
  `python3.11 backend/app.py --port 0 --print-port` for the browser E2E.
- Data is in-memory; restarting clears it. No external dependency.

## Frontend

- `frontend/index.html`, `frontend/styles.css`, `frontend/app.js`,
  `frontend/config.js`.
- `frontend/config.js` must expose `window.AO_API_BASE` (default empty string,
  so requests are relative).
- The UI must list tasks, create a task, show validation errors, and toggle
  done.
- It must be usable without a build step.

## Tests

- Backend unit tests: `python3.11 -m unittest discover -s tests -v`.
- Browser E2E: `e2e/check.cjs` (CommonJS) uses Playwright from `NODE_PATH`,
  launches Chromium, starts the backend, creates a task, rejects an empty
  title, toggles done, and asserts the UI.
- `NODE_PATH` and the browser channel are supplied by the orchestrator.

## Ownership

- Backend implementer: `backend/**`, `tests/test_backend.py`.
- Frontend implementer: `frontend/**`, `e2e/check.cjs`.
- Tester/verifier: may add verification tests but must not rewrite product code.
- Challenger: `tests/challenger/**` only.
- Critic/auditor: read-only findings; no product edits.
