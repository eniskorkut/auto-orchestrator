(() => {
  'use strict';

  const state = {
    data: null,
    selected: null,
    detail: null,
    detailToken: 0,
    fetching: false,
    filter: 'all',
    query: '',
    tab: 'runs',
    boardRun: null,
    lastFetch: 0,
    demo: false,
    etag: null,
    runListSignature: '',
    searchTimer: null,
    toastTimer: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const RUN_DISPLAY_LIMIT = 250;
  const AGENT_DISPLAY_LIMIT = 120;
  const MUTATION_DISPLAY_LIMIT = 250;

  /* ── DOM and formatting ─────────────────────────────────────────────── */
  function h(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value == null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'dataset') {
        for (const [dataKey, dataValue] of Object.entries(value)) {
          if (dataValue != null) node.dataset[dataKey] = String(dataValue);
        }
      } else if (key === 'style') {
        for (const [styleKey, styleValue] of Object.entries(value)) {
          node.style.setProperty(styleKey, String(styleValue));
        }
      } else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, String(value));
    }
    append(node, children);
    return node;
  }

  function append(node, children) {
    for (const child of children.flat(Infinity)) {
      if (child == null || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function icon(name, className = 'icon') {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', className);
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function fmtDuration(seconds, precision = false) {
    if (seconds == null || !Number.isFinite(Number(seconds))) return '—';
    const value = Math.max(0, Number(seconds));
    if (value === 0) return '0s';
    if (value < 1) return `${value.toFixed(precision ? 2 : 1)}s`;
    if (value < 60) return `${value.toFixed(precision ? 1 : 0)}s`;
    if (value < 3600) {
      const minutes = Math.floor(value / 60);
      const rest = Math.round(value % 60);
      return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
    }
    if (value < 86400) {
      const hours = Math.floor(value / 3600);
      const minutes = Math.round((value % 3600) / 60);
      return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
    }
    const days = Math.floor(value / 86400);
    const hours = Math.round((value % 86400) / 3600);
    return hours ? `${days}d ${hours}h` : `${days}d`;
  }

  function fmtRelative(timestamp) {
    if (!timestamp) return '—';
    const diff = Math.max(0, Date.now() / 1000 - Number(timestamp));
    if (diff < 5) return 'just now';
    if (diff < 60) return `${Math.floor(diff)}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
    return new Date(timestamp * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  function fmtClock(timestamp) {
    if (!timestamp) return '—';
    return new Date(timestamp * 1000).toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  }

  function fmtDateTime(timestamp) {
    if (!timestamp) return '—';
    return `${new Date(timestamp * 1000).toLocaleDateString([], {
      month: 'short', day: 'numeric',
    })} ${fmtClock(timestamp)}`;
  }

  function fmtNumber(value) {
    if (value == null) return '—';
    return Number(value).toLocaleString();
  }

  function pct(value) {
    if (value == null) return '—';
    return `${Math.round(Number(value) * 100)}%`;
  }

  function shortId(value) {
    if (!value) return '—';
    return String(value).slice(0, 8);
  }

  function sum(values) {
    return values.reduce((total, value) => total + (Number(value) || 0), 0);
  }

  function statusMeta(run) {
    if (!run) return { key: 'unknown', label: 'Unknown', tone: 'neutral' };
    if (run.status === 'invalid') return { key: 'invalid', label: 'Invalid log', tone: 'bad' };
    if (run.status === 'open') {
      return run.live
        ? { key: 'live', label: 'Live · open', tone: 'info' }
        : { key: 'open', label: 'Open · no terminal', tone: 'warn' };
    }
    if (run.status === 'success') {
      if (run.verified) return { key: 'verified', label: 'Verified', tone: 'ok' };
      if (run.self_reported) return { key: 'unverified', label: 'Self-reported', tone: 'warn' };
      return { key: 'unverified', label: 'Unverified', tone: 'warn' };
    }
    if (run.status === 'failed') return { key: 'failed', label: 'Failed', tone: 'bad' };
    if (run.status === 'cancelled') return { key: 'cancelled', label: 'Cancelled', tone: 'warn' };
    return { key: run.status || 'unknown', label: run.status || 'Unknown', tone: 'neutral' };
  }

  function toneClass(tone) {
    return { ok: 'verified', info: 'live', warn: 'claimed', bad: 'failed' }[tone] || '';
  }

  function platformLabel(value) {
    return String(value || 'unknown').toUpperCase();
  }

  function relativeOffset(started, at) {
    if (at == null || !started) return '—';
    return `+${fmtDuration(Math.max(0, at - started))}`;
  }

  function toast(message) {
    const node = $('#toast');
    node.textContent = message;
    node.classList.add('visible');
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => node.classList.remove('visible'), 1900);
  }

  async function copyText(value) {
    try {
      await navigator.clipboard.writeText(value);
      toast('Copied to clipboard');
    } catch {
      const area = h('textarea', { style: { position: 'fixed', opacity: '0' } });
      area.value = value;
      document.body.append(area);
      area.select();
      try {
        document.execCommand('copy');
        toast('Copied to clipboard');
      } catch {
        toast('Copy failed');
      }
      area.remove();
    }
  }

  function copyButton(value, label = 'Copy') {
    return h('button', {
      class: 'copy-btn', type: 'button', title: label, 'aria-label': label,
      onclick: (event) => { event.stopPropagation(); copyText(value); },
    }, icon('copy'));
  }

  /* ── Tooltip ────────────────────────────────────────────────────────── */
  function setupTooltip() {
    const tooltip = $('#tooltip');
    let active = null;
    let bounds = { width: 0, height: 0 };
    let frame = null;
    let pointer = { x: 0, y: 0 };

    function show(target, x, y) {
      const text = target.getAttribute('data-tip');
      if (!text) return;
      active = target;
      tooltip.textContent = text;
      tooltip.classList.add('visible');
      const rect = tooltip.getBoundingClientRect();
      bounds = { width: rect.width, height: rect.height };
      move(x, y);
    }

    function move(x, y) {
      if (!tooltip.classList.contains('visible')) return;
      const pad = 12;
      let left = x + pad;
      let top = y + pad;
      if (left + bounds.width > window.innerWidth - 8) left = x - bounds.width - pad;
      if (top + bounds.height > window.innerHeight - 8) top = y - bounds.height - pad;
      tooltip.style.left = `${Math.max(8, left)}px`;
      tooltip.style.top = `${Math.max(8, top)}px`;
    }

    function hide() {
      active = null;
      tooltip.classList.remove('visible');
    }

    document.addEventListener('pointerover', (event) => {
      const target = event.target.closest?.('[data-tip]');
      if (target) show(target, event.clientX, event.clientY);
    });
    document.addEventListener('pointermove', (event) => {
      if (!active) return;
      pointer = { x: event.clientX, y: event.clientY };
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        move(pointer.x, pointer.y);
      });
    });
    document.addEventListener('pointerout', (event) => {
      const target = event.target.closest?.('[data-tip]');
      if (target && !target.contains(event.relatedTarget)) hide();
    });
    document.addEventListener('focusin', (event) => {
      const target = event.target.closest?.('[data-tip]');
      if (!target) return;
      const rect = target.getBoundingClientRect();
      show(target, rect.left + rect.width / 2, rect.bottom);
    });
    document.addEventListener('focusout', (event) => {
      if (event.target.closest?.('[data-tip]')) hide();
    });
    window.addEventListener('scroll', hide, { passive: true });
  }

  /* ── Data fetching ──────────────────────────────────────────────────── */
  async function refresh(force = false) {
    if (state.fetching) return;
    if (document.hidden && !force) return;
    state.fetching = true;
    const button = $('#refresh');
    if (force) button.classList.add('busy');
    try {
      const headers = {};
      if (state.etag) headers['If-None-Match'] = state.etag;
      const response = await fetch('/api/state', { cache: 'no-store', headers });
      if (response.status === 304) {
        state.lastFetch = Date.now() / 1000;
        renderUpdated();
        return;
      }
      if (!response.ok) throw new Error(`State request failed (${response.status})`);
      state.etag = response.headers.get('ETag') || null;
      const data = await response.json();
      const changed = !state.data || data.fingerprint !== state.data.fingerprint;
      state.data = data;
      state.demo = Boolean(data.demo);
      state.lastFetch = Date.now() / 1000;
      if (changed || force) {
        renderAll();
        await syncDetail();
      }
      renderUpdated();
    } catch (error) {
      $('#live-badge').dataset.state = 'idle';
      $('#live-text').textContent = 'offline';
      if (!state.data) renderFatal(error);
    } finally {
      state.fetching = false;
      button.classList.remove('busy');
    }
  }

  async function syncDetail() {
    if (!state.data?.runs?.length) {
      state.selected = null;
      state.detail = null;
      renderDetail();
      return;
    }
    const exists = state.data.runs.some((run) => run.id === state.selected);
    if (!exists) state.selected = state.data.runs[0].id;
    const summary = state.data.runs.find((run) => run.id === state.selected);
    if (!summary) return;
    if (state.detail && state.detail.run?.fingerprint === summary.fingerprint) return;
    await selectRun(state.selected, { keepList: true });
  }

  async function selectRun(runId, options = {}) {
    state.selected = runId;
    if (!options.keepList) {
      state.detail = null;
      renderDetail();
    }
    renderRunList();
    const token = ++state.detailToken;
    try {
      const response = await fetch(`/api/run/${encodeURIComponent(runId)}`, { cache: 'no-store' });
      const payload = await response.json();
      if (token !== state.detailToken) return;
      if (!response.ok) throw new Error(payload.error || `Run request failed (${response.status})`);
      state.detail = payload;
      renderDetail();
    } catch (error) {
      if (token !== state.detailToken) return;
      state.detail = { error: error.message, run: { id: runId } };
      renderDetail();
    }
  }

  /* ── Chrome and overview ────────────────────────────────────────────── */
  function renderAll() {
    if (!state.data) return;
    state.demo = Boolean(state.data.demo);
    state.boardRun = state.data.supervisor?.runs?.some((run) => run.id === state.boardRun)
      ? state.boardRun
      : state.data.supervisor?.runs?.[0]?.id || null;

    $('#project-path').textContent = state.data.project || '—';
    $('#project-path').title = state.data.project || '';
    $('#demo-badge').classList.toggle('hidden', !state.demo);
    const demo = $('#demo-badge');
    demo.textContent = 'DEMO DATA';
    demo.title = 'Synthetic evidence. Not a benchmark and not written to disk.';

    renderOverview();
    renderFilters();
    renderRunList();
    renderBoard();
    renderWorkspaces();
    renderDetail();
    updateLiveBadge();
    $('#tab-runs-count').textContent = fmtNumber(state.data.overview?.runs || 0);
    $('#tab-board-count').textContent = fmtNumber(
      state.data.supervisor?.runs?.reduce((total, run) => total + run.tasks.length, 0) || 0,
    );
    $('#tab-workspaces-count').textContent = fmtNumber(state.data.worktrees?.length || 0);
  }

  function renderOverview() {
    const overview = state.data.overview || {};
    const host = $('#overview');
    clear(host);
    const items = [
      {
        label: 'Runs recorded',
        value: fmtNumber(overview.runs || 0),
        sub: `${fmtNumber(overview.verified || 0)} gate verified · ${fmtNumber(overview.self_reported || 0)} claimed`,
        tone: '',
      },
      {
        label: 'Gate verified',
        value: `${fmtNumber(overview.verified || 0)}`,
        sub: `${pct(overview.verified_rate)} of runs · ${fmtNumber(overview.benchmark_valid || 0)} benchmark-valid`,
        tone: 'accent',
        title: 'Verified means a quality-gate event was re-checked against the current policy. It is not proof of full correctness.',
      },
      {
        label: 'Open runs',
        value: fmtNumber(overview.open || 0),
        sub: `${fmtNumber(overview.live || 0)} live now · ${fmtNumber(overview.open_agents || 0)} open agent span${overview.open_agents === 1 ? '' : 's'}`,
        tone: overview.live ? 'warn' : '',
        title: 'Live is based on event-log freshness. An open span is not evidence that a process is still running.',
      },
      {
        label: 'Agent spans',
        value: fmtNumber(overview.agents || 0),
        sub: `${fmtNumber(overview.mutations || 0)} mutations · ${Object.keys(overview.platforms || {}).length} platforms`,
        tone: '',
      },
      {
        label: 'Peak overlap',
        value: fmtDuration(overview.overlap_seconds || 0),
        sub: 'one run · not time saved',
        tone: 'warn',
        title: 'Largest union-of-spans overlap in a single run. The CLI explicitly qualifies this as not measured human time saved.',
      },
    ];
    for (const item of items) {
      const card = h('div', { class: `kpi ${item.tone}`, title: item.title || null },
        h('div', { class: 'kpi-label', text: item.label }),
        h('div', { class: 'kpi-value' }, item.value, item.unit ? h('small', { text: ` ${item.unit}` }) : null),
        h('div', { class: 'kpi-sub', text: item.sub }),
      );
      host.append(card);
    }
  }

  function renderFilters() {
    const runs = state.data?.runs || [];
    const filters = [
      { key: 'all', label: 'All', count: runs.length },
      { key: 'verified', label: 'Verified', count: runs.filter((run) => run.verified).length },
      { key: 'open', label: 'Open', count: runs.filter((run) => run.status === 'open').length },
      { key: 'failed', label: 'Failed', count: runs.filter((run) => run.status === 'failed').length },
    ];
    const host = $('#filters');
    clear(host);
    for (const filter of filters) {
      host.append(h('button', {
        class: 'filter', type: 'button', 'aria-pressed': String(state.filter === filter.key),
        onclick: () => {
          clearTimeout(state.searchTimer);
          state.filter = filter.key;
          renderFilters();
          renderRunList();
        },
      }, filter.label, h('b', { text: filter.count })));
    }
  }

  function renderRunList() {
    const host = $('#run-list');
    const runs = (state.data?.runs || []).filter((run) => {
      if (state.filter === 'verified' && !run.verified) return false;
      if (state.filter === 'open' && run.status !== 'open') return false;
      if (state.filter === 'failed' && run.status !== 'failed') return false;
      if (state.filter === 'claimed' && !run.self_reported) return false;
      if (!state.query) return true;
      const haystack = `${run.id} ${run.platform} ${run.status} ${statusMeta(run).label}`.toLowerCase();
      return haystack.includes(state.query.toLowerCase());
    });
    const visibleRuns = runs.slice(0, RUN_DISPLAY_LIMIT);
    const signature = [
      state.filter, state.query, state.selected, runs.length,
      visibleRuns.map((run) => `${run.id}:${run.fingerprint}:${run.status}:${run.verified}:${run.self_reported}`).join(','),
    ].join('|');
    if (signature === state.runListSignature) return;
    state.runListSignature = signature;
    clear(host);
    if (!runs.length) {
      host.append(h('div', { class: 'rail-empty' },
        state.data?.runs?.length ? 'No runs match this filter.' : 'No native run evidence yet.',
        h('code', { text: 'python tools/dashboard.py --demo' }),
      ));
      return;
    }
    for (const run of visibleRuns) {
      const meta = statusMeta(run);
      const verifiedClass = run.verified ? 'meta-verified' : run.self_reported ? 'meta-claimed'
        : run.status === 'open' ? 'meta-open' : run.status === 'failed' ? 'meta-failed' : '';
      const statusText = run.verified ? '✓ verified' : run.self_reported ? 'claimed'
        : run.status === 'open' ? 'open' : run.status === 'success' ? 'unverified' : run.status;
      const item = h('button', {
        class: 'run-item', type: 'button', 'data-status': meta.key,
        'aria-current': String(state.selected === run.id),
        title: `${run.id} · ${meta.label}`,
        onclick: () => selectRun(run.id),
      },
        h('div', { class: 'run-top' },
          h('span', { class: 'run-id', text: run.id }),
          h('span', { class: 'run-ago', text: fmtRelative(run.last_event_at) }),
        ),
        h('div', { class: 'run-meta' },
          h('span', { class: 'platform', text: run.platform }),
          h('span', { text: `${run.agent_count || 0} agent${run.agent_count === 1 ? '' : 's'}` }),
          h('span', { class: 'sep' }),
          h('span', { text: fmtDuration(run.duration_seconds) }),
          h('span', { class: 'sep' }),
          h('span', { class: verifiedClass, text: statusText }),
        ),
      );
      host.append(item);
    }
    if (runs.length > visibleRuns.length) {
      host.append(h('div', { class: 'rail-empty', text: `Showing the latest ${visibleRuns.length} of ${runs.length} matching runs.` }));
    }
  }

  function updateLiveBadge() {
    const badge = $('#live-badge');
    const text = $('#live-text');
    if (state.demo) {
      badge.dataset.state = 'demo';
      text.textContent = 'demo mode';
      return;
    }
    const overview = state.data?.overview;
    if (overview?.live) {
      badge.dataset.state = 'live';
      text.textContent = `${overview.live} live`;
    } else {
      badge.dataset.state = 'idle';
      text.textContent = overview?.runs ? 'idle' : 'no runs';
    }
  }

  function renderUpdated() {
    if (!state.lastFetch) return;
    $('#updated').textContent = `updated ${fmtRelative(state.lastFetch).replace(' ago', ' ago')}`;
  }

  function renderFatal(error) {
    const host = $('#run-detail');
    clear(host);
    host.append(h('div', { class: 'empty-state' },
      h('div', { class: 'empty-inner' },
        h('div', { class: 'empty-mark' }, icon('alert')),
        h('h2', { text: 'Dashboard could not read evidence' }),
        h('p', { text: error.message || 'Unknown local error.' }),
        h('code', { text: 'python tools/dashboard.py --demo' }),
      ),
    ));
  }

  /* ── Detail header, KPIs, notices ───────────────────────────────────── */
  function renderDetail() {
    const host = $('#run-detail');
    clear(host);
    if (!state.data) return;
    if (!state.selected) {
      host.append(emptyRunsState());
      return;
    }
    if (!state.detail) {
      host.append(h('div', { class: 'empty-state' },
        h('div', { class: 'empty-inner' },
          h('div', { class: 'empty-mark' }, h('span', { class: 'spinner' })),
          h('h2', { text: 'Reading evidence…' }),
          h('p', { text: 'Loading the selected run log. The console never guesses missing fields.' }),
        ),
      ));
      return;
    }
    if (state.detail.error) {
      host.append(h('div', { class: 'empty-state' },
        h('div', { class: 'empty-inner' },
          h('div', { class: 'empty-mark' }, icon('alert')),
          h('h2', { text: 'Run evidence is unavailable' }),
          h('p', { text: state.detail.error }),
          h('code', { text: `python tools/perf.py --root . diagnose --run-id ${state.detail.run?.id || ''}` }),
        ),
      ));
      return;
    }

    const { run, agents = [], checks = [], events = [], mutations = [] } = state.detail;
    const meta = statusMeta(run);
    const card = h('div', { class: 'detail-card' });

    card.append(h('div', { class: 'detail-head' },
      h('div', {},
        h('div', { class: 'eyebrow' },
          h('span', { text: 'run / evidence' }),
          h('span', { class: 'platform', text: run.platform }),
          run.partial ? h('span', { class: 'pill claimed', text: 'partial write' }) : null,
        ),
        h('h1', { class: 'run-title' }, run.id, copyButton(run.id, 'Copy run ID')),
        h('div', { class: 'run-sub' },
          h('span', { text: `started ${fmtDateTime(run.started_at)}` }),
          h('span', { text: `${fmtDuration(run.duration_seconds)} elapsed` }),
          h('span', { text: `${run.agent_count || 0} agent span${run.agent_count === 1 ? '' : 's'}` }),
          h('span', { text: `peak ${run.max_concurrency || 0} concurrent` }),
          run.checks_failed ? h('span', { class: 'meta-failed', text: `${run.checks_failed} failed check${run.checks_failed === 1 ? '' : 's'}` }) : null,
        ),
      ),
      h('div', { class: 'status-stack' },
        h('span', { class: `pill ${toneClass(meta.tone)}`, text: meta.label }),
        run.verified ? h('span', { class: 'pill verified', text: 'quality gate' }) : null,
        run.self_reported ? h('span', { class: 'pill claimed', text: 'claim only' }) : null,
        run.status === 'open' ? h('span', { class: 'pill open', text: run.live ? 'event stream fresh' : 'no finish event' }) : null,
      ),
    ));

    card.append(h('div', { class: 'detail-kpis' },
      kpi('elapsed', fmtDuration(run.duration_seconds)),
      kpi('agents', String(run.agent_count || 0),
        !run.agent_count ? 'none recorded' : run.open_agent_count ? `${run.open_agent_count} open` : 'all stopped'),
      kpi('peak concurrency', String(run.max_concurrency || 0), 'observed'),
      kpi('observed overlap', fmtDuration(run.observed_overlap_seconds), 'not time saved', 'warn'),
      kpi('mutations', fmtNumber(run.mutation_count), 'tool-observed'),
      kpi('checks', `${run.checks_passed || 0}/${(run.checks_passed || 0) + (run.checks_failed || 0)}`,
        !(run.checks_passed || run.checks_failed) ? 'none recorded'
          : run.checks_failed ? `${run.checks_failed} failed` : 'native exit codes',
        run.checks_failed ? 'bad' : 'ok'),
    ));

    const body = h('div', { class: 'detail-body' });
    body.append(verificationNotice(run));
    body.append(routeInsight(run));
    body.append(section(
      'Native agent timeline',
      'Opaque IDs · spans from native tool events',
      h('div', { style: { display: 'grid', gap: '8px' } },
        timeline(run, state.detail),
        benchmarkNote(run),
      ),
    ));
    body.append(section(
      'Evidence chain',
      'completion, checks and verification are separate facts',
      evidenceChain(run, state.detail),
    ));
    if (agents.length) body.append(section(
      'Agent spans',
      `${agents.length} recorded · roles are not persisted`,
      agentTable(agents, run),
    ));
    if (checks.length) body.append(section(
      'Native checks',
      'only exact configured commands are observed',
      checkTable(checks, run),
    ));
    if (run.diagnostics?.length) {
      body.append(h('div', { class: 'notice notice-warn' },
        icon('alert'),
        h('div', {},
          h('strong', { text: 'Lifecycle diagnostic recorded. ' }),
          'The log contains categorical reasons only: ',
          h('code', { text: run.diagnostics.join(', ') }),
          `. Classification: ${run.diagnosis}. No terminal event was invented.`,
        ),
      ));
    }
    body.append(section('Reproduce this readout', 'the console only reads; the CLI remains the source of truth',
      h('div', { style: { display: 'grid', gap: '7px' } },
        commandRow(state.detail.report_command),
        commandRow(state.detail.diagnose_command),
      ),
    ));
    body.append(rawEvents(events, run));
    card.append(body);
    host.append(card);
  }

  function benchmarkNote(run) {
    if (run.has_valid_estimate) {
      return h('div', { class: 'notice notice-ok' },
        icon('shield'),
        h('div', {},
          h('strong', { text: 'Benchmark-valid overlap estimate. ' }),
          `${fmtDuration(run.estimated_parallelism_seconds)} of estimated scheduling overlap. This is still not measured human time saved; only matched serial/orchestrated repeats establish a speedup.`,
        ),
      );
    }
    return h('div', { class: 'notice' },
      icon('clock'),
      h('div', {},
        h('strong', { text: 'No benchmark-valid estimate. ' }),
        run.status === 'open'
          ? 'The run has no terminal event, so no estimate can qualify.'
          : run.reason || 'The recorded evidence does not meet the CLI qualification rules.',
      ),
    );
  }

  function kpi(label, value, sub, tone = '') {
    return h('div', { class: 'detail-kpi' },
      h('div', { class: 'k', text: label }),
      h('div', { class: 'v' },
        h('span', { class: tone, text: value }),
        sub ? h('small', { text: ` ${sub}` }) : null,
      ),
    );
  }

  function section(title, hint, content) {
    return h('div', { class: 'detail-section' },
      h('div', { class: 'section-head' },
        h('h2', { text: title }),
        hint ? h('span', { class: 'hint', text: hint }) : null,
      ),
      content,
    );
  }

  function commandRow(command) {
    return h('div', { class: 'command-block' },
      h('code', { text: command }),
      copyButton(command, 'Copy command'),
    );
  }

  function verificationNotice(run) {
    if (run.status === 'invalid') {
      return h('div', { class: 'notice notice-bad' },
        icon('alert'), h('div', {}, h('strong', { text: 'Unreadable evidence. ' }), run.error || run.reason || 'The log is malformed.'),
      );
    }
    if (run.status === 'open') {
      return h('div', { class: 'notice notice-warn' },
        icon('alert'),
        h('div', {},
          h('strong', { text: 'No terminal event. ' }),
          run.open_agent_count
            ? `${run.open_agent_count} agent span${run.open_agent_count === 1 ? ' has' : 's have'} no stop event. They are shown as open, not assumed to be running.`
            : 'This run has no finish event. Completion and verification are unknown.',
        ),
      );
    }
    if (run.status === 'failed' || run.status === 'cancelled') {
      return h('div', { class: 'notice notice-bad' },
        icon('alert'), h('div', {}, h('strong', { text: `Run ${run.status}. ` }), 'No verification can be inferred from a non-successful terminal state.'),
      );
    }
    if (run.verified) {
      return h('div', { class: 'notice notice-ok' },
        icon('shield'),
        h('div', {},
          h('strong', { text: 'Gate verified. ' }),
          run.verification?.reason || 'All configured checks passed after the last observed mutation.',
          run.verification?.check_ids?.length
            ? h('span', { class: 'check-chips' }, ...run.verification.check_ids.map((id) => h('span', { class: 'check-chip', 'data-pass': 'true', text: id })))
            : null,
        ),
      );
    }
    if (run.verification?.status === 'rejected') {
      return h('div', { class: 'notice notice-bad' },
        icon('alert'),
        h('div', {}, h('strong', { text: 'Verification rejected. ' }), run.verification.reason,
          ' The completion remains recorded, but the claim does not become evidence.'),
      );
    }
    if (run.self_reported) {
      return h('div', { class: 'notice notice-warn' },
        icon('alert'),
        h('div', {}, h('strong', { text: 'Self-reported verification. ' }), run.verification?.reason || 'No native quality gate attested this run.',
          ' It is not counted as verified in the overview.'),
      );
    }
    return h('div', { class: 'notice notice-warn' },
      icon('alert'),
      h('div', {}, h('strong', { text: 'Finished, not verified. ' }), run.reason || 'No verification event was recorded.',
        run.checks_failed ? ` ${run.checks_failed} configured check${run.checks_failed === 1 ? '' : 's'} failed.` : '',
      ),
    );
  }

  function routeInsight(run) {
    const route = run.route;
    if (!route) {
      return h('div', { class: 'insight' },
        h('span', { class: 'insight-badge', text: 'router' }),
        h('div', { class: 'insight-text' }, h('strong', { text: 'No routing advice recorded. ' }), 'This platform or run may not expose the prompt hook. Absence is not a recommendation.'),
        h('div', { class: 'insight-side', text: 'advisory only' }),
      );
    }
    const followed = run.routing_advice_followed;
    const mode = route.mode === 'single' ? 'a single agent' : `up to ${route.max_agents} parallel agents`;
    let text;
    let tone = '';
    if (followed === true) {
      text = `Observed ${run.native_starts} native start${run.native_starts === 1 ? '' : 's'} and peak concurrency ${run.max_concurrency}. The advice was followed.`;
    } else if (followed === false) {
      tone = 'warn';
      text = `Observed ${run.native_starts} native start${run.native_starts === 1 ? '' : 's'} and peak concurrency ${run.max_concurrency}; this exceeded or contradicted the advice.`;
    } else {
      text = 'Adherence could not be computed from the recorded events.';
    }
    const side = [
      route.selected_count != null ? `${route.selected_count} skill${route.selected_count === 1 ? '' : 's'}` : null,
      route.latency_ms != null ? `${Math.round(route.latency_ms)}ms route` : null,
      'advisory only',
    ].filter(Boolean).join(' · ');
    return h('div', { class: 'insight', 'data-tone': tone || null },
      h('span', { class: 'insight-badge', text: followed === false ? 'exceeded' : 'router' }),
      h('div', { class: 'insight-text' }, h('strong', { text: `Router advised ${mode}. ` }), text),
      h('div', { class: 'insight-side', text: side }),
    );
  }

  /* ── Timeline ────────────────────────────────────────────────────────── */
  function overlapIntervals(spans) {
    if (spans.length < 2) return [];
    const points = [];
    for (const span of spans) {
      points.push([span.start, 1]);
      points.push([span.end, -1]);
    }
    points.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    let count = 0;
    let start = null;
    const bands = [];
    for (const [at, delta] of points) {
      const before = count;
      count += delta;
      if (before < 2 && count >= 2) start = at;
      else if (before >= 2 && count < 2 && start != null) {
        if (at > start) bands.push([start, at]);
        start = null;
      }
    }
    return bands;
  }

  function axisTicks(from, to) {
    const span = Math.max(0.001, to - from);
    const steps = [0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
    const step = steps.find((value) => span / value <= 6) || 7200;
    const ticks = [];
    let at = Math.ceil(from / step) * step;
    if (at - from < step * 0.15) at += step;
    for (; at <= to + 0.0001 && ticks.length < 12; at += step) {
      ticks.push(at);
    }
    return { ticks, step };
  }

  function timeline(run, detail) {
    const spans = detail.agents || [];
    const renderSpans = spans.slice(0, AGENT_DISPLAY_LIMIT);
    const mutations = detail.mutations || [];
    const checks = detail.checks || [];
    const started = run.started_at || 0;
    const ends = [
      run.last_event_at,
      run.finished_at,
      ...spans.map((span) => span.end),
      ...checks.map((check) => check.at),
    ].filter((value) => Number.isFinite(value) && value > 0);
    let end = Math.max(started + 1, ...ends);
    const pad = Math.max(0.08, (end - started) * 0.018);
    const from = started - pad;
    const to = end + pad;
    const span = to - from;
    const position = (at) => ((at - from) / span) * 100;
    const clamp = (value) => Math.max(0, Math.min(100, value));

    const wrap = h('div', { class: 'timeline-wrap' },
      h('div', { class: 'timeline-legend' },
        h('span', {}, h('i', { class: 'legend-swatch' }), 'completed span'),
        h('span', {}, h('i', { class: 'legend-swatch open' }), 'no stop observed'),
        h('span', {}, h('i', { class: 'legend-swatch overlap' }), 'overlap band'),
        h('span', {}, h('i', { class: 'legend-dot pass' }), 'check passed'),
        h('span', {}, h('i', { class: 'legend-dot fail' }), 'check failed'),
        h('span', {}, h('i', { class: 'legend-line' }), 'terminal'),
      ),
    );

    if (!spans.length && !mutations.length && !checks.length && !detail.finish) {
      wrap.append(h('div', { class: 'chart-empty' },
        'No native agent, check or mutation events were recorded. ',
        h('code', { text: 'perf.py report' }),
        ' will explain what is missing.',
      ));
      return wrap;
    }

    const gantt = h('div', { class: 'gantt' });
    const axis = h('div', { class: 'gantt-axis' },
      h('div', { class: 'axis-label', text: 'agent / signal' }),
      h('div', { class: 'axis-track' }),
    );
    const axisTrack = axis.lastChild;
    const { ticks } = axisTicks(from, to);
    for (const tick of ticks) {
      const tickEl = h('span', {
        class: 'axis-tick', text: fmtDuration(Math.max(0, tick - started)),
        style: { left: `${clamp(position(tick))}%` },
      });
      axisTrack.append(tickEl);
    }

    const rows = h('div', { class: 'gantt-rows' });
    const grid = h('div', { class: 'gantt-grid' });
    for (const tick of ticks) {
      grid.append(h('div', { class: 'grid-line', style: { left: `${clamp(position(tick))}%` } }));
    }
    for (const [bandStart, bandEnd] of overlapIntervals(spans)) {
      const width = position(bandEnd) - position(bandStart);
      if (width <= 0) continue;
      grid.append(h('div', {
        class: 'overlap-band',
        style: { left: `${clamp(position(bandStart))}%`, width: `${clamp(width)}%` },
        'data-tip': `Observed overlap ${fmtDuration(bandEnd - bandStart)} · not time saved`,
      }));
    }
    rows.append(grid);

    for (const agent of renderSpans) {
      const left = clamp(position(agent.start));
      const width = Math.max(0.6, clamp(position(agent.end) - position(agent.start)));
      const bar = h('div', {
        class: 'gantt-bar',
        style: { left: `${left}%`, width: `${width}%` },
        'data-open': String(Boolean(agent.open)),
        'data-tip': `agent ${agent.short_id || shortId(agent.id)} · ${fmtDuration(agent.duration)} · ${agent.open ? 'no stop observed' : 'stopped'}`,
        tabindex: '0',
        role: 'img',
        'aria-label': `Agent ${agent.short_id || shortId(agent.id)}, duration ${fmtDuration(agent.duration)}, ${agent.open ? 'no stop observed' : 'completed span'}`,
      });
      if (run.status === 'failed') bar.classList.add('failed-run');
      else if (run.status === 'open') bar.classList.add('open-run');
      rows.append(h('div', { class: 'gantt-row' },
        h('div', { class: 'gantt-label' },
          h('span', { class: 'name' }, h('i', { class: 'agent-dot' }), agent.short_id || shortId(agent.id)),
          h('span', { class: 'sub', text: `${fmtDuration(agent.duration)} · ${agent.open ? 'open' : 'stopped'}` }),
        ),
        h('div', { class: 'gantt-track' }, bar),
      ));
    }

    if (mutations.length) {
      const track = h('div', { class: 'gantt-track' });
      const dots = h('div', { class: 'mutation-dots' });
      for (const offset of mutations.slice(0, MUTATION_DISPLAY_LIMIT)) {
        dots.append(h('span', {
          class: 'mutation-dot',
          style: { left: `${clamp(position(started + offset))}%` },
          'data-tip': `code mutation observed at +${fmtDuration(offset)}`,
        }));
      }
      track.append(dots);
      rows.append(h('div', { class: 'gantt-row activity-row' },
        h('div', { class: 'gantt-label' },
          h('span', { class: 'name', text: 'mutations' }),
          h('span', { class: 'sub', text: mutations.length > MUTATION_DISPLAY_LIMIT ? `${MUTATION_DISPLAY_LIMIT} of ${mutations.length} shown` : `${mutations.length} observed` }),
        ),
        track,
      ));
    }

    if (checks.length) {
      const track = h('div', { class: 'gantt-track' });
      for (const check of checks) {
        track.append(h('span', {
          class: 'check-marker',
          'data-pass': String(check.passed),
          style: { left: `${clamp(position(check.at))}%` },
          'data-tip': `check ${check.id} · exit ${check.exit_code} · +${fmtDuration(check.offset)}`,
          tabindex: '0',
          role: 'img',
          'aria-label': `Check ${check.id}, exit code ${check.exit_code}`,
        }));
      }
      rows.append(h('div', { class: 'gantt-row activity-row' },
        h('div', { class: 'gantt-label' },
          h('span', { class: 'name', text: 'checks' }),
          h('span', { class: 'sub', text: `${checks.filter((check) => check.passed).length}/${checks.length} passed` }),
        ),
        track,
      ));
    }

    if (spans.length > renderSpans.length) {
      rows.append(h('div', { class: 'chart-empty', text: `Showing the first ${renderSpans.length} of ${spans.length} agent lanes. The table below retains all recorded spans.` }));
    }

    const markers = h('div', { class: 'gantt-markers' });
    if (detail.finish) {
      markers.append(h('div', {
        class: 'finish-marker',
        'data-status': detail.finish.status,
        style: { left: `${clamp(position(detail.finish.at))}%` },
      }, h('span', { text: detail.finish.status })));
    }
    rows.append(markers);
    gantt.append(axis, rows);
    wrap.append(gantt);
    return wrap;
  }

  /* ── Evidence chain and tables ──────────────────────────────────────── */
  function evidenceChain(run, detail) {
    const list = h('ol', { class: 'evidence' });
    const started = run.started_at || 0;
    const add = (tone, title, meta, side) => {
      list.append(h('li', { 'data-tone': tone },
        h('span', { class: 'ev-dot' }),
        h('div', {},
          h('div', { class: 'ev-title', text: title }),
          meta ? h('div', { class: 'ev-meta' }, meta) : null,
        ),
        h('div', { class: 'ev-side', text: side || '' }),
      ));
    };

    add('ok', 'Run started', `${run.platform} · ${fmtDateTime(started)}`, 't+0s');
    if (run.route) {
      add('info',
        `Router advised ${run.route.mode === 'single' ? 'a single agent' : `up to ${run.route.max_agents} parallel agents`}`,
        `${run.native_starts} native starts · peak concurrency ${run.max_concurrency} · ${run.routing_advice_followed === true ? 'advice followed' : run.routing_advice_followed === false ? 'advice exceeded' : 'adherence unknown'}`,
        run.route.latency_ms != null ? `${Math.round(run.route.latency_ms)}ms` : '',
      );
    }
    add(run.open_agent_count ? 'warn' : 'ok',
      run.agent_count ? `${run.agent_count} native agent span${run.agent_count === 1 ? '' : 's'}` : 'No native agent span',
      run.open_agent_count
        ? `${run.open_agent_count} open without a stop event · not assumed running`
        : run.agent_count ? 'Every recorded start has a matching stop' : 'Single-agent or telemetry without subagents',
      run.agent_count ? `Σ ${fmtDuration(sum((detail.agents || []).map((agent) => agent.duration)))}` : '—',
    );
    if (run.mutation_count) {
      add('info', `${run.mutation_count} code mutation${run.mutation_count === 1 ? '' : 's'} observed`,
        'Earlier check evidence is invalidated by mutations',
        detail.mutations?.length ? `last +${fmtDuration(Math.max(...detail.mutations))}` : '',
      );
    }
    if (run.checks_passed || run.checks_failed) {
      const chips = h('span', { class: 'check-chips' });
      for (const check of detail.checks || []) {
        chips.append(h('span', {
          class: 'check-chip', 'data-pass': String(check.passed),
          text: `${check.id} · exit ${check.exit_code}`,
        }));
      }
      add(run.checks_failed ? 'bad' : 'ok',
        `${run.checks_passed} of ${run.checks_passed + run.checks_failed} native checks passed`,
        chips,
        detail.checks?.length ? `last +${fmtDuration(Math.max(...detail.checks.map((check) => check.offset)))}` : '',
      );
    } else {
      add('', 'No native checks recorded', 'Only exact commands in an opt-in verification policy can satisfy the gate', '—');
    }
    if (detail.finish) {
      add(detail.finish.status === 'success' ? 'ok' : 'bad',
        `Terminal record: ${detail.finish.status}`,
        detail.finish.status === 'success' ? 'Completion is not verification by itself' : 'No verification can be inferred from a failed terminal state',
        `+${fmtDuration(detail.finish.offset)}`,
      );
    } else {
      add('warn', 'No terminal record', 'The run is open; missing events are not reconstructed', '—');
    }
    if (run.verified) {
      add('ok', 'Quality gate verified', run.verification?.reason || 'Configured native checks passed',
        detail.verified_event ? `+${fmtDuration(detail.verified_event.offset)}` : '');
    } else if (run.verification?.status === 'rejected') {
      add('bad', 'Verification rejected', run.verification.reason, '');
    } else if (run.self_reported) {
      add('warn', 'Self-reported verification', 'Not counted as gate-verified evidence', '');
    } else {
      const reason = run.status === 'open'
        ? 'No terminal event was recorded'
        : run.status === 'failed' || run.status === 'cancelled'
          ? 'The run did not finish successfully'
          : run.reason || 'No verification event';
      add('', 'Not verified', reason, '');
    }
    return list;
  }

  function agentTable(agents, run) {
    const table = h('table', {},
      h('thead', {}, h('tr', {},
        h('th', { text: 'agent' }),
        h('th', { text: 'start' }),
        h('th', { text: 'duration' }),
        h('th', { text: 'span' }),
        h('th', { text: 'native hash' }),
      )),
      h('tbody', {}, ...agents.map((agent) => h('tr', {},
        h('td', {}, h('span', { class: 'mono', text: agent.short_id || shortId(agent.id) })),
        h('td', { class: 'num', text: relativeOffset(run.started_at, agent.start) }),
        h('td', { class: 'num', text: fmtDuration(agent.duration, true) }),
        h('td', {}, h('span', {
          class: 'state-chip', 'data-state': agent.open ? 'open' : 'completed',
          text: agent.open ? 'open' : 'stopped',
        })),
        h('td', {}, h('span', { class: 'mono', text: agent.native_hash_short || '—' })),
      ))),
    );
    return h('div', { class: 'table-wrap' }, table);
  }

  function checkTable(checks, run) {
    const table = h('table', {},
      h('thead', {}, h('tr', {},
        h('th', { text: 'check' }),
        h('th', { text: 'exit' }),
        h('th', { text: 'observed' }),
        h('th', { text: 'source' }),
      )),
      h('tbody', {}, ...checks.map((check) => h('tr', {},
        h('td', { class: 'mono', text: check.id }),
        h('td', { class: 'num' }, h('span', { style: { color: check.passed ? 'var(--ok)' : 'var(--bad)' }, text: String(check.exit_code) })),
        h('td', { class: 'num', text: relativeOffset(run.started_at, check.at) }),
        h('td', { class: 'mono', text: check.source || '—' }),
      ))),
    );
    return h('div', { class: 'table-wrap' }, table);
  }

  function rawEvents(events, run) {
    const rows = events.map((event) => h('tr', {},
      h('td', { class: 'num', text: event.offset == null ? '—' : `+${fmtDuration(event.offset)}` }),
      h('td', {}, h('span', { class: 'event-kind', 'data-kind': event.event, text: event.event })),
      h('td', { text: eventDetail(event) }),
    ));
    const details = h('details', { class: 'raw' },
      h('summary', { text: `Raw evidence · ${events.length} event${events.length === 1 ? '' : 's'}` }),
      h('div', { class: 'event-table' },
        h('table', {},
          h('thead', {}, h('tr', {},
            h('th', { text: 't+' }), h('th', { text: 'event' }), h('th', { text: 'recorded detail' }),
          )),
          h('tbody', {}, ...rows),
        ),
      ),
    );
    return details;
  }

  function eventDetail(event) {
    switch (event.event) {
      case 'start': return `platform ${event.platform || 'unknown'}`;
      case 'route': return `${event.delegation} · max ${event.max_agents} · ${event.selected_count ?? '—'} skills${event.latency_ms != null ? ` · ${Math.round(event.latency_ms)}ms` : ''}`;
      case 'agent-start': return `agent ${event.agent_id || '—'} started${event.native_hash_short ? ` · hash ${event.native_hash_short}` : ''}`;
      case 'agent-stop': return `agent ${event.agent_id || '—'} stopped`;
      case 'mutation': return 'native code mutation observed';
      case 'check': return `${event.check_id || '—'} · exit ${event.exit_code}`;
      case 'verified': return `${event.source || 'recorded'}${event.check_ids?.length ? ` · ${event.check_ids.join(', ')}` : ''}`;
      case 'finish': return `${event.status || 'unknown'}${event.verified ? ' · verified flag' : ''}`;
      case 'diagnostic': return `reason ${event.reason || 'unknown'}`;
      default: return 'recorded';
    }
  }

  /* ── Board ──────────────────────────────────────────────────────────── */
  function taskGroup(task) {
    if (task.state === 'completed') return 'done';
    if (task.state === 'failed' || task.state === 'cancelled' || task.state === 'timed_out') return 'attention';
    if (task.state === 'running' || task.state === 'cancel_requested') return 'running';
    if (task.state === 'pending') return task.dependencies_met ? 'ready' : 'blocked';
    return 'attention';
  }

  function renderBoard() {
    const supervisor = state.data?.supervisor;
    const host = $('#board');
    const kpis = $('#board-kpis');
    const runHost = $('#board-runs');
    clear(host); clear(kpis); clear(runHost);

    if (!supervisor?.available || !supervisor.runs?.length) {
      kpis.append(...emptyBoardKpis());
      host.append(h('div', { class: 'empty-state', style: { gridColumn: '1 / -1' } },
        h('div', { class: 'empty-inner' },
          h('div', { class: 'empty-mark' }, icon('branch')),
          h('h2', { text: 'No Supervisor plan yet' }),
          h('p', { text: 'The board reads durable task state from the project SQLite database. It does not invent tasks from prompts or worktrees.' }),
          h('code', { text: 'python tools/supervisor.py --root . init --run-id demo --plan examples/v6-native-plan.json' }),
        ),
      ));
      return;
    }

    const runs = supervisor.runs;
    if (!state.boardRun || !runs.some((run) => run.id === state.boardRun)) state.boardRun = runs[0].id;
    for (const run of runs) {
      runHost.append(h('button', {
        class: 'run-chip', type: 'button', 'aria-pressed': String(run.id === state.boardRun),
        onclick: () => { state.boardRun = run.id; renderBoard(); },
      }, run.id));
    }

    const run = runs.find((item) => item.id === state.boardRun) || runs[0];
    const tasks = run.tasks || [];
    const groups = { ready: [], running: [], blocked: [], done: [], attention: [] };
    for (const task of tasks) groups[taskGroup(task)].push(task);
    const completed = tasks.filter((task) => task.state === 'completed').length;
    const running = groups.running.length;
    const blocked = groups.blocked.length;
    const reviewAttested = tasks.filter((task) => task.review_attested).length;
    const nativeStops = tasks.filter((task) => task.native_stop_observed).length;
    const dependencyCount = sum(tasks.map((task) => task.dependencies.length));

    const kpiItems = [
      ['tasks', fmtNumber(tasks.length), `${dependencyCount} dependencies · max ${run.max_parallel} parallel`],
      ['done', `${completed}/${tasks.length}`, run.complete ? 'plan complete' : 'plan open'],
      ['active', fmtNumber(running), run.native_supervision ? 'native supervision on' : 'supervision off'],
      ['blocked', fmtNumber(blocked), 'waiting on dependencies'],
      ['evidence', `${nativeStops}/${tasks.filter((task) => task.state !== 'pending').length}`,
        run.require_review ? `${reviewAttested} review attested` : 'review not required'],
    ];
    for (const [label, value, sub] of kpiItems) {
      kpis.append(h('div', { class: 'board-kpi' },
        h('div', { class: 'k', text: label }),
        h('div', { class: 'v' }, value, h('small', { text: ` ${sub}` })),
      ));
    }

    const columns = [
      ['ready', 'Ready', 'dependencies satisfied'],
      ['running', 'Running', 'claimed or cancel requested'],
      ['blocked', 'Blocked', 'waiting on a dependency'],
      ['done', 'Done', 'completed tasks'],
      ['attention', 'Attention', 'failed, cancelled or timed out'],
    ];
    for (const [key, label, hint] of columns) {
      const body = h('div', { class: 'board-col-body' });
      if (!groups[key].length) body.append(h('div', { class: 'board-empty', text: 'No tasks' }));
      for (const task of groups[key]) body.append(taskCard(task, run));
      host.append(h('section', { class: 'board-col', 'data-col': key },
        h('div', { class: 'board-col-head' },
          h('span', { class: 'dot' }),
          h('h3', { text: label, title: hint }),
          h('span', { class: 'count', text: groups[key].length }),
        ),
        body,
      ));
    }
  }

  function emptyBoardKpis() {
    return ['tasks', 'done', 'active', 'blocked', 'evidence'].map((label) =>
      h('div', { class: 'board-kpi' },
        h('div', { class: 'k', text: label }),
        h('div', { class: 'v', text: '—' }),
      ));
  }

  function taskCard(task, run) {
    const group = taskGroup(task);
    const foot = [];
    if (task.attempts) foot.push(h('span', { class: 'mini-chip', text: `${task.attempts} attempt${task.attempts === 1 ? '' : 's'}` }));
    else foot.push(h('span', { class: 'mini-chip', text: 'not claimed' }));
    if (task.state !== 'pending') {
      foot.push(h('span', {
        class: `mini-chip ${task.native_stop_observed ? 'ok' : 'warn'}`,
        text: task.native_stop_observed ? 'native stop ✓' : 'stop unobserved',
      }));
    }
    if (run.require_review) {
      foot.push(h('span', {
        class: `mini-chip ${task.review_attested ? 'ok' : 'warn'}`,
        text: task.review_attested ? 'review ✓' : 'review —',
      }));
    }
    if (task.dependencies.length) {
      foot.push(h('span', {
        class: `mini-chip ${task.dependencies_met ? '' : 'warn'}`,
        text: task.dependencies_met ? `${task.dependencies.length} dep met` : `${task.dependencies.length} dep waiting`,
      }));
    }

    const details = h('details', { class: 'task-more' },
      h('summary', { text: 'Scopes & result' }),
      h('div', { class: 'more-body' },
        task.write_scope.length
          ? h('div', {}, h('strong', { text: 'write ' }), h('span', { class: 'scope-list' }, ...task.write_scope.map((scope) => h('span', { class: 'scope', text: scope }))))
          : h('div', {}, h('strong', { text: 'write ' }), 'none (read-only task)'),
        task.read_scope.length
          ? h('div', { style: { 'margin-top': '4px' } }, h('strong', { text: 'read ' }), h('span', { class: 'scope-list' }, ...task.read_scope.map((scope) => h('span', { class: 'scope', text: scope }))))
          : null,
        task.reason ? h('div', { style: { 'margin-top': '5px', color: 'var(--bad)' }, text: task.reason }) : null,
        task.result?.summary ? h('div', { style: { 'margin-top': '5px' } }, h('strong', { text: 'result ' }), task.result.summary) : null,
        task.result?.changed_files?.length
          ? h('div', { style: { 'margin-top': '4px' } }, h('strong', { text: 'changed ' }), h('span', { class: 'mono', text: task.result.changed_files.join(', ') }))
          : null,
        task.result?.tests?.length
          ? h('div', { style: { 'margin-top': '5px' } }, ...task.result.tests.map((test) =>
            h('div', { class: 'test-row', 'data-status': test.status },
              h('span', { class: 'dot' }), `${test.name} · ${test.status}`)))
          : null,
      ),
    );

    return h('article', {
      class: 'task-card', 'data-role': task.role,
      title: task.objective || task.id,
    },
      h('div', { class: 'task-top' },
        h('span', { class: 'role', text: task.role || 'unknown' }),
        h('span', { class: 'task-id', text: task.id }),
      ),
      h('p', { class: 'task-objective', text: task.objective || 'No objective recorded.' }),
      task.dependencies.length
        ? h('div', { class: 'task-deps' },
          h('b', { text: 'after ' }),
          task.dependencies.join(', '),
          task.dependencies_met ? '' : ' · waiting',
        )
        : null,
      h('div', { class: 'task-foot' }, ...foot),
      details,
    );
  }

  /* ── Workspaces ─────────────────────────────────────────────────────── */
  function renderWorkspaces() {
    const host = $('#workspaces');
    clear(host);
    const worktrees = state.data?.worktrees || [];
    if (!worktrees.length) {
      host.append(h('div', { class: 'empty-state', style: { gridColumn: '1 / -1' } },
        h('div', { class: 'empty-inner' },
          h('div', { class: 'empty-mark' }, icon('branch')),
          h('h2', { text: 'No managed worktrees' }),
          h('p', { text: 'Worktree manifests appear here after a task workspace is created. The console only reads them.' }),
          h('code', { text: 'python tools/worktree.py --repo . create --task-id <task-id>' }),
        ),
      ));
      return;
    }
    for (const tree of worktrees) {
      const dirty = tree.dirty_files;
      const dirtyLabel = dirty == null ? 'status unknown' : dirty === 0 ? 'clean' : `${dirty} changed`;
      host.append(h('article', { class: 'workspace-card' },
        h('div', { class: 'workspace-top' },
          icon('branch'),
          h('span', { class: 'workspace-task', text: tree.task }),
          h('span', {
            class: 'workspace-dirty',
            'data-dirty': String(Boolean(dirty)),
            text: dirtyLabel,
          }),
        ),
        h('div', { class: 'workspace-row' },
          h('span', { class: 'k', text: 'branch' }),
          h('span', { class: 'v branch', text: tree.branch || '—' }),
        ),
        h('div', { class: 'workspace-row' },
          h('span', { class: 'k', text: 'base' }),
          h('span', { class: 'v', text: tree.base_sha || '—' }),
        ),
        h('div', { class: 'workspace-row' },
          h('span', { class: 'k', text: 'path' }),
          h('span', { class: 'v', text: tree.path || '—' }),
        ),
        tree.exists
          ? null
          : h('div', { class: 'workspace-missing', text: 'Worktree directory is missing; manual reconciliation required.' }),
      ));
    }
  }

  function emptyRunsState() {
    return h('div', { class: 'empty-state' },
      h('div', { class: 'empty-inner' },
        h('div', { class: 'empty-mark' }, icon('clock')),
        h('h2', { text: 'No run evidence in this project' }),
        h('p', { text: 'Runs appear when an installed native hook or plugin records lifecycle events. You can inspect the interface immediately with synthetic, clearly labelled demo data.' }),
        h('code', { text: 'python tools/dashboard.py --demo --open' }),
      ),
    );
  }

  /* ── Tabs and boot ──────────────────────────────────────────────────── */
  function setTab(tab, updateHash = true) {
    const valid = ['runs', 'board', 'workspaces', 'method'];
    if (!valid.includes(tab)) tab = 'runs';
    state.tab = tab;
    for (const button of $$('.tab')) {
      const active = button.dataset.tab === tab;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
    }
    for (const view of $$('.view')) {
      view.classList.toggle('hidden', view.id !== `view-${tab}`);
    }
    if (updateHash) {
      history.replaceState(null, '', `#${tab}`);
    }
    if (tab === 'board') renderBoard();
    if (tab === 'workspaces') renderWorkspaces();
  }

  function bindEvents() {
    for (const button of $$('.tab')) {
      button.addEventListener('click', () => setTab(button.dataset.tab));
      button.addEventListener('keydown', (event) => {
        if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
        event.preventDefault();
        const tabs = $$('.tab');
        const index = tabs.indexOf(button);
        const next = event.key === 'ArrowRight'
          ? tabs[(index + 1) % tabs.length]
          : tabs[(index - 1 + tabs.length) % tabs.length];
        next.focus();
        setTab(next.dataset.tab);
      });
    }

    $('#refresh').addEventListener('click', () => refresh(true));

    $('#run-search').addEventListener('input', (event) => {
      const value = event.target.value.trim();
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(() => {
        state.query = value;
        renderRunList();
      }, 120);
    });

    $('#theme').addEventListener('click', () => {
      const current = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
      const next = current === 'light' ? 'dark' : 'light';
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem('ao-theme', next); } catch { /* local preference is optional */ }
    });

    document.addEventListener('keydown', (event) => {
      if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
        event.preventDefault();
        setTab('runs');
        $('#run-search').focus();
      }
      if (event.key === 'Escape' && document.activeElement === $('#run-search')) {
        clearTimeout(state.searchTimer);
        $('#run-search').value = '';
        state.query = '';
        renderRunList();
        $('#run-search').blur();
      }
    });

    window.addEventListener('hashchange', () => {
      setTab(location.hash.replace('#', ''), false);
    });
  }

  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem('ao-theme'); } catch { /* ignore */ }
    if (saved === 'light' || saved === 'dark') {
      document.documentElement.dataset.theme = saved;
    } else {
      document.documentElement.dataset.theme = window.matchMedia?.('(prefers-color-scheme: light)').matches
        ? 'light' : 'dark';
    }
  }

  async function boot() {
    initTheme();
    setupTooltip();
    bindEvents();
    setTab(location.hash.replace('#', '') || 'runs', false);
    await refresh(true);
    setInterval(() => refresh(false), 2500);
    setInterval(() => { if (!document.hidden) renderUpdated(); }, 1000);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refresh(true);
    });
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
