/* Auto-Orchestrator — virtual agent office and inspector renderer.
 * CSS-only pixel art. No external images, fonts, frameworks or fake streams.
 * The renderer is deliberately dumb: every identity and status comes from the
 * office/inspector payload. Missing evidence is rendered as unknown.
 */
(() => {
  'use strict';

  const THEMES = ['ai', 'security', 'web', 'custom'];
  const MAX_CHANGED_FILES = 60;
  const MAX_EVENTS = 30;
  const MAX_PINNED = 40;

  function h(tag, props, ...children) {
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

  function ts(value) {
    return typeof globalThis !== 'undefined' ? globalThis.AOI18n : null;
  }

  function translator(lang) {
    const i18n = ts();
    return (key, params) => (i18n ? i18n.translate(lang || i18n.getLanguage(), key, params) : key);
  }

  function normalizeTheme(value) {
    return THEMES.includes(value) ? value : 'ai';
  }

  function safeColor(value, fallback = '#7dd3fc') {
    return typeof value === 'string' && /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(value.trim())
      ? value.trim()
      : fallback;
  }

  function shortId(value, length = 10) {
    if (!value) return '—';
    const text = String(value);
    return text.length > length ? `${text.slice(0, length)}…` : text;
  }

  function fmtDuration(seconds) {
    if (seconds == null || !Number.isFinite(Number(seconds))) return '—';
    const value = Math.max(0, Number(seconds));
    if (value === 0) return '0s';
    if (value < 1) return `${value.toFixed(1)}s`;
    if (value < 60) return `${Math.round(value)}s`;
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

  function locale(lang) {
    return lang === 'tr' ? 'tr-TR' : 'en-US';
  }

  function fmtDateTime(value, lang) {
    if (!value) return '—';
    const date = new Date(Number(value) * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString(locale(lang), {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  }

  function fmtRelative(value, lang) {
    if (!value) return '—';
    const diff = Math.max(0, Date.now() / 1000 - Number(value));
    try {
      const format = new Intl.RelativeTimeFormat(locale(lang), { numeric: 'auto' });
      if (diff < 60) return format.format(-Math.round(diff), 'second');
      if (diff < 3600) return format.format(-Math.round(diff / 60), 'minute');
      if (diff < 86400) return format.format(-Math.round(diff / 3600), 'hour');
      return format.format(-Math.round(diff / 86400), 'day');
    } catch {
      return fmtDateTime(value, lang);
    }
  }

  /* ── Pure helpers (unit-tested in tests/js/test_dashboard_ui.mjs) ──────── */

  function shouldApplyEvent({ type = 'update', sequenceId, lastSequenceId } = {}) {
    if (type === 'snapshot') return true;
    const sequence = sequenceValue(sequenceId);
    if (sequence == null) return true;
    const last = sequenceValue(lastSequenceId);
    if (last == null) return true;
    return sequence > last;
  }

  function sequenceValue(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function nextSequence(lastSequenceId, sequenceId, type = 'update') {
    const last = sequenceValue(lastSequenceId);
    const sequence = sequenceValue(sequenceId);
    if (sequence == null) return last == null ? 0 : last;
    if (type === 'snapshot') return sequence;
    return Math.max(last == null ? 0 : last, sequence);
  }

  function sortKeys(keys, order) {
    const unique = [...new Set(keys)];
    if (!Array.isArray(order) || !order.length) return unique;
    const rank = new Map();
    for (const key of order) {
      if (typeof key === 'string' && !rank.has(key)) rank.set(key, rank.size);
    }
    return unique
      .map((key, index) => ({ key, index, rank: rank.has(key) ? rank.get(key) : Number.MAX_SAFE_INTEGER }))
      .sort((a, b) => (a.rank - b.rank) || (a.index - b.index))
      .map((item) => item.key);
  }

  function moveDesk(order, keys, key, delta) {
    const effective = sortKeys(keys, order);
    const from = effective.indexOf(key);
    if (from < 0) return Array.isArray(order) ? order.slice() : [];
    const to = from + Number(delta || 0);
    if (to < 0 || to >= effective.length) return effective;
    [effective[from], effective[to]] = [effective[to], effective[from]];
    return effective;
  }

  function agentKey(agent, index = 0) {
    if (agent && agent.agent_definition_id) return `a:${agent.agent_definition_id}`;
    return `a:missing-${index}`;
  }

  function sessionKey(session, index = 0) {
    return `s:${session?.run_id || 'run'}:${session?.agent_id || index}`;
  }

  function officeEntries(payload, order) {
    const agents = Array.isArray(payload?.agents) ? payload.agents : [];
    const sessions = Array.isArray(payload?.unlinked_sessions) ? payload.unlinked_sessions : [];
    const entries = [
      ...agents.map((agent, index) => ({ kind: 'agent', key: agentKey(agent, index), agent })),
      ...sessions.map((session, index) => ({ kind: 'session', key: sessionKey(session, index), session })),
    ];
    const byKey = new Map(entries.map((entry) => [entry.key, entry]));
    return sortKeys(entries.map((entry) => entry.key), order)
      .map((key) => byKey.get(key))
      .filter(Boolean);
  }

  /* ── Office rendering ──────────────────────────────────────────────────── */

  function confidenceLabel(value, lang) {
    const t = translator(lang);
    const raw = String(value || '').toLowerCase();
    if (raw === 'observed') return t('status.observed');
    if (raw === 'inferred') return t('status.inferred');
    if (raw === 'unknown' || !raw) return t('status.unknownEvidence');
    return String(value);
  }

  const CORE_STATUS_BUCKETS = ['completed', 'working', 'dispatching', 'unknown'];
  const STATUS_BUCKET_ORDER = [
    ...CORE_STATUS_BUCKETS,
    'failed', 'blocked', 'awaiting-approval', 'idle', 'running-tests', 'reviewing',
  ];

  function statusKey(value) {
    const i18n = ts();
    if (i18n) return i18n.normalizeStatus(value);
    const raw = String(value == null ? '' : value).trim().toLowerCase().replace(/[\s_]+/g, '-');
    if (!raw) return 'unknown';
    return {
      ok: 'completed', done: 'completed', success: 'completed',
      dispatch: 'dispatching', dispatched: 'dispatching',
      unlinked: 'unknown', disconnected: 'unknown', unbound: 'unknown',
      none: 'unknown', 'not-observed': 'unknown',
      cancelled: 'failed', canceled: 'failed', error: 'failed',
    }[raw] || raw;
  }

  function statusBadge(status, lang, extraClass = '') {
    const i18n = ts();
    const meta = i18n ? i18n.statusMeta(status, lang) : { key: 'unknown', label: 'Unknown', tone: 'neutral', symbol: '?' };
    return h('span', {
      class: `status-badge ${extraClass}`.trim(),
      'data-status': meta.key,
      'data-tone': meta.tone,
    },
      h('span', { class: 'status-symbol', 'aria-hidden': 'true', text: meta.symbol }),
      h('span', { class: 'status-label', text: meta.label }),
    );
  }

  function scene(kind, theme) {
    const isGhost = kind === 'session';
    const furniture = h('span', { class: 'pixel-workstation', 'aria-hidden': 'true' },
      h('span', { class: 'pixel-chair' }),
      h('span', { class: `pixel-agent${isGhost ? ' is-ghost' : ''}` },
        h('span', { class: 'pixel-agent-body' }),
        h('span', { class: 'pixel-agent-arm left' }),
        h('span', { class: 'pixel-agent-arm right' }),
        h('span', { class: 'pixel-agent-head' }),
        h('span', { class: 'pixel-agent-hair' }),
      ),
      h('span', { class: 'pixel-desk' },
        h('span', { class: 'pixel-desk-top' }),
        h('span', { class: 'pixel-desk-leg left' }),
        h('span', { class: 'pixel-desk-leg right' }),
      ),
      h('span', { class: 'pixel-monitor' },
        h('span', { class: 'pixel-monitor-bezel' }),
        h('span', { class: 'pixel-screen' },
          h('span', { class: 'pixel-scan' }),
          h('span', { class: 'pixel-line line-a' }),
          h('span', { class: 'pixel-line line-b' }),
          h('span', { class: 'pixel-line line-c' }),
        ),
        h('span', { class: 'pixel-monitor-neck' }),
        h('span', { class: 'pixel-monitor-base' }),
      ),
      h('span', { class: 'pixel-keyboard' }),
      h('span', { class: 'pixel-mug' }),
      h('span', { class: 'pixel-papers' }),
      h('span', { class: 'pixel-lamp' },
        h('span', { class: 'pixel-lamp-head' }),
        h('span', { class: 'pixel-lamp-glow' }),
        h('span', { class: 'pixel-lamp-stem' }),
      ),
    );
    return h('span', { class: 'desk-scene', 'data-room-theme': theme, 'aria-hidden': 'true' },
      h('span', { class: 'scene-wall' }),
      h('span', { class: 'scene-ceiling' }),
      h('span', { class: 'scene-window' },
        h('span', { class: 'scene-stars' }),
        h('span', { class: 'scene-window-frame' }),
      ),
      h('span', { class: 'scene-poster' },
        h('span', { class: 'scene-poster-line' }),
        h('span', { class: 'scene-poster-line short' }),
      ),
      h('span', { class: 'scene-whiteboard' },
        h('i', { class: 'board-line a' }),
        h('i', { class: 'board-line b' }),
        h('i', { class: 'board-line c' }),
      ),
      h('span', { class: 'scene-radar' },
        h('i', { class: 'radar-ring a' }),
        h('i', { class: 'radar-ring b' }),
        h('i', { class: 'radar-sweep' }),
      ),
      h('span', { class: 'scene-clock' }, h('i', { class: 'clock-hand' })),
      h('span', { class: 'scene-shelf' },
        h('i'), h('i'), h('i'), h('i'), h('i'),
      ),
      h('span', { class: 'scene-rack' },
        h('span', { class: 'scene-rack-light' }),
        h('span', { class: 'scene-rack-light dim' }),
      ),
      h('span', { class: 'scene-floor' }),
      h('span', { class: 'scene-rug' }),
      h('span', { class: 'scene-plant' },
        h('span', { class: 'plant-pot' }),
        h('span', { class: 'plant-leaf a' }),
        h('span', { class: 'plant-leaf b' }),
        h('span', { class: 'plant-leaf c' }),
      ),
      furniture,
      h('span', { class: 'scene-room-tag', lang: 'en', text: theme }),
    );
  }

  function deskInfo(entry, options) {
    const { lang } = options;
    const t = translator(lang);
    const i18n = ts();
    if (entry.kind === 'session') {
      const session = entry.session || {};
      const meta = i18n ? i18n.statusMeta(session.status, lang) : { label: 'Unknown', key: 'unknown' };
      return h('span', { class: 'desk-info' },
        h('span', { class: 'desk-name-row' },
          h('span', { class: 'desk-name', text: t('office.ghostName') }),
          h('span', { class: 'ghost-chip', text: t('office.unlinked') }),
        ),
        h('span', { class: 'desk-role', text: `${String(session.platform || 'unknown').toUpperCase()} · ${t('office.ghostRun', { id: shortId(session.run_id, 12) })}` }),
        h('span', { class: 'desk-meta' },
          statusBadge(session.status, lang),
          h('span', { class: 'desk-evidence', text: session.open ? t('inspector.sessionOpen') : t('inspector.sessionClosed') }),
          session.agent_id ? h('span', { class: 'desk-session', text: `#${shortId(session.agent_id, 8)}` }) : null,
        ),
        session.open
          ? h('span', { class: 'desk-warning', text: t('status.openSpanWarning', { status: meta.label }) })
          : null,
        h('span', { class: 'desk-warning', text: t('office.ghostNote') }),
      );
    }
    const agent = entry.agent || {};
    const evidence = agent.evidence || {};
    const name = agent.name || shortId(agent.agent_definition_id, 14);
    const role = agent.role || t('office.roleFallback');
    const source = evidence.source && evidence.source !== 'none' ? evidence.source : null;
    const confidence = evidence.confidence || 'unknown';
    const openWarning = Boolean(evidence.open_span);
    const label = ts() ? ts().statusMeta(agent.status, options.lang).label : String(agent.status || '');
    return h('span', { class: 'desk-info' },
      h('span', { class: 'desk-name-row' },
        h('span', { class: 'desk-name', text: name }),
        h('span', { class: 'desk-platform', lang: 'en', text: String(agent.platform || 'unknown').toUpperCase() }),
      ),
      h('span', { class: 'desk-role', text: role }),
      h('span', { class: 'desk-meta' },
        statusBadge(agent.status, lang),
        h('span', { class: 'desk-evidence', text: source ? `${source} · ${confidenceLabel(confidence, options.lang)}` : t('office.evidenceNone') }),
      ),
      openWarning
        ? h('span', { class: 'desk-warning', text: t('status.openSpanWarning', { status: label }) })
        : null,
    );
  }

  function arrangeControls(entry, options, effectiveKeys) {
    const t = translator(options.lang);
    const index = effectiveKeys.indexOf(entry.key);
    const buttons = [
      { delta: -1, glyph: '←', key: 'office.moveEarlier' },
      { delta: 1, glyph: '→', key: 'office.moveLater' },
    ];
    return h('span', { class: 'desk-arrange', role: 'group', 'aria-label': t('office.arrange') },
      ...buttons.map((item) => {
        const target = index + item.delta;
        return h('button', {
          class: 'arrange-btn', type: 'button', title: t(item.key, { name: entryLabel(entry, options) }),
          'aria-label': t(item.key, { name: entryLabel(entry, options) }),
          disabled: index < 0 || target < 0 || target >= effectiveKeys.length,
          onclick: (event) => {
            event.stopPropagation();
            options.callbacks?.onMove?.(entry.key, item.delta);
          },
        }, item.glyph);
      }),
    );
  }

  function entryLabel(entry, options) {
    const t = translator(options.lang);
    if (entry.kind === 'session') {
      return `${t('office.ghostName')} · ${String(entry.session?.platform || 'unknown').toUpperCase()}`;
    }
    return entry.agent?.name || shortId(entry.agent?.agent_definition_id, 14);
  }

  function deskAriaLabel(entry, options) {
    const t = translator(options.lang);
    const isGhost = entry.kind === 'session';
    const platform = String((isGhost ? entry.session?.platform : entry.agent?.platform) || 'unknown').toUpperCase();
    const status = isGhost ? entry.session?.status : entry.agent?.status;
    const meta = ts() ? ts().statusMeta(status, options.lang) : { label: String(status || 'unknown') };
    const base = isGhost
      ? t('office.openSessionStatus', { platform, status: meta.label })
      : t('office.inspectStatus', { name: entryLabel(entry, options), status: meta.label });
    const open = isGhost ? Boolean(entry.session?.open) : Boolean(entry.agent?.evidence?.open_span);
    return open ? `${base}. ${t('status.openSpanWarning', { status: meta.label })}` : base;
  }

  function deskCard(entry, options, effectiveKeys, index) {
    const isGhost = entry.kind === 'session';
    const status = isGhost ? entry.session?.status : entry.agent?.status;
    const statusMeta = ts() ? ts().statusMeta(status, options.lang) : { key: 'unknown' };
    const color = !isGhost ? safeColor(entry.agent?.color) : '#8a94a6';
    const aria = deskAriaLabel(entry, options);
    const open = isGhost
      ? options.callbacks?.onInspectSession
      : () => options.callbacks?.onInspect?.(entry.agent);
    const card = h('article', {
      class: 'desk-card',
      'data-status': statusMeta.key,
      'data-kind': entry.kind,
      'data-ghost': String(isGhost),
      'data-index': String(index + 1),
      style: { '--agent-accent': color },
    },
      h('button', {
        class: 'desk-open', type: 'button', 'aria-label': aria, 'data-desk-key': entry.key,
        onclick: (event) => {
          options.callbacks?.onTrigger?.(event.currentTarget);
          open?.();
        },
      },
        scene(entry.kind, options.theme),
        deskInfo(entry, options),
        h('span', { class: 'desk-index', 'aria-hidden': 'true', text: String(index + 1).padStart(2, '0') }),
      ),
    );
    if (options.arranging) card.append(arrangeControls(entry, options, effectiveKeys));
    return card;
  }

  function summaryCounts(payload, entries) {
    const list = Array.isArray(entries) ? entries : officeEntries(payload, []);
    const counts = { desks: list.length, agents: 0, unlinked: 0 };
    for (const key of STATUS_BUCKET_ORDER) counts[key] = 0;
    for (const entry of list) {
      const status = statusKey(entry.kind === 'session' ? entry.session?.status
        : entry.kind === 'managed' ? entry.managed?.status : entry.agent?.status);
      if (entry.kind === 'session') counts.unlinked += 1;
      else counts.agents += 1;
      if (!Object.prototype.hasOwnProperty.call(counts, status)) counts[status] = 0;
      counts[status] += 1;
    }
    return counts;
  }

  function summaryBucketLabel(key, count, lang) {
    const t = translator(lang);
    if (['completed', 'working', 'dispatching', 'unknown', 'failed'].includes(key)) {
      return t(`office.summary.${key}`, { count });
    }
    const meta = ts() ? ts().statusMeta(key, lang) : { label: key };
    return t('office.summaryBucket', { count, label: meta.label });
  }

  function renderSummary(host, payload, entries, lang) {
    if (!host) return;
    const t = translator(lang);
    clear(host);
    const summary = summaryCounts(payload, entries);
    const chips = [
      h('span', { class: 'office-chip total', text: t('office.agentCount', { count: summary.desks }) }),
    ];
    for (const key of STATUS_BUCKET_ORDER) {
      const count = summary[key] || 0;
      if (!CORE_STATUS_BUCKETS.includes(key) && count === 0) continue;
      chips.push(h('span', {
        class: 'office-chip', 'data-bucket': key,
        text: summaryBucketLabel(key, count, lang),
      }));
    }
    const known = new Set([...STATUS_BUCKET_ORDER, 'desks', 'agents', 'unlinked']);
    for (const key of Object.keys(summary)) {
      if (known.has(key) || !summary[key]) continue;
      chips.push(h('span', {
        class: 'office-chip', 'data-bucket': key,
        text: summaryBucketLabel(key, summary[key], lang),
      }));
    }
    chips.push(h('span', {
      class: 'office-chip muted', 'data-bucket': 'unlinked',
      text: t('office.summary.unlinked', { count: summary.unlinked }),
    }));
    chips.push(h('span', { class: 'office-chip muted', text: t('office.legend') }));
    host.append(...chips);
  }

  function renderOfficeLoading(root, summaryHost, lang) {
    const t = translator(lang);
    if (summaryHost) {
      clear(summaryHost);
      summaryHost.append(h('span', { class: 'office-chip muted', text: t('common.loading') }));
    }
    clear(root);
    const grid = h('div', { class: 'office-grid', 'aria-busy': 'true', 'aria-label': t('common.loading') });
    for (let index = 0; index < 6; index += 1) {
      grid.append(h('div', { class: 'desk-card is-loading' },
        h('div', { class: 'desk-skeleton' }),
        h('div', { class: 'desk-skeleton-line' }),
        h('div', { class: 'desk-skeleton-line short' }),
      ));
    }
    root.append(grid);
  }

  function renderOfficeError(root, summaryHost, options, error) {
    const t = translator(options.lang);
    if (summaryHost) clear(summaryHost);
    clear(root);
    const status = error?.status ? `HTTP ${error.status}` : null;
    root.append(h('div', { class: 'office-error' },
      h('div', { class: 'empty-state' },
        h('div', { class: 'empty-inner' },
          h('div', { class: 'empty-mark' }, h('span', { class: 'empty-glyph', 'aria-hidden': 'true', text: '!' })),
          h('h2', { text: t('office.errorTitle') }),
          h('p', { text: t('office.errorBody') }),
          error?.message ? h('code', { text: t('office.errorDetail', { status }) + ` ${error.message}` }) : null,
          h('div', { class: 'empty-actions' },
            h('button', {
              class: 'btn btn-primary', type: 'button',
              onclick: () => options.callbacks?.onRetry?.(),
            }, t('office.retry')),
            h('button', {
              class: 'btn', type: 'button',
              onclick: () => options.callbacks?.onOpenRuns?.(),
            }, t('office.openRuns')),
          ),
        ),
      ),
    ));
  }

  function renderOfficeEmpty(root, summaryHost, options) {
    const t = translator(options.lang);
    if (summaryHost) {
      clear(summaryHost);
      summaryHost.append(h('span', { class: 'office-chip muted', text: t('office.emptyTitle') }));
    }
    clear(root);
    root.append(h('div', { class: 'empty-state office-empty' },
      h('div', { class: 'empty-inner' },
        h('div', { class: 'empty-mark' }, h('span', { class: 'empty-glyph', 'aria-hidden': 'true', text: '▦' })),
        h('h2', { text: t('office.emptyTitle') }),
        h('p', { text: t('office.emptyBody') }),
        h('code', { text: t('office.emptyHint') }),
      ),
    ));
  }

  function renderOfficeCards(root, summaryHost, options) {
    if (!root) return;
    const lang = options.lang;
    if (options.error) {
      renderOfficeError(root, summaryHost, options, options.error);
      return;
    }
    if (!options.payload) {
      renderOfficeLoading(root, summaryHost, lang);
      return;
    }
    const payload = options.payload;
    const entries = officeEntries(payload, options.order);
    renderSummary(summaryHost, payload, entries, lang);
    if (!entries.length) {
      renderOfficeEmpty(root, summaryHost, options);
      return;
    }
    const theme = normalizeTheme(options.theme || payload.theme);
    const effectiveKeys = entries.map((entry) => entry.key);
    const grid = h('div', {
      class: 'office-grid',
      role: 'list',
      'aria-label': translator(lang)('office.title'),
      'data-motion': options.reducedMotion ? 'reduced' : 'full',
    });
    entries.forEach((entry, index) => {
      const card = deskCard(entry, { ...options, theme }, effectiveKeys, index);
      card.setAttribute('role', 'listitem');
      grid.append(card);
    });
    clear(root);
    root.append(h('div', {
      class: 'office-room', 'data-room-theme': theme,
      role: 'group', 'aria-label': `${translator(lang)('office.theme')}: ${translator(lang)(`office.theme.${theme}`)}`,
    },
      grid,
    ));
    const t = translator(lang);
    if (options.arranging) {
      root.append(h('p', { class: 'office-arrange-note', role: 'status', text: t('office.arrangeHint') }));
    }
  }

  /* ── Canvas office map (V7.2) ───────────────────────────────────────────── */

  const officeMapState = {
    scene: null,
    editor: null,
    projectId: null,
    theme: null,
    lang: null,
    layout: null,
    payload: null,
    entries: [],
    activeRoom: null,
    refs: null,
  };

  function roomLabel(roomId, lang) {
    const i18n = ts();
    if (i18n) {
      const key = `office.room.${roomId}`;
      const value = i18n.translate(lang || i18n.getLanguage(), key);
      if (value && value !== key) return value;
    }
    const fallback = {
      frontend: 'Frontend Room', backend: 'Backend Room', qa: 'QA / Test Lab',
      review: 'Code Review Room', research: 'Research Lab', meeting: 'Meeting Room',
      lobby: 'Spawn / Lobby',
    };
    return fallback[roomId] || roomId;
  }

  function localizeLayout(layout, lang) {
    if (!layout || !Array.isArray(layout.rooms)) return layout;
    return {
      ...layout,
      rooms: layout.rooms.map((room) => ({ ...room, name: roomLabel(room.department || room.id, lang) })),
    };
  }

  function normalizedOfficeEntries(payload) {
    const agents = (payload?.agents || []).map((agent, index) => {
      const room = agent.room || (window.AORoomManager
        ? window.AORoomManager.assign(agent).room
        : 'lobby');
      return {
        kind: 'agent',
        key: agent.session_key || agentKey(agent, index),
        agent,
        room,
        status: agent.status,
        spawnable: agent.spawnable !== false && Boolean(
          agent.binding || agent.session_key || agent.current?.run_id || agent.current?.native_hash,
        ),
      };
    });
    const sessions = (payload?.unlinked_sessions || []).map((session, index) => ({
      kind: 'session',
      key: session.session_key || sessionKey(session, index),
      session,
      room: session.room || 'lobby',
      status: session.status,
      spawnable: true,
    }));
    const managed = (payload?.managed_sessions || []).map((item, index) => ({
      kind: 'managed',
      key: item.session_key || item.managed?.session_hash || `m:${index}`,
      managed: item,
      room: item.room || 'lobby',
      status: item.status,
      spawnable: item.spawnable !== false,
    }));
    return [...agents, ...sessions, ...managed];
  }

  function buildOfficeShell(root, lang) {
    const t = translator(lang);
    const canvas = h('canvas', {
      class: 'office-canvas', tabindex: '0',
      'aria-label': t('office.mapAria'),
      role: 'img',
    });
    const tooltip = h('div', { class: 'office-map-tooltip', hidden: true, 'aria-hidden': 'true' });
    const editbar = h('div', { class: 'office-editbar', hidden: true },
      h('span', { text: t('office.editorActive') }),
      h('button', { class: 'btn btn-primary', type: 'button', 'data-office-save': 'true', text: t('office.editorSave') }),
      h('button', { class: 'btn', type: 'button', 'data-office-reset': 'true', text: t('office.editorReset') }),
    );
    const stage = h('section', { class: 'office-stage', 'aria-label': t('office.mapAria') },
      h('div', { class: 'office-map-toolbar' },
        h('div', { class: 'office-roomchips', role: 'group', 'aria-label': t('office.rooms') }),
        h('div', { class: 'office-map-actions' },
          h('button', { class: 'map-btn', type: 'button', 'data-office-zoom': 'out', title: t('office.zoomOut'), text: '−' }),
          h('button', { class: 'map-btn', type: 'button', 'data-office-zoom': 'reset', title: t('office.zoomReset'), text: '⤢' }),
          h('button', { class: 'map-btn', type: 'button', 'data-office-zoom': 'in', title: t('office.zoomIn'), text: '+' }),
        ),
      ),
      canvas,
      tooltip,
      h('div', { class: 'office-map-hint' },
        h('span', { class: 'hint-pointer', text: t('office.mapHint') }),
        h('span', { class: 'hint-touch', text: t('office.mapHintTouch') }),
      ),
      editbar,
    );
    const roster = h('aside', { class: 'office-roster', 'aria-label': t('office.roster') },
      h('div', { class: 'office-roster-head' },
        h('h2', { text: t('office.roster') }),
        h('span', { class: 'office-roster-count' }),
      ),
      h('div', { class: 'office-roster-list', role: 'list' }),
      h('p', { class: 'office-roster-note', text: t('office.rosterNote') }),
    );
    const shell = h('div', { class: 'office-shell' }, stage, roster);
    clear(root);
    root.append(shell);
    root.querySelectorAll('[data-office-zoom]').forEach((button) => {
      button.addEventListener('click', () => {
        const action = button.dataset.officeZoom;
        if (!officeMapState.scene) return;
        if (action === 'in') officeMapState.scene.zoomIn();
        else if (action === 'out') officeMapState.scene.zoomOut();
        else officeMapState.scene.resetView();
      });
    });
    root.querySelector('[data-office-save]')?.addEventListener('click', () => {
      if (officeMapState.editor?.save) officeMapState.editor.save();
    });
    root.querySelector('[data-office-reset]')?.addEventListener('click', () => {
      if (officeMapState.editor?.reset) officeMapState.editor.reset();
    });
    officeMapState.refs = { shell, stage, canvas, tooltip, editbar, roster, chips: stage.querySelector('.office-roomchips'), list: roster.querySelector('.office-roster-list'), count: roster.querySelector('.office-roster-count') };
    return officeMapState.refs;
  }

  function officeStatusText(entry, lang) {
    const i18n = ts();
    const status = entry.kind === 'session' ? entry.session?.status
      : entry.kind === 'managed' ? entry.managed?.status : entry.agent?.status;
    return i18n ? i18n.statusLabel(status || 'unknown', lang) : String(status || 'Unknown');
  }

  function officeEntryName(entry) {
    if (entry.kind === 'session') return translator(officeMapState.lang)('office.ghostName');
    if (entry.kind === 'managed') {
      const item = entry.managed || {};
      return `${item.name || item.task_id || 'managed'} · ${item.role || 'agent'}`;
    }
    return entry.agent?.name || shortId(entry.agent?.agent_definition_id, 14);
  }

  function officeEntryRoom(entry) {
    return entry.room || 'lobby';
  }

  function syncOfficeChips() {
    const refs = officeMapState.refs;
    if (!refs?.chips) return;
    const t = translator(officeMapState.lang);
    const rooms = window.AOOfficeLayouts?.rooms || ['frontend', 'backend', 'qa', 'review', 'research', 'meeting', 'lobby'];
    clear(refs.chips);
    const all = h('button', {
      class: 'room-chip', type: 'button', 'aria-pressed': String(!officeMapState.activeRoom),
      text: t('office.allRooms'),
      onclick: () => setOfficeRoomFilter(null),
    });
    refs.chips.append(all);
    for (const room of rooms) {
      refs.chips.append(h('button', {
        class: 'room-chip', type: 'button', 'aria-pressed': String(officeMapState.activeRoom === room),
        text: roomLabel(room, officeMapState.lang),
        onclick: () => setOfficeRoomFilter(officeMapState.activeRoom === room ? null : room),
      }));
    }
  }

  function setOfficeRoomFilter(room) {
    officeMapState.activeRoom = room;
    officeMapState.scene?.setRoomFilter?.(room);
    syncOfficeChips();
    renderOfficeRoster();
    const message = room
      ? translator(officeMapState.lang)('office.roomFiltered', { room: roomLabel(room, officeMapState.lang) })
      : translator(officeMapState.lang)('office.roomFilterCleared');
    const announcer = document.querySelector('#screen-reader-status');
    if (announcer) announcer.textContent = message;
  }

  function renderOfficeRoster() {
    const refs = officeMapState.refs;
    if (!refs?.list) return;
    const t = translator(officeMapState.lang);
    const entries = officeMapState.entries.filter((entry) =>
      !officeMapState.activeRoom || officeEntryRoom(entry) === officeMapState.activeRoom);
    const visible = entries.slice(0, 64);
    clear(refs.list);
    refs.count.textContent = `${visible.length}/${officeMapState.entries.length}`;
    if (!entries.length) {
      refs.list.append(h('p', { class: 'office-roster-empty', text: t('office.noAgents') }));
      return;
    }
    for (const entry of visible) {
      const name = officeEntryName(entry);
      const room = roomLabel(officeEntryRoom(entry), officeMapState.lang);
      const status = officeStatusText(entry, officeMapState.lang);
      const button = h('button', {
        class: 'office-agent-row', type: 'button',
        'aria-label': t('office.inspectStatus', { name, status }),
        onclick: (event) => openOfficeEntry(entry, event.currentTarget),
      },
        h('span', { class: 'office-agent-dot', 'data-status': entry.kind === 'session' ? 'unknown'
          : entry.kind === 'managed' ? entry.managed?.status : entry.agent?.status }),
        h('span', { class: 'office-agent-copy' },
          h('strong', { text: name }),
          h('span', { text: `${room} · ${status}` }),
        ),
        h('span', { class: 'office-agent-arrow', 'aria-hidden': 'true', text: '›' }),
      );
      refs.list.append(h('div', { role: 'listitem' }, button));
    }
  }

  function openOfficeEntry(entry, trigger = officeMapState.refs?.canvas || null) {
    if (!entry) return;
    if (entry.kind === 'session') {
      officeMapState.options?.callbacks?.onInspectSession?.(entry.session, trigger);
    } else if (entry.kind === 'managed') {
      officeMapState.options?.callbacks?.onInspectManaged?.(entry.managed, trigger);
    } else if (entry.agent) {
      officeMapState.options?.callbacks?.onInspect?.(entry.agent, trigger);
    }
  }

  function showOfficeTooltip(hit, point) {
    const tooltip = officeMapState.refs?.tooltip;
    if (!tooltip) return;
    if (!hit) { tooltip.hidden = true; return; }
    const entry = hit.entry || hit;
    const name = officeEntryName(entry);
    const room = roomLabel(officeEntryRoom(entry), officeMapState.lang);
    const status = officeStatusText(entry, officeMapState.lang);
    tooltip.textContent = `${name} · ${status} · ${room}`;
    tooltip.hidden = false;
    const stage = officeMapState.refs?.stage;
    if (stage && point && Number.isFinite(point.x) && Number.isFinite(point.y)) {
      const rect = stage.getBoundingClientRect();
      tooltip.style.left = `${Math.max(8, Math.min(rect.width - 180, point.x + 14))}px`;
      tooltip.style.top = `${Math.max(8, Math.min(rect.height - 36, point.y + 14))}px`;
    }
  }

  /* Read-only diagnostics for browser tests and local debugging. It exposes no
   * execution control: the scene is a renderer, never an orchestrator. */
  function officeDebugState() {
    return {
      projectId: officeMapState.projectId,
      theme: officeMapState.theme,
      lang: officeMapState.lang,
      activeRoom: officeMapState.activeRoom,
      layout: officeMapState.layout,
      entries: officeMapState.entries,
      scene: officeMapState.scene,
      editor: officeMapState.editor,
    };
  }

  function destroyOffice() {
    officeMapState.editor?.destroy?.();
    officeMapState.scene?.destroy?.();
    officeMapState.scene = null;
    officeMapState.editor = null;
    officeMapState.refs = null;
    officeMapState.layout = null;
    officeMapState.projectId = null;
    officeMapState.theme = null;
    officeMapState.lang = null;
    officeMapState.payload = null;
    officeMapState.entries = [];
    officeMapState.activeRoom = null;
  }

  function ensureOfficeEditor(refs, options) {
    if (!window.AOLayoutEditor || !officeMapState.scene) return;
    if (!officeMapState.editor) {
      officeMapState.editor = window.AOLayoutEditor.attach({
        canvas: refs.canvas,
        scene: officeMapState.scene,
        getLayout: () => officeMapState.layout,
        onChange: (layout) => {
          officeMapState.layout = layout;
          officeMapState.scene?.setLayout?.(layout);
        },
        onSave: () => {
          if (!officeMapState.projectId || !officeMapState.layout) return;
          const ok = window.AOOfficeLayouts?.save?.(officeMapState.projectId, officeMapState.layout);
          const announcer = document.querySelector('#office-status');
          if (announcer) announcer.textContent = translator(officeMapState.lang)(ok ? 'office.layoutSaved' : 'office.layoutSaveFailed');
        },
        onReset: () => {
          if (!officeMapState.projectId) return;
          window.AOOfficeLayouts?.reset?.(officeMapState.projectId);
          const base = window.AOOfficeLayouts?.get?.(officeMapState.theme, officeMapState.projectId);
          if (base) {
            officeMapState.layout = localizeLayout(base, officeMapState.lang);
            officeMapState.scene?.setLayout?.(officeMapState.layout);
          }
        },
      });
    }
    officeMapState.scene?.editMode?.(Boolean(options.arranging));
    officeMapState.editor.enable?.(Boolean(options.arranging));
    refs.editbar.hidden = !options.arranging;
  }

  function renderOffice(root, summaryHost, options) {
    const payload = options.payload;
    const lang = options.lang || 'en';
    officeMapState.options = options;
    if (!root) return;
    if (!payload || !Array.isArray(payload.agents)) {
      if (options.error) {
        renderOfficeError(root, summaryHost, options, options.error);
      } else {
        renderOfficeLoading(root, summaryHost, lang);
      }
      return;
    }
    if (!window.AOOfficeScene || !window.AOOfficeLayouts) {
      renderOfficeError(root, summaryHost, options, { message: 'Office renderer is not installed' });
      return;
    }
    const projectId = payload.project_id || payload.project?.project_id || options.project?.project_id || 'default';
    const theme = options.theme || payload.theme || 'custom';
    const projectChanged = officeMapState.projectId !== projectId || officeMapState.theme !== theme
      || officeMapState.lang !== lang || !officeMapState.scene;
    if (projectChanged) {
      destroyOffice();
      const refs = buildOfficeShell(root, lang);
      const layout = localizeLayout(window.AOOfficeLayouts.load(projectId, theme), lang);
      officeMapState.projectId = projectId;
      officeMapState.theme = theme;
      officeMapState.lang = lang;
      officeMapState.layout = layout;
      officeMapState.payload = payload;
      officeMapState.scene = window.AOOfficeScene.create({
        canvas: refs.canvas,
        layout,
        reducedMotion: options.reducedMotion,
        onSelectAgent: (hit) => openOfficeEntry(hit?.entry || hit),
        onSelectRoom: (room) => setOfficeRoomFilter(room || null),
        onHover: (hit, point) => showOfficeTooltip(hit, point),
        onLayoutChange: (layout) => { officeMapState.layout = layout; },
      });
    } else {
      officeMapState.payload = payload;
      officeMapState.layout = localizeLayout(officeMapState.layout || window.AOOfficeLayouts.load(projectId, theme), lang);
      if (options.layout) officeMapState.scene.setLayout(options.layout);
    }
    officeMapState.entries = normalizedOfficeEntries(payload);
    const drawable = officeMapState.entries.filter((entry) => entry.spawnable);
    officeMapState.scene.syncAgents(drawable);
    officeMapState.scene.setReducedMotion?.(Boolean(options.reducedMotion));
    officeMapState.scene.resize?.();
    renderSummary(summaryHost, payload, officeMapState.entries, lang);
    syncOfficeChips();
    renderOfficeRoster();
    ensureOfficeEditor(officeMapState.refs, options);
    if (!drawable.length) {
      const refs = officeMapState.refs;
      if (refs?.stage && !refs.stage.querySelector('.office-idle-note')) {
        refs.stage.append(h('div', { class: 'office-idle-note', text: translator(lang)('office.mapIdle') }));
      }
    } else {
      officeMapState.refs?.stage?.querySelector('.office-idle-note')?.remove();
    }
  }

  /* ── Inspector rendering ───────────────────────────────────────────────── */

  function field(label, value, options = {}) {
    return h('div', { class: `inspector-field${options.wide ? ' wide' : ''}` },
      h('dt', { text: label, lang: options.lang || null }),
      h('dd', {},
        value == null || value === ''
          ? h('span', { class: 'muted', text: options.empty || '—' })
          : value,
      ),
    );
  }

  function section(title, hint, ...children) {
    return h('section', { class: 'inspector-section' },
      h('div', { class: 'inspector-section-head' },
        h('h3', { text: title }),
        hint ? h('span', { class: 'hint', text: hint }) : null,
      ),
      ...children,
    );
  }

  function verifiedModel(identity, lang) {
    const t = translator(lang);
    if (identity?.verified_model) {
      return h('span', { class: 'mono', text: String(identity.verified_model) });
    }
    return h('span', { class: 'not-attested' },
      h('span', { class: 'not-attested-mark', 'aria-hidden': 'true', text: '—' }),
      t('inspector.notAttested'),
    );
  }

  function commitValue(commit) {
    if (!commit) return null;
    if (typeof commit === 'string') return h('span', { class: 'mono', text: commit });
    if (typeof commit === 'object') {
      const sha = commit.sha || commit.hash || commit.short_sha;
      const message = commit.message || commit.subject;
      if (!sha && !message) return null;
      return h('span', { class: 'commit-row' },
        sha ? h('span', { class: 'mono', text: String(sha) }) : null,
        message ? h('span', { class: 'commit-message', text: String(message) }) : null,
      );
    }
    return null;
  }

  function fileEvidence(files, lang) {
    const t = translator(lang);
    const worktree = files?.worktree || null;
    const changed = Array.isArray(files?.changed_files) ? files.changed_files : [];
    const diff = files?.diff_available;
    const commit = commitValue(files?.commit);
    const merge = files?.merge_ready;
    const hasAny = Boolean(worktree || changed.length || diff === true || commit || merge === true || merge === false);
    if (!hasAny) {
      return section(t('inspector.files'), null,
        h('p', { class: 'inspector-note', text: t('inspector.filesNone') }),
        h('p', { class: 'inspector-note hint', text: t('inspector.filesNote') }),
      );
    }
    const changedList = changed.length
      ? h('div', { class: 'file-list' },
        ...changed.slice(0, MAX_CHANGED_FILES).map((path) => h('code', { class: 'file-chip', text: String(path) })),
        changed.length > MAX_CHANGED_FILES
          ? h('span', { class: 'muted', text: `+${changed.length - MAX_CHANGED_FILES}` })
          : null,
      )
      : h('span', { class: 'muted', text: t('common.none') });
    return section(t('inspector.files'), null,
      h('dl', { class: 'inspector-fields' },
        worktree ? field(t('inspector.worktree'), h('code', { text: String(worktree) }), { wide: true }) : null,
        field(t('inspector.changedFiles'), changedList, { wide: true }),
        field(t('inspector.diff'), diff === true ? t('inspector.diffAvailable') : t('inspector.diffUnavailable')),
        field(t('inspector.commit'), commit || h('span', { class: 'muted', text: t('common.unknown') }), { wide: true, lang: 'en' }),
        field(t('inspector.mergeReady'), merge === true ? t('inspector.mergeYes') : merge === false ? t('inspector.mergeNo') : null,
          { empty: t('common.unknown') }),
      ),
      h('p', { class: 'inspector-note hint', text: t('inspector.filesNote') }),
    );
  }

  function eventRow(event, lang) {
    const type = event?.event_type || event?.type || 'event';
    const at = event?.timestamp ?? event?.at ?? null;
    const source = event?.evidence_source || event?.source || null;
    const confidence = event?.confidence || null;
    return h('li', { class: 'inspector-event' },
      h('span', { class: 'event-time', text: fmtRelative(at, lang) }),
      h('span', { class: 'event-kind', text: String(type) }),
      h('span', { class: 'event-meta', text: [source, confidence ? confidenceLabel(confidence, lang) : null].filter(Boolean).join(' · ') || translator(lang)('common.unknown') }),
    );
  }

  function checksTable(checks, lang) {
    const t = translator(lang);
    if (!Array.isArray(checks) || !checks.length) {
      return h('p', { class: 'inspector-note', text: t('inspector.noChecks') });
    }
    const rows = checks.slice(0, 40).map((check) => {
      const id = check?.id || check?.check_id || check?.name || '—';
      const passed = check?.passed === true || check?.status === 'passed' || check?.status === 'pass';
      const failed = check?.passed === false || check?.status === 'failed' || check?.status === 'fail';
      const state = passed ? 'passed' : failed ? 'failed' : 'unknown';
      const exit = check?.exit_code ?? check?.exit ?? null;
      const stateLabel = passed ? t('common.passed') : failed ? t('common.failed') : t('common.unknown');
      return h('tr', {},
        h('td', { class: 'mono', text: String(id) }),
        h('td', {}, h('span', { class: 'state-chip', 'data-state': state, text: stateLabel })),
        h('td', { class: 'num', text: exit == null ? '—' : String(exit) }),
        h('td', { class: 'num', text: fmtRelative(check?.at ?? check?.timestamp, lang) }),
      );
    });
    return h('div', { class: 'table-wrap inspector-table' },
      h('table', {},
        h('thead', {}, h('tr', {},
          h('th', { text: t('runs.checkTable.check') }),
          h('th', { text: t('inspector.status') }),
          h('th', { text: t('runs.checkTable.exit') }),
          h('th', { text: t('runs.checkTable.observed') }),
        )),
        h('tbody', {}, ...rows),
      ),
    );
  }

  function terminalPanel(terminal, lang) {
    const t = translator(lang);
    const supported = terminal?.supported === true;
    return h('section', { class: 'terminal-panel', 'data-supported': String(supported) },
      h('div', { class: 'terminal-head' },
        h('span', { class: 'terminal-traffic', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')),
        h('h3', { text: t('inspector.terminal') }),
        h('span', { class: 'pill', text: supported ? t('inspector.terminalReadOnly') : t('common.notAvailable') }),
      ),
      h('p', { text: supported ? t('inspector.terminalSupported') : t('inspector.terminalUnsupported') }),
      h('p', { class: 'terminal-hint', text: t('inspector.terminalNoFake') }),
    );
  }

  function evidenceWarning(agent, payload, lang) {
    const t = translator(lang);
    const evidence = payload?.evidence || {};
    const confidence = agent?.evidence?.confidence || null;
    const open = Boolean(agent?.evidence?.open_span);
    const notes = [];
    if (open) notes.push(t('status.openSpanNote'));
    if (confidence === 'unknown') notes.push(t('inspector.unknownEvidenceBody'));
    if (payload?.evidence?.review_attested === false && payload?.evidence?.verification?.required) {
      notes.push(t('inspector.reviewNo'));
    }
    if (!notes.length) return null;
    return h('div', { class: 'inspector-warning', 'data-tone': 'warn' },
      h('strong', { text: `${t('inspector.evidenceCaveat')} · ` }),
      notes.join(' '),
    );
  }

  function identitySection(agent, payload, lang) {
    const t = translator(lang);
    const identity = payload?.identity || {};
    const project = payload?.project || null;
    const nativeSession = identity.native_session;
    return section(t('inspector.identity'), null,
      h('dl', { class: 'inspector-fields' },
        field(t('inspector.name'), agent?.name ? h('strong', { text: String(agent.name) }) : null, { empty: t('inspector.notAttested') }),
        field(t('inspector.role'), agent?.role ? String(agent.role) : null, { empty: t('inspector.notAttested') }),
        field(t('inspector.project'), project?.name ? String(project.name) : null, { empty: t('inspector.notAttested') }),
        field(t('inspector.platform'), identity.platform || agent?.platform
          ? h('span', { class: 'mono', text: String(identity.platform || agent.platform) })
          : null, { empty: t('inspector.notAttested'), lang: 'en' }),
        field(t('inspector.model'), verifiedModel(identity, lang), { lang: 'en' }),
        field(t('inspector.nativeSession'), nativeSession
          ? h('span', { class: 'mono', text: String(nativeSession) })
          : null, { empty: t('inspector.noNativeSession') }),
        field(t('inspector.binding'), payload?.binding
          ? h('span', { class: 'attested', text: t('inspector.bound') })
          : h('span', { class: 'muted', text: t('inspector.unbound') })),
      ),
      identity.model_note && !identity.verified_model
        ? h('p', { class: 'inspector-note hint', text: t('inspector.modelNote') })
        : null,
    );
  }

  function activitySection(agent, payload, lang, callbacks) {
    const t = translator(lang);
    const activity = payload?.activity || {};
    const current = agent?.current || {};
    const run = activity.run || null;
    const task = payload?.task || null;
    const hasRunEvidence = Boolean(run && (run.id || run.run_id));
    const runId = (hasRunEvidence ? (run.id || run.run_id) : null) || current.run_id || null;
    const taskId = task?.id || task?.task_id || current.task_id || null;
    const started = run?.started_at || current.started_at || null;
    const lastEvent = activity.last_event_at || run?.last_event_at || current.last_event_at || null;
    const duration = run?.duration_seconds != null
      ? run.duration_seconds
      : (started && lastEvent ? Math.max(0, lastEvent - started) : null);
    const events = Array.isArray(activity.events) ? activity.events : [];
    const recent = events.slice(-MAX_EVENTS);
    const last = recent.length
      ? (recent[recent.length - 1]?.event_type || recent[recent.length - 1]?.type)
      : (current.last_event || null);

    const currentFields = [];
    if (runId) {
      const runValue = hasRunEvidence
        ? h('button', {
          class: 'link-btn mono', type: 'button', 'data-open-run': String(runId),
          onclick: () => callbacks?.onOpenRun?.(String(runId)),
        }, String(runId))
        : h('span', { class: 'mono', text: String(runId) });
      currentFields.push(field(hasRunEvidence ? t('inspector.currentRun') : t('inspector.boundRun'), runValue, { wide: true }));
    }
    if (taskId) currentFields.push(field(t('inspector.currentTask'), h('span', { class: 'mono', text: String(taskId) }), { wide: true }));
    if (started) currentFields.push(field(t('inspector.started'), `${fmtDateTime(started, lang)} · ${fmtRelative(started, lang)}`));
    if (lastEvent) currentFields.push(field(t('inspector.lastEvent'), `${fmtDateTime(lastEvent, lang)} · ${fmtRelative(lastEvent, lang)}`));
    if (duration != null) currentFields.push(field(t('inspector.duration'), fmtDuration(duration)));
    currentFields.push(field(t('inspector.lastAction'), last ? h('code', { text: String(last) }) : null, { empty: t('inspector.lastActionNone') }));

    return section(t('inspector.activity'), null,
      runId || taskId ? h('dl', { class: 'inspector-fields' }, ...currentFields)
        : h('p', { class: 'inspector-note', text: t('inspector.noCurrent') }),
      h('h4', { class: 'inspector-subhead', text: t('inspector.events') }),
      recent.length
        ? h('ul', { class: 'inspector-events' }, ...recent.slice().reverse().map((event) => eventRow(event, lang)))
        : h('p', { class: 'inspector-note', text: t('inspector.noEvents') }),
      events.length > MAX_EVENTS
        ? h('p', { class: 'inspector-note hint', text: t('inspector.eventLimit', { shown: MAX_EVENTS, total: events.length }) })
        : null,
      h('h4', { class: 'inspector-subhead', text: t('inspector.checks') }),
      checksTable(activity.checks, lang),
    );
  }

  function evidenceSection(agent, payload, lang) {
    const t = translator(lang);
    const evidence = payload?.evidence || {};
    const verification = evidence.verification;
    const run = payload?.activity?.run || null;
    const task = payload?.task || null;
    const boundRunId = agent?.current?.run_id || null;
    const boundTaskId = agent?.current?.task_id || null;
    const hasRunEvidence = Boolean(
      (run && (run.id || run.run_id)) || (task && (task.id || task.task_id))
      || boundRunId || boundTaskId,
    );
    const bound = Boolean(payload?.binding || agent?.binding)
      && String(agent?.evidence?.source || 'none') !== 'none';
    const nativeStop = evidence.native_stop_observed === true
      ? h('span', { class: 'attested', text: t('inspector.nativeStopYes') })
      : !bound
        ? h('span', { class: 'not-attested' },
          h('span', { class: 'not-attested-mark', 'aria-hidden': 'true', text: '—' }),
          t('inspector.nativeStopNotAttested'))
        : h('span', { class: 'warn-text', text: t(agent?.evidence?.open_span
          ? 'inspector.nativeStopNo'
          : 'inspector.nativeStopNotObserved') });
    const verificationValue = verification
      ? h('span', {}, h('span', { class: 'state-chip', lang: 'en', 'data-state': verification.status || (verification.verified ? 'completed' : 'unknown') },
        String(verification.status || (verification.verified ? 'verified' : t('common.unknown')))),
        verification.reason ? h('span', { class: 'verification-reason', text: ` · ${String(verification.reason)}` }) : null)
      : h('span', { class: 'muted', text: t(hasRunEvidence
        ? 'inspector.verificationNone'
        : 'inspector.verificationNoRun') });
    const diagnostics = Array.isArray(evidence.diagnostics) ? evidence.diagnostics : [];
    return section(t('inspector.evidence'), null,
      h('dl', { class: 'inspector-fields' },
        field(t('inspector.verification'), verificationValue, { wide: true }),
        field(t('inspector.nativeStop'), nativeStop),
        field(t('inspector.review'), evidence.review_attested === true
          ? h('span', { class: 'attested', text: t('inspector.reviewYes') })
          : h('span', { class: 'muted', text: t('inspector.reviewNo') })),
        field(t('inspector.evidenceSource'), agent?.evidence?.source || null, { empty: t('common.unknown') }),
        field(t('inspector.confidence'), agent?.evidence?.confidence ? confidenceLabel(agent.evidence.confidence, lang) : null, { empty: t('common.unknown') }),
      ),
      agent?.evidence?.detail && agent?.evidence?.source !== 'none'
        ? h('p', { class: 'inspector-note', text: String(agent.evidence.detail) })
        : null,
      evidence.review_note ? h('p', { class: 'inspector-note', text: String(evidence.review_note) }) : null,
      evidenceWarning(agent, payload, lang),
      h('h4', { class: 'inspector-subhead', text: t('inspector.diagnostics') }),
      diagnostics.length
        ? h('ul', { class: 'diagnostic-list' }, ...diagnostics.slice(0, 30).map((item) => {
          const label = typeof item === 'string' ? item : (item?.reason || item?.code || JSON.stringify(item));
          return h('li', { class: 'mono', text: String(label) });
        }))
        : h('p', { class: 'inspector-note', text: t('inspector.noDiagnostics') }),
    );
  }

  function agentInspector(payload, fallbackAgent, options) {
    const lang = options.lang;
    const t = translator(lang);
    const agent = payload?.agent || fallbackAgent || {};
    const evidence = agent.evidence || {};
    const statusLabel = ts() ? ts().statusMeta(agent.status, lang).label : String(agent.status || '');
    return h('div', { class: 'inspector-body' },
      h('header', { class: 'inspector-header' },
        h('div', { class: 'inspector-heading' },
          h('div', { class: 'eyebrow', text: payload?.project?.name || t('inspector.title') }),
          h('h2', { id: 'inspector-title', class: 'inspector-name', text: agent.name || shortId(agent.agent_definition_id, 14) }),
          h('p', { class: 'inspector-role', text: agent.role || t('office.roleFallback') }),
          h('div', { class: 'inspector-status-row' },
            statusBadge(agent.status, lang),
            h('span', { class: 'inspector-status-detail', text: evidence.source === 'none'
              ? t('status.noBinding')
              : (evidence.detail || evidence.confidence || t('common.unknown')) }),
          ),
        ),
        h('button', {
          id: 'inspector-close', class: 'icon-btn inspector-close', type: 'button',
          title: t('inspector.close'), 'aria-label': t('inspector.close'),
          onclick: () => options.callbacks?.onClose?.(),
        }, h('span', { class: 'close-glyph', 'aria-hidden': 'true', text: '×' })),
      ),
      evidence?.open_span
        ? h('div', { class: 'inspector-warning', role: 'status' },
          h('strong', { text: `${t('status.openSpanWarning', { status: statusLabel })}. ` }),
          t('status.openSpanNote'))
        : null,
      identitySection(agent, payload, lang),
      activitySection(agent, payload, lang, options.callbacks),
      fileEvidence(payload?.files, lang),
      evidenceSection(agent, payload, lang),
      terminalPanel(payload?.terminal, lang),
      h('p', { class: 'inspector-note hint', text: t('inspector.evidenceNote') }),
    );
  }

  function unlinkedInspector(session, project, options) {
    const lang = options.lang;
    const t = translator(lang);
    const meta = ts() ? ts().statusMeta(session?.status, lang) : { key: 'unknown' };
    const runId = session?.run_id || null;
    return h('div', { class: 'inspector-body' },
      h('header', { class: 'inspector-header' },
        h('div', { class: 'inspector-heading' },
          h('div', { class: 'eyebrow', text: project?.name || t('inspector.title') }),
          h('h2', {
            id: 'inspector-title', class: 'inspector-name',
            text: session?.managed ? t('inspector.managedTitle') : t('inspector.unlinkedTitle'),
          }),
          h('p', {
            class: 'inspector-role',
            text: session?.managed
              ? `${session.role || 'agent'} · ${session.objective || session.task_id || ''}`
              : t('office.ghostNote'),
          }),
          h('div', { class: 'inspector-status-row' },
            statusBadge(session?.status, lang),
            h('span', {
              class: 'inspector-status-detail',
              text: session?.managed
                ? `${session.managed_status || session.status} · ${session.provider || 'managed'}`
                : (session?.open ? t('inspector.sessionOpen') : t('inspector.sessionClosed')),
            }),
          ),
        ),
        h('button', {
          id: 'inspector-close', class: 'icon-btn inspector-close', type: 'button',
          title: t('inspector.close'), 'aria-label': t('inspector.close'),
          onclick: () => options.callbacks?.onClose?.(),
        }, h('span', { class: 'close-glyph', 'aria-hidden': 'true', text: '×' })),
      ),
      session?.open
        ? h('div', { class: 'inspector-warning' },
          h('strong', { text: `${t('status.openSpanWarning', { status: meta.label })}. ` }),
          t('status.openSpanNote'))
        : null,
      h('div', { class: 'inspector-warning' },
        h('strong', { text: `${session?.managed ? t('inspector.managedTitle') : t('inspector.unlinkedTitle')}. ` }),
        session?.managed ? t('inspector.managedLead') : t('inspector.unlinkedLead')),
      section(t('inspector.identity'), null,
        h('dl', { class: 'inspector-fields' },
          field(t('inspector.name'), session?.managed ? String(session.name || session.task_id || '') : null, { empty: t('inspector.notAttested') }),
          field(t('inspector.role'), session?.managed ? String(session.role || '') : null, { empty: t('inspector.notAttested') }),
          field(t('inspector.project'), project?.name ? String(project.name) : null, { empty: t('inspector.notAttested') }),
          field(t('inspector.platform'), session?.platform ? h('span', { class: 'mono', text: String(session.platform) }) : null, { empty: t('inspector.notAttested'), lang: 'en' }),
          field(t('inspector.model'), session?.managed && session.model ? h('span', { class: 'mono', text: String(session.model) }) : null, { empty: t('inspector.notAttested') }),
          field(t('inspector.nativeSession'), null, { empty: t('inspector.noNativeSession') }),
        ),
      ),
      section(t('inspector.activity'), null,
        h('dl', { class: 'inspector-fields' },
          field(t('inspector.sessionRun'), runId
            ? h('button', {
              class: 'link-btn mono', type: 'button',
              onclick: () => options.callbacks?.onOpenRun?.(String(runId)),
            }, String(runId))
            : null, { empty: t('inspector.sessionNone'), wide: true }),
          field(t('inspector.sessionStatus'), meta.label),
          field(t('inspector.sessionSpan'), session?.managed
            ? (session.open ? t('inspector.managedSpanOpen') : t('inspector.managedSpanClosed'))
            : (session?.open ? t('inspector.sessionOpen') : t('inspector.sessionClosed'))),
          field(t('inspector.sessionDuration'), session?.duration == null ? null : fmtDuration(session.duration)),
          field(t('inspector.sessionLastEvent'), session?.last_event_at ? `${fmtDateTime(session.last_event_at, lang)} · ${fmtRelative(session.last_event_at, lang)}` : null),
          session?.managed ? field(t('inspector.task'), String(session.task_id || ''), { wide: true }) : null,
          session?.managed ? field(t('inspector.toolCount'), String(session.tool_count ?? 0)) : null,
          session?.managed ? field(t('inspector.cost'), session.cost == null ? null : `$${Number(session.cost).toFixed(6)}`) : null,
          session?.managed ? field(t('inspector.sessionStarted'), session.started_at ? fmtDateTime(session.started_at, lang) : null) : null,
          session?.managed ? field(t('inspector.sessionFinished'), session.finished_at ? fmtDateTime(session.finished_at, lang) : null) : null,
          session?.managed ? field(t('inspector.sessionHash'), session.session_hash ? h('span', { class: 'mono', text: String(session.session_hash) }) : null) : null,
        ),
        h('p', { class: 'inspector-note', text: session?.managed ? t('inspector.managedNote') : t('inspector.noEvents') }),
      ),
      section(t('inspector.evidence'), null,
        h('p', { class: 'inspector-note', text: t('inspector.noEvents') }),
        h('ul', { class: 'diagnostic-list' },
          h('li', { class: 'mono', text: `${t('inspector.evidenceSource')}: ${session?.managed ? `managed/${session.provider || 'unknown'}` : 'office/unlinked_sessions'}` }),
          h('li', { class: 'mono', text: `${t('inspector.confidence')}: ${session?.managed ? t('status.observed') : t('status.unknownEvidence')}` }),
        ),
      ),
      terminalPanel({ supported: false }, lang),
      h('p', { class: 'inspector-note hint', text: t('inspector.evidenceNote') }),
    );
  }

  function renderInspectorPinned(host, options) {
    if (!host) return;
    const t = translator(options.lang);
    clear(host);
    const agents = Array.isArray(options.agents) ? options.agents.slice(0, MAX_PINNED) : [];
    host.append(h('h3', { class: 'pinned-title', text: t('inspector.pinned') }));
    if (!agents.length) {
      host.append(h('p', { class: 'pinned-empty', text: t('inspector.pinnedEmpty') }));
      return;
    }
    for (const agent of agents) {
      const id = agent.agent_definition_id;
      const active = id && id === options.activeAgentId;
      const meta = ts() ? ts().statusMeta(agent.status, options.lang) : { key: 'unknown' };
      host.append(h('button', {
        class: 'pinned-agent', type: 'button',
        'aria-current': active ? 'true' : 'false',
        'data-agent-id': id ? String(id) : null,
        onclick: () => id && options.callbacks?.onSelectAgent?.(id),
      },
        h('span', { class: 'pinned-dot', 'data-status': meta.key, 'aria-hidden': 'true' }),
        h('span', { class: 'pinned-copy' },
          h('span', { class: 'pinned-name', text: agent.name || shortId(id, 12) }),
          h('span', { class: 'pinned-role', text: agent.role || t('office.roleFallback') }),
        ),
      ));
    }
  }

  function renderInspectorLoading(pinned, content, options) {
    const t = translator(options.lang);
    if (pinned) {
      clear(pinned);
      pinned.append(h('h3', { class: 'pinned-title', text: t('inspector.pinned') }));
    }
    clear(content);
    content.append(h('div', { class: 'inspector-body' },
      h('header', { class: 'inspector-header' },
        h('div', { class: 'inspector-heading' },
          h('div', { class: 'eyebrow', text: options.project?.name || t('inspector.title') }),
          h('h2', { id: 'inspector-title', class: 'inspector-name', text: options.agent?.name || t('inspector.title') }),
          h('p', { class: 'inspector-role', text: options.agent?.role || t('office.roleFallback') }),
        ),
        h('button', {
          id: 'inspector-close', class: 'icon-btn inspector-close', type: 'button',
          title: t('inspector.close'), 'aria-label': t('inspector.close'),
          onclick: () => options.callbacks?.onClose?.(),
        }, h('span', { class: 'close-glyph', 'aria-hidden': 'true', text: '×' })),
      ),
      h('div', { class: 'inspector-loading', role: 'status' },
        h('span', { class: 'spinner', 'aria-hidden': 'true' }),
        h('span', { text: t('inspector.loading') }),
      ),
      h('div', { class: 'inspector-skeleton' }, h('i'), h('i'), h('i')),
    ));
  }

  function renderInspectorError(pinned, content, options, error) {
    const t = translator(options.lang);
    if (pinned) {
      clear(pinned);
      pinned.append(h('h3', { class: 'pinned-title', text: t('inspector.pinned') }));
    }
    clear(content);
    content.append(h('div', { class: 'inspector-body' },
      h('header', { class: 'inspector-header' },
        h('div', { class: 'inspector-heading' },
          h('div', { class: 'eyebrow', text: options.project?.name || t('inspector.title') }),
          h('h2', { id: 'inspector-title', class: 'inspector-name', text: t('inspector.errorTitle') }),
        ),
        h('button', {
          id: 'inspector-close', class: 'icon-btn inspector-close', type: 'button',
          title: t('inspector.close'), 'aria-label': t('inspector.close'),
          onclick: () => options.callbacks?.onClose?.(),
        }, h('span', { class: 'close-glyph', 'aria-hidden': 'true', text: '×' })),
      ),
      h('div', { class: 'empty-state inspector-empty' },
        h('div', { class: 'empty-inner' },
          h('div', { class: 'empty-mark' }, h('span', { class: 'empty-glyph', 'aria-hidden': 'true', text: '!' })),
          h('h2', { text: t('inspector.errorTitle') }),
          h('p', { text: t('inspector.errorBody') }),
          error?.status ? h('code', { text: t('inspector.errorDetail', { status: error.status }) }) : null,
          h('div', { class: 'empty-actions' },
            h('button', { class: 'btn btn-primary', type: 'button', onclick: () => options.callbacks?.onRetry?.() }, t('inspector.retry')),
          ),
        ),
      ),
    ));
  }

  function renderInspector(pinned, content, options) {
    if (!content) return;
    if (options.loading) {
      renderInspectorLoading(pinned, content, options);
      return;
    }
    if (options.error) {
      renderInspectorError(pinned, content, options, options.error);
      return;
    }
    if (options.mode === 'unlinked') {
      renderInspectorPinned(pinned, { ...options, agents: [], activeAgentId: null });
      clear(content);
      content.append(unlinkedInspector(options.session, options.project, options));
      return;
    }
    renderInspectorPinned(pinned, options);
    if (!options.payload) {
      renderInspectorLoading(pinned, content, options);
      return;
    }
    clear(content);
    content.append(agentInspector(options.payload, options.agent, options));
  }

  const api = {
    themes: THEMES,
    normalizeTheme,
    safeColor,
    shortId,
    fmtDuration,
    fmtDateTime,
    fmtRelative,
    shouldApplyEvent,
    nextSequence,
    sortKeys,
    moveDesk,
    agentKey,
    sessionKey,
    officeEntries,
    summaryCounts,
    renderOffice,
    destroyOffice,
    officeDebugState,
    renderInspector,
  };

  if (typeof globalThis !== 'undefined') globalThis.AOOffice = api;
})();
