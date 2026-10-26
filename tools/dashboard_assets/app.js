(() => {
  'use strict';

  const I18n = window.AOI18n || null;
  const Office = window.AOOffice || null;
  const LEGACY_PROJECT = '__default__';
  const TABS = ['office', 'runs', 'board', 'workspaces', 'method'];
  const RUN_DISPLAY_LIMIT = 250;
  const AGENT_DISPLAY_LIMIT = 120;
  const MUTATION_DISPLAY_LIMIT = 250;
  const INSPECTOR_EVENT_LIMIT = 30;
  const STORE_PROJECT = 'ao-project';
  const STORE_LANGUAGE = 'ao-language';
  const STORE_APPEARANCE = 'ao-theme';
  const STORE_MOTION = 'ao-reduced-motion';
  const NO_LIVE = new URLSearchParams(location.search).has('no-live');

  const state = {
    ready: false,
    mode: 'boot',
    registryLoading: false,
    registryError: null,
    registryPath: null,
    projects: [],
    global: null,
    globalError: null,
    globalLoading: false,
    globalEtag: null,
    globalToken: 0,
    lastGlobalFetch: 0,
    legacy: false,
    fatalError: null,
    projectId: null,
    projectMeta: null,
    data: null,
    stateError: null,
    stateLoading: false,
    stateToken: 0,
    etag: null,
    lastFetch: 0,
    lastOfficeFetch: 0,
    demo: false,
    selected: null,
    detail: null,
    detailProjectId: null,
    detailToken: 0,
    detailLoadingId: null,
    tab: 'office',
    filter: 'all',
    query: '',
    boardRun: null,
    runListSignature: '',
    searchTimer: null,
    toastTimer: null,
    office: null,
    officeError: null,
    officeLoading: false,
    officeSignature: '',
    officeToken: 0,
    milestones: null,
    memory: null,
    milestoneError: null,
    milestoneToken: 0,
    lastMilestoneFetch: 0,
    milestoneFilter: null,
    arranging: false,
    reducedMotion: false,
    language: 'en',
    inspector: {
      open: false,
      mode: 'agent',
      agentId: null,
      agent: null,
      session: null,
      payload: null,
      loading: false,
      error: null,
      trigger: null,
      token: 0,
      requestId: 0,
    },
    settingsOpen: false,
    settingsReturnFocus: null,
    live: {
      source: null,
      projectId: null,
      mode: 'offline',
      failures: 0,
      lastSequence: 0,
      retryTimer: null,
    },
    officeRefreshTimer: null,
    inspectorRefreshTimer: null,
    globalDemo: false,
    demoProbed: false,
    refreshing: false,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  /* ── DOM, language and formatting ─────────────────────────────────────── */
  function t(key, params) {
    if (I18n) return I18n.t(key, params);
    return String(key).split('.').pop();
  }

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
    if (!node) return;
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

  function locale() {
    return state.language === 'tr' ? 'tr-TR' : 'en-US';
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
    try {
      const format = new Intl.RelativeTimeFormat(locale(), { numeric: 'auto' });
      if (diff < 60) return format.format(-Math.round(diff), 'second');
      if (diff < 3600) return format.format(-Math.round(diff / 60), 'minute');
      if (diff < 86400) return format.format(-Math.round(diff / 3600), 'hour');
      if (diff < 604800) return format.format(-Math.round(diff / 86400), 'day');
      return new Date(Number(timestamp) * 1000).toLocaleDateString(locale(), { month: 'short', day: 'numeric' });
    } catch {
      return fmtClock(timestamp);
    }
  }

  function fmtClock(timestamp) {
    if (!timestamp) return '—';
    return new Date(Number(timestamp) * 1000).toLocaleTimeString(locale(), {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  }

  function fmtDateTime(timestamp) {
    if (!timestamp) return '—';
    return `${new Date(Number(timestamp) * 1000).toLocaleDateString(locale(), {
      month: 'short', day: 'numeric',
    })} ${fmtClock(timestamp)}`;
  }

  function fmtNumber(value) {
    if (value == null) return '—';
    return Number(value).toLocaleString(locale());
  }

  function pct(value) {
    if (value == null) return '—';
    const rounded = Math.round(Number(value) * 100);
    return state.language === 'tr' ? `%${rounded}` : `${rounded}%`;
  }

  function shortId(value) {
    if (!value) return '—';
    return String(value).slice(0, 8);
  }

  function sum(values) {
    return values.reduce((total, value) => total + (Number(value) || 0), 0);
  }

  function readStore(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function writeStore(key, value) {
    try {
      if (value == null) localStorage.removeItem(key);
      else localStorage.setItem(key, String(value));
    } catch {
      /* A local preference is optional. */
    }
  }

  function readJsonStore(key) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function errorFrom(payload, status) {
    const error = new Error(payload?.error || t('errors.request', { status }));
    error.status = status;
    error.data = payload;
    return error;
  }

  function errorStatus(error) {
    return Number.isFinite(Number(error?.status)) && Number(error.status) > 0 ? Number(error.status) : null;
  }

  async function requestJSON(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout || 9000);
    try {
      const response = await fetch(url, {
        cache: 'no-store',
        credentials: 'same-origin',
        headers: options.headers || {},
        signal: controller.signal,
      });
      const result = {
        ok: response.ok,
        status: response.status,
        etag: response.headers.get('ETag') || null,
        notModified: response.status === 304,
        data: null,
      };
      if (response.status === 304) return result;
      const text = await response.text();
      if (text) {
        try {
          result.data = JSON.parse(text);
        } catch {
          const error = new Error(t('errors.invalidResponse'));
          error.status = response.status;
          throw error;
        }
      }
      if (!response.ok) throw errorFrom(result.data, response.status);
      return result;
    } catch (error) {
      if (error?.name === 'AbortError') {
        const timeout = new Error(t('errors.timeout'));
        timeout.status = 0;
        throw timeout;
      }
      if (error instanceof TypeError) {
        const offline = new Error(t('errors.network'));
        offline.status = 0;
        throw offline;
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  function toast(message) {
    const node = $('#toast');
    if (!node) return;
    node.textContent = message;
    node.classList.add('visible');
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => node.classList.remove('visible'), 2100);
  }

  async function copyText(value) {
    try {
      await navigator.clipboard.writeText(value);
      toast(t('common.copied'));
    } catch {
      const area = h('textarea', { style: { position: 'fixed', opacity: '0' } });
      area.value = value;
      document.body.append(area);
      area.select();
      try {
        document.execCommand('copy');
        toast(t('common.copied'));
      } catch {
        toast(t('common.copyFailed'));
      }
      area.remove();
    }
  }

  function copyButton(value, label = t('common.copy')) {
    return h('button', {
      class: 'copy-btn', type: 'button', title: label, 'aria-label': label,
      onclick: (event) => { event.stopPropagation(); copyText(value); },
    }, icon('copy'));
  }

  function button(label, onclick, className = 'btn') {
    return h('button', { class: className, type: 'button', onclick }, label);
  }

  function emptyState({ glyph = '!', title, body, hint, actions = [] }) {
    return h('div', { class: 'empty-state' },
      h('div', { class: 'empty-inner' },
        h('div', { class: 'empty-mark' }, h('span', { class: 'empty-glyph', 'aria-hidden': 'true', text: glyph })),
        h('h2', { text: title }),
        body ? h('p', { text: body }) : null,
        hint ? h('code', { text: hint }) : null,
        actions.length ? h('div', { class: 'empty-actions' }, ...actions) : null,
      ),
    );
  }

  /* ── Tooltip ───────────────────────────────────────────────────────────── */
  function setupTooltip() {
    const tooltip = $('#tooltip');
    if (!tooltip) return;
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

  /* ── Settings: language, appearance, motion ───────────────────────────── */
  function currentAppearance() {
    return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
  }

  function applyAppearance(value, options = {}) {
    const theme = value === 'light' ? 'light' : 'dark';
    const root = document.documentElement;
    if (options.persist !== false && root.dataset.theme !== theme) {
      root.classList.add('theme-switching');
      root.dataset.theme = theme;
      void root.offsetWidth;
      requestAnimationFrame(() => root.classList.remove('theme-switching'));
    } else {
      root.dataset.theme = theme;
    }
    if (options.persist !== false) writeStore(STORE_APPEARANCE, theme);
    const select = $('#set-appearance');
    if (select) select.value = theme;
  }

  function initAppearance() {
    const requested = new URLSearchParams(location.search).get('theme');
    if (requested === 'light' || requested === 'dark') {
      applyAppearance(requested, { persist: false });
      return;
    }
    const stored = readStore(STORE_APPEARANCE);
    if (stored === 'light' || stored === 'dark') {
      applyAppearance(stored, { persist: false });
    } else {
      applyAppearance(window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark', { persist: false });
    }
  }

  function applyMotion(reduced, options = {}) {
    state.reducedMotion = Boolean(reduced);
    document.documentElement.dataset.motion = state.reducedMotion ? 'reduced' : 'full';
    if (options.persist !== false) writeStore(STORE_MOTION, state.reducedMotion ? '1' : '0');
    window.AOOffice?.officeDebugState?.().scene?.setReducedMotion?.(state.reducedMotion);
    syncMotionControls();
  }

  function initMotion() {
    const stored = readStore(STORE_MOTION);
    if (stored === '1' || stored === '0') {
      applyMotion(stored === '1', { persist: false });
    } else {
      applyMotion(Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches), { persist: false });
    }
  }

  function syncMotionControls() {
    const toggle = $('#office-motion');
    if (toggle) {
      toggle.setAttribute('aria-pressed', String(state.reducedMotion));
      toggle.textContent = t('office.motion');
      toggle.title = state.reducedMotion ? t('office.motionOn') : t('office.motionOff');
    }
    const checkbox = $('#set-reduced-motion');
    if (checkbox) checkbox.checked = state.reducedMotion;
  }

  function syncLanguageControls() {
    const toggle = $('#lang-toggle');
    if (toggle) {
      toggle.textContent = state.language.toUpperCase();
      toggle.setAttribute('aria-label', state.language === 'en' ? t('top.languageSwitch') : t('top.languageSwitchToEn'));
      toggle.title = t('top.language');
    }
    const select = $('#set-language');
    if (select) select.value = state.language;
  }

  function changeLanguage(value) {
    if (!I18n) return;
    I18n.setLanguage(value);
    state.language = I18n.getLanguage();
    syncLanguageControls();
  }

  function onLanguageChanged() {
    state.language = I18n ? I18n.getLanguage() : 'en';
    state.runListSignature = '';
    if (I18n) I18n.apply(document);
    document.documentElement.lang = state.language;
    document.title = t('app.title');
    syncLanguageControls();
    syncSettingsControls();
    syncProjectSelect();
    updateDemoBadge();
    updateLiveBadge();
    renderUpdated();
    if (state.mode === 'overview') {
      renderGlobal();
    } else if (state.projectId) {
      renderAll();
      renderOffice();
      renderInspector({ focus: false });
    }
    applyMotion(state.reducedMotion, { persist: false });
  }

  function officeThemeKey(projectId) {
    return `ao-office-theme:${projectId}`;
  }

  function officeOrderKey(projectId) {
    return `ao-desk-order:${projectId}`;
  }

  function currentOfficeTheme() {
    if (!state.projectId) return 'ai';
    const stored = readStore(officeThemeKey(state.projectId));
    const candidate = stored || state.office?.theme || state.projectMeta?.theme || 'ai';
    return Office ? Office.normalizeTheme(candidate) : 'ai';
  }

  function currentDeskOrder() {
    if (!state.projectId) return [];
    const value = readJsonStore(officeOrderKey(state.projectId));
    return Array.isArray(value) ? value.filter((item) => typeof item === 'string') : [];
  }

  function syncSettingsControls() {
    const language = $('#set-language');
    if (language) language.value = state.language;
    const appearance = $('#set-appearance');
    if (appearance) appearance.value = currentAppearance();
    const motion = $('#set-reduced-motion');
    if (motion) motion.checked = state.reducedMotion;
    const theme = currentOfficeTheme();
    const officeTheme = $('#office-theme');
    if (officeTheme) {
      officeTheme.value = theme;
      officeTheme.disabled = !state.projectId;
    }
    const settingsTheme = $('#set-office-theme');
    if (settingsTheme) {
      settingsTheme.value = theme;
      settingsTheme.disabled = !state.projectId;
    }
    const hint = $('#settings-office-hint');
    if (hint) hint.textContent = state.projectId ? t('settings.officeThemeHint') : t('settings.officeThemeProjectOnly');
    const arrange = $('#office-arrange');
    if (arrange) {
      arrange.disabled = !state.projectId;
      arrange.setAttribute('aria-pressed', String(state.arranging));
      arrange.textContent = state.arranging ? t('office.arrangeDone') : t('office.arrange');
    }
    syncMotionControls();
    syncLanguageControls();
  }

  function openSettings(open) {
    const panel = $('#settings');
    const toggle = $('#settings-toggle');
    if (!panel) return;
    state.settingsOpen = Boolean(open);
    panel.hidden = !open;
    if (toggle) toggle.setAttribute('aria-expanded', String(Boolean(open)));
    if (open) {
      state.settingsReturnFocus = document.activeElement;
      panel.setAttribute('role', 'dialog');
      panel.setAttribute('aria-modal', 'true');
      syncSettingsControls();
      $('#settings-close')?.focus();
    } else if (state.settingsReturnFocus instanceof HTMLElement && state.settingsReturnFocus.isConnected) {
      state.settingsReturnFocus.focus();
      state.settingsReturnFocus = null;
    }
  }

  /* ── Routing ───────────────────────────────────────────────────────────── */
  function tabFromHash(raw) {
    return TABS.includes(raw) ? raw : 'office';
  }

  function parseHash() {
    const raw = String(location.hash || '').replace(/^#/, '');
    if (!raw || raw === '/') return { view: 'none' };
    if (raw === 'overview' || raw === '/overview') return { view: 'overview' };
    const match = raw.match(/^\/?p\/([^/]+)(?:\/([a-z-]+))?\/?$/);
    if (match) {
      let projectId = match[1];
      try { projectId = decodeURIComponent(match[1]); } catch { /* keep raw */ }
      return { view: 'project', projectId, tab: tabFromHash(match[2] || 'office') };
    }
    if (TABS.includes(raw)) return { view: 'legacy-tab', tab: raw };
    return { view: 'overview' };
  }

  function hashFor(projectId, tab) {
    return projectId ? `#/p/${encodeURIComponent(projectId)}/${tabFromHash(tab)}` : '#/overview';
  }

  function writeHash(projectId, tab, options = {}) {
    const next = hashFor(projectId, tab);
    if (location.hash === next) return;
    try {
      if (options.replace) history.replaceState(null, '', next);
      else history.pushState(null, '', next);
    } catch {
      location.hash = next.slice(1);
    }
  }

  function focusViewHeading() {
    if (document.activeElement !== document.body) return;
    const view = state.mode === 'overview' ? '#view-overview' : `#view-${state.tab}`;
    const heading = document.querySelector(`${view} h1`);
    if (heading) {
      heading.tabIndex = -1;
      heading.style.outline = 'none';
      heading.focus();
    }
  }

  function showView(name) {
    for (const view of $$('.view')) {
      view.classList.toggle('hidden', view.id !== `view-${name}`);
    }
  }

  function updatePath(project) {
    const path = $('#project-path');
    if (!path) return;
    if (!project) {
      path.hidden = true;
      path.textContent = '';
      path.title = '';
      return;
    }
    const text = project.root || project.name || '—';
    path.hidden = false;
    path.textContent = text;
    path.title = text;
  }

  function fmtAgoShort(timestamp) {
    if (!timestamp) return '—';
    const diff = Math.max(0, Date.now() / 1000 - Number(timestamp));
    if (diff < 60) return `${Math.round(diff)}s`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
    return `${Math.floor(diff / 86400)}d`;
  }

  function fmtDateTimeShort(timestamp) {
    if (!timestamp) return '—';
    return new Date(Number(timestamp) * 1000).toLocaleString(locale(), {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  function findProject(projectId) {
    if (!projectId) return null;
    return state.projects.find((project) => project.project_id === projectId) || null;
  }

  function normalizeProjectMetadata(project) {
    if (!project || typeof project !== 'object' || typeof project.project_id !== 'string') return null;
    return {
      project_id: project.project_id,
      name: project.name || t('common.unknown'),
      description: project.description || '',
      root: project.root || null,
      theme: project.theme || null,
      agents: Array.isArray(project.agents) ? project.agents : [],
      created_at: project.created_at ?? null,
      updated_at: project.updated_at ?? null,
      legacy: false,
    };
  }

  function projectMetaFromState(payload) {
    if (!payload) return null;
    const raw = payload.project_meta && typeof payload.project_meta === 'object'
      ? payload.project_meta
      : payload.project;
    if (raw && typeof raw === 'object') {
      const meta = normalizeProjectMetadata({ ...raw, project_id: payload.project_id || raw.project_id });
      if (meta) return meta;
    }
    const root = typeof payload.project === 'string' ? payload.project : null;
    const derivedName = root ? root.split(/[\\/]/).filter(Boolean).pop() : null;
    return {
      project_id: payload.project_id || LEGACY_PROJECT,
      name: derivedName || t('common.defaultProject'),
      description: '',
      root,
      theme: null,
      agents: [],
      legacy: false,
    };
  }

  function mergeProjectMeta(projectId, base, data) {
    const current = base || findProject(projectId) || null;
    const fromState = projectMetaFromState(data);
    if (!fromState) return current;
    const merged = { ...fromState, ...(current || {}) };
    merged.project_id = projectId;
    if (!current?.root && fromState.root) merged.root = fromState.root;
    if (!current?.name && fromState.name) merged.name = fromState.name;
    if (!current?.theme && fromState.theme) merged.theme = fromState.theme;
    return merged;
  }

  function syncProjectSelect() {
    const select = $('#project-select');
    if (!select) return;
    const selected = state.projectId || '';
    clear(select);
    select.append(h('option', { value: '', text: t('top.allProjects') }));
    for (const project of state.projects) {
      select.append(h('option', {
        value: project.project_id,
        text: project.name || project.project_id,
        title: project.root || project.name || null,
      }));
    }
    if (selected && !state.projects.some((project) => project.project_id === selected)) {
      select.value = '';
    } else {
      select.value = selected;
    }
    select.disabled = state.registryLoading;
  }

  function updateDemoBadge() {
    const badge = $('#demo-badge');
    if (!badge) return;
    badge.classList.toggle('hidden', !state.demo);
    badge.textContent = t('top.demo');
    badge.title = t('top.demoTitle');
  }

  function updateLiveBadge() {
    const badge = $('#live-badge');
    const text = $('#live-text');
    if (!badge || !text) return;
    if (!state.projectId) {
      badge.classList.add('hidden');
      return;
    }
    badge.classList.remove('hidden');
    const mode = state.live.mode;
    const compact = window.innerWidth <= 700;
    const modeLabel = mode === 'sse'
      ? t(compact ? 'live.liveShort' : 'live.live')
      : mode === 'polling'
        ? t(compact ? 'live.pollingShort' : 'live.polling')
        : mode === 'connecting' ? t('live.connecting') : t(compact ? 'live.offlineShort' : 'live.offline');
    if (state.demo) {
      badge.dataset.state = 'demo';
      const demoMode = mode === 'sse' ? t('live.demoLive')
        : mode === 'polling' ? t('live.demoPolling') : t('live.demoOffline');
      const demoCompact = mode === 'sse' ? t('live.liveShort')
        : mode === 'polling' ? t('live.pollingShort') : t('live.offlineShort');
      text.textContent = compact ? demoCompact : demoMode;
      badge.setAttribute('aria-label', t('live.statusAria', { mode: demoMode }));
      return;
    }
    if (mode === 'sse') {
      badge.dataset.state = 'live';
    } else if (mode === 'connecting') {
      badge.dataset.state = 'connecting';
    } else {
      badge.dataset.state = 'idle';
    }
    text.textContent = modeLabel;
    badge.setAttribute('aria-label', t('live.statusAria', { mode: modeLabel }));
  }

  function renderUpdated() {
    const host = $('#updated');
    if (!host) return;
    if (!state.lastFetch) {
      host.textContent = t('live.notUpdated');
      return;
    }
    host.textContent = t('live.updated', { time: fmtRelative(state.lastFetch) });
  }

  /* ── Registry, global overview and project selection ───────────────────── */
  function synthesizeGlobalFromState(payload) {
    const meta = projectMetaFromState(payload) || {
      project_id: LEGACY_PROJECT,
      name: t('common.defaultProject'),
      root: null,
      theme: null,
      agents: [],
    };
    const overview = payload.overview || {};
    const tasks = (payload.supervisor?.runs || []).flatMap((run) => run.tasks || []);
    const active = tasks.filter((task) => task.state === 'running' || task.state === 'cancel_requested').length;
    const blockers = tasks.filter((task) => task.state === 'pending' && !task.dependencies_met).length;
    const errors = tasks.filter((task) => ['failed', 'cancelled', 'timed_out'].includes(task.state)).length;
    return {
      version: 1,
      fingerprint: payload.fingerprint,
      legacy: true,
      totals: {
        projects: 1,
        runs: overview.runs || 0,
        verified: overview.verified || 0,
        open: overview.open || 0,
        agents: overview.agents || 0,
        active_tasks: active,
        blockers,
        errors,
      },
      projects: [{
        ...meta,
        overview: {
          runs: overview.runs || 0,
          verified: overview.verified || 0,
          open: overview.open || 0,
          live: overview.live || 0,
          agents: overview.agents || 0,
          tasks: tasks.length,
          active_tasks: active,
          blockers,
          last_activity: payload.runs?.[0]?.last_event_at || null,
          verified_rate: overview.verified_rate ?? null,
        },
        agent_count: meta.agents.length,
        linked_agent_count: 0,
        status: 'ok',
        error: null,
        legacy: true,
      }],
    };
  }

  async function loadRegistry() {
    state.registryLoading = true;
    state.fatalError = null;
    renderGlobal();
    syncProjectSelect();
    const [projectsResult, globalResult] = await Promise.allSettled([
      requestJSON('/api/projects'),
      requestJSON('/api/global'),
    ]);

    let projects = [];
    let registryPath = null;
    let registryError = null;
    let global = null;
    let globalError = null;

    if (projectsResult.status === 'fulfilled' && Array.isArray(projectsResult.value.data?.projects)) {
      projects = projectsResult.value.data.projects.map(normalizeProjectMetadata).filter(Boolean);
      registryPath = projectsResult.value.data.registry_path || null;
    } else {
      registryError = projectsResult.status === 'rejected'
        ? projectsResult.reason
        : new Error(t('errors.invalidResponse'));
    }

    if (globalResult.status === 'fulfilled' && Array.isArray(globalResult.value.data?.projects)) {
      global = globalResult.value.data;
    } else {
      globalError = globalResult.status === 'rejected'
        ? globalResult.reason
        : new Error(t('errors.invalidResponse'));
    }

    if (!projects.length && global) {
      projects = global.projects.map(normalizeProjectMetadata).filter(Boolean);
    }

    if (!projects.length && !global && registryError) {
      try {
        const legacyResponse = await requestJSON('/api/state');
        const payload = legacyResponse.data;
        if (!payload || typeof payload !== 'object') throw new Error(t('errors.invalidResponse'));
        state.legacy = true;
        const meta = projectMetaFromState(payload);
        if (meta) {
          meta.legacy = true;
          projects = [meta];
        }
        global = synthesizeGlobalFromState(payload);
        state.globalDemo = Boolean(payload.demo);
        state.demo = state.globalDemo;
        state.demoProbed = true;
        globalError = null;
        registryError = null;
      } catch (error) {
        state.legacy = false;
        state.fatalError = error;
        if (!globalError) globalError = error;
      }
    } else {
      state.legacy = false;
    }

    state.projects = projects;
    state.registryPath = registryPath;
    state.registryError = registryError;
    state.global = global;
    state.globalError = globalError;
    state.registryLoading = false;
    state.lastGlobalFetch = global ? Date.now() / 1000 : 0;
    state.ready = true;
    syncProjectSelect();
    updateDemoBadge();
    renderGlobal();
  }

  function probeDemoMode() {
    if (state.demoProbed) return;
    state.demoProbed = true;
    requestJSON('/api/state', { timeout: 6000 })
      .then((response) => {
        if (!response.data?.demo) return;
        state.globalDemo = true;
        if (state.mode === 'overview' && !state.projectId) {
          state.demo = true;
          updateDemoBadge();
        }
      })
      .catch(() => { /* The demo flag is a label, not a reason to block the UI. */ });
  }

  async function loadGlobal(options = {}) {
    if (state.globalLoading && !options.force) return;
    if (state.mode === 'overview' && !options.force && Date.now() / 1000 - state.lastGlobalFetch < 2) return;
    const token = ++state.globalToken;
    state.globalLoading = true;
    try {
      const headers = {};
      if (state.globalEtag && !options.force) headers['If-None-Match'] = state.globalEtag;
      const response = await requestJSON('/api/global', { headers });
      if (token !== state.globalToken) return;
      if (response.notModified) {
        state.lastGlobalFetch = Date.now() / 1000;
        state.globalError = null;
        return;
      }
      if (!Array.isArray(response.data?.projects)) throw new Error(t('errors.invalidResponse'));
      state.global = response.data;
      state.globalEtag = response.etag;
      state.demo = Boolean(response.data.demo);
      updateDemoBadge();
      state.globalError = null;
      state.lastGlobalFetch = Date.now() / 1000;
      state.lastFetch = state.lastGlobalFetch;
      if (state.projectId) {
        const entry = state.global.projects?.find((project) => project.project_id === state.projectId);
        if (entry) {
          state.projectMeta = {
            ...entry,
            ...(state.projectMeta || {}),
            name: state.projectMeta?.name && state.projectMeta.name !== t('common.unknown') ? state.projectMeta.name : entry.name,
            theme: state.projectMeta?.theme || entry.theme || null,
          };
        }
      }
      if (state.mode === 'overview') {
        renderGlobal();
        renderUpdated();
      }
    } catch (error) {
      if (token !== state.globalToken) return;
      state.globalError = error;
      if (state.mode === 'overview') renderGlobal();
    } finally {
      if (token === state.globalToken) state.globalLoading = false;
    }
  }

  function projectStatusMeta(project) {
    const raw = String(project?.status || '').toLowerCase();
    if (raw === 'ok' || raw === 'healthy') return { label: t('overview.status.ok'), tone: 'ok' };
    if (raw === 'error' || raw === 'failed') return { label: t('overview.status.error'), tone: 'bad' };
    if (raw === 'unavailable' || raw === 'offline') return { label: t('overview.status.unavailable'), tone: 'warn' };
    if (raw) return { label: t('overview.status.unknown'), tone: 'neutral' };
    return null;
  }

  function overviewCard(project, globalProject) {
    const overview = globalProject?.overview || null;
    const error = globalProject?.error || null;
    const status = projectStatusMeta(globalProject);
    const blockers = Number(overview?.blockers || 0);
    const errors = overview?.errors != null ? Number(overview.errors) : (error ? 1 : 0);
    const tone = error || status?.tone === 'bad' ? 'bad' : blockers || errors ? 'warn' : 'ok';
    const theme = project.theme || globalProject?.theme || null;
    const runs = overview?.runs ?? null;
    const agents = overview?.agents ?? null;
    const active = overview?.active_tasks ?? null;
    const verified = overview?.verified ?? null;
    const linked = globalProject?.linked_agent_count ?? 0;
    const registered = globalProject?.agent_count ?? project.agents?.length ?? 0;
    const lastActivity = overview?.last_activity ?? null;
    const aria = t('overview.cardAria', {
      name: project.name,
      runs: runs ?? '—',
      agents: agents ?? '—',
      active: active ?? '—',
      blockers: overview?.blockers ?? '—',
      errors,
    });
    const stats = h('div', { class: 'project-stats' });
    if (!overview) {
      stats.append(h('p', { class: 'project-error-line', text: t('overview.statsUnavailable') }));
    } else {
      stats.append(
        h('div', { class: 'project-stat' },
          h('div', { class: 'k', text: t('overview.tasks') }),
          h('div', { class: 'v', text: fmtNumber(overview.tasks) }),
          h('div', { class: 'sub', text: t('overview.tasksSub', { active: fmtNumber(overview.active_tasks) }) }),
        ),
        h('div', { class: 'project-stat' },
          h('div', { class: 'k', text: t('overview.agents') }),
          h('div', { class: 'v', text: fmtNumber(agents) }),
          h('div', { class: 'sub', text: t('overview.registeredSub', { registered: fmtNumber(registered), linked: fmtNumber(linked) }) }),
        ),
        h('div', { class: 'project-stat', 'data-tone': blockers ? 'warn' : '' },
          h('div', { class: 'k', text: t('overview.blockers') }),
          h('div', { class: 'v', text: fmtNumber(overview.blockers) }),
          h('div', { class: 'sub', text: t('overview.blockersSub') }),
        ),
        h('div', { class: 'project-stat', 'data-tone': errors ? 'bad' : '' },
          h('div', { class: 'k', text: t('overview.errors') }),
          h('div', { class: 'v', text: fmtNumber(errors) }),
          h('div', { class: 'sub', text: `${t('overview.runs')} ${fmtNumber(overview.runs)}` }),
        ),
        h('div', { class: 'project-stat', 'data-tone': overview.verified ? 'ok' : '' },
          h('div', { class: 'k', text: t('overview.verification') }),
          h('div', { class: 'v' }, fmtNumber(overview.verified), h('small', { text: ` ${pct(overview.verified_rate)}` })),
          h('div', { class: 'sub', text: t('overview.verifiedCount', { count: fmtNumber(verified) }) }),
        ),
        h('div', { class: 'project-stat', title: lastActivity ? fmtDateTime(lastActivity) : null },
          h('div', { class: 'k', text: t('overview.lastActivity') }),
          h('div', { class: 'v', text: fmtAgoShort(lastActivity) }),
          h('div', { class: 'sub', text: lastActivity ? fmtDateTimeShort(lastActivity) : t('common.unknown') }),
        ),
      );
    }
    return h('a', {
      class: 'project-card', href: `#/p/${encodeURIComponent(project.project_id)}/office`,
      'data-tone': tone, 'aria-label': aria,
    },
      h('div', { class: 'project-card-head' },
        h('h2', { class: 'project-name', text: project.name }),
        theme ? h('span', { class: 'project-theme', 'data-theme': theme, text: t(`office.theme.${theme}`) }) : null,
      ),
      h('p', { class: 'project-desc', text: project.description || t('overview.noDescription') }),
      project.root ? h('p', { class: 'project-root', text: project.root }) : null,
      stats,
      error ? h('p', { class: 'project-error-line', text: `${t('overview.projectError')}: ${String(error)}` }) : null,
      h('div', { class: 'project-foot' },
        status ? h('span', { class: 'status-badge', 'data-status': status.tone === 'ok' ? 'completed' : status.tone === 'bad' ? 'failed' : 'unknown', text: status.label }) : null,
        overview?.live ? h('span', { text: t('overview.liveCount', { count: fmtNumber(overview.live) }) }) : null,
        h('span', { class: 'open' }, t('overview.open'), icon('arrow')),
      ),
    );
  }

  function renderGlobalNotice() {
    const host = $('#overview-notice');
    if (!host) return;
    clear(host);
    host.classList.add('hidden');
    host.removeAttribute('data-tone');
    let title = null;
    let body = null;
    let tone = 'warn';
    if (state.globalError) {
      title = t('overview.errorTitle');
      body = t('overview.errorBody');
      tone = 'bad';
    } else if (state.registryError) {
      title = t('overview.projectsUnavailable');
      body = state.registryError.message;
    } else if (state.legacy) {
      title = t('common.legacy');
      body = null;
      tone = 'info';
    }
    if (!title) return;
    host.classList.remove('hidden');
    if (tone === 'bad') host.dataset.tone = 'bad';
    host.append(
      icon(tone === 'bad' ? 'alert' : 'clock'),
      h('div', {},
        h('strong', { text: `${title} ` }),
        body || '',
        state.globalError?.status ? h('span', { text: ` ${t('overview.errorDetail', { status: state.globalError.status })}` }) : null,
        h('span', { class: 'notice-actions' },
          button(t('overview.retry'), () => refresh(true)),
        ),
      ),
    );
  }

  function renderGlobal() {
    const totalsHost = $('#global-totals');
    const grid = $('#project-grid');
    const stateHost = $('#overview-state');
    const registry = $('#registry-path');
    if (!totalsHost || !grid || !stateHost) return;
    clear(totalsHost);
    clear(grid);
    clear(stateHost);
    renderGlobalNotice();

    if (registry) {
      if (state.registryPath) {
        registry.hidden = false;
        registry.textContent = `${t('top.registry')}: ${state.registryPath}`;
        registry.title = state.registryPath;
      } else {
        registry.hidden = true;
      }
    }

    const globalProjects = new Map((state.global?.projects || []).map((project) => [project.project_id, project]));
    let projects = state.projects.slice();
    if (!projects.length && globalProjects.size) {
      projects = [...globalProjects.values()].map(normalizeProjectMetadata).filter(Boolean);
    }

    if (state.registryLoading && !projects.length) {
      for (let index = 0; index < 6; index += 1) {
        totalsHost.append(h('div', { class: 'kpi' },
          h('div', { class: 'kpi-label', text: t('common.loading') }),
          h('div', { class: 'kpi-value', text: '—' }),
        ));
      }
      stateHost.append(h('div', { class: 'overview-loading' },
        ...Array.from({ length: 4 }, () => h('div', { class: 'ghost-card' }))));
      return;
    }

    const totals = state.global?.totals || null;
    const totalsItems = [
      { label: t('overview.projects'), value: totals?.projects ?? projects.length, sub: t('overview.projectsSub'), tone: 'accent' },
      { label: t('overview.runs'), value: totals?.runs, sub: t('overview.runsSub'), tone: '' },
      { label: t('overview.verified'), value: totals?.verified, sub: t('overview.verifiedSub'), tone: totals?.verified ? 'accent' : '' },
      { label: t('overview.agents'), value: totals?.agents, sub: t('overview.agentsSessionsSub'), tone: '' },
      { label: t('overview.activeTasks'), value: totals?.active_tasks, sub: t('overview.activeTasksSub'), tone: totals?.active_tasks ? 'warn' : '' },
      { label: t('overview.blockers'), value: totals?.blockers, sub: t('overview.blockersSub'), tone: totals?.blockers ? 'warn' : '' },
      { label: t('overview.errors'), value: totals?.errors, sub: t('overview.errorsSub'), tone: totals?.errors ? 'bad' : '' },
    ];
    for (const item of totalsItems) {
      totalsHost.append(h('div', { class: `kpi ${item.tone || ''}` },
        h('div', { class: 'kpi-label', text: item.label }),
        h('div', { class: 'kpi-value', text: fmtNumber(item.value) }),
        h('div', { class: 'kpi-sub', text: item.sub }),
      ));
    }

    if (!projects.length) {
      if (state.globalError || state.fatalError) {
        stateHost.append(emptyState({
          glyph: '!',
          title: t('overview.errorTitle'),
          body: state.fatalError?.message || state.globalError?.message || t('overview.errorBody'),
          hint: state.registryPath || null,
          actions: [button(t('overview.retry'), () => refresh(true), 'btn btn-primary')],
        }));
      } else {
        stateHost.append(emptyState({
          glyph: '▦',
          title: t('overview.emptyTitle'),
          body: t('overview.emptyBody'),
          hint: state.registryPath ? t('overview.emptyPath', { path: state.registryPath }) : null,
          actions: [button(t('overview.retry'), () => refresh(true))],
        }));
      }
    } else {
      for (const project of projects) {
        const globalProject = globalProjects.get(project.project_id) || null;
        grid.append(overviewCard(project, globalProject));
      }
    }

    const announce = $('#global-status');
    if (announce) announce.textContent = t('overview.loaded', { count: projects.length });
  }

  /* ── Project-scoped data ───────────────────────────────────────────────── */
  function resetProjectData() {
    state.stateToken += 1;
    state.detailToken += 1;
    state.detailLoadingId = null;
    state.officeToken += 1;
    state.inspector.token += 1;
    state.data = null;
    state.detail = null;
    state.detailProjectId = null;
    state.office = null;
    state.officeError = null;
    state.officeLoading = false;
    state.officeSignature = '';
    state.stateError = null;
    state.stateLoading = false;
    state.etag = null;
    state.selected = null;
    state.boardRun = null;
    state.runListSignature = '';
    state.demo = false;
    state.lastFetch = 0;
    state.lastOfficeFetch = 0;
    state.lastMilestoneFetch = 0;
    state.milestones = null;
    state.milestoneError = null;
    state.milestoneFilter = null;
    state.arranging = false;
    state.inspector = {
      open: false,
      mode: 'agent',
      agentId: null,
      agent: null,
      session: null,
      payload: null,
      loading: false,
      error: null,
      trigger: null,
      token: state.inspector.token,
    };
    const inspectorContent = $('#inspector-content');
    if (inspectorContent) clear(inspectorContent);
    const inspectorPinned = $('#inspector-pinned');
    if (inspectorPinned) clear(inspectorPinned);
    clear($('#run-list'));
    clear($('#run-detail'));
    clear($('#filters'));
    clear($('#overview'));
    clear($('#board'));
    clear($('#board-kpis'));
    clear($('#board-runs'));
    clear($('#workspaces'));
    if (window.AOOffice?.destroyOffice) window.AOOffice.destroyOffice();
    clear($('#office-root'));
    clear($('#office-summary'));
    clear($('#office-milestones'));
    clear($('#board-milestones'));
    ['runs', 'board', 'workspaces'].forEach((name) => {
      const counter = $(`#tab-${name}-count`);
      if (counter) counter.textContent = '—';
    });
    const officeCount = $('#tab-office-count');
    if (officeCount) officeCount.textContent = '—';
  }

  function disconnectLive() {
    clearTimeout(state.live.retryTimer);
    state.live.retryTimer = null;
    if (state.live.source) {
      try { state.live.source.close(); } catch { /* ignore */ }
    }
    state.live.source = null;
    state.live.projectId = null;
    state.live.mode = 'offline';
    state.live.failures = 0;
    state.live.lastSequence = 0;
  }

  function closeInspector(options = {}) {
    if (!state.inspector.open) return;
    const trigger = state.inspector.trigger;
    state.inspector.open = false;
    state.inspector.token += 1;
    const shell = $('#inspector-shell');
    if (shell) shell.hidden = true;
    document.body.classList.remove('inspector-open');
    if (options.restore !== false) {
      if (trigger instanceof HTMLElement && trigger.isConnected) {
        trigger.focus();
      } else {
        const key = state.inspector.triggerKey;
        const replacement = key
          ? document.querySelector(`[data-desk-key="${CSS.escape(key)}"]`)
          : null;
        (replacement || $('#tab-office'))?.focus();
      }
    }
  }

  function resetToOverviewState() {
    disconnectLive();
    closeInspector({ restore: false });
    resetProjectData();
    state.demo = state.globalDemo;
    state.projectId = null;
    state.projectMeta = null;
    state.tab = 'office';
    state.mode = 'overview';
    writeStore(STORE_PROJECT, null);
    document.body.dataset.mode = 'overview';
    const tabs = $('#project-tabs');
    if (tabs) tabs.classList.add('hidden');
    $('#live-badge')?.classList.add('hidden');
    updateDemoBadge();
    updatePath(null);
    const search = $('#run-search');
    if (search) search.value = '';
    state.query = '';
    state.filter = 'all';
    syncProjectSelect();
    showView('overview');
  }

  async function openOverview(options = {}) {
    resetToOverviewState();
    if (!options.skipHash) writeHash(null, null, { replace: true });
    renderGlobal();
    if (!state.global || options.refresh || Date.now() / 1000 - state.lastGlobalFetch > 4) {
      await loadGlobal({ force: Boolean(options.refresh || !state.global) });
    }
    renderGlobal();
    probeDemoMode();
    focusViewHeading();
  }

  async function openProject(projectId, tab, options = {}) {
    const project = findProject(projectId);
    if (!project) {
      toast(t('common.projectNotFound'));
      await openOverview({ skipHash: options.skipHash });
      return;
    }
    const nextTab = tabFromHash(tab);
    if (state.mode === 'project' && state.projectId === projectId) {
      setTab(nextTab, { updateHash: !options.skipHash });
      return;
    }
    disconnectLive();
    closeInspector({ restore: false });
    resetProjectData();
    state.mode = 'project';
    state.projectId = projectId;
    state.projectMeta = project;
    state.tab = nextTab;
    state.arranging = false;
    document.body.dataset.mode = 'project';
    writeStore(STORE_PROJECT, projectId);
    syncProjectSelect();
    updatePath(project);
    const tabs = $('#project-tabs');
    if (tabs) tabs.classList.remove('hidden');
    if (!options.skipHash) writeHash(projectId, nextTab, { replace: Boolean(options.replaceHash) });
    setTab(nextTab, { updateHash: false });
    renderProjectLoading();
    renderOffice();
    updateDemoBadge();
    updateLiveBadge();
    renderUpdated();
    syncSettingsControls();
    connectLive();
    await Promise.all([
      loadState({ force: true }),
      loadOffice({ force: true }),
      loadMilestones({ force: true }),
    ]);
    if (state.projectId !== projectId) return;
    await syncDetail();
    renderUpdated();
    focusViewHeading();
  }

  function renderProjectLoading() {
    const rail = $('#run-list');
    if (rail) {
      clear(rail);
      rail.append(h('div', { class: 'rail-empty' },
        h('span', { class: 'spinner', 'aria-hidden': 'true' }),
        h('span', { text: ` ${t('common.loading')}` }),
      ));
    }
    const detail = $('#run-detail');
    if (detail) {
      clear(detail);
      detail.append(emptyState({
        glyph: '◌',
        title: t('runs.reading'),
        body: t('runs.readingBody'),
      }));
    }
    const board = $('#board');
    if (board) {
      clear(board);
      board.append(h('div', { class: 'empty-state', style: { 'grid-column': '1 / -1' } },
        h('div', { class: 'empty-inner' },
          h('div', { class: 'empty-mark' }, h('span', { class: 'spinner', 'aria-hidden': 'true' })),
          h('h2', { text: t('common.loading') }),
        ),
      ));
    }
    const workspaces = $('#workspaces');
    if (workspaces) {
      clear(workspaces);
      workspaces.append(h('div', { class: 'empty-state', style: { 'grid-column': '1 / -1' } },
        h('div', { class: 'empty-inner' },
          h('div', { class: 'empty-mark' }, h('span', { class: 'spinner', 'aria-hidden': 'true' })),
          h('h2', { text: t('common.loading') }),
        ),
      ));
    }
  }

  async function loadState(options = {}) {
    const projectId = state.projectId;
    if (!projectId) return false;
    const token = ++state.stateToken;
    state.stateLoading = true;
    try {
      const headers = {};
      if (state.etag && !options.force) headers['If-None-Match'] = state.etag;
      let response;
      try {
        response = await requestJSON(`/api/projects/${encodeURIComponent(projectId)}/state`, { headers });
      } catch (error) {
        if (errorStatus(error) === 404 && (state.legacy || projectId === LEGACY_PROJECT)) {
          response = await requestJSON('/api/state', { headers });
        } else {
          throw error;
        }
      }
      if (token !== state.stateToken || projectId !== state.projectId) return false;
      if (response.notModified) {
        state.lastFetch = Date.now() / 1000;
        state.stateError = null;
        renderUpdated();
        updateLiveBadge();
        return true;
      }
      const payload = response.data;
      if (!payload || typeof payload !== 'object') throw new Error(t('errors.invalidResponse'));
      const previousFingerprint = state.data?.fingerprint;
      state.etag = response.etag;
      state.data = payload;
      state.stateError = null;
      state.demo = Boolean(payload.demo);
      if (state.demo) state.globalDemo = true;
      state.projectMeta = mergeProjectMeta(projectId, state.projectMeta, payload);
      state.lastFetch = Date.now() / 1000;
      if (!state.projectMeta?.name) {
        state.projectMeta = mergeProjectMeta(projectId, state.projectMeta, payload);
      }
      const changed = previousFingerprint !== payload.fingerprint || !previousFingerprint;
      if (changed || options.force) {
        renderAll();
        if (options.force) state.detail = null;
        await syncDetail({ force: Boolean(options.force) });
      } else {
        renderUpdated();
      }
      updateDemoBadge();
      updateLiveBadge();
      syncSettingsControls();
      return true;
    } catch (error) {
      if (token !== state.stateToken || projectId !== state.projectId) return false;
      state.stateError = error;
      renderProjectStateError(error);
      updateLiveBadge();
      return false;
    } finally {
      if (token === state.stateToken) state.stateLoading = false;
    }
  }

  function renderProjectStateError(error) {
    const rail = $('#run-list');
    if (rail) {
      clear(rail);
      rail.append(h('div', { class: 'rail-empty' },
        h('strong', { text: t('runs.projectUnavailableTitle') }),
        h('p', { text: error.message }),
        button(t('common.retry'), () => loadState({ force: true })),
      ));
    }
    const detail = $('#run-detail');
    if (detail) {
      clear(detail);
      detail.append(emptyState({
        glyph: '!',
        title: t('runs.projectUnavailableTitle'),
        body: `${t('runs.projectUnavailableBody')} ${error.message}`,
        hint: errorStatus(error) ? t('errors.request', { status: errorStatus(error) }) : null,
        actions: [button(t('common.retry'), () => loadState({ force: true }), 'btn btn-primary')],
      }));
    }
    const board = $('#board');
    if (board) {
      clear(board);
      board.append(h('div', { class: 'empty-state', style: { 'grid-column': '1 / -1' } },
        h('div', { class: 'empty-inner' },
          h('div', { class: 'empty-mark' }, h('span', { class: 'empty-glyph', 'aria-hidden': 'true', text: '!' })),
          h('h2', { text: t('runs.projectUnavailableTitle') }),
          h('p', { text: error.message }),
          h('div', { class: 'empty-actions' }, button(t('common.retry'), () => loadState({ force: true }))),
        ),
      ));
    }
    const workspaces = $('#workspaces');
    if (workspaces) {
      clear(workspaces);
      workspaces.append(h('div', { class: 'empty-state', style: { 'grid-column': '1 / -1' } },
        h('div', { class: 'empty-inner' },
          h('div', { class: 'empty-mark' }, h('span', { class: 'empty-glyph', 'aria-hidden': 'true', text: '!' })),
          h('h2', { text: t('runs.projectUnavailableTitle') }),
          h('p', { text: error.message }),
        ),
      ));
    }
    ['runs', 'board', 'workspaces'].forEach((name) => {
      const counter = $(`#tab-${name}-count`);
      if (counter) counter.textContent = '—';
    });
  }

  async function loadOffice(options = {}) {
    const projectId = state.projectId;
    if (!projectId) return false;
    const token = ++state.officeToken;
    state.officeLoading = true;
    if (!state.office) renderOffice();
    try {
      const response = await requestJSON(`/api/projects/${encodeURIComponent(projectId)}/office`);
      if (token !== state.officeToken || projectId !== state.projectId) return false;
      const payload = response.data;
      if (!payload || typeof payload !== 'object' || !Array.isArray(payload.agents)) {
        throw new Error(t('errors.invalidResponse'));
      }
      state.office = payload;
      state.officeError = null;
      state.lastOfficeFetch = Date.now() / 1000;
      if (payload.project) {
        state.projectMeta = mergeProjectMeta(projectId, state.projectMeta, { project: payload.project, project_id: projectId });
      }
      const signature = officeSignature();
      if (signature !== state.officeSignature || options.force) {
        state.officeSignature = signature;
        renderOffice();
      }
      syncSettingsControls();
      return true;
    } catch (error) {
      if (token !== state.officeToken || projectId !== state.projectId) return false;
      state.officeError = error;
      state.office = null;
      renderOffice();
      const counter = $('#tab-office-count');
      if (counter) counter.textContent = '—';
      return false;
    } finally {
      if (token === state.officeToken) state.officeLoading = false;
    }
  }

  function officeSignature() {
    return JSON.stringify({
      office: state.office,
      error: state.officeError ? String(state.officeError.message || state.officeError) : null,
      language: state.language,
      theme: currentOfficeTheme(),
      order: currentDeskOrder(),
      arranging: state.arranging,
      reducedMotion: state.reducedMotion,
    });
  }

  async function loadMilestones(options = {}) {
    const projectId = state.projectId;
    if (!projectId) return false;
    const token = ++state.milestoneToken;
    try {
      const response = await requestJSON(`/api/projects/${encodeURIComponent(projectId)}/milestones`);
      if (token !== state.milestoneToken || projectId !== state.projectId) return false;
      if (!response.ok || !response.data || typeof response.data !== 'object') {
        throw new Error(t('errors.invalidResponse'));
      }
      state.milestones = response.data;
      try {
        const memoryResponse = await requestJSON(`/api/projects/${encodeURIComponent(projectId)}/memory`);
        state.memory = memoryResponse.ok ? memoryResponse.data : null;
      } catch {
        state.memory = null;
      }
      state.milestoneError = null;
      state.lastMilestoneFetch = Date.now() / 1000;
      if (state.milestoneFilter) {
        const stillThere = (response.data.runs || []).some((run) =>
          run.run_id === state.milestoneFilter.runId
          && (run.milestones || []).some((item) => item.id === state.milestoneFilter.milestoneId));
        if (!stillThere) state.milestoneFilter = null;
      }
      renderMilestones();
      if (state.tab === 'board') renderBoard();
      return true;
    } catch (error) {
      if (token !== state.milestoneToken || projectId !== state.projectId) return false;
      state.milestoneError = error;
      renderMilestones();
      return false;
    }
  }

  function milestoneStatusLabel(status) {
    return t(`milestone.status.${status || 'planned'}`);
  }

  function selectMilestone(run, milestone) {
    if (!run || !milestone) return;
    const current = state.milestoneFilter;
    if (current && current.runId === run.run_id && current.milestoneId === milestone.id) {
      state.milestoneFilter = null;
    } else {
      state.milestoneFilter = {
        runId: run.run_id, milestoneId: milestone.id,
        taskIds: (milestone.tasks || []).map((task) => task.id),
      };
      state.boardRun = run.run_id;
    }
    renderMilestones();
    setTab('board');
    renderBoard();
  }

  function milestoneCard(run, milestone, options = {}) {
    const tasks = milestone.tasks || [];
    const completed = tasks.filter((task) => task.state === 'completed').length;
    const running = tasks.filter((task) => ['running', 'cancel_requested'].includes(task.state)).length;
    const failed = tasks.filter((task) => ['failed', 'cancelled', 'timed_out'].includes(task.state)).length;
    const selected = state.milestoneFilter?.runId === run.run_id
      && state.milestoneFilter?.milestoneId === milestone.id;
    const roleList = milestone.require_roles || [];
    const handoff = milestone.handoff || null;
    return h('button', {
      class: `milestone-card${selected ? ' selected' : ''}`,
      type: 'button', 'data-status': milestone.status,
      'aria-pressed': String(Boolean(selected)),
      onclick: () => selectMilestone(run, milestone),
    },
      h('span', { class: 'milestone-top' },
        h('span', { class: 'milestone-id', text: milestone.id }),
        h('span', { class: 'milestone-status', 'data-status': milestone.status, text: milestoneStatusLabel(milestone.status) }),
      ),
      h('strong', { class: 'milestone-objective', text: milestone.objective || milestone.id }),
      h('span', { class: 'milestone-metrics' },
        h('span', { text: t('milestone.tasksCount', { done: completed, total: tasks.length }) }),
        running ? h('span', { class: 'milestone-running', text: t('milestone.runningCount', { count: running }) }) : null,
        failed ? h('span', { class: 'milestone-failed', text: t('milestone.failedCount', { count: failed }) }) : null,
        handoff ? h('span', { class: 'milestone-handoff', text: t('milestone.handoff') }) : null,
      ),
      roleList.length
        ? h('span', { class: 'milestone-roles', text: t('milestone.roles', { roles: roleList.join(', ') }) })
        : null,
      options.compact ? null : h('span', {
        class: 'milestone-acceptance',
        text: (milestone.acceptance || []).slice(0, 2).join(' · '),
      }),
    );
  }

  function renderProjectMemory() {
    const memory = state.memory;
    if (!memory?.available) return null;
    const decisions = (memory.decisions || []).slice(0, 5);
    const requirements = (memory.requirements || []).slice(0, 5);
    const interviews = (memory.interviews || []).slice(0, 3);
    return h('div', { class: 'project-memory' },
      h('div', { class: 'project-memory-head' },
        h('div', {},
          h('div', { class: 'eyebrow', text: t('memory.eyebrow') }),
          h('strong', { text: t('memory.title') }),
        ),
        h('span', { class: 'memory-platform', text: t('memory.lastPlatform', {
          platform: memory.state?.last_platform || 'unknown',
        }) }),
      ),
      h('pre', { class: 'memory-summary', text: (memory.summary || '').slice(0, 1600) }),
      memory.question
        ? h('div', { class: 'memory-question' },
          h('span', { class: 'memory-label', text: t('memory.currentQuestion') }),
          h('strong', { text: memory.question.question }),
          h('ul', {}, ...(memory.question.options || []).map((option) => h('li', {
            text: `${option.label}${option.recommended ? ' — recommended' : ''}`,
          }))),
        )
        : null,
      h('div', { class: 'memory-columns' },
        h('div', {},
          h('span', { class: 'memory-label', text: t('memory.decisions') }),
          decisions.length
            ? h('ul', {}, ...decisions.map((item) => h('li', {
              text: `${item.topic}: ${item.decision} (${item.status})`,
            })))
            : h('p', { class: 'milestone-note', text: t('memory.none') }),
        ),
        h('div', {},
          h('span', { class: 'memory-label', text: t('memory.openRequirements') }),
          requirements.length
            ? h('ul', {}, ...requirements.filter((item) => item.status.startsWith('unresolved'))
              .slice(0, 5).map((item) => h('li', { text: item.text })))
            : h('p', { class: 'milestone-note', text: t('memory.none') }),
        ),
        h('div', {},
          h('span', { class: 'memory-label', text: t('memory.interviews') }),
          interviews.length
            ? h('ul', {}, ...interviews.map((item) => h('li', {
              text: `${item.status} · ${item.origin_platform || 'unknown'} · ${item.request_summary}`,
            })))
            : h('p', { class: 'milestone-note', text: t('memory.none') }),
        ),
      ),
    );
  }

  function renderMilestones() {
    const boardHost = $('#board-milestones');
    const officeHost = $('#office-milestones');
    if (!boardHost && !officeHost) return;
    const payload = state.milestones;
    const demo = Boolean(payload?.demo);
    const runs = payload?.runs || [];
    const activeRun = runs.find((run) => run.run_id === payload?.current_run_id) || runs[0] || null;
    if (boardHost) {
      clear(boardHost);
      if (!activeRun) {
        boardHost.append(h('div', { class: 'milestone-empty' },
          h('p', { text: state.milestoneError ? t('milestone.error') : t('milestone.empty') }),
          h('p', { class: 'milestone-note', text: t('milestone.emptyHint') }),
        ));
      } else {
        const head = h('div', { class: 'milestone-head' },
          h('div', {},
            h('div', { class: 'eyebrow', text: t('milestone.eyebrow') }),
            h('h2', { text: t('milestone.title') }),
          ),
          h('div', { class: 'milestone-progress' },
            demo ? h('span', { class: 'demo-chip', text: 'DEMO DATA' }) : null,
            activeRun.work_mode
              ? h('span', { class: 'mode-chip', 'data-mode': activeRun.work_mode,
                            text: t(`milestone.mode.${activeRun.work_mode}`) })
              : null,
            activeRun.recovery_state
              ? h('span', {
                class: 'recovery-chip',
                'data-state': activeRun.recovery_state,
                text: t(`milestone.recovery.${activeRun.recovery_state}`),
              })
              : null,
            activeRun.orphaned_managed_sessions > 0
              ? h('span', {
                class: 'orphan-chip',
                text: t('milestone.orphaned', {
                  count: activeRun.orphaned_managed_sessions,
                }),
              })
              : null,
            activeRun.scheduler
              ? h('span', {
                class: 'scheduler-chip',
                text: t('milestone.activeSessions', {
                  count: activeRun.scheduler.active_sessions || 0,
                  queued: activeRun.scheduler.queued_tasks || 0,
                }),
              })
              : null,
            activeRun.scheduler?.repairing
              ? h('span', { class: 'repair-chip', text: t('milestone.repairing') })
              : null,
            activeRun.usage?.cost_measured
              ? h('span', { class: 'cost-chip', text: t('milestone.cost', {
                cost: Number(activeRun.usage.cost_total_usd).toFixed(4),
              }) })
              : null,
            h('span', {
              text: t('milestone.verifiedProgress', {
                passed: activeRun.passed_milestones,
                total: activeRun.milestone_count,
                percent: Math.round((activeRun.verified_progress || 0) * 100),
              }),
            }),
            h('span', { class: 'milestone-estimate', text: t('milestone.estimateNotShown') }),
            state.milestoneFilter
              ? h('button', {
                class: 'text-btn', type: 'button', text: t('milestone.clearFilter'),
                onclick: () => { state.milestoneFilter = null; renderMilestones(); renderBoard(); },
              })
              : null,
          ),
        );
        boardHost.append(head);
        const list = h('div', { class: 'milestone-list', role: 'list' });
        for (const milestone of activeRun.milestones || []) {
          list.append(h('div', { role: 'listitem' }, milestoneCard(activeRun, milestone)));
        }
        boardHost.append(list);
        const timelineTasks = (activeRun.milestones || [])
          .flatMap((milestone) => milestone.tasks || [])
          .sort((left, right) => {
            const rank = { running: 0, cancel_requested: 0, pending: 1, completed: 2,
                           failed: 2, cancelled: 2, timed_out: 2 };
            return (rank[left.state] ?? 3) - (rank[right.state] ?? 3)
              || (left.id > right.id ? 1 : -1);
          })
          .slice(0, 12);
        if (timelineTasks.length) {
          const timeline = h('div', { class: 'milestone-timeline' },
            h('div', { class: 'milestone-timeline-head' },
              h('span', { text: t('milestone.timeline') }),
              h('span', {
                class: 'milestone-note',
                text: t('milestone.queue', {
                  queued: activeRun.scheduler?.queued_tasks || 0,
                }),
              }),
            ),
          );
          for (const task of timelineTasks) {
            const waiting = (task.waiting_on || []).join(', ');
            timeline.append(h('div', { class: 'milestone-timeline-row', 'data-state': task.state },
              h('span', { class: 'timeline-task', text: task.id }),
              h('span', { class: 'timeline-role', text: task.role }),
              h('span', { class: 'timeline-state', text: task.state }),
              waiting ? h('span', { class: 'timeline-waiting', text: t('milestone.waitingOn', { deps: waiting }) }) : null,
              task.duration_seconds != null ? h('span', { class: 'timeline-metric', text: fmtDuration(task.duration_seconds) }) : null,
              task.managed?.tool_count != null ? h('span', { class: 'timeline-metric', text: t('milestone.tools', { count: task.managed.tool_count }) }) : null,
              task.managed?.cost != null ? h('span', { class: 'timeline-metric', text: `$${Number(task.managed.cost).toFixed(4)}` }) : null,
            ));
          }
          boardHost.append(timeline);
        }
        const memoryPanel = renderProjectMemory();
        if (memoryPanel) boardHost.append(memoryPanel);
      }
    }
    if (officeHost) {
      clear(officeHost);
      if (!activeRun) return;
      const current = (activeRun.milestones || []).find((item) =>
        ['running', 'verifying', 'repairing', 'ready'].includes(item.status))
        || [...(activeRun.milestones || [])].reverse().find((item) => item.status === 'passed')
        || (activeRun.milestones || [])[0];
      if (!current) return;
      officeHost.append(
        h('div', { class: 'office-milestone-strip' },
          h('span', { class: 'office-milestone-label', text: t('milestone.current') }),
          activeRun.work_mode
            ? h('span', { class: 'mode-chip', 'data-mode': activeRun.work_mode,
                          text: t(`milestone.mode.${activeRun.work_mode}`) })
            : null,
          h('button', {
            class: 'office-milestone-open', type: 'button',
            onclick: () => selectMilestone(activeRun, current),
          },
            h('strong', { text: `${current.id} · ${current.objective}` }),
            h('span', {
              text: t('milestone.verifiedShort', {
                passed: activeRun.passed_milestones,
                total: activeRun.milestone_count,
              }),
            }),
            activeRun.scheduler
              ? h('span', {
                class: 'scheduler-chip',
                text: t('milestone.activeSessions', {
                  count: activeRun.scheduler.active_sessions || 0,
                  queued: activeRun.scheduler.queued_tasks || 0,
                }),
              })
              : null,
          ),
          h('span', { class: 'office-milestone-status', 'data-status': current.status, text: milestoneStatusLabel(current.status) }),
        ),
      );
    }
  }

  function renderOffice() {
    const focusedDesk = document.activeElement?.closest?.('.desk-card')?.querySelector('.desk-open')?.dataset.deskKey
      || document.activeElement?.dataset?.deskKey
      || null;
    if (!Office) {
      const root = $('#office-root');
      if (root) {
        clear(root);
        root.append(emptyState({
          glyph: '!',
          title: t('office.errorTitle'),
          body: t('office.endpointMissing'),
        }));
      }
      return;
    }
    const root = $('#office-root');
    const summary = $('#office-summary');
    const theme = currentOfficeTheme();
    Office.renderOffice(root, summary, {
      lang: state.language,
      payload: state.office,
      error: state.officeError,
      theme,
      order: currentDeskOrder(),
      arranging: state.arranging,
      reducedMotion: state.reducedMotion,
      callbacks: {
        onInspect: (agent, trigger) => openAgentInspector(agent, trigger),
        onInspectSession: (session, trigger) => openUnlinkedInspector(session, trigger),
        onInspectManaged: (managed, trigger) => openManagedInspector(managed, trigger),
        onTrigger: (element) => { state.inspector.trigger = element; },
        onMove: (key, delta) => moveDesk(key, delta),
        onRetry: () => loadOffice({ force: true }),
        onOpenRuns: () => setTab('runs'),
      },
    });
    state.officeSignature = officeSignature();
    if (focusedDesk) {
      root?.querySelector(`[data-desk-key="${CSS.escape(focusedDesk)}"]`)?.focus();
    }
    const count = state.office
      ? ((state.office.summary?.agents ?? state.office.agents?.length ?? 0)
         + (state.office.summary?.managed || 0))
      : null;
    const counter = $('#tab-office-count');
    if (counter) counter.textContent = count == null ? '—' : fmtNumber(count);
    const officeTheme = $('#office-theme');
    if (officeTheme) officeTheme.value = theme;
    const settingsTheme = $('#set-office-theme');
    if (settingsTheme) settingsTheme.value = theme;
  }

  function setOfficeTheme(value) {
    if (!state.projectId) return;
    const theme = Office ? Office.normalizeTheme(value) : 'ai';
    writeStore(officeThemeKey(state.projectId), theme);
    renderOffice();
    syncSettingsControls();
    toast(t('office.themeSaved'));
  }

  function moveDesk(key, delta) {
    if (!Office || !state.office) return;
    const order = currentDeskOrder();
    const keys = Office.officeEntries(state.office, order).map((entry) => entry.key);
    const next = Office.moveDesk(order, keys, key, delta);
    if (!Array.isArray(next) || !next.length) return;
    writeStore(officeOrderKey(state.projectId), JSON.stringify(next));
    renderOffice();
    const announce = $('#office-status');
    if (announce) announce.textContent = t('office.orderSaved');
  }

  /* ── Live updates: SSE with ETag polling fallback ──────────────────────── */
  function connectLive() {
    const projectId = state.projectId;
    if (!projectId) return;
    disconnectLive();
    state.live.projectId = projectId;
    state.live.lastSequence = 0;
    if (NO_LIVE) {
      state.live.mode = 'polling';
      updateLiveBadge();
      loadState();
      return;
    }
    if (typeof EventSource === 'undefined') {
      state.live.mode = 'polling';
      updateLiveBadge();
      loadState();
      return;
    }
    let source;
    try {
      source = new EventSource(`/api/projects/${encodeURIComponent(projectId)}/events`);
    } catch {
      state.live.mode = 'polling';
      updateLiveBadge();
      loadState();
      return;
    }
    state.live.source = source;
    state.live.mode = 'connecting';
    updateLiveBadge();
    source.addEventListener('open', () => {
      if (state.live.source !== source || state.projectId !== projectId) return;
      state.live.mode = 'sse';
      state.live.failures = 0;
      updateLiveBadge();
      if (!state.data || state.stateError) loadState();
    });
    const receive = (event, snapshotHint) => handleLiveEvent(event, snapshotHint, projectId, source);
    source.addEventListener('snapshot', (event) => receive(event, true));
    source.addEventListener('update', (event) => receive(event, false));
    source.addEventListener('message', (event) => receive(event, false));
    source.addEventListener('error', () => {
      if (state.live.source !== source || state.projectId !== projectId) return;
      state.live.mode = 'polling';
      state.live.failures += 1;
      updateLiveBadge();
      if (state.live.failures >= 3) {
        try { source.close(); } catch { /* ignore */ }
        if (state.live.source === source) state.live.source = null;
        clearTimeout(state.live.retryTimer);
        state.live.retryTimer = setTimeout(() => {
          if (state.projectId === projectId && !state.live.source) {
            connectLive();
            loadOffice({ force: true });
          }
        }, 20000);
      }
    });
  }

  function handleLiveEvent(event, snapshotHint, projectId, source) {
    if (state.projectId !== projectId || state.live.source !== source) return;
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!payload || typeof payload !== 'object') return;
    if (payload.project_id && String(payload.project_id) !== String(projectId)) return;
    const type = payload.type === 'snapshot' || snapshotHint ? 'snapshot' : 'update';
    const accepted = Office
      ? Office.shouldApplyEvent({ type, sequenceId: payload.sequence_id, lastSequenceId: state.live.lastSequence })
      : true;
    if (!accepted) return;
    if (Office) state.live.lastSequence = Office.nextSequence(state.live.lastSequence, payload.sequence_id, type);
    if (!payload.state || typeof payload.state !== 'object') return;
    const previousFingerprint = state.data?.fingerprint;
    state.data = payload.state;
    state.stateError = null;
    state.stateLoading = false;
    state.demo = Boolean(payload.state.demo);
    state.lastFetch = Date.now() / 1000;
    state.projectMeta = mergeProjectMeta(projectId, state.projectMeta, payload.state);
    if (previousFingerprint !== payload.state.fingerprint || type === 'snapshot') {
      renderAll();
      syncDetail();
      updateDemoBadge();
      updateLiveBadge();
      renderUpdated();
    }
    clearTimeout(state.officeRefreshTimer);
    state.officeRefreshTimer = setTimeout(() => {
      if (state.projectId === projectId && !document.hidden) loadOffice();
    }, 900);
    if (state.inspector.open && state.inspector.mode === 'agent') {
      clearTimeout(state.inspectorRefreshTimer);
      state.inspectorRefreshTimer = setTimeout(() => {
        if (state.inspector.open && state.inspector.mode === 'agent') loadInspector({ silent: true });
      }, 1600);
    }
  }

  /* ── Run detail and evidence screens ──────────────────────────────────── */
  async function syncDetail(options = {}) {
    if (!state.data?.runs?.length) {
      state.selected = null;
      state.detail = null;
      state.detailProjectId = null;
      renderDetail();
      return;
    }
    if (!state.data.runs.some((run) => run.id === state.selected)) {
      state.selected = state.data.runs[0].id;
    }
    const summary = state.data.runs.find((run) => run.id === state.selected);
    if (!summary) return;
    if (!options.force && state.detail && state.detail.run?.fingerprint === summary.fingerprint
        && state.detail.run?.live === summary.live
        && state.detailProjectId === state.projectId) {
      return;
    }
    await selectRun(state.selected, { keepList: true });
  }

  async function selectRun(runId, options = {}) {
    if (!state.projectId) return;
    if (state.detailLoadingId === runId) return;
    state.detailLoadingId = runId;
    state.selected = runId;
    if (!options.keepList) {
      state.detail = null;
      renderDetail();
    }
    renderRunList();
    const token = ++state.detailToken;
    const projectId = state.projectId;
    const legacy = state.legacy || projectId === LEGACY_PROJECT;
    const url = legacy
      ? `/api/run/${encodeURIComponent(runId)}`
      : `/api/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}`;
    try {
      const response = await requestJSON(url);
      if (token !== state.detailToken || projectId !== state.projectId) return;
      state.detail = response.data;
      state.detailProjectId = projectId;
      renderDetail();
    } catch (error) {
      if (token !== state.detailToken || projectId !== state.projectId) return;
      state.detail = { error: error.message, run: { id: runId }, status: error.status || 0 };
      state.detailProjectId = projectId;
      renderDetail();
    } finally {
      if (state.detailLoadingId === runId) state.detailLoadingId = null;
    }
  }

  function runStatusMeta(run) {
    if (!run) return { key: 'unknown', label: t('runStatus.unknown'), tone: 'neutral' };
    if (run.status === 'invalid') return { key: 'invalid', label: t('runStatus.invalid'), tone: 'bad' };
    if (run.status === 'open') {
      return run.live
        ? { key: 'live', label: t('runStatus.live'), tone: 'info' }
        : { key: 'open', label: t('runStatus.open'), tone: 'warn' };
    }
    if (run.status === 'success') {
      if (run.verified) return { key: 'verified', label: t('runStatus.verified'), tone: 'ok' };
      if (run.self_reported) return { key: 'unverified', label: t('runStatus.selfReported'), tone: 'warn' };
      return { key: 'unverified', label: t('runStatus.unverified'), tone: 'warn' };
    }
    if (run.status === 'failed') return { key: 'failed', label: t('runStatus.failed'), tone: 'bad' };
    if (run.status === 'cancelled') return { key: 'cancelled', label: t('runStatus.cancelled'), tone: 'warn' };
    return { key: run.status || 'unknown', label: run.status || t('runStatus.unknown'), tone: 'neutral' };
  }

  function toneClass(tone) {
    return { ok: 'verified', info: 'live', warn: 'claimed', bad: 'failed' }[tone] || '';
  }

  function runStatusWord(status) {
    if (status === 'failed') return t('runStatus.failed');
    if (status === 'cancelled') return t('runStatus.cancelled');
    if (status === 'success') return t('runStatus.completed');
    if (status === 'open') return t('runStatus.open');
    return String(status || t('common.unknown'));
  }

  function platformLabel(value) {
    return String(value || 'unknown').toUpperCase();
  }

  function relativeOffset(starter, at) {
    if (at == null || !starter) return '—';
    return `+${fmtDuration(Math.max(0, at - starter))}`;
  }

  function renderAll() {
    if (!state.data) return;
    state.demo = Boolean(state.data.demo);
    if (state.projectMeta) updatePath(state.projectMeta);
    updateDemoBadge();
    state.boardRun = state.data.supervisor?.runs?.some((run) => run.id === state.boardRun)
      ? state.boardRun
      : state.data.supervisor?.runs?.[0]?.id || null;
    renderOverview();
    renderFilters();
    renderRunList();
    renderBoard();
    renderMilestones();
    renderWorkspaces();
    renderDetail();
    updateLiveBadge();
    const runsCounter = $('#tab-runs-count');
    if (runsCounter) runsCounter.textContent = fmtNumber(state.data.overview?.runs || 0);
    const boardCounter = $('#tab-board-count');
    if (boardCounter) {
      boardCounter.textContent = fmtNumber(
        state.data.supervisor?.runs?.reduce((total, run) => total + run.tasks.length, 0) || 0,
      );
    }
    const workspacesCounter = $('#tab-workspaces-count');
    if (workspacesCounter) workspacesCounter.textContent = fmtNumber(state.data.worktrees?.length || 0);
  }

  function renderOverview() {
    const overview = state.data?.overview || {};
    const host = $('#overview');
    if (!host) return;
    clear(host);
    const openAgents = Number(overview.open_agents || 0);
    const items = [
      {
        label: t('runs.overview.runs'),
        value: fmtNumber(overview.runs || 0),
        sub: t('runs.overview.runsSub', {
          verified: fmtNumber(overview.verified || 0),
          claimed: fmtNumber(overview.self_reported || 0),
        }),
        tone: '',
      },
      {
        label: t('overview.verified'),
        value: fmtNumber(overview.verified || 0),
        sub: t('runs.overview.verifiedSub', {
          rate: pct(overview.verified_rate),
          benchmark: fmtNumber(overview.benchmark_valid || 0),
        }),
        tone: 'accent',
        title: t('runs.overview.verifiedTitle'),
      },
      {
        label: t('runStatus.open'),
        value: fmtNumber(overview.open || 0),
        sub: t(openAgents === 1 ? 'runs.overview.openAgentOne' : 'runs.overview.openSub', {
          live: fmtNumber(overview.live || 0),
          agents: fmtNumber(openAgents),
        }),
        tone: overview.live ? 'warn' : '',
        title: t('runs.overview.openTitle'),
      },
      {
        label: t('overview.agents'),
        value: fmtNumber(overview.agents || 0),
        sub: t('runs.overview.agentsSub', {
          mutations: fmtNumber(overview.mutations || 0),
          platforms: Object.keys(overview.platforms || {}).length,
        }),
        tone: '',
      },
      {
        label: t('runs.kpi.overlap'),
        value: fmtDuration(overview.overlap_seconds || 0),
        sub: t('runs.overview.overlapSub'),
        tone: 'warn',
        title: t('runs.overview.openTitle'),
      },
    ];
    for (const item of items) {
      host.append(h('div', { class: `kpi ${item.tone}`, title: item.title || null },
        h('div', { class: 'kpi-label', text: item.label }),
        h('div', { class: 'kpi-value' }, item.value),
        h('div', { class: 'kpi-sub', text: item.sub }),
      ));
    }
  }

  function renderFilters() {
    const runs = state.data?.runs || [];
    const filters = [
      { key: 'all', label: t('runs.filter.all'), count: runs.length },
      { key: 'verified', label: t('runs.filter.verified'), count: runs.filter((run) => run.verified).length },
      { key: 'open', label: t('runs.filter.open'), count: runs.filter((run) => run.status === 'open').length },
      { key: 'failed', label: t('runs.filter.failed'), count: runs.filter((run) => run.status === 'failed').length },
    ];
    const host = $('#filters');
    if (!host) return;
    const activeFilter = document.activeElement?.closest?.('.filter')?.dataset.filter;
    clear(host);
    for (const filter of filters) {
      host.append(h('button', {
        class: 'filter', type: 'button', 'data-filter': filter.key,
        'aria-pressed': String(state.filter === filter.key),
        onclick: () => {
          clearTimeout(state.searchTimer);
          state.filter = filter.key;
          state.runListSignature = '';
          renderFilters();
          renderRunList();
          reconcileSelection();
          if (activeFilter) host.querySelector(`[data-filter="${activeFilter}"]`)?.focus();
        },
      }, filter.label, h('b', { text: filter.count })));
    }
  }

  function filteredRuns() {
    return (state.data?.runs || []).filter((run) => {
      if (state.filter === 'verified' && !run.verified) return false;
      if (state.filter === 'open' && run.status !== 'open') return false;
      if (state.filter === 'failed' && run.status !== 'failed') return false;
      if (!state.query) return true;
      const haystack = `${run.id} ${run.platform} ${run.status} ${runStatusMeta(run).label}`.toLowerCase();
      return haystack.includes(state.query.toLowerCase());
    });
  }

  function reconcileSelection() {
    const runs = filteredRuns();
    if (runs.some((run) => run.id === state.selected)) return;
    state.selected = runs[0]?.id || null;
    state.detail = null;
    state.detailProjectId = null;
    if (state.selected) selectRun(state.selected, { keepList: true });
    else renderDetail();
  }

  function renderRunList() {
    const host = $('#run-list');
    if (!host) return;
    const runs = filteredRuns();
    const visibleRuns = runs.slice(0, RUN_DISPLAY_LIMIT);
    const signature = [
      state.projectId, state.language, state.filter, state.query, state.selected, runs.length,
      visibleRuns.map((run) => `${run.id}:${run.fingerprint}:${run.status}:${run.verified}:${run.self_reported}`).join(','),
    ].join('|');
    if (signature === state.runListSignature) return;
    state.runListSignature = signature;
    const focusedRun = document.activeElement?.closest?.('.run-item')?.dataset.runId || null;
    clear(host);
    if (!runs.length) {
      host.append(h('div', { class: 'rail-empty' },
        state.data?.runs?.length ? t('runs.noMatch') : t('runs.emptyRail'),
        h('code', { text: t('runs.demoHint') }),
      ));
      return;
    }
    for (const run of visibleRuns) {
      const meta = runStatusMeta(run);
      const verifiedClass = run.verified ? 'meta-verified' : run.self_reported ? 'meta-claimed'
        : run.status === 'open' ? 'meta-open' : run.status === 'failed' ? 'meta-failed' : '';
      const statusText = run.verified ? `✓ ${t('runStatus.verified')}` : run.self_reported ? t('runStatus.selfReported')
        : run.status === 'open' ? t('runStatus.open') : run.status === 'success' ? t('runStatus.unverified') : meta.label;
      const agentsText = run.agent_count === 1
        ? t('runs.agentCountOne', { count: 1 })
        : t('runs.agentCount', { count: run.agent_count || 0 });
      const item = h('button', {
        class: 'run-item', type: 'button', 'data-status': meta.key, 'data-run-id': run.id,
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
          h('span', { text: agentsText }),
          h('span', { class: 'sep' }),
          h('span', { text: fmtDuration(run.duration_seconds) }),
          h('span', { class: 'sep' }),
          h('span', { class: verifiedClass, text: statusText }),
        ),
      );
      host.append(item);
    }
    if (focusedRun) {
      host.querySelector(`[data-run-id="${CSS.escape(focusedRun)}"]`)?.focus();
    }
    if (runs.length > visibleRuns.length) {
      host.append(h('div', { class: 'rail-empty', text: t('runs.showingLimit', { shown: visibleRuns.length, total: runs.length }) }));
    }
  }

  function renderDetail() {
    const host = $('#run-detail');
    if (!host) return;
    clear(host);
    if (!state.data) return;
    if (!state.selected) {
      host.append(emptyRunsState());
      return;
    }
    if (!state.detail || state.detailProjectId !== state.projectId) {
      host.append(emptyState({
        glyph: '◌',
        title: t('runs.reading'),
        body: t('runs.readingBody'),
      }));
      return;
    }
    if (state.detail.error) {
      host.append(emptyState({
        glyph: '!',
        title: t('runs.unavailableTitle'),
        body: state.detail.error,
        hint: t('runs.diagnoseCommand', { id: state.detail.run?.id || '' }),
        actions: [button(t('common.retry'), () => selectRun(state.detail.run?.id || state.selected), 'btn btn-primary')],
      }));
      return;
    }

    const { run, agents = [], checks = [], events = [], mutations = [] } = state.detail;
    if (!run) {
      host.append(emptyState({ glyph: '!', title: t('runs.unavailableTitle'), body: t('errors.invalidResponse') }));
      return;
    }
    const meta = runStatusMeta(run);
    const card = h('div', { class: 'detail-card' });

    card.append(h('div', { class: 'detail-head' },
      h('div', {},
        h('div', { class: 'eyebrow' },
          h('span', { text: t('runs.eyebrow') }),
          h('span', { class: 'platform', text: run.platform }),
          run.partial ? h('span', { class: 'pill claimed', text: t('runs.partial') }) : null,
        ),
        h('h1', { class: 'run-title' }, run.id, copyButton(run.id, t('runs.copyId'))),
        h('div', { class: 'run-sub' },
          h('span', { text: t('runs.started', { time: fmtDateTime(run.started_at) }) }),
          h('span', { text: t('runs.elapsed', { duration: fmtDuration(run.duration_seconds) }) }),
          h('span', { text: run.agent_count === 1 ? t('runs.agentSpanOne', { count: 1 }) : t('runs.agentSpans', { count: run.agent_count || 0 }) }),
          h('span', { text: t('runs.peak', { count: run.max_concurrency || 0 }) }),
          run.checks_failed ? h('span', { class: 'meta-failed', text: run.checks_failed === 1 ? t('runs.failedCheckOne', { count: 1 }) : t('runs.failedChecks', { count: run.checks_failed }) }) : null,
        ),
      ),
      h('div', { class: 'status-stack' },
        h('span', { class: `pill ${toneClass(meta.tone)}`, text: meta.label }),
        run.verified ? h('span', { class: 'pill verified', text: t('runs.pill.qualityGate') }) : null,
        run.self_reported ? h('span', { class: 'pill claimed', text: t('runs.pill.claimOnly') }) : null,
        run.status === 'open' ? h('span', { class: 'pill open', text: run.live ? t('runs.pill.fresh') : t('runs.pill.noFinish') }) : null,
      ),
    ));

    card.append(h('div', { class: 'detail-kpis' },
      kpi(t('runs.kpi.elapsed'), fmtDuration(run.duration_seconds)),
      kpi(t('runs.kpi.agents'), String(run.agent_count || 0),
        !run.agent_count ? t('runs.kpi.noneRecorded') : run.open_agent_count ? t('runs.kpi.openCount', { count: run.open_agent_count }) : t('runs.kpi.allStopped')),
      kpi(t('runs.kpi.peak'), String(run.max_concurrency || 0), t('runs.kpi.observed')),
      kpi(t('runs.kpi.overlap'), fmtDuration(run.observed_overlap_seconds), t('runs.kpi.notTimeSaved'), 'warn'),
      kpi(t('runs.kpi.mutations'), fmtNumber(run.mutation_count), t('runs.kpi.observed')),
      kpi(t('runs.kpi.checks'), `${run.checks_passed || 0}/${(run.checks_passed || 0) + (run.checks_failed || 0)}`,
        !(run.checks_passed || run.checks_failed) ? t('runs.kpi.noneRecorded')
          : run.checks_failed ? t('runs.kpi.failed', { count: run.checks_failed }) : t('runs.kpi.nativeExit'),
        run.checks_failed ? 'bad' : 'ok'),
    ));

    const body = h('div', { class: 'detail-body' });
    body.append(verificationNotice(run));
    body.append(routeInsight(run));
    body.append(section(
      t('runs.section.timeline'),
      t('runs.section.timelineHint'),
      h('div', { style: { display: 'grid', gap: '8px' } },
        timeline(run, state.detail),
        benchmarkNote(run),
      ),
    ));
    body.append(section(
      t('runs.section.evidence'),
      t('runs.section.evidenceHint'),
      evidenceChain(run, state.detail),
    ));
    if (agents.length) {
      body.append(section(
        t('runs.section.agents'),
        t('runs.section.agentsHint', { count: agents.length }),
        agentTable(agents, run),
      ));
    }
    if (checks.length) {
      body.append(section(
        t('runs.section.checks'),
        t('runs.section.checksHint'),
        checkTable(checks, run),
      ));
    }
    if (run.diagnostics?.length) {
      body.append(h('div', { class: 'notice notice-warn' },
        icon('alert'),
        h('div', {},
          h('strong', { text: t('runs.diagnostic.title') }),
          t('runs.diagnostic.body'),
          h('code', { text: run.diagnostics.join(', ') }),
          t('runs.diagnostic.tail', { diagnosis: run.diagnosis }),
        ),
      ));
    }
    body.append(section(t('runs.section.reproduce'), t('runs.section.reproduceHint'),
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
          h('strong', { text: t('runs.benchmark.valid') }),
          t('runs.benchmark.validBody', { duration: fmtDuration(run.estimated_parallelism_seconds) }),
        ),
      );
    }
    return h('div', { class: 'notice' },
      icon('clock'),
      h('div', {},
        h('strong', { text: t('runs.benchmark.none') }),
        run.status === 'open'
          ? t('runs.benchmark.open')
          : run.reason || t('runs.benchmark.noReason'),
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
      copyButton(command, t('runs.copyCommand')),
    );
  }

  function verificationNotice(run) {
    if (run.status === 'invalid') {
      return h('div', { class: 'notice notice-bad' },
        icon('alert'), h('div', {}, h('strong', { text: t('runs.notice.invalid') }), run.error || run.reason || t('runs.notice.invalidFallback')));
    }
    if (run.status === 'open') {
      return h('div', { class: 'notice notice-warn' },
        icon('alert'),
        h('div', {},
          h('strong', { text: t('runs.notice.noTerminal') }),
          run.open_agent_count
            ? t(run.open_agent_count === 1 ? 'runs.notice.openSpanOne' : 'runs.notice.openSpans', { count: run.open_agent_count })
            : t('runs.notice.openNoSpans'),
        ),
      );
    }
    if (run.status === 'failed' || run.status === 'cancelled') {
      return h('div', { class: 'notice notice-bad' },
        icon('alert'), h('div', {}, h('strong', { text: t('runs.notice.failed', { status: runStatusWord(run.status) }) }), t('runs.notice.failedBody')),
      );
    }
    if (run.verified) {
      return h('div', { class: 'notice notice-ok' },
        icon('shield'),
        h('div', {},
          h('strong', { text: t('runs.notice.verified') }),
          run.verification?.reason || t('runs.notice.verifiedFallback'),
          run.verification?.check_ids?.length
            ? h('span', { class: 'check-chips' }, ...run.verification.check_ids.map((id) => h('span', { class: 'check-chip', 'data-pass': 'true', text: id })))
            : null,
        ),
      );
    }
    if (run.verification?.status === 'rejected') {
      return h('div', { class: 'notice notice-bad' },
        icon('alert'),
        h('div', {}, h('strong', { text: t('runs.notice.rejected') }), run.verification.reason,
          t('runs.notice.rejectedBody')),
      );
    }
    if (run.self_reported) {
      return h('div', { class: 'notice notice-warn' },
        icon('alert'),
        h('div', {}, h('strong', { text: t('runs.notice.selfReported') }), run.verification?.reason || t('runs.notice.selfReportedFallback'),
          t('runs.notice.selfReportedBody')),
      );
    }
    return h('div', { class: 'notice notice-warn' },
      icon('alert'),
      h('div', {}, h('strong', { text: t('runs.notice.notVerified') }), run.reason || t('runs.notice.notVerifiedFallback'),
        run.checks_failed ? ` ${run.checks_failed === 1 ? t('runs.failedCheckOne', { count: 1 }) : t('runs.failedChecks', { count: run.checks_failed })}` : '',
      ),
    );
  }

  function routeInsight(run) {
    const route = run.route;
    if (!route) {
      return h('div', { class: 'insight' },
        h('span', { class: 'insight-badge', text: t('runs.route.badge') }),
        h('div', { class: 'insight-text' }, h('strong', { text: t('runs.route.noneTitle') }), t('runs.route.noneBody')),
        h('div', { class: 'insight-side', text: t('runs.route.advisory') }),
      );
    }
    const followed = run.routing_advice_followed;
    const mode = route.mode === 'single' ? t('runs.route.single') : t('runs.route.parallel', { count: route.max_agents });
    let text;
    let tone = '';
    if (followed === true) {
      text = t('runs.route.followed', { starts: run.native_starts, peak: run.max_concurrency });
    } else if (followed === false) {
      tone = 'warn';
      text = t('runs.route.contradicted', { starts: run.native_starts, peak: run.max_concurrency });
    } else {
      text = t('runs.route.unknown');
    }
    const side = [
      route.selected_count != null ? t('runs.route.skills', { count: route.selected_count }) : null,
      route.latency_ms != null ? t('runs.route.latency', { ms: Math.round(route.latency_ms) }) : null,
      t('runs.route.advisory'),
    ].filter(Boolean).join(' · ');
    return h('div', { class: 'insight', 'data-tone': tone || null },
      h('span', { class: 'insight-badge', text: followed === false ? t('runs.route.exceeded') : t('runs.route.badge') }),
      h('div', { class: 'insight-text' }, h('strong', { text: t('runs.route.advised', { mode }) }), text),
      h('div', { class: 'insight-side', text: side }),
    );
  }

  /* ── Timeline ──────────────────────────────────────────────────────────── */
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
        h('span', {}, h('i', { class: 'legend-swatch' }), t('runs.timeline.completed')),
        h('span', {}, h('i', { class: 'legend-swatch open' }), t('runs.timeline.open')),
        h('span', {}, h('i', { class: 'legend-swatch overlap' }), t('runs.timeline.overlap')),
        h('span', {}, h('i', { class: 'legend-dot pass' }), t('runs.timeline.pass')),
        h('span', {}, h('i', { class: 'legend-dot fail' }), t('runs.timeline.fail')),
        h('span', {}, h('i', { class: 'legend-line' }), t('runs.timeline.terminal')),
      ),
    );

    if (!spans.length && !mutations.length && !checks.length && !detail.finish) {
      wrap.append(h('div', { class: 'chart-empty' },
        t('runs.timeline.empty'),
        h('code', { text: 'perf.py report' }),
        t('runs.timeline.emptyHint'),
      ));
      return wrap;
    }

    const gantt = h('div', { class: 'gantt' });
    const axis = h('div', { class: 'gantt-axis' },
      h('div', { class: 'axis-label', text: t('runs.timeline.axis') }),
      h('div', { class: 'axis-track' }),
    );
    const axisTrack = axis.lastChild;
    const { ticks } = axisTicks(from, to);
    for (const tick of ticks) {
      axisTrack.append(h('span', {
        class: 'axis-tick', text: fmtDuration(Math.max(0, tick - started)),
        style: { left: `${clamp(position(tick))}%` },
      }));
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
        'data-tip': t('runs.timeline.overlapTip', { duration: fmtDuration(bandEnd - bandStart) }),
      }));
    }
    rows.append(grid);

    for (const agent of renderSpans) {
      const left = clamp(position(agent.start));
      const width = Math.max(0.6, clamp(position(agent.end) - position(agent.start)));
      const spanState = agent.open ? t('runs.timeline.spanState.open') : t('runs.timeline.spanState.stopped');
      const bar = h('div', {
        class: 'gantt-bar',
        style: { left: `${left}%`, width: `${width}%` },
        'data-open': String(Boolean(agent.open)),
        'data-tip': t('runs.timeline.spanTip', { id: agent.short_id || shortId(agent.id), duration: fmtDuration(agent.duration), state: spanState }),
        tabindex: '0',
        role: 'img',
        'aria-label': t('runs.timeline.spanAria', { id: agent.short_id || shortId(agent.id), duration: fmtDuration(agent.duration), state: spanState }),
      });
      if (run.status === 'failed') bar.classList.add('failed-run');
      else if (run.status === 'open') bar.classList.add('open-run');
      rows.append(h('div', { class: 'gantt-row' },
        h('div', { class: 'gantt-label' },
          h('span', { class: 'name' }, h('i', { class: 'agent-dot' }), agent.short_id || shortId(agent.id)),
          h('span', { class: 'sub', text: `${fmtDuration(agent.duration)} · ${spanState}` }),
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
          'data-tip': t('runs.timeline.mutationTip', { offset: fmtDuration(offset) }),
        }));
      }
      track.append(dots);
      rows.append(h('div', { class: 'gantt-row activity-row' },
        h('div', { class: 'gantt-label' },
          h('span', { class: 'name', text: t('runs.timeline.mutations') }),
          h('span', { class: 'sub', text: mutations.length > MUTATION_DISPLAY_LIMIT ? t('runs.timeline.mutationShown', { shown: MUTATION_DISPLAY_LIMIT, total: mutations.length }) : t('runs.timeline.mutationCount', { count: mutations.length }) }),
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
          'data-tip': t('runs.timeline.checkTip', { id: check.id, code: check.exit_code, offset: fmtDuration(check.offset) }),
          tabindex: '0',
          role: 'img',
          'aria-label': t('runs.timeline.checkAria', { id: check.id, code: check.exit_code }),
        }));
      }
      rows.append(h('div', { class: 'gantt-row activity-row' },
        h('div', { class: 'gantt-label' },
          h('span', { class: 'name', text: t('runs.timeline.checks') }),
          h('span', { class: 'sub', text: t('runs.timeline.checkPassed', { passed: checks.filter((check) => check.passed).length, total: checks.length }) }),
        ),
        track,
      ));
    }

    if (spans.length > renderSpans.length) {
      rows.append(h('div', { class: 'chart-empty', text: t('runs.timeline.laneLimit', { shown: renderSpans.length, total: spans.length }) }));
    }

    const markers = h('div', { class: 'gantt-markers' });
    if (detail.finish) {
      const finishPosition = clamp(position(detail.finish.at));
      markers.append(h('div', {
        class: 'finish-marker',
        'data-status': detail.finish.status,
        style: { left: `${finishPosition}%` },
      }, h('span', {
        text: detail.finish.status,
        style: finishPosition > 90 ? { left: 'auto', right: '3px' } : null,
      })));
    }
    rows.append(markers);
    gantt.append(axis, rows);
    wrap.append(gantt);
    return wrap;
  }

  /* ── Evidence chain and tables ─────────────────────────────────────────── */
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

    add('ok', t('runs.evidence.runStarted'), `${run.platform} · ${fmtDateTime(started)}`, 't+0s');
    if (run.route) {
      const adherence = run.routing_advice_followed === true ? t('runs.evidence.followed')
        : run.routing_advice_followed === false ? t('runs.evidence.exceeded') : t('runs.evidence.unknown');
      add('info',
        t('runs.evidence.route', { mode: run.route.mode === 'single' ? t('runs.route.single') : t('runs.route.parallel', { count: run.route.max_agents }) }),
        t('runs.evidence.routeMeta', { starts: run.native_starts, peak: run.max_concurrency, adherence }),
        run.route.latency_ms != null ? `${Math.round(run.route.latency_ms)}ms` : '',
      );
    }
    add(run.open_agent_count ? 'warn' : 'ok',
      run.agent_count === 1 ? t('runs.evidence.spanOne', { count: 1 }) : run.agent_count ? t('runs.evidence.spans', { count: run.agent_count }) : t('runs.evidence.noSpan'),
      run.open_agent_count
        ? t('runs.evidence.openSpans', { count: run.open_agent_count })
        : run.agent_count ? t('runs.evidence.allStopped') : t('runs.evidence.noSubagents'),
      run.agent_count ? `Σ ${fmtDuration(sum((detail.agents || []).map((agent) => agent.duration)))}` : '—',
    );
    if (run.mutation_count) {
      add('info', run.mutation_count === 1 ? t('runs.evidence.mutationOne', { count: 1 }) : t('runs.evidence.mutations', { count: run.mutation_count }),
        t('runs.evidence.mutationMeta'),
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
        t('runs.evidence.checksPassed', { passed: run.checks_passed, total: run.checks_passed + run.checks_failed }),
        chips,
        detail.checks?.length ? `last +${fmtDuration(Math.max(...detail.checks.map((check) => check.offset)))}` : '',
      );
    } else {
      add('', t('runs.evidence.noChecks'), t('runs.evidence.noChecksMeta'), '—');
    }
    if (detail.finish) {
      add(detail.finish.status === 'success' ? 'ok' : 'bad',
        t('runs.evidence.terminal', { status: runStatusWord(detail.finish.status) }),
        detail.finish.status === 'success' ? t('runs.evidence.terminalSuccess') : t('runs.evidence.terminalFailed'),
        `+${fmtDuration(detail.finish.offset)}`,
      );
    } else {
      add('warn', t('runs.evidence.noTerminal'), t('runs.evidence.noTerminalMeta'), '—');
    }
    if (run.verified) {
      add('ok', t('runs.evidence.verified'), run.verification?.reason || t('runs.evidence.verifiedMeta'),
        detail.verified_event ? `+${fmtDuration(detail.verified_event.offset)}` : '');
    } else if (run.verification?.status === 'rejected') {
      add('bad', t('runs.evidence.rejected'), run.verification.reason, '');
    } else if (run.self_reported) {
      add('warn', t('runs.evidence.selfReported'), t('runs.evidence.selfReportedMeta'), '');
    } else {
      const reason = run.status === 'open'
        ? t('runs.evidence.notVerifiedOpen')
        : run.status === 'failed' || run.status === 'cancelled'
          ? t('runs.evidence.notVerifiedFailed')
          : run.reason || t('runs.evidence.notVerifiedReason');
      add('', t('runs.evidence.notVerified'), reason, '');
    }
    return list;
  }

  function agentTable(agents, run) {
    const table = h('table', {},
      h('thead', {}, h('tr', {},
        h('th', { text: t('runs.agentTable.agent') }),
        h('th', { text: t('runs.agentTable.start') }),
        h('th', { text: t('runs.agentTable.duration') }),
        h('th', { text: t('runs.agentTable.span') }),
        h('th', { text: t('runs.agentTable.hash') }),
      )),
      h('tbody', {}, ...agents.map((agent) => h('tr', {},
        h('td', {}, h('span', { class: 'mono', text: agent.short_id || shortId(agent.id) })),
        h('td', { class: 'num', text: relativeOffset(run.started_at, agent.start) }),
        h('td', { class: 'num', text: fmtDuration(agent.duration, true) }),
        h('td', {}, h('span', {
          class: 'state-chip', 'data-state': agent.open ? 'open' : 'completed',
          text: agent.open ? t('runs.agentTable.open') : t('runs.agentTable.stopped'),
        })),
        h('td', {}, h('span', { class: 'mono', text: agent.native_hash_short || '—' })),
      ))),
    );
    return h('div', { class: 'table-wrap' }, table);
  }

  function checkTable(checks, run) {
    const table = h('table', {},
      h('thead', {}, h('tr', {},
        h('th', { text: t('runs.checkTable.check') }),
        h('th', { text: t('runs.checkTable.exit') }),
        h('th', { text: t('runs.checkTable.observed') }),
        h('th', { text: t('runs.checkTable.source') }),
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
      h('summary', { text: t(events.length === 1 ? 'runs.raw.summaryOne' : 'runs.raw.summary', { count: events.length }) }),
      h('div', { class: 'event-table' },
        h('table', {},
          h('thead', {}, h('tr', {},
            h('th', { text: t('runs.raw.offset') }),
            h('th', { text: t('runs.raw.event') }),
            h('th', { text: t('runs.raw.detail') }),
          )),
          h('tbody', {}, ...rows),
        ),
      ),
    );
    return details;
  }

  function eventDetail(event) {
    switch (event.event) {
      case 'start': return t('runs.event.start', { platform: event.platform || 'unknown' });
      case 'route': {
        const params = {
          delegation: event.delegation,
          max: event.max_agents,
          count: event.selected_count ?? '—',
          ms: Math.round(event.latency_ms || 0),
        };
        return event.latency_ms != null
          ? t('runs.event.routeLatency', params)
          : t('runs.event.route', params);
      }
      case 'agent-start':
        return event.native_hash_short
          ? t('runs.event.agentStartHash', { id: event.agent_id || '—', hash: event.native_hash_short })
          : t('runs.event.agentStart', { id: event.agent_id || '—' });
      case 'agent-stop': return t('runs.event.agentStop', { id: event.agent_id || '—' });
      case 'mutation': return t('runs.event.mutation');
      case 'check': return t('runs.event.check', { id: event.check_id || '—', code: event.exit_code });
      case 'verified': return `${event.source || t('runs.event.verified')}${event.check_ids?.length ? ` · ${event.check_ids.join(', ')}` : ''}`;
      case 'finish': return event.verified ? t('runs.event.finishVerified', { status: event.status || 'unknown' }) : t('runs.event.finish', { status: event.status || 'unknown' });
      case 'diagnostic': return t('runs.event.diagnostic', { reason: event.reason || 'unknown' });
      default: return t('runs.event.recorded');
    }
  }

  /* ── Task board ────────────────────────────────────────────────────────── */
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
    if (!host || !kpis || !runHost) return;
    clear(host); clear(kpis); clear(runHost);

    if (supervisor && supervisor.available === false) {
      kpis.append(...emptyBoardKpis());
      host.append(h('div', { class: 'empty-state', style: { 'grid-column': '1 / -1' } },
        h('div', { class: 'empty-inner' },
          h('div', { class: 'empty-mark' }, icon('alert')),
          h('h2', { text: t('board.unavailableTitle') }),
          h('p', { text: t('board.unavailableBody') }),
        ),
      ));
      return;
    }
    if (!supervisor?.runs?.length) {
      kpis.append(...emptyBoardKpis());
      host.append(h('div', { class: 'empty-state', style: { 'grid-column': '1 / -1' } },
        h('div', { class: 'empty-inner' },
          h('div', { class: 'empty-mark' }, icon('branch')),
          h('h2', { text: t('board.emptyTitle') }),
          h('p', { text: t('board.emptyBody') }),
          h('code', { text: t('board.emptyCommand') }),
        ),
      ));
      return;
    }

    const runs = supervisor.runs;
    if (!state.boardRun || !runs.some((run) => run.id === state.boardRun)) state.boardRun = runs[0].id;
    const focusedBoardRun = document.activeElement?.closest?.('.run-chip')?.dataset.boardRun || null;
    for (const run of runs) {
      runHost.append(h('button', {
        class: 'run-chip', type: 'button', 'data-board-run': run.id,
        'aria-pressed': String(run.id === state.boardRun),
        onclick: () => { state.boardRun = run.id; renderBoard(); },
      }, run.id));
    }
    if (focusedBoardRun) {
      runHost.querySelector(`[data-board-run="${CSS.escape(focusedBoardRun)}"]`)?.focus();
    }

    const run = runs.find((item) => item.id === state.boardRun) || runs[0];
    let tasks = run.tasks || [];
    if (state.milestoneFilter && state.milestoneFilter.runId === run.id) {
      const allowed = new Set(state.milestoneFilter.taskIds || []);
      tasks = tasks.filter((task) => allowed.has(task.id));
    }
    const groups = { ready: [], running: [], blocked: [], done: [], attention: [] };
    for (const task of tasks) groups[taskGroup(task)].push(task);
    const completed = tasks.filter((task) => task.state === 'completed').length;
    const running = groups.running.length;
    const blocked = groups.blocked.length;
    const reviewAttested = tasks.filter((task) => task.review_attested).length;
    const nativeStops = tasks.filter((task) => task.native_stop_observed).length;
    const dependencyCount = sum(tasks.map((task) => task.dependencies.length));

    const kpiItems = [
      [t('board.kpi.tasks'), fmtNumber(tasks.length), t('board.kpi.tasksSub', { deps: dependencyCount, max: run.max_parallel })],
      [t('board.kpi.done'), `${completed}/${tasks.length}`, t('board.kpi.doneSub', { status: run.complete ? t('board.kpi.doneComplete') : t('board.kpi.doneOpen') })],
      [t('board.kpi.active'), fmtNumber(running), t('board.kpi.activeSub', { state: run.native_supervision ? t('board.kpi.on') : t('board.kpi.off') })],
      [t('board.kpi.blocked'), fmtNumber(blocked), t('board.kpi.blockedSub')],
      [t('board.kpi.evidence'), `${nativeStops}/${tasks.filter((task) => task.state !== 'pending').length}`,
        run.require_review ? t('board.kpi.evidenceSub', { count: reviewAttested }) : t('board.kpi.reviewNotRequired')],
    ];
    for (const [label, value, sub] of kpiItems) {
      kpis.append(h('div', { class: 'board-kpi' },
        h('div', { class: 'k', text: label }),
        h('div', { class: 'v' }, value, h('small', { text: ` ${sub}` })),
      ));
    }

    const columns = [
      ['ready', t('board.col.ready'), t('board.col.readyHint')],
      ['running', t('board.col.running'), t('board.col.runningHint')],
      ['blocked', t('board.col.blocked'), t('board.col.blockedHint')],
      ['done', t('board.col.done'), t('board.col.doneHint')],
      ['attention', t('board.col.attention'), t('board.col.attentionHint')],
    ];
    for (const [key, label, hint] of columns) {
      const body = h('div', { class: 'board-col-body' });
      if (!groups[key].length) body.append(h('div', { class: 'board-empty', text: t('board.noTasks') }));
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
    return [
      t('board.kpi.tasks'), t('board.kpi.done'), t('board.kpi.active'), t('board.kpi.blocked'), t('board.kpi.evidence'),
    ].map((label) => h('div', { class: 'board-kpi' },
      h('div', { class: 'k', text: label }),
      h('div', { class: 'v', text: '—' }),
    ));
  }

  function taskCard(task, run) {
    const foot = [];
    if (task.attempts) foot.push(h('span', { class: 'mini-chip', text: task.attempts === 1 ? t('board.attemptOne', { count: 1 }) : t('board.attempts', { count: task.attempts }) }));
    else foot.push(h('span', { class: 'mini-chip', text: t('board.notClaimed') }));
    if (task.state !== 'pending') {
      foot.push(h('span', {
        class: `mini-chip ${task.native_stop_observed ? 'ok' : 'warn'}`,
        text: task.native_stop_observed ? t('board.nativeStop') : t('board.stopUnobserved'),
      }));
    }
    if (run.require_review) {
      foot.push(h('span', {
        class: `mini-chip ${task.review_attested ? 'ok' : 'warn'}`,
        text: task.review_attested ? t('board.review') : t('board.reviewMissing'),
      }));
    }
    if (task.dependencies.length) {
      foot.push(h('span', {
        class: `mini-chip ${task.dependencies_met ? '' : 'warn'}`,
        text: task.dependencies_met
          ? t('board.depMet', { count: task.dependencies.length })
          : t('board.depWaiting', { count: task.dependencies.length }),
      }));
    }

    const details = h('details', { class: 'task-more' },
      h('summary', { text: t('board.scopes') }),
      h('div', { class: 'more-body' },
        task.write_scope.length
          ? h('div', {}, h('strong', { text: `${t('board.write')} ` }), h('span', { class: 'scope-list' }, ...task.write_scope.map((scope) => h('span', { class: 'scope', text: scope }))))
          : h('div', {}, h('strong', { text: `${t('board.write')} ` }), t('board.readOnly')),
        task.read_scope.length
          ? h('div', { style: { 'margin-top': '4px' } }, h('strong', { text: `${t('board.read')} ` }), h('span', { class: 'scope-list' }, ...task.read_scope.map((scope) => h('span', { class: 'scope', text: scope }))))
          : null,
        task.reason ? h('div', { style: { 'margin-top': '5px', color: 'var(--bad)' }, text: task.reason }) : null,
        task.result?.summary ? h('div', { style: { 'margin-top': '5px' } }, h('strong', { text: `${t('board.result')} ` }), task.result.summary) : null,
        task.result?.changed_files?.length
          ? h('div', { style: { 'margin-top': '4px' } }, h('strong', { text: `${t('board.changed')} ` }), h('span', { class: 'mono', text: task.result.changed_files.join(', ') }))
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
      h('p', { class: 'task-objective', text: task.objective || t('board.noObjective') }),
      task.dependencies.length
        ? h('div', { class: 'task-deps' },
          h('b', { text: `${t('board.after')} ` }),
          task.dependencies.join(', '),
          task.dependencies_met ? '' : ` · ${t('board.depWaiting', { count: task.dependencies.length })}`,
        )
        : null,
      h('div', { class: 'task-foot' }, ...foot),
      details,
    );
  }

  /* ── Workspaces ────────────────────────────────────────────────────────── */
  function renderWorkspaces() {
    const host = $('#workspaces');
    if (!host) return;
    clear(host);
    const worktrees = state.data?.worktrees || [];
    if (!worktrees.length) {
      host.append(h('div', { class: 'empty-state', style: { 'grid-column': '1 / -1' } },
        h('div', { class: 'empty-inner' },
          h('div', { class: 'empty-mark' }, icon('branch')),
          h('h2', { text: t('workspaces.emptyTitle') }),
          h('p', { text: t('workspaces.emptyBody') }),
          h('code', { text: t('workspaces.emptyCommand') }),
        ),
      ));
      return;
    }
    for (const tree of worktrees) {
      const dirty = tree.dirty_files;
      const dirtyLabel = dirty == null ? t('workspaces.unknown') : dirty === 0 ? t('workspaces.clean') : t('workspaces.changed', { count: dirty });
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
          h('span', { class: 'k', text: t('workspaces.branch') }),
          h('span', { class: 'v branch', text: tree.branch || '—' }),
        ),
        h('div', { class: 'workspace-row' },
          h('span', { class: 'k', text: t('workspaces.base') }),
          h('span', { class: 'v', text: tree.base_sha || '—' }),
        ),
        h('div', { class: 'workspace-row' },
          h('span', { class: 'k', text: t('workspaces.path') }),
          h('span', { class: 'v', text: tree.path || '—' }),
        ),
        tree.exists ? null : h('div', { class: 'workspace-missing', text: t('workspaces.missing') }),
      ));
    }
  }

  function emptyRunsState() {
    return emptyState({
      glyph: '◷',
      title: t('runs.emptyTitle'),
      body: t('runs.emptyBody'),
      hint: t('runs.demoCommand'),
    });
  }

  /* ── Tabs ──────────────────────────────────────────────────────────────── */
  function setTab(tab, options = {}) {
    if (!state.projectId) return;
    const next = tabFromHash(tab);
    state.tab = next;
    for (const tabButton of $$('.tab')) {
      const active = tabButton.dataset.tab === next;
      tabButton.setAttribute('aria-selected', String(active));
      tabButton.tabIndex = active ? 0 : -1;
    }
    showView(next);
    if (options.updateHash !== false) writeHash(state.projectId, next, { replace: Boolean(options.replaceHash) });
    if (next === 'runs') {
      renderOverview();
      renderFilters();
      renderRunList();
      renderDetail();
      if (state.selected && (!state.detail || state.detailProjectId !== state.projectId)) syncDetail();
    }
    if (next === 'board') renderBoard();
    if (next === 'workspaces') renderWorkspaces();
    if (next === 'office') renderOffice();
    syncSettingsControls();
  }

  /* ── Inspector ─────────────────────────────────────────────────────────── */
  function openInspectorShell(trigger) {
    const shell = $('#inspector-shell');
    if (!shell) return;
    state.inspector.trigger = trigger instanceof HTMLElement ? trigger : document.activeElement;
    state.inspector.triggerKey = state.inspector.trigger?.dataset?.deskKey || null;
    shell.hidden = false;
    document.body.classList.add('inspector-open');
  }

  function renderInspector(options = {}) {
    const pinned = $('#inspector-pinned');
    const content = $('#inspector-content');
    if (!content) return;
    if (!Office) {
      clear(content);
      content.append(emptyState({
        glyph: '!',
        title: t('inspector.errorTitle'),
        body: t('office.endpointMissing'),
      }));
      return;
    }
    const inspector = state.inspector;
    Office.renderInspector(pinned, content, {
      lang: state.language,
      loading: inspector.loading,
      error: inspector.error,
      mode: inspector.mode,
      payload: inspector.payload,
      agent: inspector.agent,
      session: inspector.session,
      project: state.projectMeta,
      agents: state.office?.agents || [],
      activeAgentId: inspector.agentId,
      callbacks: {
        onClose: () => closeInspector(),
        onRetry: () => loadInspector(),
        onOpenRun: (runId) => openRunFromInspector(runId),
        onSelectAgent: (agentId) => {
          const agent = (state.office?.agents || []).find((item) => item.agent_definition_id === agentId);
          if (agent) openAgentInspector(agent, document.activeElement);
        },
      },
    });
    const dialog = $('#agent-inspector');
    if (state.inspector.open && dialog && !dialog.contains(document.activeElement)) {
      const close = $('#inspector-close');
      (close || dialog).focus();
    }
  }

  async function openAgentInspector(agent, trigger) {
    if (!agent?.agent_definition_id) return;
    state.inspector = {
      open: true,
      mode: 'agent',
      agentId: agent.agent_definition_id,
      agent,
      session: null,
      payload: null,
      loading: true,
      error: null,
      trigger: trigger instanceof HTMLElement ? trigger : state.inspector.trigger,
      token: state.inspector.token + 1,
      requestId: 0,
    };
    openInspectorShell(state.inspector.trigger);
    renderInspector({ focus: true });
    await loadInspector();
  }

  function openManagedInspector(managed, trigger) {
    if (!managed || typeof managed !== 'object') return;
    const details = managed.managed && typeof managed.managed === 'object' ? managed.managed : {};
    openUnlinkedInspector({
      ...managed,
      ...details,
      managed: true,
      open: details.status === 'running' || managed.status === 'working',
      platform: managed.provider || managed.platform || details.provider,
      name: managed.name || details.task_id,
      run_id: details.run_id || managed.run_id,
      task_id: details.task_id || managed.task_id,
    }, trigger);
  }

  function openUnlinkedInspector(session, trigger) {
    state.inspector = {
      open: true,
      mode: 'unlinked',
      agentId: null,
      agent: null,
      session,
      payload: null,
      loading: false,
      error: null,
      trigger: trigger instanceof HTMLElement ? trigger : state.inspector.trigger,
      token: state.inspector.token + 1,
    };
    openInspectorShell(state.inspector.trigger);
    renderInspector({ focus: true });
  }

  async function loadInspector(options = {}) {
    const inspector = state.inspector;
    if (!inspector.open || inspector.mode !== 'agent') return;
    const agentId = inspector.agentId;
    const projectId = state.projectId;
    if (!agentId || !projectId) return;
    const token = inspector.token;
    const requestId = ++inspector.requestId;
    if (!options.silent) {
      inspector.loading = true;
      inspector.error = null;
      renderInspector();
    }
    try {
      const response = await requestJSON(`/api/projects/${encodeURIComponent(projectId)}/agents/${encodeURIComponent(agentId)}`);
      if (state.inspector.token !== token || state.inspector.requestId !== requestId
          || state.projectId !== projectId || !state.inspector.open) return;
      state.inspector.payload = response.data;
      state.inspector.loading = false;
      state.inspector.error = null;
      renderInspector();
    } catch (error) {
      if (state.inspector.token !== token || state.inspector.requestId !== requestId
          || state.projectId !== projectId || !state.inspector.open) return;
      state.inspector.loading = false;
      state.inspector.error = error;
      renderInspector();
    }
  }

  function openRunFromInspector(runId) {
    if (!runId) return;
    closeInspector({ restore: false });
    setTab('runs');
    state.selected = runId;
    state.detail = null;
    state.detailProjectId = null;
    selectRun(runId);
  }

  /* ── Refresh and timers ────────────────────────────────────────────────── */
  async function refresh(force = false) {
    if (state.refreshing) return;
    state.refreshing = true;
    const refreshButton = $('#refresh');
    refreshButton?.classList.add('busy');
    try {
      if (state.mode === 'overview') {
        await loadRegistry();
        await loadGlobal({ force: true });
        renderGlobal();
        probeDemoMode();
      } else if (state.projectId) {
        clearTimeout(state.officeRefreshTimer);
        await Promise.all([
          loadState({ force: Boolean(force) }),
          loadOffice({ force: true }),
        ]);
        await syncDetail({ force: Boolean(force) });
        renderOffice();
        renderUpdated();
      }
    } finally {
      state.refreshing = false;
      refreshButton?.classList.remove('busy');
    }
  }

  function tick() {
    if (document.hidden) return;
    const now = Date.now() / 1000;
    if (state.mode === 'overview') {
      if (!state.globalLoading && now - state.lastGlobalFetch > 6) loadGlobal();
      return;
    }
    if (state.mode === 'project' && state.projectId) {
      if (state.live.mode !== 'sse' && !state.stateLoading) loadState();
      if ((state.live.mode !== 'sse' || state.tab === 'office') && now - state.lastOfficeFetch > 7) loadOffice();
      if ((state.tab === 'board' || state.tab === 'office') && now - state.lastMilestoneFetch > 7) loadMilestones();
    }
  }

  function startTimers() {
    setInterval(tick, 2500);
    setInterval(() => { if (!document.hidden) renderUpdated(); }, 1000);
  }

  /* ── Event wiring and boot ─────────────────────────────────────────────── */
  function applyRoute(route) {
    if (!state.ready) return;
    if (route.view === 'overview') {
      openOverview({ skipHash: true });
      return;
    }
    if (route.view === 'project') {
      if (!findProject(route.projectId)) {
        toast(t('common.projectNotFound'));
        openOverview({ replaceHash: true });
        return;
      }
      openProject(route.projectId, route.tab, { skipHash: true });
      return;
    }
    if (route.view === 'legacy-tab') {
      const stored = readStore(STORE_PROJECT);
      if (stored && findProject(stored)) openProject(stored, route.tab, { skipHash: true });
      else openOverview({ replaceHash: true });
      return;
    }
    openOverview({ skipHash: true });
  }

  function trapFocusWithin(event, rootSelector, fallbackSelector) {
    const root = document.querySelector(rootSelector);
    if (!root) return;
    const focusable = $$(rootSelector + ' button, ' + rootSelector + ' a[href], ' + rootSelector + ' input, ' + rootSelector + ' select, ' + rootSelector + ' textarea, ' + rootSelector + ' [tabindex]:not([tabindex="-1"])')
      .filter((element) => !element.disabled && element.offsetParent !== null);
    if (!focusable.length) {
      event.preventDefault();
      document.querySelector(fallbackSelector)?.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !active?.closest?.(rootSelector))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !active?.closest?.(rootSelector))) {
      event.preventDefault();
      first.focus();
    }
  }

  function trapInspectorFocus(event) {
    trapFocusWithin(event, '#agent-inspector', '#agent-inspector');
  }

  function bindEvents() {
    for (const tabButton of $$('.tab')) {
      tabButton.addEventListener('click', () => setTab(tabButton.dataset.tab));
      tabButton.addEventListener('keydown', (event) => {
        if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
        event.preventDefault();
        const tabs = $$('.tab');
        const index = tabs.indexOf(tabButton);
        const next = event.key === 'ArrowRight'
          ? tabs[(index + 1) % tabs.length]
          : tabs[(index - 1 + tabs.length) % tabs.length];
        next.focus();
        setTab(next.dataset.tab);
      });
    }

    $('#refresh')?.addEventListener('click', () => refresh(true));
    $('#project-select')?.addEventListener('change', (event) => {
      const projectId = event.target.value;
      if (!projectId) {
        openOverview();
        return;
      }
      if (findProject(projectId)) openProject(projectId, 'office');
      else toast(t('common.projectNotFound'));
    });
    $('#theme')?.addEventListener('click', () => {
      applyAppearance(currentAppearance() === 'light' ? 'dark' : 'light');
    });
    $('#lang-toggle')?.addEventListener('click', () => {
      changeLanguage(state.language === 'en' ? 'tr' : 'en');
    });
    $('#settings-toggle')?.addEventListener('click', () => openSettings(!state.settingsOpen));
    $('#settings-close')?.addEventListener('click', () => openSettings(false));
    $('#settings')?.addEventListener('click', (event) => {
      if (!event.target.closest?.('.settings-card')) openSettings(false);
    });
    $('#set-language')?.addEventListener('change', (event) => changeLanguage(event.target.value));
    $('#set-appearance')?.addEventListener('change', (event) => applyAppearance(event.target.value));
    $('#set-reduced-motion')?.addEventListener('change', (event) => applyMotion(event.target.checked));
    $('#set-office-theme')?.addEventListener('change', (event) => setOfficeTheme(event.target.value));
    $('#office-theme')?.addEventListener('change', (event) => setOfficeTheme(event.target.value));
    $('#office-arrange')?.addEventListener('click', () => {
      state.arranging = !state.arranging;
      renderOffice();
      syncSettingsControls();
    });
    $('#office-motion')?.addEventListener('click', () => applyMotion(!state.reducedMotion));

    $('#run-search')?.addEventListener('input', (event) => {
      const value = event.target.value.trim();
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(() => {
        state.query = value;
        state.runListSignature = '';
        renderRunList();
      }, 120);
    });

    $('#inspector-shell')?.addEventListener('click', (event) => {
      if (event.target.closest?.('[data-inspector-close]')) closeInspector();
    });

    document.addEventListener('keydown', (event) => {
      if (state.inspector.open) {
        if (event.key === 'Escape') {
          event.preventDefault();
          closeInspector();
          return;
        }
        if (event.key === 'Tab') {
          trapInspectorFocus(event);
          return;
        }
      }
      if (state.settingsOpen && event.key === 'Tab') {
        trapFocusWithin(event, '#settings', '#settings-close');
        return;
      }
      if (event.key === 'Escape' && state.settingsOpen) {
        event.preventDefault();
        openSettings(false);
        return;
      }
      if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName || '')) {
        if (state.projectId) {
          event.preventDefault();
          setTab('runs');
          $('#run-search')?.focus();
        }
      }
      if (event.key === 'Escape' && document.activeElement === $('#run-search')) {
        clearTimeout(state.searchTimer);
        $('#run-search').value = '';
        state.query = '';
        state.runListSignature = '';
        renderRunList();
        $('#run-search').blur();
      }
    });

    document.addEventListener('ao:languagechange', onLanguageChanged);
    window.addEventListener('hashchange', () => applyRoute(parseHash()));
    window.addEventListener('popstate', () => applyRoute(parseHash()));
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) return;
      if (state.mode === 'overview') {
        loadGlobal({ force: true });
      } else if (state.projectId) {
        if (state.live.mode !== 'sse') loadState();
        loadOffice({ force: true });
      }
    });
  }

  function initLanguageAndSettings() {
    if (I18n) {
      state.language = I18n.initLanguage();
      I18n.apply(document);
    } else {
      const stored = readStore(STORE_LANGUAGE);
      state.language = stored === 'tr' ? 'tr' : 'en';
    }
    document.documentElement.lang = state.language;
    document.title = t('app.title');
    initAppearance();
    initMotion();
    syncLanguageControls();
    syncSettingsControls();
  }

  async function boot() {
    initLanguageAndSettings();
    setupTooltip();
    bindEvents();
    showView('overview');
    await loadRegistry();
    const route = parseHash();
    if (route.view === 'project') {
      if (findProject(route.projectId)) await openProject(route.projectId, route.tab, { skipHash: true });
      else {
        toast(t('common.projectNotFound'));
        await openOverview();
      }
    } else if (route.view === 'legacy-tab') {
      const stored = readStore(STORE_PROJECT);
      if (stored && findProject(stored)) await openProject(stored, route.tab);
      else await openOverview();
    } else if (route.view === 'overview') {
      await openOverview({ skipHash: true });
    } else {
      const stored = readStore(STORE_PROJECT);
      if (stored && findProject(stored)) await openProject(stored, 'office');
      else await openOverview();
    }
    startTimers();
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
