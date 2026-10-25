import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import vm from 'node:vm';

const root = process.cwd();
const assets = join(root, 'tools', 'dashboard_assets');

function loadScript(name) {
  const sandbox = { console };
  vm.createContext(sandbox);
  vm.runInContext(readFileSync(join(assets, name), 'utf8'), sandbox, { filename: name });
  return sandbox;
}

const i18nSandbox = loadScript('i18n.js');
const I18n = i18nSandbox.AOI18n;
const officeSandbox = loadScript('office.js');
const Office = officeSandbox.AOOffice;

const html = readFileSync(join(assets, 'index.html'), 'utf8');

test('i18n dictionaries are complete, parallel and non-empty', () => {
  assert.ok(I18n, 'i18n.js must expose AOI18n');
  const en = I18n.dictionary.en;
  const tr = I18n.dictionary.tr;
  const enKeys = Object.keys(en).sort();
  const trKeys = Object.keys(tr).sort();
  assert.deepEqual(trKeys, enKeys, 'TR and EN must expose exactly the same keys');
  assert.ok(enKeys.length >= 250, `expected a full UI dictionary, got ${enKeys.length} keys`);
  for (const key of enKeys) {
    assert.equal(typeof en[key], 'string', `EN ${key} must be a string`);
    assert.equal(typeof tr[key], 'string', `TR ${key} must be a string`);
    assert.ok(en[key].trim().length > 0, `EN ${key} must not be empty`);
    assert.ok(tr[key].trim().length > 0, `TR ${key} must not be empty`);
  }
  assert.equal(I18n.t('nav.office'), 'Office');
  I18n.setLanguage('tr', { persist: false, announce: false });
  assert.equal(I18n.t('nav.office'), 'Ofis');
  assert.equal(I18n.t('status.working'), 'Çalışıyor');
  assert.equal(I18n.t('office.summary', { agents: 3, working: 1, unknown: 1, unlinked: 1 }), '3 kayıtlı masa · 1 etkin kanıtlı · 1 bilinmiyor · 1 bağlantısız hayalet');
  I18n.setLanguage('en', { persist: false, announce: false });
  assert.equal(I18n.t('office.summary', { agents: 3, working: 1, unknown: 1, unlinked: 1 }), '3 registered desks · 1 with active evidence · 1 unknown · 1 unlinked ghosts');
  assert.equal(I18n.t('missing.key'), 'missing.key', 'missing keys fall back to the key, never to fake copy');
});

test('every data-i18n key in index.html exists in both dictionaries', () => {
  const keys = new Set();
  for (const match of html.matchAll(/data-i18n(?:-[a-z-]+)?="([^"]+)"/g)) keys.add(match[1]);
  assert.ok(keys.size >= 80, `expected broad static i18n coverage, got ${keys.size} keys`);
  for (const key of keys) {
    assert.ok(key in I18n.dictionary.en, `missing EN translation for ${key}`);
    assert.ok(key in I18n.dictionary.tr, `missing TR translation for ${key}`);
  }
});

test('status vocabulary covers every required evidence status and alias', () => {
  const required = [
    'idle',
    'dispatching',
    'working',
    'running-tests',
    'reviewing',
    'awaiting-approval',
    'blocked',
    'failed',
    'completed',
    'unknown',
  ];
  for (const status of required) {
    assert.ok(I18n.requiredStatuses.includes(status), `status ${status} must be mapped`);
    const meta = I18n.statusMeta(status, 'en');
    assert.equal(meta.key, status);
    assert.ok(meta.label.length > 0);
    assert.ok(meta.symbol.length > 0, `${status} needs a non-colour symbol cue`);
    assert.ok(meta.labelKey.startsWith('status.'));
  }
  assert.equal(I18n.statusLabel('Working', 'en'), 'Working');
  assert.equal(I18n.statusLabel('running_tests', 'en'), 'Running Tests');
  assert.equal(I18n.statusLabel('awaiting_approval', 'tr'), 'Onay Bekliyor');
  assert.equal(I18n.statusLabel('disconnected', 'en'), 'Unknown / Disconnected');
  assert.equal(I18n.statusLabel('done', 'tr'), 'Tamamlandı');
  assert.equal(I18n.statusLabel('unlinked', 'en'), 'Unknown / Disconnected');
  assert.equal(I18n.normalizeStatus('not-a-real-status'), 'unknown');
  assert.equal(I18n.statusLabel('uh-oh', 'en'), 'Unknown / Disconnected');
  // The label alone is not the only cue: tones differ for outcomes.
  assert.notEqual(I18n.statusMeta('failed', 'en').tone, I18n.statusMeta('completed', 'en').tone);
  assert.equal(I18n.statusMeta('working', 'en').tone, 'accent');
});

test('sequence dedupe accepts new updates, rejects stale ones and always accepts snapshots', () => {
  assert.ok(Office, 'office.js must expose AOOffice');
  assert.equal(Office.shouldApplyEvent({ type: 'update', sequenceId: 4, lastSequenceId: 5 }), false);
  assert.equal(Office.shouldApplyEvent({ type: 'update', sequenceId: 5, lastSequenceId: 5 }), false);
  assert.equal(Office.shouldApplyEvent({ type: 'update', sequenceId: 6, lastSequenceId: 5 }), true);
  assert.equal(Office.shouldApplyEvent({ type: 'snapshot', sequenceId: 3, lastSequenceId: 5 }), true);
  assert.equal(Office.shouldApplyEvent({ type: 'update', sequenceId: null, lastSequenceId: 5 }), true);
  assert.equal(Office.shouldApplyEvent({ type: 'update' }), true);
  assert.equal(Office.nextSequence(5, 7, 'update'), 7);
  assert.equal(Office.nextSequence(5, 3, 'snapshot'), 3);
  assert.equal(Office.shouldApplyEvent({ type: 'update', sequenceId: 4, lastSequenceId: Office.nextSequence(5, 3, 'snapshot') }), true);
  assert.equal(Office.nextSequence(null, 2, 'update'), 2);
});

test('desk ordering is stable, project-local and exposes bounded earlier/later moves', () => {
  const keys = ['a:1', 'a:2', 's:run:3', 'a:1'];
  assert.deepEqual([...Office.sortKeys(keys, [])], ['a:1', 'a:2', 's:run:3']);
  assert.deepEqual([...Office.sortKeys(keys, ['s:run:3', 'a:2'])], ['s:run:3', 'a:2', 'a:1']);
  assert.deepEqual([...Office.moveDesk([], ['a:1', 'a:2', 'a:3'], 'a:2', 1)], ['a:1', 'a:3', 'a:2']);
  assert.deepEqual([...Office.moveDesk([], ['a:1', 'a:2', 'a:3'], 'a:1', -1)], ['a:1', 'a:2', 'a:3']);
  // The arrange UI uses adjacent earlier/later moves, which stay correct at 1, 2 or 3 columns.
  assert.deepEqual([...Office.moveDesk([], ['a:1', 'a:2', 'a:3', 'a:4'], 'a:4', -1)], ['a:1', 'a:2', 'a:4', 'a:3']);
  assert.deepEqual([...Office.moveDesk([], ['a:1', 'a:2'], 'a:2', 1)], ['a:1', 'a:2']);
  const officeSource = readFileSync(join(assets, 'office.js'), 'utf8');
  assert.doesNotMatch(officeSource, /delta:\s*(?:-?3)\b/, 'the arrange UI must not hard-code a three-column jump');
  const payload = {
    agents: [
      { agent_definition_id: 'a_1', name: 'One' },
      { agent_definition_id: 'a_2', name: 'Two' },
    ],
    unlinked_sessions: [{ run_id: 'r1', agent_id: 'x', platform: 'opencode' }],
  };
  const entries = [...Office.officeEntries(payload, ['s:r1:x'])];
  assert.deepEqual(entries.map((entry) => entry.key), ['s:r1:x', 'a:a_1', 'a:a_2']);
  assert.equal(entries[0].kind, 'session');
});

test('office summary reconciles agent and ghost status buckets instead of trusting summary.working', () => {
  const payload = {
    summary: { agents: 4, working: 0, unknown: 0, completed: 0, unlinked: 0 },
    agents: [
      { agent_definition_id: 'a_1', name: 'Completed', status: 'completed' },
      { agent_definition_id: 'a_2', name: 'Working', status: 'working' },
      { agent_definition_id: 'a_3', name: 'Dispatching', status: 'dispatching' },
      { agent_definition_id: 'a_4', name: 'Unknown', status: 'unknown' },
    ],
    unlinked_sessions: [
      { run_id: 'r1', agent_id: 'x', platform: 'opencode', status: 'completed' },
      { run_id: 'r2', agent_id: 'y', platform: 'codex', status: 'working' },
      { run_id: 'r3', agent_id: 'z', platform: 'cursor', status: 'failed' },
    ],
  };
  const entries = [...Office.officeEntries(payload, [])];
  const summary = Office.summaryCounts(payload, entries);
  assert.equal(summary.desks, 7);
  assert.equal(summary.agents, 4);
  assert.equal(summary.unlinked, 3);
  assert.equal(summary.completed, 2, 'completed ghosts count too');
  assert.equal(summary.working, 2);
  assert.equal(summary.dispatching, 1, 'a dispatching desk is its own bucket, never folded into a zero active count');
  assert.equal(summary.unknown, 1);
  assert.equal(summary.failed, 1);
});

test('index.html loads external modules in order and exposes required containers', () => {
  const i18nIndex = html.indexOf('/assets/i18n.js');
  const officeIndex = html.indexOf('/assets/office.js');
  const appIndex = html.indexOf('/assets/app.js');
  assert.ok(i18nIndex > 0, 'index.html must reference /assets/i18n.js');
  assert.ok(officeIndex > i18nIndex, 'office.js must load after i18n.js');
  assert.ok(appIndex > officeIndex, 'app.js must load after office.js');
  for (const id of [
    'project-select', 'project-tabs', 'tab-office', 'tab-runs', 'tab-board', 'tab-workspaces', 'tab-method',
    'view-overview', 'view-office', 'view-runs', 'view-board', 'view-workspaces', 'view-method',
    'global-totals', 'project-grid', 'overview-state', 'overview-notice',
    'office-root', 'office-summary', 'office-status', 'office-theme', 'office-arrange', 'office-motion',
    'agent-inspector', 'inspector-shell', 'inspector-content', 'inspector-pinned',
    'settings', 'settings-toggle', 'set-language', 'set-appearance', 'set-office-theme', 'set-reduced-motion',
    'run-list', 'run-detail', 'run-search', 'filters', 'board', 'board-kpis', 'workspaces',
    'toast', 'tooltip', 'live-badge', 'live-text', 'demo-badge',
  ]) {
    assert.match(html, new RegExp(`id="${id}"`), `missing required container #${id}`);
  }
  assert.match(html, /Evidence Console/, 'the existing product title must be preserved');
  assert.match(html, /#i-office/, 'office tab icon symbol must exist');
});

test('focus and live-region semantics avoid duplicate chatter', () => {
  for (const id of ['updated', 'run-detail', 'office-summary']) {
    const tag = html.match(new RegExp(`<[^>]+id="${id}"[^>]*>`))?.[0] || '';
    assert.doesNotMatch(tag, /aria-live=/, `#${id} must not announce every refresh`);
  }
  assert.match(html, /id="screen-reader-status"[^>]*class="sr-only"[^>]*role="status"[^>]*aria-live="polite"/, 'a single polite status region must exist');
  assert.match(html, /id="all-projects-link"[^>]*aria-label="All projects"[^>]*data-i18n-aria-label="nav.back"/, 'the icon-only back link keeps an accessible name');
  assert.match(html, /id="settings"[^>]*role="dialog"[^>]*aria-modal="true"/, 'the settings surface is the single modal dialog container');
  assert.doesNotMatch(html, /aria-modal="false"/, 'no modal container may declare itself non-modal');
});

test('office summary and inspector copy have translated, placeholder-free keys', () => {
  const keys = [
    'office.summary.completed',
    'office.summary.working',
    'office.summary.dispatching',
    'office.summary.unknown',
    'office.summary.failed',
    'office.summary.unlinked',
    'office.summaryBucket',
    'office.inspectStatus',
    'office.openSessionStatus',
    'office.moveEarlier',
    'office.moveLater',
    'inspector.nativeStopNotAttested',
    'inspector.nativeStopNotObserved',
    'inspector.verificationNoRun',
  ];
  for (const key of keys) {
    assert.ok(key in I18n.dictionary.en, `missing EN translation for ${key}`);
    assert.ok(key in I18n.dictionary.tr, `missing TR translation for ${key}`);
  }
  assert.equal(I18n.t('office.summary.completed', { count: 2 }), '2 completed');
  assert.equal(I18n.t('office.summary.dispatching', { count: 1 }), '1 dispatching');
  I18n.setLanguage('tr', { persist: false, announce: false });
  assert.equal(I18n.t('office.summary.completed', { count: 2 }), '2 tamamlandı');
  assert.equal(I18n.t('office.inspectStatus', { name: 'Ada', status: 'Tamamlandı' }), 'Ada için ajan denetçisini aç — Tamamlandı');
  for (const key of keys) {
    const rendered = I18n.t(key, { count: 2, label: 'X', name: 'Ada', status: 'Tamamlandı', platform: 'opencode' });
    assert.doesNotMatch(rendered, /\{\w+\}/, `TR ${key} must not leak a raw placeholder`);
  }
  I18n.setLanguage('en', { persist: false, announce: false });
  for (const key of keys) {
    const rendered = I18n.t(key, { count: 2, label: 'X', name: 'Ada', status: 'Completed', platform: 'opencode' });
    assert.doesNotMatch(rendered, /\{\w+\}/, `EN ${key} must not leak a raw placeholder`);
  }
});

test('CSP-safe markup: no inline scripts, handlers or external subresources', () => {
  assert.doesNotMatch(html, /<script(?![^>]*\bsrc=)/i, 'inline <script> is forbidden by CSP');
  assert.doesNotMatch(html, /<style[\s>]/i, 'inline <style> is forbidden by CSP');
  assert.doesNotMatch(html, /\son[a-z]+\s*=/i, 'inline event handlers are forbidden');
  assert.doesNotMatch(html, /\sstyle\s*=/i, 'inline style attributes are unnecessary and CSP-hostile');
  for (const match of html.matchAll(/\b(?:src|href)="([^"]+)"/g)) {
    const value = match[1];
    assert.ok(
      value.startsWith('#') || value.startsWith('/assets/'),
      `unexpected external or remote subresource: ${value}`,
    );
  }
  for (const name of ['i18n.js', 'office.js', 'app.js', 'app.css']) {
    assert.ok(statSync(join(assets, name)).size > 0, `${name} must exist`);
  }
  const combined = ['app.js', 'office.js', 'i18n.js']
    .map((name) => readFileSync(join(assets, name), 'utf8'))
    .join('\n');
  assert.doesNotMatch(combined, /https?:\/\/(?!www\.w3\.org\/2000\/svg)/, 'no remote URLs or CDNs');
  assert.doesNotMatch(combined, /\.innerHTML\s*=/, 'dynamic content must use textContent');
  assert.match(combined, /EventSource/, 'live updates must use EventSource');
});

test('successful finish is not labelled verified without evidence', () => {
  const app = readFileSync(join(assets, 'app.js'), 'utf8');
  assert.match(app, /status === 'success'\) return t\('runStatus\.completed'\)/);
  assert.doesNotMatch(app, /status === 'success'\) return t\('runStatus\.verified'\)/);
});

test('project-scoped endpoints and legacy fallbacks are wired', () => {
  const app = readFileSync(join(assets, 'app.js'), 'utf8');
  assert.match(app, /\/api\/global/);
  assert.match(app, /\/api\/projects/);
  assert.match(app, /\/state/);
  assert.match(app, /\/runs\//);
  assert.match(app, /\/office/);
  assert.match(app, /\/agents\//);
  assert.match(app, /\/events/);
  assert.match(app, /\/api\/state/, 'the legacy ETag endpoint must remain as a fallback');
  assert.match(app, /\/api\/run\//, 'the legacy run endpoint must remain as a fallback');
  assert.match(app, /If-None-Match/, 'ETag polling fallback must be kept');
  assert.match(app, /encodeURIComponent\(projectId\)/);
  assert.match(app, /state\.projectId !== projectId/, 'stale responses from another project must be rejected');
});

test('inspector never invents terminal content', () => {
  const office = readFileSync(join(assets, 'office.js'), 'utf8');
  assert.match(office, /terminalUnsupported/);
  assert.match(office, /terminalNoFake/);
  assert.match(office, /supported/);
  assert.doesNotMatch(office, /command:|prompt|\$ .*&&/i);
  assert.match(office, /agent_definition_id/);
  assert.match(office, /unlinked_sessions/);
});
