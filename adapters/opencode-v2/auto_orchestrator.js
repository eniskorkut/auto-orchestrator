/** OpenCode V2 native hook adapter (no raw prompt/session data persisted).
 *
 * A globally loaded plugin instance can see events for other projects. Never
 * create a telemetry run from an event without an explicit matching location.
 */
import { createHash, randomUUID } from 'node:crypto';
import { appendFileSync, closeSync, mkdirSync, openSync, readFileSync, lstatSync } from 'node:fs';
import { join, resolve } from 'node:path';

export const POLICY = 'Auto-Orchestrator: For non-trivial coding tasks read the auto-orchestrator skill and delegate independent work to native subagents automatically; max three concurrent. Verify results and never invent timings or human time savings. Trivial tasks use one agent.';

const hash = (value) => createHash('sha256').update(String(value)).digest('hex').slice(0, 24);

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
    const approvedTools = new Set(['bash', 'shell', 'powershell']);
    const mutatingTools = new Set(['edit', 'write', 'apply_patch', 'multiedit']);
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
      const run = runs.get(sid);
      if (!run || run.done) return;
      run.done = true;
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
    const finish = async (sid, status) => {
      const run = existing(sid);
      if (!run) return;
      const finishRecorded = write(run, { event: 'finish', status, verified: false });
      let verified = false;
      const currentPolicy = policyForRun();
      if (finishRecorded && status === 'success' && run.policy && currentPolicy &&
          currentPolicy.hash === run.policy.hash && run.tasks.size === 0 &&
          run.pendingChecks.size === 0 &&
          run.policy.required_checks.every(check =>
            run.checks.get(check.id)?.passed === true &&
            run.checks.get(check.id)?.tick > run.invalidatedAt)) {
        verified = write(run, { event: 'verified', source: 'quality-gate',
          check_ids: run.policy.required_checks.map(c => c.id).sort() });
      }
      run.done = true;
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

    await ctx.session.hook('context', (event) => {
      if (event.agent?.startsWith('auto_')) return;
      event.system.push({ type: 'text', text: POLICY });
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
        // OpenCode Desktop v2.0.28 reports shell working directory as
        // input.workdir, not input.cwd. Missing/foreign cwd is not evidence.
        const cwd = input.workdir ?? input.cwd;
        const check = run.policy && typeof command === 'string' &&
          run.policy.required_checks.find(c => c.tool === name && c.command === command);
        if (!check || typeof id !== 'string' || !id ||
            normalized(cwd) !== directory) {
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
      write(run, { event: 'agent-start', agent_id: agent });
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
        // Tool "completed" without an actual native integer exit code is NOT evidence.
        if (event.status !== 'completed' || !Number.isInteger(code)) return;
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
      if (run.backgroundDispatches.has(agent)) {
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
      if (event.status === 'completed') write(run, { event: 'agent-stop', agent_id: agent });
    });
    const controller = new AbortController();
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          const sid = session(event);
          if (!(await ownsSession(sid, event))) continue;
          if (event.type === 'session.execution.started' || event.type === 'session.created') {
            // Never open a run in another project (or from location-less events).
            begin(sid);
          } else if (event.type === 'session.execution.succeeded') {
            await finish(sid, 'success');
          } else if (event.type === 'session.execution.failed') {
            await finish(sid, 'failed');
          } else if (event.type === 'session.execution.interrupted') {
            await finish(sid, 'cancelled');
          }
        }
      } catch {
        // Aborted/incomplete streams must never imply a successful finish.
      }
    })();
    return () => controller.abort();
  },
};
