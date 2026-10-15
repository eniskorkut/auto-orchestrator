import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, readdirSync, rmSync, existsSync, mkdirSync, writeFileSync } from 'node:fs';
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


const enableGate = project => {
  const dir = join(project, '.auto-orchestrator');
  mkdirSync(dir, {recursive:true});
  writeFileSync(join(dir, 'verification.json'), JSON.stringify({
    version:1, auto_verify:true,
    required_checks:[{id:'unit',tool:'bash',command:'python -m pytest -q'}],
  }));
};
const testCommand = (id, sid, code=0, directory) => ({
  id, tool:'bash', status:'completed',
  ...(directory ? {location:{directory}} : {}),
  data:{sessionID:sid},input:{command:'python -m pytest -q',workdir:directory},
  result:{metadata:{exitCode:code}},
});

test('V5 auto verifies only after configured native tool exit and native agent completion', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','verified-run',project));
    await hooks['execute.before'](subagent('agent-1','verified-run'));
    await hooks['execute.after'](subagent('agent-1','verified-run','completed'));
    await hooks['execute.before'](testCommand('test-1','verified-run',0,project));
    await hooks['execute.after'](testCommand('test-1','verified-run',0,project));
    await emit(lifecycle('session.execution.succeeded','verified-run',project));
    const events=read()[0];
    assert.deepEqual(events.map(e=>e.event), ['start','agent-start','agent-stop','check','finish','verified']);
    assert.equal(events[3].check_id,'unit');
    assert.equal(events[3].exit_code,0);
    assert.equal(events[3].source,'native-tool');
    assert.equal(events[5].source,'quality-gate');
    assert.deepEqual(events[5].check_ids,['unit']);
    assert.doesNotMatch(JSON.stringify(events),/python -m pytest|verified-run|test-1/);
  });
});

test('V5 rejects failed native check even when execution succeeds', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','failed-check',project));
    await hooks['execute.before'](testCommand('test1','failed-check',0,project));
    await hooks['execute.after'](testCommand('test1','failed-check',1,project));
    await emit(lifecycle('session.execution.succeeded','failed-check',project));
    assert.deepEqual(read()[0].map(e=>e.event),['start','check','finish']);
  });
});

test('V5 missing exit code and later modifications fail closed', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','unknown-exit',project));
    await hooks['execute.before'](testCommand('test1','unknown-exit',0,project));
    await hooks['execute.after']({...testCommand('test1','unknown-exit',0,project),result:{metadata:{}}});
    await emit(lifecycle('session.execution.succeeded','unknown-exit',project));
    assert.deepEqual(read()[0].map(e=>e.event),['start','finish']);
    await emit(lifecycle('session.execution.started','stale',project));
    await hooks['execute.before'](testCommand('test2','stale',0,project));
    await hooks['execute.after'](testCommand('test2','stale',0,project));
    await hooks['execute.after']({id:'edit1',tool:'edit',data:{sessionID:'stale'},status:'completed'});
    await emit(lifecycle('session.execution.succeeded','stale',project));
    const stale=read().find(es=>es.some(e=>e.event==='mutation'));
    assert.deepEqual(stale.map(e=>e.event),['start','check','mutation','finish']);
  });
});

test('V5 foreign project and unapproved test command never add evidence', async () => {
  await sandbox(async ({project,foreignProject,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','local',project));
    await hooks['execute.before'](testCommand('foreign','local',0,foreignProject));
    await hooks['execute.after'](testCommand('foreign','local',0,foreignProject));
    const invalid={...testCommand('wrong','local',0,project),input:{command:'echo success'}};
    await hooks['execute.before'](invalid);
    await hooks['execute.after'](invalid);
    await emit(lifecycle('session.execution.succeeded','local',project));
    assert.deepEqual(read()[0].map(e=>e.event),['start','mutation','finish']);
    assert.equal(read(foreignProject).length,0);
  });
});

test('V5 requires check to run after native child completion', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','late-agent',project));
    await hooks['execute.before'](testCommand('test1','late-agent',0,project));
    await hooks['execute.after'](testCommand('test1','late-agent',0,project));
    await hooks['execute.before'](subagent('agent1','late-agent'));
    await hooks['execute.after'](subagent('agent1','late-agent','completed'));
    await emit(lifecycle('session.execution.succeeded','late-agent',project));
    assert.deepEqual(read()[0].map(e=>e.event),
      ['start','check','agent-start','agent-stop','finish']);
  });
});


test('V5 policy changes during execution invalidate observed native checks', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','policy-change',project));
    await hooks['execute.before'](testCommand('test1','policy-change',0,project));
    await hooks['execute.after'](testCommand('test1','policy-change',0,project));
    writeFileSync(join(project,'.auto-orchestrator','verification.json'),
      JSON.stringify({version:1,auto_verify:true,required_checks:[
        {id:'unit',tool:'bash',command:'python -m unittest discover -s tests'}
      ]}));
    await emit(lifecycle('session.execution.succeeded','policy-change',project));
    assert.deepEqual(read()[0].map(e=>e.event),['start','check','finish']);
  });
});

test('V5 failed edit tools also invalidate earlier passed checks', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','error-edit',project));
    await hooks['execute.before'](testCommand('test1','error-edit',0,project));
    await hooks['execute.after'](testCommand('test1','error-edit',0,project));
    await hooks['execute.after']({id:'edit1',tool:'edit',status:'error',
      data:{sessionID:'error-edit'}});
    await emit(lifecycle('session.execution.succeeded','error-edit',project));
    assert.deepEqual(read()[0].map(e=>e.event),['start','check','mutation','finish']);
  });
});


test('V5 real Desktop shell shape: result.metadata.exit and input.workdir', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','desktop-exit',project));
    const real = {
      id:'desktop-1', tool:'bash', data:{sessionID:'desktop-exit'},
      input:{command:'python -m pytest -q',workdir:project},
      status:'completed',
      result:{output:'secret output must not be logged',content:[],
        metadata:{status:'completed',truncated:false,exit:0}},
    };
    await hooks['execute.before'](real);
    await hooks['execute.after'](real);
    await emit(lifecycle('session.execution.succeeded','desktop-exit',project));
    const events=read()[0];
    assert.deepEqual(events.map(x=>x.event),['start','check','finish','verified']);
    assert.equal(events[1].exit_code,0);
    assert.doesNotMatch(JSON.stringify(events),/secret output|desktop-exit|python -m pytest/);
  });
});

test('V5 rejects nonzero Desktop exit and foreign input.workdir even with matching location', async () => {
  await sandbox(async ({project,foreignProject,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','desktop-failed',project));
    const failed=testCommand('bad','desktop-failed',1,project);
    failed.result.metadata={status:'completed',truncated:false,exit:1};
    await hooks['execute.before'](failed);
    await hooks['execute.after'](failed);
    await emit(lifecycle('session.execution.succeeded','desktop-failed',project));

    await emit(lifecycle('session.execution.started','foreign-cwd',project));
    const foreign=testCommand('foreign-tool','foreign-cwd',0,foreignProject);
    // The event's owning project is correct but shell actually runs elsewhere.
    foreign.location={directory:project};
    foreign.result.metadata={status:'completed',exit:0};
    await hooks['execute.before'](foreign);
    await hooks['execute.after'](foreign);
    await emit(lifecycle('session.execution.succeeded','foreign-cwd',project));
    const runs=read();
    assert.equal(runs.length,2);
    assert.deepEqual(runs.map(r=>r.some(e=>e.event==='verified')), [false,false]);
    assert.ok(runs.some(r=>r.some(e=>e.event==='check'&&e.exit_code===1)));
    assert.ok(runs.some(r=>r.some(e=>e.event==='mutation')));
    assert.equal(read(foreignProject).length,0);
  });
});

test('V5 shell file edits after a successful check revoke stale verification evidence', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','shell-edit',project));
    const passed=testCommand('test-before','shell-edit',0,project);
    passed.result.metadata={exit:0};
    await hooks['execute.before'](passed);
    await hooks['execute.after'](passed);
    const mutation={
      id:'shell-edit-command',tool:'bash',data:{sessionID:'shell-edit'},
      input:{command:'node -e "require(\'fs\').writeFileSync(\'source.py\', \'changed\')"',workdir:project},
      status:'completed',result:{metadata:{exit:0}},
    };
    await hooks['execute.before'](mutation);
    await hooks['execute.after'](mutation);
    await emit(lifecycle('session.execution.succeeded','shell-edit',project));
    assert.deepEqual(read()[0].map(e=>e.event),
      ['start','check','mutation','finish']);
  });
});

test('V5 a new approved check after shell edits restores verification', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','recheck-shell',project));
    const check1=testCommand('before-edit','recheck-shell',0,project);
    await hooks['execute.before'](check1);
    await hooks['execute.after'](check1);
    await hooks['execute.before']({
      id:'unapproved-shell',tool:'bash',data:{sessionID:'recheck-shell'},
      input:{command:'Write-Output changed',workdir:project},
    });
    const check2=testCommand('after-edit','recheck-shell',0,project);
    await hooks['execute.before'](check2);
    await hooks['execute.after'](check2);
    await emit(lifecycle('session.execution.succeeded','recheck-shell',project));
    assert.deepEqual(read()[0].map(e=>e.event),
      ['start','check','mutation','check','finish','verified']);
  });
});

test('V5 a repeated native check with missing exit cannot inherit its previous pass', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','repeat-no-exit',project));
    const first=testCommand('first','repeat-no-exit',0,project);
    await hooks['execute.before'](first);
    await hooks['execute.after'](first);
    const second=testCommand('second','repeat-no-exit',0,project);
    second.result={metadata:{status:'completed'}};
    await hooks['execute.before'](second);
    await hooks['execute.after'](second);
    await emit(lifecycle('session.execution.succeeded','repeat-no-exit',project));
    assert.deepEqual(read()[0].map(e=>e.event),['start','check','finish']);
  });
});


test('V5 approved test cannot pass if an unapproved shell operation begins while it is running', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','overlap-shell',project));
    const testEvent=testCommand('long-test','overlap-shell',0,project);
    await hooks['execute.before'](testEvent);
    // The test result arrives after an unrelated shell operation started.
    await hooks['execute.before']({
      id:'shell-during-test',tool:'bash',data:{sessionID:'overlap-shell'},
      input:{command:'echo unrelated',workdir:project},
    });
    await hooks['execute.after'](testEvent);
    await emit(lifecycle('session.execution.succeeded','overlap-shell',project));
    assert.deepEqual(read()[0].map(e=>e.event),['start','mutation','finish']);
  });
});
