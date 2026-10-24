import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, readdirSync, rmSync, existsSync, mkdirSync, writeFileSync, copyFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import plugin, { observedOverlapMs } from '../../adapters/opencode-v2/auto_orchestrator.js';

const tick = async () => { await new Promise(resolve => setImmediate(resolve)); await new Promise(resolve => setImmediate(resolve)); };

async function sandbox(fn, getSession = null, options = {}) {
  const root = mkdtempSync(join(tmpdir(), 'orchestrator-v2-'));
  const project = join(root, 'project');
  const foreignProject = join(root, 'other-project');
  const hooks = {};
  const toasts = [];
  const pending = [];
  let release;
  const ctx = {
    location: { directory: project },
    tui: options.tuiMissing ? undefined : {
      showToast: async value => {
        if (options.tuiThrows) throw new Error('TUI not connected');
        toasts.push(value);
        return true;
      },
    },
    session: {
      hook: async (name, callback) => { hooks[name] = callback; },
      ...(getSession ? {get: async ({sessionID}) => getSession(sessionID)} : {}),
    },
    skill: { list: async () => ({ data: options.skillList ?? [] }) },
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
    const dispose = await (options.pluginImpl ?? plugin).setup(ctx);
    try { await fn({ project, foreignProject, hooks, emit, read, toasts }); }
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
      ['start','check','mutation','mutation','check','finish','verified']);
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
    assert.deepEqual(read()[0].map(e=>e.event),['start','check','mutation','finish']);
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


test('V5 moved parent starts native run using authoritative session.location without event location', async () => {
  const owners = new Map([['moved-parent', null]]);
  await sandbox(async ({project,foreignProject,hooks,emit,read}) => {
    owners.set('moved-parent', project);
    enableGate(project);
    // The native session was moved from a different project; its event still
    // has the old location, but session.get returns the current canonical one.
    await emit(lifecycle('session.execution.started','moved-parent',foreignProject));
    const check = testCommand('moved-test','moved-parent',0,project);
    delete check.location;
    await hooks['execute.before'](check);
    await hooks['execute.after'](check);
    await emit(lifecycle('session.execution.succeeded','moved-parent'));
    assert.deepEqual(read()[0].map(x=>x.event),['start','check','finish','verified']);
    assert.equal(read(foreignProject).length,0);
  }, sid => ({id:sid,location:{directory:owners.get(sid)}}));
});

test('V5 forged event location cannot replace authoritative foreign session ownership', async () => {
  const owners = new Map();
  await sandbox(async ({project,foreignProject,hooks,emit,read}) => {
    owners.set('foreign', foreignProject);
    await emit(lifecycle('session.execution.started','foreign',project));
    await hooks['execute.before'](subagent('foreign-tool','foreign',undefined,project));
    await hooks['execute.after'](subagent('foreign-tool','foreign','completed',project));
    await emit(lifecycle('session.execution.succeeded','foreign',project));
    assert.equal(read().length,0);
    assert.equal(read(foreignProject).length,0);
  }, sid => ({id:sid,location:{directory:owners.get(sid)}}));
});

test('V5 does not use shell workdir as a substitute for session ownership', async () => {
  const owners = new Map();
  await sandbox(async ({project,foreignProject,hooks,emit,read}) => {
    owners.set('default-project-parent', foreignProject);
    enableGate(project);
    await emit(lifecycle('session.execution.started','default-project-parent',project));
    const check = testCommand('other-cwd','default-project-parent',0,project);
    await hooks['execute.before'](check);
    await hooks['execute.after'](check);
    await emit(lifecycle('session.execution.succeeded','default-project-parent',project));
    assert.equal(read(project).length,0);
    assert.equal(read(foreignProject).length,0);
  }, sid => ({id:sid,location:{directory:owners.get(sid)}}));
});

test('V5 fail-closed when authoritative session lookup is missing or errors', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    await emit(lifecycle('session.execution.started','missing-parent',project));
    await hooks['execute.before'](subagent('tool','missing-parent',undefined,project));
    await emit(lifecycle('session.execution.succeeded','missing-parent',project));
    assert.equal(read().length,0);
  }, async () => {throw new Error('unavailable')});
});

test('V5 ignores a moved-out session after a run started under this plugin scope', async () => {
  const owners = new Map();
  await sandbox(async ({project,foreignProject,hooks,emit,read}) => {
    owners.set('moving',project);
    enableGate(project);
    await emit(lifecycle('session.execution.started','moving',project));
    owners.set('moving',foreignProject);
    const check=testCommand('wrong-after-move','moving',0,project);
    await hooks['execute.before'](check);
    await hooks['execute.after'](check);
    await emit(lifecycle('session.execution.succeeded','moving',project));
    assert.deepEqual(read()[0].map(e=>e.event),['start']);
    assert.equal(read(foreignProject).length,0);
  }, sid => ({id:sid,location:{directory:owners.get(sid)}}));
});

test('V5 retains current opt-in policy for directly created parent with session.get', async () => {
  const owners = new Map();
  await sandbox(async ({project,hooks,emit,read}) => {
    owners.set('direct-parent',project);
    enableGate(project);
    await emit(lifecycle('session.execution.started','direct-parent'));
    const cmd=testCommand('approved','direct-parent',0,project);
    await hooks['execute.before'](cmd);
    await hooks['execute.after'](cmd);
    await emit(lifecycle('session.execution.succeeded','direct-parent'));
    assert.deepEqual(read()[0].map(e=>e.event),['start','check','finish','verified']);
  }, sid => ({id:sid,location:{directory:owners.get(sid)}}));
});


test('V5 moving away and back does not reuse stale check evidence from the prior location epoch', async () => {
  const owners = new Map();
  await sandbox(async ({project,foreignProject,hooks,emit,read}) => {
    owners.set('roundtrip-parent',project);
    enableGate(project);
    await emit(lifecycle('session.execution.started','roundtrip-parent',project));
    const passed=testCommand('initial-test','roundtrip-parent',0,project);
    await hooks['execute.before'](passed);
    await hooks['execute.after'](passed);
    owners.set('roundtrip-parent',foreignProject);
    await emit(lifecycle('session.execution.started','roundtrip-parent',foreignProject));
    owners.set('roundtrip-parent',project);
    await emit(lifecycle('session.execution.started','roundtrip-parent',project));
    await emit(lifecycle('session.execution.succeeded','roundtrip-parent',project));
    const entries=read();
    assert.equal(entries.length,2);
    assert.ok(entries.some(events=>events.map(x=>x.event).join(',')==='start,check'));
    assert.ok(entries.some(events=>events.map(x=>x.event).join(',')==='start,finish'));
    assert.ok(entries.every(events=>!events.some(x=>x.event==='verified')));
  }, sid => ({id:sid,location:{directory:owners.get(sid)}}));
});

test('V5 native session lookup error abandons old proof before recovery', async () => {
  let broken=false;
  let owner='';
  await sandbox(async ({project,hooks,emit,read}) => {
    owner=project;
    enableGate(project);
    await emit(lifecycle('session.execution.started','temporary-failure',project));
    const passed=testCommand('old-proof','temporary-failure',0,project);
    await hooks['execute.before'](passed);
    await hooks['execute.after'](passed);
    broken=true;
    await hooks['execute.before'](subagent('unknown','temporary-failure'));
    broken=false;
    await emit(lifecycle('session.execution.started','temporary-failure',project));
    await emit(lifecycle('session.execution.succeeded','temporary-failure',project));
    const entries=read();
    assert.equal(entries.length,2);
    assert.ok(entries.every(x=>!x.some(e=>e.event==='verified')));
  }, sid => {
    if (broken) throw new Error('native lookup failed');
    return {id:sid,location:{directory:owner}};
  });
});


test('IDE measured overlap uses union of intervals, not wall-time savings', () => {
  assert.equal(observedOverlapMs([[0,100],[50,180],[220,250]]),50);
  assert.equal(observedOverlapMs([[0,100],[0,100],[0,100]]),200);
  assert.equal(observedOverlapMs([[0,100],[100,200]]),0);
  assert.equal(observedOverlapMs([[200,100],[1,2]]),0);
  assert.equal(observedOverlapMs([[1,Infinity],[1,2]]),0);
  assert.equal(observedOverlapMs([]),0);
});

test('IDE toast is emitted once only after automatic verified evidence', async () => {
  await sandbox(async ({ project, hooks, emit, read, toasts }) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','toast-success',project));
    for (const id of ['one','two']) {
      await hooks['execute.before'](subagent(id,'toast-success'));
      await hooks['execute.after'](subagent(id,'toast-success','completed'));
    }
    const check=testCommand('test','toast-success',0,project);
    await hooks['execute.before'](check);
    await hooks['execute.after'](check);
    await emit(lifecycle('session.execution.succeeded','toast-success',project));
    assert.equal(read()[0].at(-1).event,'verified');
    assert.equal(toasts.length,1);
    assert.match(toasts[0].body.message,/doğrulandı/);
    assert.doesNotMatch(JSON.stringify(toasts),/toast-success|test-1|SENSITIVE-PROMPT|tasarruf edildi/);
  });
});

test('IDE never reports overlap on failed, absent or rejected verification', async () => {
  await sandbox(async ({ project, hooks, emit, toasts }) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','toast-no',project));
    await hooks['execute.before'](subagent('one','toast-no'));
    await hooks['execute.after'](subagent('one','toast-no','completed'));
    await emit(lifecycle('session.execution.succeeded','toast-no',project));
    await emit(lifecycle('session.execution.started','toast-failed',project));
    await emit(lifecycle('session.execution.failed','toast-failed',project));
    assert.equal(toasts.length,0);
  });
});

test('IDE toast unavailable or failing never breaks verified native run', async () => {
  // Most non-GUI OpenCode V2 installations may not expose a TUI at all.
  await sandbox(async ({project,hooks,emit,read,toasts}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','toast-safe',project));
    const check=testCommand('native','toast-safe',0,project);
    await hooks['execute.before'](check);
    await hooks['execute.after'](check);
    await emit(lifecycle('session.execution.succeeded','toast-safe',project));
    assert.equal(read()[0].at(-1).event,'verified');
    assert.equal(toasts.length,0);
  }, null, {tuiMissing:true});
  await sandbox(async ({project,hooks,emit,read,toasts}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','toast-throw',project));
    const check=testCommand('native','toast-throw',0,project);
    await hooks['execute.before'](check);
    await hooks['execute.after'](check);
    await emit(lifecycle('session.execution.succeeded','toast-throw',project));
    assert.equal(read()[0].at(-1).event,'verified');
    assert.equal(toasts.length,0);
  }, null, {tuiThrows:true});
});


test('V6 background subagent tool completion cannot verify while child lifetime unproven', async () => {
  await sandbox(async ({project,hooks,emit,read,toasts}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','background-open',project));
    const request={...subagent('background-spawn','background-open'), input:{background:true}};
    await hooks['execute.before'](request);
    await hooks['execute.after']({...request,status:'completed',result:{metadata:{status:'completed'}}});
    const check=testCommand('approved','background-open',0,project);
    await hooks['execute.before'](check);
    await hooks['execute.after'](check);
    await emit(lifecycle('session.execution.succeeded','background-open',project));
    const events=read()[0];
    assert.deepEqual(events.map(e=>e.event),['start','agent-start','check','finish']);
    assert.ok(!events.some(e=>e.event==='agent-stop' || e.event==='verified'));
    assert.equal(toasts.length,0);
  });
});

test('V6 native failed subagent tool cannot be followed by a verified parent', async () => {
  await sandbox(async ({project,hooks,emit,read,toasts}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','native-failed',project));
    await hooks['execute.before'](subagent('bad-dispatch','native-failed'));
    await hooks['execute.after'](subagent('bad-dispatch','native-failed','error'));
    const check=testCommand('after-failure','native-failed',0,project);
    await hooks['execute.before'](check);
    await hooks['execute.after'](check);
    await emit(lifecycle('session.execution.succeeded','native-failed',project));
    const events=read()[0];
    assert.deepEqual(events.map(e=>e.event),['start','agent-start','check','finish']);
    assert.equal(toasts.length,0);
  });
});

test('V6 foreground native subagent tool still permits quality-gated verification', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    enableGate(project);
    await emit(lifecycle('session.execution.started','foreground-fine',project));
    const foreground={...subagent('fg-agent','foreground-fine'),input:{background:false}};
    await hooks['execute.before'](foreground);
    await hooks['execute.after']({...foreground,status:'completed'});
    const check=testCommand('ok','foreground-fine',0,project);
    await hooks['execute.before'](check);
    await hooks['execute.after'](check);
    await emit(lifecycle('session.execution.succeeded','foreground-fine',project));
    assert.deepEqual(read()[0].map(e=>e.event),
      ['start','agent-start','agent-stop','check','finish','verified']);
  });
});

test('V6 OpenCode 2.1 shell command-only shape verifies with authoritative session lookup', async () => {
  let owner;
  await sandbox(async ({project,hooks,emit,read}) => {
    owner = project;
    enableGate(project);
    writeFileSync(join(project,'.auto-orchestrator','verification.json'), JSON.stringify({
      version:1,auto_verify:true,
      required_checks:[{id:'unit',tool:'shell',command:'python -m pytest -q'}],
    }));
    await emit(lifecycle('session.execution.started','shell-native',project));
    // Observed v2.1.x wrapper: input has command only; no cwd, no status.
    const native = { id:'native-command-1',tool:'shell',sessionID:'shell-native',
      input:{command:'python -m pytest -q'},result:{metadata:{exit:0}} };
    await hooks['execute.before'](native);
    await hooks['execute.after'](native);
    await emit(lifecycle('session.execution.succeeded','shell-native',project));
    assert.deepEqual(read()[0].map(e=>e.event),['start','check','finish','verified']);
  }, () => ({location:{directory:owner}}));
});

test('V6 shell command-only without authoritative lookup cannot verify', async () => {
  await sandbox(async ({project,hooks,emit,read}) => {
    enableGate(project);
    writeFileSync(join(project,'.auto-orchestrator','verification.json'), JSON.stringify({
      version:1,auto_verify:true,
      required_checks:[{id:'unit',tool:'shell',command:'python -m pytest -q'}],
    }));
    await emit(lifecycle('session.execution.started','untrusted-default',project));
    const native = {id:'native-command-2',tool:'shell',
      location:{directory:project},data:{sessionID:'untrusted-default'},
      input:{command:'python -m pytest -q'},result:{metadata:{exit:0}}};
    await hooks['execute.before'](native);
    await hooks['execute.after'](native);
    await emit(lifecycle('session.execution.succeeded','untrusted-default',project));
    assert.deepEqual(read()[0].map(e=>e.event),['start','mutation','finish']);
  });
});

test('V6 shell command-only rejects explicit native failure and moved sessions', async () => {
  let owner;
  await sandbox(async ({project,foreignProject,hooks,emit,read}) => {
    owner=project;
    enableGate(project);
    writeFileSync(join(project,'.auto-orchestrator','verification.json'), JSON.stringify({
      version:1,auto_verify:true,
      required_checks:[{id:'unit',tool:'shell',command:'python -m pytest -q'}],
    }));
    await emit(lifecycle('session.execution.started','error-shell',project));
    const native = {id:'failed-shell-1',tool:'shell',data:{sessionID:'error-shell'},
      input:{command:'python -m pytest -q'},result:{metadata:{exit:0,status:'failed'}}};
    await hooks['execute.before'](native);
    await hooks['execute.after'](native);
    await emit(lifecycle('session.execution.succeeded','error-shell',project));
    assert.deepEqual(read()[0].map(e=>e.event),['start','finish']);
    owner=foreignProject;
    await emit(lifecycle('session.execution.started','error-shell',foreignProject));
    assert.equal(read().length,1);
  },()=>({location:{directory:owner}}));
});

test('V6 OpenCode plugin forwards hashed native lifecycle to SQLite even if binding is late', async (t) => {
  const python = process.env.AUTO_ORCHESTRATOR_PYTHON || 'python3';
  if (process.platform === 'win32' || spawnSync(python,['--version']).status !== 0) {
    t.skip('Requires a POSIX Python runtime for native supervisor bridge');
    return;
  }
  const root = mkdtempSync(join(tmpdir(),'ao-v6-installed-'));
  try {
    const plugins = join(root,'plugins');
    const helpers = join(root,'auto-orchestrator','tools');
    mkdirSync(plugins,{recursive:true});
    mkdirSync(helpers,{recursive:true});
    writeFileSync(join(root,'package.json'),'{"type":"module"}');
    const installed = join(plugins,'auto_orchestrator.js');
    copyFileSync(new URL('../../adapters/opencode-v2/auto_orchestrator.js',import.meta.url),installed);
    const script = join(helpers,'supervisor.py');
    copyFileSync(new URL('../../tools/supervisor.py',import.meta.url),script);
    copyFileSync(new URL('../../tools/review_gate.py',import.meta.url),
      join(helpers,'review_gate.py'));
    const installedPlugin = (await import(pathToFileURL(installed).href)).default;
    let owner;
    await sandbox(async ({project,hooks,emit,read}) => {
      owner=project;
      const plan = join(project,'plan.json');
      mkdirSync(project,{recursive:true});
      writeFileSync(plan,JSON.stringify({native_supervision:true,tasks:[{
        id:'verify',role:'tester',objective:'Run checks',read_scope:[],write_scope:[],
      }]}));
      const invoke=(...args)=>{
        const p=spawnSync(python,[script,'--root',project,...args],{encoding:'utf8'});
        assert.equal(p.status,0,p.stderr);
        return JSON.parse(p.stdout);
      };
      invoke('init','--run-id','live-1','--plan',plan);
      const lease=invoke('claim','--run-id','live-1').claimed[0].lease;
      await emit(lifecycle('session.execution.started','live-native',project));
      await hooks['execute.before'](subagent('real-tool-call-123','live-native'));
      await hooks['execute.after'](subagent('real-tool-call-123','live-native','completed'));
      const events=read()[0];
      const digest=events.find(e=>e.event==='agent-start').native_hash;
      assert.match(digest,/^[0-9a-f]{64}$/);
      assert.equal(events.find(e=>e.event==='agent-stop').native_hash,digest);
      invoke('bind-native','--run-id','live-1','--task-id','verify',
        '--lease',lease,'--platform','opencode','--native-hash',digest);
      const state=invoke('status','--run-id','live-1');
      assert.equal(state.tasks[0].native_stop_observed,true);
      const resultFile=join(project,'result.json');
      writeFileSync(resultFile,JSON.stringify({summary:'ok',changed_files:[],
        tests:[{name:'unit',status:'passed'}],findings:[]}));
      assert.equal(invoke('completed','--run-id','live-1','--task-id','verify',
        '--lease',lease,'--result',resultFile).state,'completed');
    },()=>({location:{directory:owner}}),{pluginImpl:installedPlugin});
  } finally {rmSync(root,{recursive:true,force:true});}
});


test('V6.3 native prompt admission auto-selects OpenCode skill IDs and advises single agent', async (t) => {
  const python = process.env.AUTO_ORCHESTRATOR_PYTHON || 'python3';
  if (process.platform === 'win32' || spawnSync(python,['--version']).status !== 0) {
    t.skip('POSIX Python integration fixture required');
    return;
  }
  const root = mkdtempSync(join(tmpdir(),'ao-v63-native-'));
  try {
    const plugins = join(root,'plugins');
    const helpers = join(root,'auto-orchestrator','tools');
    mkdirSync(plugins,{recursive:true});
    mkdirSync(helpers,{recursive:true});
    writeFileSync(join(root,'package.json'),'{"type":"module"}');
    copyFileSync(new URL('../../adapters/opencode-v2/auto_orchestrator.js',import.meta.url),
      join(plugins,'auto_orchestrator.js'));
    for (const f of ['router.py','supervisor.py','review_gate.py']) {
      copyFileSync(new URL('../../tools/'+f,import.meta.url),join(helpers,f));
    }
    const installedPlugin = (await import(pathToFileURL(join(plugins,'auto_orchestrator.js')).href)).default;
    let owned;
    const skills = [
      {id:'skill-backend',name:'backend-api',description:'Build FastAPI backend API endpoints'},
      {id:'skill-motion',name:'ui-animation',description:'Animate frontend UI transitions'},
      {id:'skill-tests',name:'test-automation',description:'Automate pytest regression tests'},
    ];
    await sandbox(async ({project,foreignProject,hooks,read,emit}) => {
      owned = project;
      const prompt = {text:'Implement FastAPI backend API endpoint. SENSITIVE_CREDENTIAL_TEXT'};
      await hooks.prompt({sessionID:'routed-one',prompt});
      assert.deepEqual(prompt.skills,[{id:'skill-backend'}]);
      const context = {sessionID:'routed-one',system:[],agent:'build'};
      await hooks.context(context);
      assert.match(context.system.at(-1).text,/Prefer one agent/);
      assert.equal(read().length,1);
      assert.deepEqual(read()[0].map(e=>e.event),['start','route']);
      assert.equal(read()[0][1].selected_count,1);
      assert.doesNotMatch(JSON.stringify(read()),/SENSITIVE_CREDENTIAL_TEXT|backend-api|routed-one/);

      const manual = {text:'Implement FastAPI backend API endpoint',
        skills:[{id:'skill-motion'}]};
      await hooks.prompt({sessionID:'manual',prompt:manual});
      assert.deepEqual(manual.skills,[{id:'skill-motion'}]);
      assert.equal(read().length,1);

      owned = foreignProject;
      const foreign = {text:'Implement FastAPI backend API endpoint'};
      await hooks.prompt({sessionID:'foreign',prompt:foreign});
      assert.equal(foreign.skills,undefined);
      assert.equal(read().length,1);
      owned = project;

      const children = {text:'Implement FastAPI backend API endpoint'};
      await hooks.prompt({sessionID:'child',prompt:children});
      assert.equal(children.skills,undefined);

      const parallel = {text:'Independent modules can run in parallel:\n1. Build backend API\n2. Write UI\n3. Write tests'};
      await hooks.prompt({sessionID:'multi',prompt:parallel});
      const parallelContext = {sessionID:'multi',system:[],agent:'build'};
      await hooks.context(parallelContext);
      assert.match(parallelContext.system.at(-1).text,/at most 3/);
      assert.equal(read().length,2);
    },(sessionID)=>({
      location:{directory:owned},
      ...(sessionID==='child' ? {parentID:'routed-one'} : {}),
    }),{pluginImpl:installedPlugin,skillList:skills});
  } finally { rmSync(root,{recursive:true,force:true}); }
});

test('V6.3 prompt admission never modifies explicit skills, missing-helper or untrusted sessions',async()=>{
  await sandbox(async ({project,hooks,read})=>{
    const manual={text:'Implement backend API',skills:[{id:'original'}]};
    await hooks.prompt({sessionID:'manual',prompt:manual});
    assert.deepEqual(manual.skills,[{id:'original'}]);
    const missing={text:'Implement backend API'};
    await hooks.prompt({sessionID:'router-missing',prompt:missing});
    assert.equal(missing.skills,undefined);
    assert.deepEqual(read(),[]);
  },()=>({location:{directory:'/foreign'}}),{skillList:[
    {id:'backend',name:'backend-api',description:'FastAPI backend API'}]});
});
