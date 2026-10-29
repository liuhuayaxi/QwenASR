// Synthetic IO only. Production click handlers, HTML and CSS stay unchanged.
(() => {
  const f = window.__fixture;
  f.requests = []; f.pending = {}; f.responses = []; f.archived = [];
  f.bodyReads = 0;
  // Await consumed Response bodies and their UI continuations. A fixed 25/40ms
  // sleep races real browser JSON stream delivery, especially under CPU load.
  // Intentionally held requests are excluded so in-flight assertions still run.
  f.settle = async () => {
    let quiet = 0, previous = -1;
    const deadline = Date.now() + 3000;
    while (quiet < 3) {
      await new Promise(resolve => setTimeout(resolve, 10));
      const count = f.requests.length;
      quiet = f.bodyReads === 0 && count === previous ? quiet + 1 : 0;
      previous = count;
      if (Date.now() > deadline) throw new Error('Fixture response consumption did not settle');
    }
  };
  f.location = new URL(f.url || 'http://fixture.invalid/tasks');
  f.history = {state:null, replaceState(state, unused, value) { this.state=state; f.location.href=new URL(value,f.location).href; }};
  const storage = new Map(Object.entries(f.storage || {}));
  Object.defineProperty(window,'localStorage',{configurable:true,value:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,String(v)),removeItem:k=>storage.delete(k)}});
  f.resolve = (name, spec = {}) => { if (!f.pending[name]) throw new Error('No pending request: '+name); const done=f.pending[name]; delete f.pending[name]; done(spec); };
  f.failIds = f.failIds || [];
  function act(item) {
    const task=f.tasks.find(t=>t.id===item.id);
    if (!task || !task.actions.includes(item.action) || f.failIds.includes(item.id)) return {id:item.id,ok:false,error:'任务状态已改变，请核对'};
    if (!f.keepStatus) {task.status=({pause:'paused',cancel:'cancelled'})[item.action] || 'queued';task.actions=task.status==='cancelled'?[]:task.status==='queued'?['pause','cancel']:['continue','cancel'];}
    return {id:item.id,ok:true};
  }
  function serve(path, method, body, headers) {
    if (path==='/api/tasks' && method==='GET') return {json:{tasks:f.tasks,history:{can_restore:!!f.archived.length},warnings:[]}};
    if (path.startsWith('/api/tasks/') && method==='POST' && headers.get('X-QwenASR-Client')!=='task-center') return {status:403,json:{detail:'请从任务中心操作'}};
    if (path==='/api/tasks/action') {const r=act(body);return {status:r.ok?200:409,json:r.ok?r:{detail:r.error}};}
    if (path==='/api/tasks/bulk-action' || path==='/api/tasks/retry-failed') {
      const items=path.endsWith('retry-failed') ? f.tasks.filter(t=>['failed','error','interrupted','stale','completed_with_errors'].includes(t.status)&&!t.recovered&&!t.is_context).map(t=>({id:t.id,action:t.actions.includes('retry')?'retry':'continue'})) : body.items;
      if (!Array.isArray(items)||items.length>200) return {status:400,json:{detail:'每次最多 200 项'}};
      const results=items.map(act); return {json:{results,accepted:results.filter(x=>x.ok).length,requested:items.length}};
    }
    if (path==='/api/tasks/clear-completed') {
      const keys=new Set(body.items.map(x=>x.id+':'+x.generation));
      f.archived=f.tasks.filter(t=>t.status==='completed'&&keys.has(t.id+':'+t.history_generation));
      f.tasks=f.tasks.filter(t=>!f.archived.includes(t)); return {json:{cleared:f.archived.length}};
    }
    if (path==='/api/tasks/restore-completed') {const n=f.archived.length;f.tasks.push(...f.archived);f.archived=[];return {json:{restored:n}};}
    if (path==='/api/pdf-ai/choose-files') return {json:{paths:['/fixture/selected.pdf']}};
    if (path==='/api/tasks/pdf') {
      if (!body.paths.length||body.paths.length>20) return {status:400,json:{detail:'请选择 1–20 个 PDF 文件'}};
      const id='pdf:new-'+f.requests.length;f.tasks.push({id,type:'pdf',family:'mineru',title:'新建 PDF',label:'本地 PDF 识别',category:'',status:'queued',actions:['cancel'],files:body.paths.map(p=>({source_name:p,status:'queued'}))});
      return {json:{id}};
    }
    if (path.startsWith('/api/library?')) return {json:{records:f.records||[]}};
    if (path==='/api/inference/settings') {if(method==='PUT')f.inference=structuredClone(body.settings);return {json:{settings:f.inference}};}
    if (path==='/api/inference/test') return {json:{capabilities:{version:'fixture-node'}}};
    if (path==='/api/mineru/settings') {if(method==='PUT')f.mineru=structuredClone(body.settings);return {json:{settings:f.mineru,defaults:f.defaults||f.mineru,tier_choices:['auto','flash','basic','standard','advanced'],runtime:{compatible:true,available:true,version:'4.0.10'}}};}
    if (path==='/api/library/backup') {
      if(method==='POST')f.backup={id:'backup-'+f.requests.length,status:'packing',completed_files:0,file_count:5,processed_bytes:0,total_bytes:5000,percent:0};
      return {json:{backup:f.backup??null}};
    }
    if (/\/api\/recordings\/.+\/discard$/.test(path)) return {json:{session_id:decodeURIComponent(path.split('/')[3]),status:method==='POST'?'discarding':'discarded'}};
    throw new Error('Unexpected fixture request: '+method+' '+path);
  }
  window.fetch = async (raw,options={}) => {
    const path=new URL(String(raw),f.location).pathname + new URL(String(raw),f.location).search;
    const method=(options.method||'GET').toUpperCase(), body=options.body?JSON.parse(options.body):null,headers=new Headers(options.headers||{});
    const request={path,method,body,headers:Object.fromEntries(headers),index:f.requests.length}; f.requests.push(request);
    const i=(f.plans||[]).findIndex(p=>p.path===path&&(!p.method||p.method===method));
    let plan=i>=0?f.plans.splice(i,1)[0]:{};
    if (plan.hold) plan={...plan,...await new Promise((resolve,reject)=>{
      f.pending[plan.hold]=resolve;
      options.signal?.addEventListener('abort',()=>{delete f.pending[plan.hold];reject(options.signal.reason);},{once:true});
    })};
    if (plan.error) {request.error=plan.error;throw plan.error==='timeout'?new DOMException('Timed out','TimeoutError'):new TypeError('Network request failed');}
    const spec=Object.hasOwn(plan,'json')||plan.raw!==undefined ? plan : serve(path,method,body,headers);
    const status=spec.status||200;request.status=status;f.responses.push({index:request.index,status});
    const response = new Response(spec.raw!==undefined?spec.raw:JSON.stringify(spec.json),{status,headers:{'Content-Type':'application/json',...(spec.headers||{})}});
    const json = response.json.bind(response);
    response.json = async () => {
      f.bodyReads += 1;
      try { return await json(); } finally { f.bodyReads -= 1; }
    };
    return response;
  };
})();
