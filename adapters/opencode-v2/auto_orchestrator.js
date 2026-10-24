/** OpenCode V2 native hook adapter (no raw prompt/session data persisted).
 *
 * A globally loaded plugin instance can see events for other projects. Never
 * create a telemetry run from an event without an explicit matching location.
 */
import { createHash, randomUUID } from 'node:crypto';
import { appendFileSync, closeSync, existsSync, mkdirSync, openSync, readFileSync, lstatSync } from 'node:fs';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

export const POLICY = 'Auto-Orchestrator: For non-trivial coding tasks read the auto-orchestrator skill and delegate independent work to native subagents automatically; max three concurrent. Verify results and never invent timings or human time savings. Trivial tasks use one agent.';

const hash = (value) => createHash('sha256').update(String(value)).digest('hex').slice(0, 24);
const nativeHash = (id) => createHash('sha256').update('opencode|' + id).digest('hex');

/**
 * Overlap among observed native *tool-call* spans only. The OpenCode
 * background subagent spawn span is not the child's true runtime.
 * Never describe this as elapsed time saved or model/token cost savings.
 */
export const observedOverlapMs = (spans) => {
  if (!Array.isArray(spans) || spans.length < 2) return 0;
  const sorted = spans.map(([a, b]) => [Number(a), Number(b)])
    .filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b) && b >= a)
    .sort((a, b) => a[0] - b[0]);
  if (sorted.length < 2) return 0;
  let summed = 0, union = 0, begin = sorted[0][0], end = sorted[0][1];
  for (const [a, b] of sorted) {
    summed += b - a;
    if (a > end) { union += end - begin; begin = a; end = b; }
    else end = Math.max(end, b);
  }
  union += end - begin;
  return Math.max(0, summed - union);
};
const normalized = (path) => {
  if (typeof path !== 'string' || !path.trim()) return null;
  const result = resolve(path);
  return process.platform === 'win32' ? result.toLowerCase() : result;
};

export default {
  id: 'auto-orchestrator',
  async setup(ctx) {
    const directory = normalized(ctx.location?.directory);
    const base = directory && join(directory, '.auto-orchestrator', 'runs');
    const runs = new Map();
    const promptRouting = new Map();
    const approvedTools = new Set(['bash', 'shell', 'powershell']);
    const mutatingTools = new Set(['edit', 'write', 'apply_patch', 'multiedit']);
    // Global installer places this trusted helper alongside the plugin; do
    // not execute a project-owned script from a native lifecycle callback.
    const supervisorScript = join(dirname(fileURLToPath(import.meta.url)),
      '..', 'auto-orchestrator', 'tools', 'supervisor.py');
    const supervisorDb = directory && join(directory, '.auto-orchestrator', 'supervisor.sqlite3');
    const routerScript = join(dirname(fileURLToPath(import.meta.url)),
      '..', 'auto-orchestrator', 'tools', 'router.py');
    // Stdout is bounded. Raw user text is only transmitted via local stdin,
    // never argv, shell, telemetry, or a provider. No shell execution.
    const selectLocal = async (objective, catalog) => {
      try {
        const stat = lstatSync(routerScript);
        if (!stat.isFile() || stat.isSymbolicLink()) return null;
      } catch { return null; }
      const input = JSON.stringify({ objective, skills: catalog });
      if (Buffer.byteLength(input, 'utf8') > 100000) return null;
      return await new Promise(resolve => {
        try {
          const child = execFile(process.env.AUTO_ORCHESTRATOR_PYTHON || 'python3',
            [routerScript, 'select'],
            { timeout: 1800, maxBuffer: 16384, windowsHide: true },
            (error, stdout) => {
              if (error) return resolve(null);
              try {
                const value = JSON.parse(stdout);
                resolve(value?.version === 1 && Array.isArray(value.selected) ? value : null);
              } catch { resolve(null); }
            });
          child.stdin?.on('error', () => {});
          child.stdin?.end(input);
        } catch { resolve(null); }
      });
    };
    const observeNative = async (id, state) => {
      if (!supervisorDb || typeof id !== 'string' || id.length < 4 ||
          !existsSync(supervisorScript)) return;
      try {
        const stat = lstatSync(supervisorDb);
        if (!stat.isFile() || stat.isSymbolicLink()) return;
        // Never transmit raw native IDs or project content to the subprocess.
        const args = [supervisorScript, '--root', directory, 'observe-native',
          '--platform', 'opencode', '--native-hash', nativeHash(id), '--event', state];
        await new Promise(resolve => {
          execFile(process.env.AUTO_ORCHESTRATOR_PYTHON || 'python3', args,
            { timeout: 2500, maxBuffer: 8192, windowsHide: true },
            () => resolve());
        });
      } catch { /* Missing/unavailable native bridge must fail closed. */ }
    };
    const policyForRun = () => {
      if (!directory) return null;
      try {
        const file = join(directory, '.auto-orchestrator', 'verification.json');
        const stat = lstatSync(file);
        if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 16384) return null;
        const bytes = readFileSync(file);
        const policy = JSON.parse(bytes.toString('utf8'));
        if (policy?.version !== 1 || policy?.auto_verify !== true ||
            !Array.isArray(policy.required_checks) ||
            policy.required_checks.length < 1 || policy.required_checks.length > 16) return null;
        const ids = new Set();
        for (const check of policy.required_checks) {
          if (typeof check?.id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(check.id) ||
              ids.has(check.id) || !approvedTools.has(check.tool) ||
              typeof check.command !== 'string' || check.command.length < 1 ||
              check.command.length > 500 || /[\r\n]/.test(check.command)) return null;
          if (check.alternates != null && (!Array.isArray(check.alternates) ||
              check.alternates.length > 4 ||
              check.alternates.some(cmd => typeof cmd !== 'string' || !cmd ||
                cmd.length > 500 || /[\r\n]/.test(cmd) || cmd === check.command) ||
              new Set(check.alternates).size !== check.alternates.length)) return null;
          ids.add(check.id);
        }
        return { ...policy, hash: createHash('sha256').update(bytes).digest('hex') };
      } catch { return null; }
    };

    // A location belongs to the current plugin instance only if explicit.
    // The event stream is shared across workspace/plugin instances.
    const location = (event) =>
      event?.location?.directory ?? event?.data?.location?.directory ??
      (typeof event?.location === 'string' ? event.location : null);
    const own = (event) => !!directory && normalized(location(event)) === directory;
    // A location transition or unavailable session lookup invalidates prior
    // evidence. The abandoned run remains incomplete (no fabricated finish).
    const abandon = (sid) => {
      promptRouting.delete(sid);
      const run = runs.get(sid);
      if (!run || run.done) return;
      // Lost ownership is NOT a terminal execution event. Preserve evidence
      // explaining why this run has no finish rather than inventing success.
      write(run, { event: 'diagnostic', reason: 'session_ownership_lost',
        active_agents: run.tasks.size });
      run.done = true;
      promptRouting.delete(sid);
      run.tasks.clear();
      run.toolStarts.clear();
      run.backgroundDispatches.clear();
      run.pendingChecks.clear();
      run.checks.clear();
    };
    // ctx.location belongs to the plugin instance, NOT necessarily to the
    // session that emitted a globally visible event. OpenCode V2 exposes an
    // authoritative session lookup, including session_move updates.
    // Never derive ownership from mutable shell input.workdir.
    const ownsSession = async (sid, event) => {
      if (!directory || typeof sid !== 'string' || !sid) return false;
      if (typeof ctx.session.get !== 'function') {
        // Older clients have no authoritative lookup. Keep the prior V4
        // contract: only an explicitly matching location may BEGIN a run;
        // location-less callbacks may CONTINUE an already owned active run.
        const active = runs.get(sid);
        return own(event) || (!location(event) && !!active && !active.done);
      }
      try {
        const response = await ctx.session.get({ sessionID: sid });
        const info = response?.data ?? response;
        const matched = normalized(info?.location?.directory) === directory;
        if (!matched) abandon(sid);
        return matched;
      } catch {
        // Fail closed, including for moved/unknown sessions. Never fall back
        // to an event, a tool cwd, process.cwd(), or a default project.
        abandon(sid);
        return false;
      }
    };
    const data = (event) => event?.data ?? event?.properties ?? event ?? {};
    const session = (event) => data(event).sessionID ??
      data(event).session?.id ?? event?.sessionID ?? null;
    const toolName = (event) => event?.tool ?? data(event)?.tool ?? data(event)?.name;
    const callId = (event) => event?.id ?? event?.callID ?? data(event)?.callID ?? null;

    const write = (run, event) => {
      if (!base || !run || run.done) return false;
      try {
        mkdirSync(base, { recursive: true });
        const file = join(base, run.id + '.jsonl');
        if (event.event === 'start') {
          const fd = openSync(file, 'wx', 0o600);
          closeSync(fd);
        }
        appendFileSync(file, JSON.stringify({ ...event, time: Date.now() / 1000 }) + '\n');
        return true;
      } catch {
        // Telemetry is optional; never interrupt agent work or capture secrets.
        return false;
      }
    };
    const begin = (sid) => {
      // Callers must first pass ownsSession with a fresh session lookup.
      if (typeof sid !== 'string' || !sid) return null;
      const old = runs.get(sid);
      if (old && !old.done) return old;
      const run = { id: hash('opencode-v2|' + sid + '|' + randomUUID()),
        done: false, tasks: new Set(), pendingChecks: new Map(), checks: new Map(),
        toolStarts: new Map(), toolSpans: [], nativeSpanComplete: true,
        backgroundDispatches: new Set(),
        tick: 0, invalidatedAt: 0, policy: policyForRun() };
      runs.set(sid, run);
      write(run, { event: 'start', platform: 'opencode' });
      return run;
    };
    const existing = (sid) => {
      // Callers must first pass ownsSession; do not reuse a run after move.
      if (typeof sid !== 'string' || !sid) return null;
      const run = runs.get(sid);
      return run && !run.done ? run : null;
    };

    const reportStreamIssue = (reason) => {
      for (const run of runs.values()) {
        if (!run.done) write(run, { event: 'diagnostic', reason,
          active_agents: run.tasks.size });
      }
    };
    const finish = async (sid, status) => {
      const run = existing(sid);
      if (!run) return;
      const finishRecorded = write(run, { event: 'finish', status, verified: false });
      let verified = false;
      const currentPolicy = policyForRun();
      if (finishRecorded && status === 'success' && run.policy && currentPolicy &&
          currentPolicy.hash === run.policy.hash && run.nativeSpanComplete &&
          run.tasks.size === 0 &&
          run.pendingChecks.size === 0 &&
          run.policy.required_checks.every(check =>
            run.checks.get(check.id)?.passed === true &&
            run.checks.get(check.id)?.tick > run.invalidatedAt)) {
        verified = write(run, { event: 'verified', source: 'quality-gate',
          check_ids: run.policy.required_checks.map(c => c.id).sort() });
      }
      run.done = true;
      promptRouting.delete(sid);
      run.tasks.clear();
      run.toolStarts.clear();
      run.backgroundDispatches.clear();
      run.pendingChecks.clear();
      // Only verified, completed runs qualify for a positive overlap label.
      // Toast is ephemeral and best-effort; all persisted events stay private.
      if (verified && typeof ctx.tui?.showToast === 'function') {
        const overlap = run.nativeSpanComplete && run.toolSpans.length >= 2
          ? observedOverlapMs(run.toolSpans) : 0;
        const message = overlap > 0
          ? `Auto-Orchestrator: doğrulandı · ${run.toolSpans.length} native tool span · ${(overlap/1000).toFixed(1)} sn örtüşme (tasarruf değil)`
          : 'Auto-Orchestrator: testler doğrulandı · ölçülebilir paralel örtüşme yok';
        try { await ctx.tui.showToast({body:{message,variant:'success'}}); }
        catch { /* Unsupported TUI methods must never block IDE work. */ }
      }
    };

    // Native V2 prompt admission: selection occurs without the user needing
    // to remember "run router.py". No changes to their text or attachments.
    // A prompt hook is guidance/skill admission, not an enforcement point for
    // native subagent dispatch (which remains under model/permission control).
    await ctx.session.hook('prompt', async (event) => {
      const sid = event?.sessionID, prompt = event?.prompt;
      if (typeof sid !== 'string' || !sid) return;
      // The user controls manual skill attachments. Clear any previous
      // advisory state before evaluating each new user prompt in this session.
      promptRouting.delete(sid);
      if (!directory || typeof prompt?.text !== 'string' ||
          !prompt.text.trim() || prompt.text.length > 12000 ||
          (Array.isArray(prompt.skills) && prompt.skills.length > 0) ||
          typeof ctx.session.get !== 'function' || typeof ctx.skill?.list !== 'function') return;
      try {
        const session = await ctx.session.get({ sessionID: sid });
        const info = session?.data ?? session;
        if (normalized(info?.location?.directory) !== directory) {
          abandon(sid);
          return;
        }
        if (info?.parentID || info?.parentId) return;
        const available = await ctx.skill.list();
        const list = Array.isArray(available) ? available :
          (Array.isArray(available?.data) ? available.data :
            Array.isArray(available?.items) ? available.items : null);
        if (!list) return;
        const ids = new Map();
        const catalog = [];
        for (const skill of list) {
          if (!skill || typeof skill.id !== 'string' ||
              typeof skill.name !== 'string' ||
              !/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(skill.name) ||
              skill.name === 'auto-orchestrator' || skill.autoinvoke === false ||
              ids.has(skill.name)) continue;
          if (typeof skill.description !== 'string' || skill.description.length > 500) continue;
          ids.set(skill.name, skill.id);
          catalog.push({ name: skill.name, description: skill.description });
        }
        const routeStarted = Date.now();
        const decision = await selectLocal(prompt.text, catalog);
        const routeLatencyMs = Math.max(0, Date.now() - routeStarted);
        if (!decision) return;
        const picks = decision.selected.filter(name => ids.has(name)).slice(0, 2);
        if (picks.length !== decision.selected.length) return; // no partial trust
        prompt.skills = [...(Array.isArray(prompt.skills) ? prompt.skills : []),
          ...picks.map(name => ({ id: ids.get(name) }))];
        const delegation = decision.delegation;
        const max = Number.isInteger(delegation?.max_agents) &&
          delegation.max_agents >= 1 && delegation.max_agents <= 3
          ? delegation.max_agents : 1;
        promptRouting.set(sid, {
          mode: delegation?.mode === 'consider-parallel' ? 'consider-parallel' : 'single',
          max_agents: max, selected_count: picks.length
        });
        // Never persist objective, skill names, skill paths or full prompts.
        const run = existing(sid) ?? begin(sid);
        write(run, { event: 'route', source: 'native-prompt-hook',
          selected_count: picks.length,
          delegation: promptRouting.get(sid).mode,
          max_agents: max, latency_ms: routeLatencyMs });
      } catch {
        // An unavailable native API/helper must not block user prompt admission
        // or turn a guessed location into trusted routing.
      }
    });
    await ctx.session.hook('context', (event) => {
      if (event.agent?.startsWith('auto_')) return;
      event.system.push({ type: 'text', text: POLICY });
      const guidance = promptRouting.get(event.sessionID);
      if (guidance?.mode === 'single') {
        event.system.push({ type: 'text',
          text: 'Auto-Orchestrator routing: no independent work was established. Prefer one agent; do not create subagents solely for routine verification. You may reconsider if you discover genuine independent work.' });
      } else if (guidance?.mode === 'consider-parallel') {
        event.system.push({ type: 'text',
          text: 'Auto-Orchestrator routing: potentially independent subtasks detected. Use native subagents only if scopes and dependencies really permit them; at most ' + guidance.max_agents + '. Never claim routing guarantees dispatch.' });
      }
    });
    await ctx.tool.hook('execute.before', async (event) => {
      const sid = session(event), id = callId(event);
      if (!(await ownsSession(sid, event))) return;
      if (toolName(event) !== 'subagent') {
        const run = existing(sid) ?? begin(sid);
        if (!run) return;
        const name = toolName(event);
        if (!approvedTools.has(name)) return;
        const input = event?.input ?? event?.data?.input ?? {};
        const command = input.command;
        // OpenCode 2.1.x shell may provide only {command}. Explicit cwd must
        // match. Without cwd, require an authoritative ctx.session.get lookup
        // (ownsSession already checked it) and the exact allowlisted command.
        // An event location or process.cwd() alone is never enough.
        const cwdSupplied = Object.hasOwn(input, 'workdir') ||
          Object.hasOwn(input, 'cwd');
        const cwdAllowed = cwdSupplied
          ? normalized(input.workdir ?? input.cwd) === directory
          : typeof ctx.session.get === 'function';
        const check = run.policy && typeof command === 'string' &&
          run.policy.required_checks.find(c => c.tool === name &&
            (c.command === command || c.alternates?.includes(command)));
        if (!check || typeof id !== 'string' || !id || !cwdAllowed) {
          // An arbitrary shell command may edit files without an edit/write
          // tool event. Conservatively expire all previous test evidence
          // BEFORE executing it (even when it fails or throws).
          run.invalidatedAt = ++run.tick;
          write(run, { event: 'mutation' });
          return;
        }
        // A repeated test with a missing exit code must not be allowed to
        // reuse a previously successful result for this check ID.
        if (run.checks.has(check.id) ||
            [...run.pendingChecks.values()].some(c => c.checkId === check.id)) {
          // Repeating a previously successful or still-running check means
          // that older evidence cannot authorize a late CLI verification.
          run.invalidatedAt = ++run.tick;
          write(run, { event: 'mutation' });
        }
        run.checks.delete(check.id);
        run.pendingChecks.set(id, {checkId: check.id, startedAt: ++run.tick});
        return;
      }
      if (typeof id !== 'string' || !id) return;
      // Hook callbacks may omit location. Only trust them after this
      // session was explicitly associated with our own location.
      const run = existing(sid) ?? begin(sid);
      if (!run) return;
      const agent = hash(id);
      if (run.tasks.has(agent)) return;
      run.tasks.add(agent);
      run.toolStarts.set(agent, Date.now());
      const input = event?.input ?? event?.data?.input ?? {};
      if (input.background === true) {
        // A successful background dispatch is NOT a child completion.
        // Unless a future native API provides an authoritative matching
        // child-stop, leave the agent span open and block auto-verification.
        run.backgroundDispatches.add(agent);
        run.nativeSpanComplete = false;
      }
      write(run, { event: 'agent-start', agent_id: agent,
        native_hash: nativeHash(id) });
      await observeNative(id, 'start');
    });
    await ctx.tool.hook('execute.after', async (event) => {
      const sid = session(event), id = callId(event);
      if (!(await ownsSession(sid, event))) return;
      if (toolName(event) !== 'subagent') {
        const run = existing(sid);
        if (!run) return;
        if (mutatingTools.has(toolName(event))) {
          run.invalidatedAt = ++run.tick;
          write(run, { event: 'mutation' });
        }
        if (typeof id !== 'string' || !id || !run.policy) return;
        const pending = run.pendingChecks.get(id);
        run.pendingChecks.delete(id);
        if (!pending || pending.startedAt <= run.invalidatedAt) return;
        const checkId = pending.checkId;
        const code = event?.result?.metadata?.exit ??
          event?.result?.metadata?.exitCode ??
          event?.result?.metadata?.exit_code ??
          event?.result?.exitCode ?? event?.metadata?.exitCode;
        // OpenCode 2.1.x may omit the tool's top-level status in execute.after.
        // The native integer exit code plus a non-error after callback suffices;
        // any explicit failed/interrupted status or error must still fail closed.
        const statuses = [event.status, event?.result?.metadata?.status];
        const rejected = statuses.some(x => ['error', 'failed', 'cancelled', 'interrupted'].includes(x)) ||
          event?.error != null || event?.result?.error != null;
        const completed = statuses.includes('completed') ||
          (statuses.every(x => x == null) && event?.result != null);
        if (rejected || !completed || !Number.isInteger(code)) return;
        run.checks.set(checkId, { passed: code === 0, tick: ++run.tick });
        write(run, { event: 'check', check_id: checkId,
          exit_code: code, source: 'native-tool', policy_hash: run.policy.hash });
        return;
      }
      if (typeof id !== 'string' || !id) return;
      const run = existing(sid);
      if (!run) return;
      const agent = hash(id);
      if (!run.tasks.has(agent)) return;
      if (run.backgroundDispatches.has(agent) ||
          (event?.input ?? event?.data?.input)?.background === true) {
        // Tool-return only proves dispatch. A separate child's session finish
        // has no safe matching native tool-call ID in this adapter.
        run.invalidatedAt = ++run.tick;
        return;
      }
      run.tasks.delete(agent);
      const started = run.toolStarts.get(agent);
      run.toolStarts.delete(agent);
      if (event.status === 'completed' && Number.isFinite(started)) {
        run.toolSpans.push([started, Date.now()]);
      } else run.nativeSpanComplete = false;
      run.invalidatedAt = ++run.tick; // Test results must follow native agent completion.
      // A background task tool ends when the spawn request completes.
      // Its span is NOT proof of the child's total execution time.
      if (event.status === 'completed') {
        write(run, { event: 'agent-stop', agent_id: agent,
          native_hash: nativeHash(id) });
        await observeNative(id, 'stop');
      }
    });
    // The event stream can close unexpectedly while a native task is still
    // active. Retry the *subscription* with bounded exponential backoff, but
    // NEVER infer a missed execution terminal event or manufacture "finish".
    const controller = new AbortController();
    const waitRetry = (ms) => new Promise(resolve => {
      if (controller.signal.aborted) return resolve();
      const timer = setTimeout(() => { controller.signal.removeEventListener('abort', onAbort); resolve(); }, ms);
      const onAbort = () => { clearTimeout(timer); resolve(); };
      controller.signal.addEventListener('abort', onAbort, { once: true });
    });
    void (async () => {
      let retry = 0;
      while (!controller.signal.aborted) {
        try {
          for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
            retry = 0;
            const sid = session(event);
            if (!(await ownsSession(sid, event))) continue;
            if (event.type === 'session.execution.started' || event.type === 'session.created') {
              begin(sid);
            } else if (event.type === 'session.execution.succeeded') {
              await finish(sid, 'success');
            } else if (event.type === 'session.execution.failed') {
              await finish(sid, 'failed');
            } else if (event.type === 'session.execution.interrupted') {
              await finish(sid, 'cancelled');
            } else if (event.type === 'session.deleted') {
              const run = existing(sid);
              if (run) write(run, {event:'diagnostic', reason:'session_deleted_without_terminal',
                active_agents:run.tasks.size});
              abandon(sid);
            }
          }
          if (controller.signal.aborted) break;
          reportStreamIssue('event_stream_closed');
        } catch {
          if (controller.signal.aborted) break;
          // Log only a fixed diagnostic category; exception strings may
          // include secrets, prompt text or URLs.
          reportStreamIssue('event_stream_error');
        }
        await waitRetry(Math.min(5000, 200 * 2 ** Math.min(retry++, 5)));
      }
    })();
    return () => {
      if (!controller.signal.aborted) reportStreamIssue('plugin_unloaded');
      controller.abort();
    };
  },
};
