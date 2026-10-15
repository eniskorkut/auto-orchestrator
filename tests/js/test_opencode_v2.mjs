import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, readdirSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import plugin from '../../adapters/opencode-v2/auto_orchestrator.js';

const tick = async () => { await new Promise(resolve => setImmediate(resolve)); await new Promise(resolve => setImmediate(resolve)); };

async function sandbox(fn) {
  const root = mkdtempSync(join(tmpdir(), 'orchestrator-v2-'));
  const project = join(root, 'project');
  const foreignProject = join(root, 'other-project');
  const hooks = {};
  const pending = [];
  let release;
  const ctx = {
    location: { directory: project },
    session: { hook: async (name, callback) => { hooks[name] = callback; } },
    tool: { hook: async (name, callback) => { hooks[name] = callback; } },
    event: { subscribe: ({ signal }) => ({
      async *[Symbol.asyncIterator]() {
        while (!signal.aborted) {
          if (pending.length) yield pending.shift();
          else await new Promise(resolve => {
            release = resolve;
            signal.addEventListener('abort', resolve, { once: true });
          });
        }
      },
    }) },
  };
  const emit = async event => { pending.push(event); release?.(); await tick(); };
  const read = (folder = project) => {
    const dir = join(folder, '.auto-orchestrator', 'runs');
    return existsSync(dir)
      ? readdirSync(dir).map(name => readFileSync(join(dir, name), 'utf8').trim().split('\n').map(x => JSON.parse(x)))
      : [];
  };
  try {
    const dispose = await plugin.setup(ctx);
    try { await fn({ project, foreignProject, hooks, emit, read }); }
    finally { dispose(); }
  } finally { rmSync(root, { recursive: true, force: true }); }
}

const lifecycle = (type, sessionID, directory, details = {}) => ({
  id: 'evt-' + type + '-' + sessionID,
  type, ...(directory === undefined ? {} : { location: { directory } }),
  data: { sessionID, ...details },
});
const subagent = (id, sessionID, status, directory) => ({
  id, tool: 'subagent', status, ...(directory === undefined ? {} : { location: { directory } }),
  data: { sessionID, prompt: 'SENSITIVE-PROMPT', agent: 'auto_explorer' },
});

test('V2 context hook and live native subagent event spans', async () => {
  await sandbox(async ({ project, hooks, emit, read }) => {
    assert.equal(plugin.id, 'auto-orchestrator');
    const context = { system: [], agent: 'build' };
    await hooks.context(context);
    assert.match(context.system[0].text, /native subagents/);
    const child = { system: [], agent: 'auto_tester' };
    await hooks.context(child);
    assert.equal(child.system.length, 0);

    await emit(lifecycle('session.execution.started', 'parent-session-sensitive', project));
    await hooks['execute.before'](subagent('tool-1', 'parent-session-sensitive'));
    await hooks['execute.before'](subagent('tool-2', 'parent-session-sensitive'));
    await hooks['execute.after'](subagent('tool-1', 'parent-session-sensitive', 'completed'));
    await hooks['execute.after'](subagent('tool-2', 'parent-session-sensitive', 'completed'));
    await emit(lifecycle('session.execution.succeeded', 'parent-session-sensitive', project));
    const runs = read();
    assert.equal(runs.length, 1);
    const events = runs[0];
    assert.deepEqual(events.map(x => x.event),
      ['start', 'agent-start', 'agent-start', 'agent-stop', 'agent-stop', 'finish']);
    assert.equal(events.at(-1).verified, false);
    assert.equal(events.at(-1).status, 'success');
    const raw = JSON.stringify(events);
    assert.doesNotMatch(raw, /parent-session-sensitive|tool-1|tool-2|SENSITIVE-PROMPT/);
  });
});

test('V2 location isolation: cross-project activity cannot create telemetry', async () => {
  await sandbox(async ({ project, foreignProject, hooks, emit, read }) => {
    await emit(lifecycle('session.execution.started', 'foreign-session', foreignProject));
    await hooks['execute.before'](subagent('tool-foreign', 'foreign-session', undefined, foreignProject));
    await hooks['execute.after'](subagent('tool-foreign', 'foreign-session', 'completed', foreignProject));
    await emit(lifecycle('session.execution.succeeded', 'foreign-session', foreignProject));
    await emit(lifecycle('session.execution.started', 'no-location'));
    await hooks['execute.before'](subagent('tool-unscoped', 'no-location'));
    await emit(lifecycle('session.execution.succeeded', 'no-location'));
    assert.equal(read(project).length, 0);
    assert.equal(read(foreignProject).length, 0);
  });
});

test('V2 foreign callbacks never update an active local session', async () => {
  await sandbox(async ({ project, foreignProject, hooks, emit, read }) => {
    await emit(lifecycle('session.execution.started', 'local', project));
    await hooks['execute.before'](subagent('first', 'local', undefined, foreignProject));
    await hooks['execute.after'](subagent('first', 'local', 'completed', foreignProject));
    await emit(lifecycle('session.execution.succeeded', 'local', foreignProject));
    const runs = read();
    assert.equal(runs.length, 1);
    assert.deepEqual(runs[0].map(x => x.event), ['start']);
    await emit(lifecycle('session.execution.succeeded', 'local', project));
    assert.deepEqual(read()[0].map(x => x.event), ['start', 'finish']);
  });
});

test('V2 terminal failures and interruptions do not imply verified success', async () => {
  await sandbox(async ({ project, emit, read }) => {
    await emit(lifecycle('session.execution.started', 'failed', project));
    await emit(lifecycle('session.execution.failed', 'failed', project));
    await emit(lifecycle('session.execution.started', 'interrupted', project));
    await emit(lifecycle('session.execution.interrupted', 'interrupted', project));
    const statuses = read().map(run => run.at(-1).status).sort();
    assert.deepEqual(statuses, ['cancelled', 'failed']);
    assert.ok(read().every(run => run.at(-1).verified === false));
  });
});

test('V2 older task tool and session.idle events do not fabricate native execution', async () => {
  await sandbox(async ({ project, hooks, emit, read }) => {
    await emit(lifecycle('session.execution.started', 'legacy', project));
    await hooks['execute.before']({ tool: 'task', sessionID: 'legacy', callID: 'v1-only' });
    await hooks['execute.after']({ tool: 'task', sessionID: 'legacy', callID: 'v1-only', status: 'completed' });
    await emit(lifecycle('session.idle', 'legacy', project));
    assert.deepEqual(read()[0].map(x => x.event), ['start']);
    await emit(lifecycle('session.execution.succeeded', 'legacy', project));
    assert.deepEqual(read()[0].map(x => x.event), ['start', 'finish']);
  });
});

test('V2 native tool callback can start only with explicit matching location', async () => {
  await sandbox(async ({ project, hooks, read }) => {
    await hooks['execute.before'](subagent('call1', 'without-location'));
    assert.equal(read().length, 0);
    await hooks['execute.before'](subagent('call2', 'with-location', undefined, project));
    await hooks['execute.after'](subagent('call2', 'with-location', 'completed'));
    assert.deepEqual(read()[0].map(x => x.event), ['start', 'agent-start', 'agent-stop']);
  });
});

test('V2 repeated turns create separate runs and do not reuse stale spans', async () => {
  await sandbox(async ({ project, hooks, emit, read }) => {
    for (let i = 0; i < 2; i++) {
      await emit(lifecycle('session.execution.started', 'same-session', project));
      await hooks['execute.before'](subagent('same-tool', 'same-session'));
      await hooks['execute.after'](subagent('same-tool', 'same-session', 'completed'));
      await emit(lifecycle('session.execution.succeeded', 'same-session', project));
    }
    assert.equal(read().length, 2);
    assert.ok(read().every(run => run.length === 4));
  });
});
