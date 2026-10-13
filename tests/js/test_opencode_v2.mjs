import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,readFileSync,readdirSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import plugin from '../../adapters/opencode-v2/auto_orchestrator.js';
test('OpenCode V2 hook lifecycle and private event log',async()=>{
  const directory=mkdtempSync(join(tmpdir(),'orchestrator-v2-'));
  const hooks={};let emit,signalRelease;
  const ctx={location:{directory},
    session:{hook:async(n,f)=>{hooks[n]=f}},
    tool:{hook:async(n,f)=>{hooks[n]=f}},
    event:{subscribe:({signal})=>{
      const q=[];
      emit=(e)=>{q.push(e);signalRelease?.()};
      return {async *[Symbol.asyncIterator](){
        while(!signal.aborted){
          if(q.length) yield q.shift();
          else await new Promise(resolve=>{signalRelease=resolve;signal.addEventListener('abort',resolve,{once:true})});
        }
      }};
    }},
  };
  try{
    const dispose=await plugin.setup(ctx);
    assert.equal(plugin.id,'auto-orchestrator');
    const s={system:[],agent:'build'};
    hooks.context(s);
    assert.match(s.system[0].text,/subagents/);
    await hooks['execute.before']({tool:'task',sessionID:'session-secret',callID:'call1',input:{prompt:'TOKEN-SECRET'}});
    await hooks['execute.before']({tool:'task',sessionID:'session-secret',callID:'call2'});
    await hooks['execute.after']({tool:'task',sessionID:'session-secret',callID:'call1',status:'completed'});
    await hooks['execute.after']({tool:'task',sessionID:'session-secret',callID:'call2',status:'completed'});
    emit({type:'session.idle',properties:{sessionID:'session-secret'}});
    await new Promise(resolve=>setImmediate(resolve));
    await new Promise(resolve=>setImmediate(resolve));
    dispose();
    const dir=join(directory,'.auto-orchestrator','runs');
    const files=readdirSync(dir);
    assert.equal(files.length,1);
    const raw=readFileSync(join(dir,files[0]),'utf8');
    assert.doesNotMatch(raw,/TOKEN-SECRET|session-secret|call1|call2/);
    const events=raw.trim().split('\n').map(x=>JSON.parse(x));
    assert.deepEqual(events.map(x=>x.event),['start','agent-start','agent-start','agent-stop','agent-stop','finish']);
    assert.equal(events.at(-1).verified,false);
  }finally{rmSync(directory,{recursive:true,force:true})}
});
