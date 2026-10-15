/** OpenCode V2 native hook adapter (no raw prompt/session data persisted).
 *
 * A globally loaded plugin instance can see events for other projects. Never
 * create a telemetry run from an event without an explicit matching location.
 */
import { createHash, randomUUID } from 'node:crypto';
import { appendFileSync, closeSync, mkdirSync, openSync } from 'node:fs';
import { join, resolve } from 'node:path';

export const POLICY = 'Auto-Orchestrator: For non-trivial coding tasks read the auto-orchestrator skill and delegate independent work to native subagents automatically; max three concurrent. Verify results and never invent timings or human time savings. Trivial tasks use one agent.';

const hash = (value) => createHash('sha256').update(String(value)).digest('hex').slice(0, 24);
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

    // A location belongs to the current plugin instance only if explicit.
    // The event stream is shared across workspace/plugin instances.
    const location = (event) =>
      event?.location?.directory ?? event?.data?.location?.directory ??
      (typeof event?.location === 'string' ? event.location : null);
    const own = (event) => !!directory && normalized(location(event)) === directory;
    const foreign = (event) => !!location(event) && !own(event);
    const data = (event) => event?.data ?? event?.properties ?? event ?? {};
    const session = (event) => data(event).sessionID ??
      data(event).session?.id ?? event?.sessionID ?? null;
    const toolName = (event) => event?.tool ?? data(event)?.tool ?? data(event)?.name;
    const callId = (event) => event?.id ?? event?.callID ?? data(event)?.callID ?? null;

    const write = (run, event) => {
      if (!base || !run || run.done) return;
      try {
        mkdirSync(base, { recursive: true });
        const file = join(base, run.id + '.jsonl');
        if (event.event === 'start') {
          const fd = openSync(file, 'wx', 0o600);
          closeSync(fd);
        }
        appendFileSync(file, JSON.stringify({ ...event, time: Date.now() / 1000 }) + '\n');
      } catch {
        // Telemetry is optional; never interrupt agent work or capture secrets.
      }
    };
    const begin = (sid, event) => {
      if (typeof sid !== 'string' || !sid || !own(event)) return null;
      const old = runs.get(sid);
      if (old && !old.done) return old;
      const run = { id: hash('opencode-v2|' + sid + '|' + randomUUID()), done: false, tasks: new Set() };
      runs.set(sid, run);
      write(run, { event: 'start', platform: 'opencode' });
      return run;
    };
    const existing = (sid, event) => {
      if (typeof sid !== 'string' || !sid || foreign(event)) return null;
      const run = runs.get(sid);
      return run && !run.done ? run : null;
    };
    const finish = (sid, status, event) => {
      const run = existing(sid, event);
      if (!run) return;
      write(run, { event: 'finish', status, verified: false });
      run.done = true;
      run.tasks.clear();
    };

    await ctx.session.hook('context', (event) => {
      if (event.agent?.startsWith('auto_')) return;
      event.system.push({ type: 'text', text: POLICY });
    });
    await ctx.tool.hook('execute.before', (event) => {
      if (toolName(event) !== 'subagent') return;
      const sid = session(event), id = callId(event);
      if (typeof id !== 'string' || !id) return;
      // Hook callbacks may omit location. Only trust them after this
      // session was explicitly associated with our own location.
      const run = existing(sid, event) ?? begin(sid, event);
      if (!run) return;
      const agent = hash(id);
      if (run.tasks.has(agent)) return;
      run.tasks.add(agent);
      write(run, { event: 'agent-start', agent_id: agent });
    });
    await ctx.tool.hook('execute.after', (event) => {
      if (toolName(event) !== 'subagent') return;
      const sid = session(event), id = callId(event);
      if (typeof id !== 'string' || !id) return;
      const run = existing(sid, event);
      if (!run) return;
      const agent = hash(id);
      if (!run.tasks.has(agent)) return;
      run.tasks.delete(agent);
      // A background task tool ends when the spawn request completes.
      // Its span is NOT proof of the child's total execution time.
      if (event.status === 'completed') write(run, { event: 'agent-stop', agent_id: agent });
    });
    const controller = new AbortController();
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          const sid = session(event);
          if (!sid || foreign(event)) continue;
          if (event.type === 'session.execution.started' || event.type === 'session.created') {
            // Never open a run in another project (or from location-less events).
            begin(sid, event);
          } else if (event.type === 'session.execution.succeeded') {
            finish(sid, 'success', event);
          } else if (event.type === 'session.execution.failed') {
            finish(sid, 'failed', event);
          } else if (event.type === 'session.execution.interrupted') {
            finish(sid, 'cancelled', event);
          }
        }
      } catch {
        // Aborted/incomplete streams must never imply a successful finish.
      }
    })();
    return () => controller.abort();
  },
};
