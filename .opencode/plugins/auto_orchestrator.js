/** OpenCode classic plugin: optional lifecycle telemetry and context reinforcement.
 * Only records timestamps, opaque identifiers, verification markers and statuses.
 * API: https://docs.opencode.ai/docs/plugins/ (V1/classic plugin API)
 */
import { createHash, randomUUID } from 'node:crypto';
import { mkdirSync, appendFileSync, openSync, closeSync } from 'node:fs';
import { join } from 'node:path';

const hash = (v) => createHash('sha256').update(String(v)).digest('hex').slice(0, 24);
const POLICY = 'Auto-Orchestrator: read .agents/skills/auto-orchestrator/SKILL.md for non-trivial coding tasks; delegate independent work automatically using native subagents, max three concurrent; do not invent timings.';

export const AutoOrchestratorPlugin = async ({ directory }) => {
  const runs = new Map();
  const base = join(directory, '.auto-orchestrator', 'runs');
  const write = (run, event) => {
    if (!run || run.done) return;
    const file = join(base, run.id + '.jsonl');
    try {
      mkdirSync(base, { recursive: true });
      if (event.event === 'start') {
        const fd = openSync(file, 'wx', 0o600);
        closeSync(fd);
      }
      appendFileSync(file, JSON.stringify({ ...event, time: Date.now() / 1000 }) + '\n');
    } catch { /* Never block IDE operation over optional telemetry */ }
  };
  const begin = (sid) => {
    if (!sid) return null;
    const existing = runs.get(sid);
    if (existing && !existing.done) return existing;
    const run = { id: hash(`opencode|${sid}|${randomUUID()}`), done: false, tasks: new Map() };
    runs.set(sid, run);
    write(run, { event: 'start', platform: 'opencode' });
    return run;
  };
  const sidFrom = (event) => event?.properties?.sessionID ?? event?.properties?.info?.id ?? event?.properties?.session?.id;
  return {
    'experimental.chat.system.transform': async (_input, output) => {
      output.system.push(POLICY);
    },
    event: async ({ event }) => {
      const sid = sidFrom(event);
      if (!sid) return;
      if (event.type === 'session.created' || (event.type === 'session.status' && event.properties?.status?.type === 'busy')) {
        begin(sid);
      } else if (event.type === 'session.idle' || event.type === 'session.error') {
        const run = runs.get(sid);
        if (run && !run.done) {
          // Unterminated agents must not yield valid savings in perf.py analyzer.
          write(run, { event: 'finish', status: event.type === 'session.error' ? 'failed' : 'success', verified: false });
          run.done = true;
        }
      }
    },
    'tool.execute.before': async (input) => {
      if (input?.tool !== 'task' || !input.sessionID || !input.callID) return;
      const run = begin(input.sessionID);
      const aid = hash(input.callID);
      if (run.tasks.has(aid)) return;
      run.tasks.set(aid, true);
      write(run, { event: 'agent-start', agent_id: aid });
    },
    'tool.execute.after': async (input) => {
      if (input?.tool !== 'task' || !input.sessionID || !input.callID) return;
      const run = runs.get(input.sessionID);
      const aid = hash(input.callID);
      if (!run || !run.tasks.has(aid)) return;
      run.tasks.delete(aid);
      write(run, { event: 'agent-stop', agent_id: aid });
    },
  };
};
