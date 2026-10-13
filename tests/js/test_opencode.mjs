import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readdirSync, readFileSync, cpSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';

const root = process.cwd();
const copyDir = mkdtempSync(join(tmpdir(), 'orchestrator-plugin-'));
const filepath=join(copyDir,'plugin.mjs');
cpSync(join(root,'.opencode/plugins/auto_orchestrator.js'),filepath);
const { AutoOrchestratorPlugin } = await import(pathToFileURL(filepath));

test('OpenCode plugin measures native task tool and avoids prompt storage', async () => {
  const dir=mkdtempSync(join(tmpdir(),'orchestrator-run-'));
  const p=await AutoOrchestratorPlugin({directory:dir});
  const out={system:[]};
  await p['experimental.chat.system.transform']({},out);
  assert.match(out.system[0],/Auto-Orchestrator/);
  await p.event({event:{type:'session.created',properties:{info:{id:'secret-session'}}}});
  await p['tool.execute.before']({tool:'task',sessionID:'secret-session',callID:'abc',args:{prompt:'PRIVATE'}});
  await p['tool.execute.before']({tool:'task',sessionID:'secret-session',callID:'def'});
  await p['tool.execute.after']({tool:'task',sessionID:'secret-session',callID:'abc'});
  await p['tool.execute.after']({tool:'task',sessionID:'secret-session',callID:'def'});
  await p.event({event:{type:'session.idle',properties:{sessionID:'secret-session'}}});
  const [filename]=readdirSync(join(dir,'.auto-orchestrator/runs'));
  const raw=readFileSync(join(dir,'.auto-orchestrator/runs',filename),'utf8');
  const events=raw.trim().split('\n').map(JSON.parse);
  assert.deepEqual(events.map(x=>x.event),['start','agent-start','agent-start','agent-stop','agent-stop','finish']);
  assert.equal(events.at(-1).verified,false);
  assert.doesNotMatch(raw,/PRIVATE|secret-session|abc|def/);
});
