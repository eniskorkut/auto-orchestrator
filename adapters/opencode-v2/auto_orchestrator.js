/** OpenCode V2 adapter. Uses the documented context and tool hook API. */
import { createHash, randomUUID } from 'node:crypto';
import { appendFileSync, closeSync, mkdirSync, openSync } from 'node:fs';
import { join } from 'node:path';
export const POLICY = 'Auto-Orchestrator: For non-trivial coding tasks read the auto-orchestrator skill and delegate independent work to native subagents automatically; max three concurrent. Verify results and never invent timings or human time savings. Trivial tasks use one agent.';
const hash = (value) => createHash('sha256').update(String(value)).digest('hex').slice(0,24);
export default {
  id: 'auto-orchestrator',
  async setup(ctx) {
    const base = join(ctx.location.directory,'.auto-orchestrator','runs');
    const runs = new Map();
    const write = (run,event) => {
      if (!run || run.done) return;
      try {
        mkdirSync(base,{recursive:true});
        const file = join(base,run.id+'.jsonl');
        if(event.event==='start') {
          const fd = openSync(file,'wx',0o600);
          closeSync(fd);
        }
        appendFileSync(file,JSON.stringify({...event,time:Date.now()/1000})+'\n');
      } catch { /* Optional telemetry must never interrupt work. */ }
    };
    const begin = (sid) => {
      if(typeof sid!=='string'||!sid) return null;
      const prev=runs.get(sid);
      if(prev && !prev.done) return prev;
      const run={id:hash('opencode-v2|'+sid+'|'+randomUUID()),done:false,tasks:new Set()};
      runs.set(sid,run);
      write(run,{event:'start',platform:'opencode'});
      return run;
    };
    const finish=(sid,status)=>{
      const run=runs.get(sid);
      if(!run || run.done) return;
      write(run,{event:'finish',status,verified:false});
      run.done=true;
    };
    await ctx.session.hook('context',(event)=>{
      if(event.agent?.startsWith('auto_')) return;
      event.system.push({type:'text',text:POLICY});
    });
    await ctx.tool.hook('execute.before',(event)=>{
      if(event.tool!=='task'||!event.sessionID||!event.callID) return;
      const run=begin(event.sessionID);
      const id=hash(event.callID);
      if(!run || run.tasks.has(id)) return;
      run.tasks.add(id);
      write(run,{event:'agent-start',agent_id:id});
    });
    await ctx.tool.hook('execute.after',(event)=>{
      if(event.tool!=='task'||!event.sessionID||!event.callID) return;
      const run=runs.get(event.sessionID), id=hash(event.callID);
      if(!run || !run.tasks.has(id)) return;
      run.tasks.delete(id);
      if(event.status==='completed') write(run,{event:'agent-stop',agent_id:id});
    });
    const controller=new AbortController();
    void(async()=>{
      try {
        for await (const event of ctx.event.subscribe({signal:controller.signal})) {
          const sid=event?.properties?.sessionID??event?.properties?.info?.id;
          if(!sid) continue;
          if(event.type==='session.created'||(event.type==='session.status'&&event.properties?.status?.type==='busy')) begin(sid);
          else if(event.type==='session.idle') finish(sid,'success');
          else if(event.type==='session.error') finish(sid,'failed');
        }
      }catch{ /* Incomplete event streams must not infer success. */ }
    })();
    return ()=>controller.abort();
  },
};
