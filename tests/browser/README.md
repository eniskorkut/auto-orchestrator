# Browser tests (V7.2)

Playwright drives the real dashboard in the system Chrome channel. No browser
binary is downloaded by this repository.

```bash
npm install          # installs @playwright/test (Apache-2.0) only
npm run test:browser
npm run test:browser:update   # intentionally refresh visual baselines
```

The config starts `python3.11 tools/dashboard.py --demo --port 8799` unless a
server already answers on that port. Override `AO_PYTHON` or `AO_TEST_PORT` if
needed.

- `office.spec.mjs` covers the continuous canvas, room filters, registry-only
  agents, project isolation, inspector resizing and narrow-width readability,
  the layout editor, reduced motion, keyboard access, mobile pan/zoom and the
  visual baselines.
- `helpers.mjs` resolves demo project ids, waits for walks to settle and reads
  the renderer's read-only debug state. It never changes evidence.
- `screenshots/` contains the visual baselines and full-page review captures at
  1440×900, 1280×800, 768×1024 and 390×844.

The runtime office uses only original procedural Canvas 2D drawing. Agents,
furniture and the favicon are not copied from AgentSpace, Pixel Agents, PixiJS
or any other external project; external references were used for architecture
and behaviour research only. Playwright is a development-only dependency under
the Apache-2.0 license.
