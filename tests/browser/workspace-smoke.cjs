/* Offline browser acceptance: static source, fake microphone/WebSocket, synthetic
   API fixtures. Never import app.py, read user settings, or contact services. */
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'../..');
const output = path.join(root,'output/playwright');
fs.mkdirSync(output,{recursive:true});
const record = {id:'smoke-class',title:'合成课堂第 3 讲',category:'合成课程',status:'completed',kind:'upload',started_at:'2026-10-10T08:00:00+08:00',duration:60,
 segments:[{id:1,start:0,end:60,raw:'合成原文：菲涅耳公式与相位差。',corrected:'合成校对内容：菲涅耳公式与相位差。'}],ai_reviews:[],ai_summaries:[],
 ai_note_summary:{summary:'# 合成课堂笔记\n\n菲涅耳公式与相位差。',abstract:'合成摘要'},downloads:{audio:'/fake.aac',transcript:'/fake.txt',raw_transcript:'/fake.txt',corrected_transcript:'/fake.txt',bundle:'/fake.zip',ai_summary:'/fake.md'},inference:{provider:'remote',job_id:'fake-job'}};
const baseTask = {id:'summary:smoke-class',recording_id:record.id,recording_title:record.title,title:record.title,category:record.category,type:'summary',family:'ai',label:'课后复习笔记',status:'interrupted',stage:'笔记生成中断',actions:['continue','retry'],result_url:'/library?recording=smoke-class',inference:{provider:'remote'}};
let tasks,uploaded,reviewUploaded,activeSession,liveCourse,optimizedReady,syntheticRecords,unknownSync,linkedSync,courseFixtureFiles,requests=[];
const pages={'/':'index.html','/library':'index.html','/tasks':'tasks.html','/notes':'course-files.html','/workspace':'workspace.html','/course-files':'course-files.html','/course/':'smartclass.html'};
const server=http.createServer((req,res)=>{
 const url=new URL(req.url,'http://localhost');
 const file=pages[url.pathname] ? path.join(root,'static',pages[url.pathname]) : path.join(root,url.pathname);
 if(!file.startsWith(root+path.sep) || !fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}
 res.setHeader('Content-Type',(/\.m?js$/.test(file))?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html':'application/octet-stream');res.end(fs.readFileSync(file));
});
async function fixtures(route) {
 const request=route.request(),url=new URL(request.url()),p=url.pathname;
 requests.push({path:p,query:url.search,method:request.method(),body:request.postData()});
 let data={};
 if(p==='/course/api/status')data=liveCourse || {state:'idle',message:'合成课程回放',identity:null,courses:[],options:{},imports:[{state:'completed',record_id:record.id,title:record.title}],queue:{remaining:0}};
 else if(p==='/api/config')data={feature_version:'library-upgrade-v15-classroom-chat'};
 else if(p==='/api/health')data={status:'idle',detail:'合成服务就绪',model:'fake',device:'fake',asr_provider:'remote_only',local_model_loaded:false};
 else if(p==='/api/library'){
   let selected=(syntheticRecords || [record]).filter(item=>!url.searchParams.get('category') || item.category===url.searchParams.get('category'));
   if(url.searchParams.get('ids'))selected=selected.filter(item=>url.searchParams.get('ids').split(',').includes(item.id));
   const total=selected.length,offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||100);
   data={records:selected.slice(offset,offset+limit).map(item=>{
       const {segments,ai_reviews,ai_summaries,ai_note_summary,...small}=item;
       return {...small,has_note:true,ai_note_summary:{abstract:ai_note_summary.abstract}};
     }),total,note_count:total,next_offset:offset+limit<total?offset+limit:null,
     calendar:{'2026-10-10':{count:total,duration:60*total}},categories:['未分类',record.category,'第二课程'],category_profiles:{},storage_root:'/synthetic'};
 }
 else if(p==='/api/session')data={active:!!activeSession,session:activeSession};
 else if(p==='/api/tasks')data={tasks,counts:{all:tasks.length,attention:1,active:1,completed:1},history:{},warnings:[]};
 else if(p==='/api/tasks/action'){const intent=request.postDataJSON();assert.equal(intent.use_saved_ai_settings,true);tasks=tasks.map(task=>task.id===intent.id?{...task,status:'queued',stage:'已加入队列'}:task);data={ok:true};}
 else if(p==='/api/ai-tasks')data={tasks:[]};
 else if(p==='/api/inference/settings')data={settings:{asr_provider:'remote_only',mineru_provider:'local',base_url:'http://synthetic.invalid'}};
 else if(p==='/api/ai/settings')data={settings:{function_providers:{}},has_api_key:false};
 else if(p==='/api/ai/classroom-chat/settings')data={provider:{transport:'api',model:'fake-model',base_url:'http://synthetic.invalid',context_window:131072,max_output_tokens:1000},context:{},system_prompt:'合成',include_context:true,include_terminology:true,include_summary:true,include_history:true,include_partial:false};
 else if(p==='/api/ai/classroom-chat/models')data={models:[]};
 else if(p==='/api/ideashell/status')data={status:'connected',connected:true};
 else if(p==='/api/library/events'){await route.fulfill({status:200,contentType:'text/event-stream',body:': offline\n\n'});return;}
 else if(p===`/api/recordings/${record.id}`)data={session:record};
 else if(p.endsWith('/summary-status'))data={job:{running:false,status:'completed'},session:record};
 else if(p.endsWith('/chat'))data={turns:[],conversation_id:'synthetic',context:{},session_id:record.id};
 else if(p==='/api/upload-jobs' && request.method()==='POST')data={job:{id:'synthetic-upload',recording_id:record.id},id:'synthetic-upload'};
 else if(p==='/api/upload-jobs/synthetic-upload/events'){await route.fulfill({status:200,contentType:'application/x-ndjson',body:JSON.stringify({type:'complete',progress:100,stage:'完成',session:record})+'\n'});return;}
 else if(p==='/api/course-files')data={category:record.category,categories:[record.category],files:courseFixtureFiles || (optimizedReady?[{id:'synthetic-ready',name:'合成讲义.pdf',kind:'lecture',size:20,created_at:'2026-10-10',conversion:{status:'completed',pages:2,mineru_settings:{tier:'advanced'}},sync:[]}]:uploaded?[{id:'synthetic-file',name:'合成讲义.txt',kind:'lecture',size:20,created_at:'2026-10-10',conversion:{status:'none'},sync:[]}]:[]),directory:'/synthetic',capabilities:{office:false,mineru:false}};
 else if(p==='/api/course-files/upload'){uploaded=true;data={duplicate:false};}
 else if(p==='/api/notes')data={category:record.category,categories:[record.category],lessons:[{id:record.id,title:record.title,date:'2026-10-10'}],notes:[{id:'review-old',title:'已有复习 PDF',filename:'review-old.pdf',created_at:'2026-10-10',exists:true,source_lessons:[{title:record.title,date:'2026-10-10'}],download_url:'/api/notes/review-old/download'},...(reviewUploaded ? [{id:'review-new',title:'新复习 PDF',filename:'new.pdf',created_at:'2026-10-11',exists:true,download_url:'/api/notes/review-new/download'}] : [])],prompt_template:'合成 {{course}} {{lessons}}',storage_root:'/synthetic'};
 else if(p==='/api/notes/upload'){reviewUploaded=true;data={note:{title:'新复习 PDF'}};}
 else if(p.endsWith('/ideashell/preview'))data={sync:unknownSync ? {status:'unknown',create_operation:{status:'unknown'}} : linkedSync ? {status:'synced',note_id:'a'.repeat(32)} : {},content:'# 合成课堂完整归档\n\n前半课堂与后半课堂。',mode:'combined',note_count:1,characters:30,digest:'single-note-digest',split:true,audio_parts:[{name:'上半段.aac',bytes:100,sha256:'first-hash'},{name:'下半段.aac',bytes:101,sha256:'second-hash'}]};
 else if(p.endsWith('/ideashell/push')){if(unknownSync){await route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'结果未知，请先核对'})});return;}assert.deepEqual(request.postDataJSON().audio_sha256s,['first-hash','second-hash']);data={parts:2,note_count:1,audio_attached:true,note_ids:['synthetic-one-note'],session:record};}
 else if(p==='/api/mineru/settings')data={settings:{tier:'standard',engine:'pipeline'},defaults:{}};
 await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
}
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const origin=`http://127.0.0.1:${server.address().port}`;
 const browser=await chromium.launch({...(process.env.QWEN_ASR_BROWSER_CHANNEL === 'chromium' ? {} : {channel:process.env.QWEN_ASR_BROWSER_CHANNEL || 'chrome'}),headless:true,args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream']});
 const results=[];
 async function scenario(name,run) {
  tasks=[{...baseTask},{...baseTask,id:'refinement:smoke-class',type:'refinement',label:'转写校对',status:'running',stage:'正在校对',actions:['pause']},{...baseTask,id:'summary:old',recording_id:'old',title:'已完成合成课堂',status:'completed',actions:[]}];
  uploaded=false;reviewUploaded=false;activeSession=null;liveCourse=null;optimizedReady=false;syntheticRecords=null;unknownSync=false;linkedSync=false;courseFixtureFiles=null;requests=[];
  const context=await browser.newContext({permissions:['microphone'],viewport:{width:1440,height:1000}});
  const errors=[];
  await context.route('**/*',async route=>{const url=new URL(route.request().url());if(url.origin!==origin){await route.abort();return;}if(url.pathname.startsWith('/api/') || url.pathname.startsWith('/course/api/'))await fixtures(route);else await route.continue();});
  await context.addInitScript(() => localStorage.setItem('qwenasr-theme','dark'));
  const page=await context.newPage();page.setDefaultTimeout(10000);page.on('pageerror',error=>errors.push(error.message));page.on('console',message=>{if(message.type()==='error')console.error('Browser console:',message.text());});page.on('requestfailed',request=>console.error('Request failed:',request.url(),request.failure()));
  try {await run(page,context,origin);assert.deepEqual(errors,[]);results.push({name,passed:true});console.log(`PASS ${name}`);}
  catch(error){console.error('Page errors:',errors);console.error('Debug:',await page.evaluate(()=>({socket:window.__socketEvents,toast:document.getElementById('toast')?.textContent,status:document.getElementById('listeningLabel')?.textContent}))); console.error('API requests:',requests.map(r=>r.path));results.push({name,passed:false,error:error.stack});await page.screenshot({path:path.join(output,`failure-${results.length}.png`),fullPage:true});throw error;}
  finally {await context.close();}
 }
 try {
  await scenario('课程任务使用实时进度并自动刷新，断线明确提示',async(page,context,origin)=>{
    tasks=[{id:'course_queue',type:'course_queue',family:'asr',label:'课程回放队列',title:'课程回放 · 1 堂课',status:'running',progress:10.7,total:1,completed:0,actions:['pause'],course_items:[{id:'course-a',title:'合成课堂 A',state:'running',progress:10.7,phases:[{key:'asr',label:'本地语音转写',percent:12.7,state:'running'}]}]}];
    liveCourse={queue:{paused:false,items:[{id:'course-a',title:'合成课堂 A',state:'running'}]},imports:[{id:'course-a',progress:62,phases:{download:{label:'获取回放音频',percent:100,state:'completed'},asr:{label:'本地语音转写',percent:48.8,detail:'45.9/94.0 分钟',state:'running'}}}]};
    await page.goto(origin+'/tasks');
    const phase=page.locator('.course-phase').filter({hasText:'本地语音转写'}).locator('progress');
    await page.waitForFunction(()=>document.querySelector('.task-progress')?.value===62);
    assert.equal(await phase.getAttribute('value'),'48.8');
    assert.match(await page.locator('.task-stage').innerText(),/0\/1 堂课/);
    liveCourse.imports[0].progress=65.3;liveCourse.imports[0].phases.asr.percent=55.1;
    await page.waitForFunction(()=>document.querySelector('.task-progress')?.value===65.3);
    assert.equal(await phase.getAttribute('value'),'55.1');
    await page.route('**/course/api/status',route=>route.fulfill({status:503,contentType:'application/json',body:'{}'}));
    await page.locator('#refreshTasks').click();await page.locator('#sourceWarning').filter({hasText:'实时进度暂时不可用'}).waitFor();
    assert.equal(await page.locator('.task-card').count(),1);
    await page.unroute('**/course/api/status');
    liveCourse.queue.items[0].state='completed';
    await page.locator('#refreshTasks').click();await page.waitForFunction(()=>document.querySelectorAll('.task-card').length===0);
    assert.equal(await page.locator('#sourceWarning').isVisible(),false);
  });
  await scenario('优化后 PDF 文案与原件下载明确区分',async(page,context,origin)=>{
    optimizedReady=true;await page.goto(origin+'/course-files');
    const card=page.locator('[data-id="synthetic-ready"]');await card.waitFor();
    assert.match(await card.locator('.file-status').innerText(),/^AI 优化后 PDF 已就绪/);
    assert.equal(await card.locator('[data-action="convert"]').innerText(),'✓ AI 优化后 PDF 已就绪');
    assert.match(await card.getByRole('link',{name:'下载 AI 优化后 PDF',exact:true}).getAttribute('href'),/variant=pdf/);
    assert.match(await card.getByRole('link',{name:'下载原件',exact:true}).getAttribute('href'),/variant=original/);
    assert.equal(await card.getByRole('link',{name:'查看识别任务',exact:true}).count(),0);
  });
  await scenario('已完成识别隐藏任务入口，进行中和异常保留',async(page,context,origin)=>{
    const statuses=['completed','stale','queued','running','stopping','error','failed','interrupted','paused','cancelled','none'];
    courseFixtureFiles=statuses.flatMap(status=>[false,true].map(external=>({id:`file-${status}-${external}`,name:`${status}-${external}.pdf`,kind:'课件',size:20,created_at:'2026-10-10',conversion:{status,external,pages:2},sync:[]})));
    await page.goto(origin+'/course-files');await page.locator('[data-id="file-completed-false"]').waitFor();
    for(const status of statuses)for(const external of [false,true]){
      const card=page.locator(`[data-id="file-${status}-${external}"]`);
      const expected=['queued','running','stopping','error','failed','interrupted','paused','cancelled'].includes(status);
      assert.equal(await card.getByRole('link',{name:'查看识别任务',exact:true}).count(),expected?1:0,`${status} external=${external}`);
      assert.equal(await card.getByRole('link',{name:'下载原件',exact:true}).count(),1);
      assert.equal(await card.locator('[data-action="copy-original"]').count(),1);
      if(['completed','stale'].includes(status))assert.equal(await card.getByRole('link',{name:'下载 AI 优化后 PDF',exact:true}).count(),1);
    }
  });
  await scenario('知识索引显示本机位置，AI任务不冒用ASR回执',async(page,context,origin)=>{
    tasks=[{id:'knowledge:local',type:'knowledge_index',family:'knowledge',title:'合成课程知识索引',label:'知识索引',status:'running',execution_location:'local',progress:40},
      {id:'classroom-note:synthetic',type:'classroom_note',family:'knowledge',title:'合成速记',label:'速记增强',status:'running'},
      {id:'ai-synthetic',type:'summary',family:'ai',title:'合成总结',label:'课后笔记',status:'running',configured_provider:'remote_only'},
      {id:'mineru-unknown',type:'course_pdf',family:'mineru',title:'未收到识别回执',label:'资料识别',status:'running',configured_provider:'local'},
      {id:'asr-confirmed',type:'upload',family:'asr',title:'真实回执优先',label:'转写',status:'running',configured_provider:'local',inference:{provider:'remote',job_id:'synthetic-receipt'}}];
    await page.goto(origin+'/tasks');await page.locator('[data-id="knowledge:local"]').waitFor();
    for(const id of ['knowledge:local','classroom-note:synthetic','ai-synthetic']){
      const details=page.locator(`[data-id="${id}"] .task-details`);
      if(await details.count())await details.locator('summary').click();
      assert.doesNotMatch(await page.locator(`[data-id="${id}"]`).innerText(),/实际来源待确认/);
    }
    assert.match(await page.locator('[data-id="knowledge:local"]').innerText(),/处理位置：Mac 本机/);
    await page.locator('[data-id="mineru-unknown"] .task-details summary').click();
    assert.match(await page.locator('[data-id="mineru-unknown"]').innerText(),/实际来源待确认/);
    await page.locator('[data-id="asr-confirmed"] .task-details summary').click();
    assert.match(await page.locator('[data-id="asr-confirmed"]').innerText(),/实际：Windows/);
  });
  await scenario('索引说明展示待转换数量，课堂只选择有效课件',async(page,context,origin)=>{
    const source=(id,state,status='已就绪')=>({source_id:id,title:id,state,status});
    await context.route('**/api/knowledge/status?*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({active_sources_count:2,active_chunks_count:5,index_policy_label:'课程资料仅索引 MinerU 转换后 PDF；课堂转录、速记与术语继续参与检索',waiting_conversion_count:3,sources:[source('cf_ready_pdf','active'),source('cf_pending','waiting_conversion'),source('cf_stale','stale'),source('cf_deleted','tombstoned'),source('cf_legacy_pdf',undefined),source('cf_duplicate_mineru','active')]})}));
    await page.goto(origin+'/course-files?category='+encodeURIComponent(record.category));
    await page.locator('#knowledgeIndexPolicy:not([hidden])').waitFor();
    assert.match(await page.locator('#knowledgeIndexPolicy').innerText(),/仅索引 MinerU 转换后 PDF/);
    assert.match(await page.locator('#knowledgeIndexPolicy').innerText(),/3 份资料待完成 MinerU 转换/);
    await page.goto(origin+'/?chat='+record.id+'&panel=related');
    await page.waitForFunction(()=>document.getElementById('relatedSourceSelect')?.options.length===3);
    const values=await page.locator('#relatedSourceSelect option').evaluateAll(options=>options.map(option=>option.value));
    assert.deepEqual(values,['','cf_ready_pdf','cf_legacy_pdf']);
  });
  await scenario('排队索引持续展示并使用真实任务ID取消',async(page,context,origin)=>{
    let cancelled=false;
    await context.route('**/api/knowledge/jobs',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({jobs:cancelled?[]:[{job_id:'synthetic-queued-index',category:record.category,status:'queued',progress:0}]})}));
    await context.route('**/api/knowledge/jobs/synthetic-queued-index/cancel',route=>{
      assert.equal(route.request().method(),'POST');cancelled=true;
      return route.fulfill({status:200,contentType:'application/json',body:'{"cancelled":true}'});
    });
    await page.goto(origin+'/course-files?category='+encodeURIComponent(record.category));
    await page.locator('#knowledgeJobsBox:not(.hidden)').waitFor();
    assert.match(await page.locator('#knowledgeJobsText').innerText(),/索引已排队/);
    await page.locator('#knowledgeCancelJobBtn').click();await page.locator('#knowledgeJobsBox').waitFor({state:'hidden'});
    assert.equal(cancelled,true);
  });
  await scenario('首页与来源证据',async(page,context,origin)=>{await page.goto(origin);await page.locator('#continuationList [data-recording]').waitFor();await page.locator('.recognition-details summary').click();await page.waitForFunction(()=>document.getElementById('recognitionStatus').textContent.includes('识别来源'));assert.match(await page.locator('#recognitionStatus').innerText(),/来源：待确认/);assert.match(await page.locator('#recognitionStatus').innerText(),/仅远程，不自动回退/);await page.screenshot({path:path.join(output,'workbench.png'),fullPage:true});});
  await scenario('新建课堂来源入口',async(page,context,origin)=>{await page.goto(origin+'/workspace');await page.locator('#workspaceRecent [data-recording]').waitFor();assert.equal(await page.locator('#workspaceCourse').inputValue(),'');await page.locator('#newClass').click();await page.locator('#newClassDialog[open]').waitFor();assert.equal(await page.locator('#newClassDialog a').count(),3);});
  await scenario('课堂记录直接打开笔记',async(page,context,origin)=>{await page.goto(origin+'/library?recording='+record.id);await page.locator('#noteDialog[open]').waitFor();assert.match(await page.locator('#noteSummaryReader').innerText(),/菲涅耳公式/);});
  await scenario('任务默认过滤分组与恢复',async(page,context,origin)=>{await page.goto(origin+'/tasks');await page.locator('.task-group').waitFor();assert.equal(await page.locator('.task-group').count(),1);assert.equal(await page.locator('.task-card').count(),2);await page.locator('[data-id="summary:smoke-class"] [data-action="continue"]').click();await page.getByText('任务已加入后台队列',{exact:true}).waitFor();assert.ok(requests.some(request=>request.path==='/api/tasks/action'));await page.locator('[data-status="completed"]').click();assert.equal(await page.locator('.task-card').count(),1);await page.goto(origin+'/tasks?category='+encodeURIComponent('空课程'));await page.locator('#lastUpdated').filter({hasText:'已同步'}).waitFor();assert.equal(await page.locator('.task-card').count(),0);});
  await scenario('MinerU 两端稳定耗时进度、刷新与旧任务兼容',async(page,context,origin)=>{
    const task={id:'mineru-remote',type:'course_pdf',family:'mineru',label:'课程资料识别',title:'合成讲义 · Windows',category:'合成课程',status:'running',
      stage:'Windows · MinerU · 版面分析 · 阶段 100% · 第 33–36 页/90 · 窗口 9/23',progress:42.5,actions:['pause','cancel'],
      progress_estimate:{estimated:true,percent:42.5,phase:'pages',elapsed_seconds:195,remaining_seconds:260,sample_count:3,observed_windows:7},configured_provider:'remote_only'};
    tasks=[task,{...structuredClone(task),id:'mineru-local',title:'合成讲义 · Mac',configured_provider:'local',stage:'Mac · MinerU · 文字识别 · 阶段 50%',progress:21.4,
      progress_estimate:{...task.progress_estimate,percent:21.4,sample_count:0,observed_windows:0}},
      {...structuredClone(task),id:'mineru-legacy',title:'尚未启用耗时预估的旧任务',progress:null,progress_estimate:null},
      {...structuredClone(task),id:'mineru-loading',title:'刚开始准备的任务',progress:null,progress_estimate:null,stage:'正在加载模型'}];
    await page.goto(origin+'/tasks');
    const card=page.locator('[data-id="mineru-remote"]');await card.waitFor();
    assert.equal(await card.locator('progress').getAttribute('value'),'42.5');
    assert.match(await card.innerText(),/整体预估 42.5%/);
    assert.match(await card.innerText(),/预计剩余约 4 分 20 秒/);
    assert.match(await page.locator('[data-id="mineru-local"]').innerText(),/初步估算/);
    assert.equal(await page.locator('[data-id="mineru-legacy"] progress').getAttribute('value'),'35.6');
    assert.match(await page.locator('[data-id="mineru-legacy"]').innerText(),/已完成页段/);
    assert.equal(await page.locator('[data-id="mineru-loading"] .task-progress-indeterminate').evaluate(el=>getComputedStyle(el).animationName),'none');
    await page.screenshot({path:path.join(output,'mineru-time-progress.png'),fullPage:true});
    tasks[0]={...tasks[0],progress:62.1,progress_estimate:{...task.progress_estimate,percent:62.1,overrun:true,remaining_seconds:null}};
    await page.reload();await card.waitFor();
    assert.equal(await card.locator('progress').getAttribute('value'),'62.1');
    assert.match(await card.innerText(),/超出预估耗时/);
    assert.doesNotMatch(await card.innerText(),/预计剩余约/);
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  });
  await scenario('资源检查不把课程队列或旧版成功结果显示为未验证',async(page,context,origin)=>{
    tasks=[{id:'course_queue',type:'course_queue',family:'asr',label:'课程队列',title:'合成课程队列',status:'paused',course_items:[],actions:['continue']},
      {id:'remote-asr',type:'upload',family:'asr',label:'转写',title:'已确认释放的旧版节点任务',status:'completed',inference:{provider:'remote',metrics:{gpu_release_verified:true}}}];
    await page.goto(origin+'/tasks');await page.locator('#lastUpdated').filter({hasText:'已同步'}).waitFor();
    await page.locator('button[data-status="all"]').click();
    assert.doesNotMatch(await page.locator('[data-id="course_queue"]').innerText(),/遥测|来源未验证|资源释放/);
    assert.match(await page.locator('[data-id="remote-asr"]').innerText(),/显存释放已确认/);
    await page.screenshot({path:path.join(output,'resource-status.png'),fullPage:true});
  });
  await scenario('课程资料上传',async(page,context,origin)=>{await page.goto(origin+'/course-files');await page.locator('#filesInput').setInputFiles({name:'合成讲义.txt',mimeType:'text/plain',buffer:Buffer.from('合成课程资料')});await page.locator('#files [data-id="synthetic-file"]').waitFor();assert.ok(requests.some(request=>request.path==='/api/course-files/upload'));});
  await scenario('刷新恢复课程与未结束会话',async(page,context,origin)=>{activeSession={...record,status:'active',kind:'live'};await page.goto(origin);await page.waitForFunction(()=>document.getElementById('recordName').value==='合成课堂第 3 讲');await page.reload();await page.waitForFunction(()=>document.getElementById('recordCategory').value==='合成课程');assert.match(await page.locator('#sessionState').innerText(),/会话进行中/);});
  await scenario('上传完成直接打开笔记',async(page,context,origin)=>{await page.goto(origin);await page.locator('#uploadButton:not([disabled])').waitFor();await page.locator('#fileInput').setInputFiles({name:'synthetic.wav',mimeType:'audio/wav',buffer:Buffer.from('synthetic only')});await page.locator('#openCompletedNote:not(.hidden)').waitFor();await page.locator('#closeBatchDialog').click();await page.locator('#openCompletedNote').click();await page.locator('#noteDialog[open]').waitFor();});
  await scenario('麦克风开始结束与打开笔记',async(page,context,origin)=>{
    await context.addInitScript(session => {
      window.__socketEvents=[];class SyntheticSocket {
        static OPEN=1;
        constructor() {window.__socketEvents.push('constructed');this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.();},10);}
        send(value) {window.__socketEvents.push(typeof value==='string'?value:'audio');if(typeof value!=='string')return;const message=JSON.parse(value);if(message.type==='start')setTimeout(()=>this.onmessage?.({data:JSON.stringify({type:'session_started',session:{...session,status:'active',kind:'live'}})}),10);if(message.type==='end')setTimeout(()=>this.onmessage?.({data:JSON.stringify({type:'ended',session})}),10);}
        close() {this.readyState=3;this.onclose?.();}
      }
      window.WebSocket=SyntheticSocket;
    },record);
    await page.goto(origin+'/?category='+encodeURIComponent(record.category));
    await page.locator('#recordButton:not([disabled])').waitFor();
    await page.locator('#recordButton').click();
    await page.waitForFunction(()=>document.getElementById('recordButton').classList.contains('recording'));
    assert.match(await page.locator('#recordName').inputValue(),/合成课堂/);
    await page.locator('#endButton').click();
    await page.locator('#openCompletedNote:not(.hidden)').waitFor();
    await page.locator('#openCompletedNote').click();
    await page.locator('#noteDialog[open]').waitFor();
  });
  await scenario('课程回放入库链接打开同一课堂笔记',async(page,context,origin)=>{
    await page.goto(origin+'/course/');
    await page.locator('#result a').waitFor();
    assert.equal(await page.locator('#result a').getAttribute('href'),'/library?recording=smoke-class');
    await page.locator('#result a').click();await page.locator('#noteDialog[open]').waitFor();
  });
  await scenario('课程内问答选择与笔记快捷入口',async(page,context,origin)=>{
    await page.goto(origin+'/workspace?category='+encodeURIComponent(record.category));
    await page.locator('#workspaceTabs [data-tab="review"]').click();
    await page.locator('[data-chat="smoke-class"]').click();
    await page.frameLocator('#workspaceFrame').locator('#chatQuestion:not([disabled])').waitFor();
    assert.ok(requests.some(request=>request.path==='/api/recordings/smoke-class/chat'));
    await page.goto(origin+'/library?recording='+record.id);
    await page.locator('#noteDialog[open]').waitFor();await page.locator('#askNoteClass').click();
    await page.locator('#chatQuestion:not([disabled])').waitFor();assert.equal(await page.locator('#noteDialog[open]').count(),0);
  });
  await scenario('外部复习资料导入默认折叠',async(page,context,origin)=>{
    await page.goto(origin+'/notes');
    assert.equal(await page.locator('.external-import').getAttribute('open'),null);
    await page.locator('.external-import > summary').click();await page.locator('#review-noteFile').waitFor();
  });
  await scenario('课程资料与已有复习 PDF 同页检索与上传',async(page,context,origin)=>{
    await page.goto(origin+'/course-files');
    await page.locator('[data-review-id="review-old"]').waitFor();
    assert.match(await page.locator('[data-review-id="review-old"]').innerText(),/来源课次/);
    assert.equal(await page.locator('.app-nav a[href="/course-files"]').count(),1);
    await page.locator('#search').fill('已有复习');assert.equal(await page.locator('#files article').count(),1);
    await page.locator('#search').fill('');await page.locator('#kindFilter').selectOption('复习资料');
    assert.equal(await page.locator('#files article').count(),1);
    await page.locator('#uploadKind').selectOption('复习资料');
    await page.locator('#filesInput').setInputFiles({name:'new.pdf',mimeType:'application/pdf',buffer:Buffer.from('%PDF-synthetic')});
    await page.locator('[data-review-id="review-new"]').waitFor();
    assert.ok(requests.some(request=>request.path==='/api/notes/upload'));
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth));
    assert.equal(await page.locator('.app-header').count(),1);
    assert.equal(page.frames().length,1);
    await page.screenshot({path:path.join(output,'merged-materials-mobile.png')});
    await page.locator('.file-section').screenshot({path:path.join(output,'merged-materials-list-mobile.png')});
  });
  await scenario('录音拆为两个附件并推送同一篇笔记',async(page,context,origin)=>{
    await page.goto(origin+'/library?recording='+record.id);
    await page.locator('#noteDialog[open]').waitFor();
    await page.locator('#pushNoteSummary').click();
    await page.locator('#pushDialog[open]').waitFor();
    await page.waitForFunction(()=>document.getElementById('pushPreviewStatus').textContent.includes('1 篇笔记'));
    assert.match(await page.locator('#pushPreviewStatus').innerText(),/上下半段录音保存到同一篇笔记/);
    assert.ok(!(await page.locator('#pushPreviewStatus').innerText()).includes('条笔记'));
    await page.locator('#confirmPush').click();
    await page.waitForFunction(()=>!document.getElementById('pushDialog').open);
    assert.ok(requests.some(request=>request.path.endsWith('/ideashell/push')));
  });
  await scenario('摘要列表分页、课程筛选与笔记入口',async(page,context,origin)=>{
    syntheticRecords=Array.from({length:45},(_,i)=>({...record,id:`page-${i}`,title:`合成分页课堂 ${i}`,category:i<42?record.category:'第二课程'}));
    await page.goto(origin+'/library');
    await page.waitForFunction(()=>document.querySelectorAll('.library-card').length===20);
    assert.match(await page.locator('#libraryPageStatus').innerText(),/45 条录音/);
    await page.locator('#libraryNext').click();
    await page.locator('.library-card[data-id="page-20"]').waitFor();
    await page.locator('#libraryNext').click();
    await page.locator('.library-card[data-id="page-40"]').waitFor();
    assert.equal(await page.locator('.library-card').count(),5);
    await page.locator('#libraryCategory').selectOption('第二课程');
    await page.waitForFunction(()=>document.querySelectorAll('.library-card').length===3);
    assert.match(await page.locator('#libraryPageStatus').innerText(),/3 条录音/);
    assert.ok(requests.some(request=>request.path==='/api/library' && new URLSearchParams(request.query).get('offset')==='20'));
    assert.ok(requests.some(request=>request.path==='/api/library' && new URLSearchParams(request.query).get('category')==='第二课程'));
    await page.goto(origin+'/library');
    await page.locator('.library-card[data-id="page-0"]').waitFor();
    assert.equal(await page.locator('.library-card[data-id="page-0"] .library-regenerate-abstract').count(),1);
  });
  await scenario('课程首页总数跨分页保持准确',async(page,context,origin)=>{
    syntheticRecords=Array.from({length:45},(_,i)=>({...record,id:`course-${i}`,title:`合成课堂 ${i}`,category:i<42?record.category:'第二课程'}));
    await page.goto(origin+'/workspace');
    await page.waitForFunction(()=>document.getElementById('workspaceMeta').textContent.includes('45 堂课堂'));
    await page.locator('#workspaceTabs [data-tab="review"]').click();
    await page.locator('[data-more-records]').click();
    await page.waitForFunction(()=>document.querySelectorAll('[data-chat]').length===40);
    await page.locator('#workspaceCourse').selectOption('第二课程');
    await page.waitForFunction(()=>document.getElementById('workspaceMeta').textContent.includes('3 堂课堂'));
    assert.match(await page.locator('#workspaceMeta').innerText(),/3 份笔记/);
  });
  await scenario('未知同步结果默认核对，普通重试不会选择新建',async(page,context,origin)=>{
    unknownSync=true;
    await page.goto(origin+'/library?recording='+record.id);
    await page.locator('#noteDialog[open]').waitFor();await page.locator('#pushNoteSummary').click();
    await page.locator('#pushReconcile:not([hidden])').waitFor();
    assert.equal(await page.locator('#pushDestination').inputValue(),'update');
    await page.locator('#confirmPush').click();
    await page.locator('#pushPreviewStatus').filter({hasText:'结果未知'}).waitFor();
    const push=requests.filter(request=>request.path.endsWith('/ideashell/push')).at(-1);
    assert.equal(JSON.parse(push.body).create_new,false);
    assert.equal(await page.locator('#pushDestination').inputValue(),'update');
  });
  await scenario('服务端已确认关联时预览默认更新已有笔记',async(page,context,origin)=>{
    linkedSync=true;
    await page.goto(origin+'/library?recording='+record.id);
    await page.locator('#noteDialog[open]').waitFor();await page.locator('#pushNoteSummary').click();
    await page.waitForFunction(()=>document.getElementById('pushPreviewStatus').textContent.includes('1 篇笔记'));
    assert.equal(await page.locator('#pushDestination').inputValue(),'update');
    await page.locator('#confirmPush').click();
    await page.waitForFunction(()=>!document.getElementById('pushDialog').open);
    const push=requests.filter(request=>request.path.endsWith('/ideashell/push')).at(-1);
    assert.equal(JSON.parse(push.body).create_new,false);
  });
  await scenario('课程内笔记与任务抽屉',async(page,context,origin)=>{await page.goto(origin+'/workspace?category='+encodeURIComponent(record.category));await page.locator('[data-recording="smoke-class"]').waitFor();await page.screenshot({path:path.join(output,'workspace.png'),fullPage:true});await page.locator('[data-recording="smoke-class"]').click();await page.frameLocator('#workspaceNoteFrame').locator('#noteDialog[open]').waitFor();await page.frameLocator('#workspaceNoteFrame').locator('#closeNoteDialog').click();await page.locator('#workspaceNoteDialog').waitFor({state:'hidden'});await page.locator('#workspaceTasks').click();await page.locator('#timelineDialog[open]').waitFor();await page.frameLocator('#timelineFrame').locator('.task-card').first().waitFor();});
  await scenario('最近课堂全屏笔记保存后关闭，恢复课程和滚动位置',async(page,context,origin)=>{
    let releaseSave,markStarted;
    const saveReady=new Promise(resolve=>releaseSave=resolve),saveStarted=new Promise(resolve=>markStarted=resolve);
    await context.route('**/api/recordings/smoke-class/summary',async route=>{
      const body=route.request().postDataJSON();
      assert.equal(route.request().method(),'PUT');
      assert.match(body.summary,/全屏编辑保留/);
      markStarted();await saveReady;
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({summary:{summary:body.summary},session:{...record,ai_note_summary:{summary:body.summary}}})});
    });
    await page.setViewportSize({width:1440,height:620});
    const workspaceUrl=origin+'/workspace?category='+encodeURIComponent(record.category);
    await page.goto(workspaceUrl);
    const row=page.locator('#workspaceRecent [data-recording="smoke-class"]');await row.waitFor();
    await row.scrollIntoViewIfNeeded();await page.evaluate(()=>window.scrollTo(0,Math.min(100,document.documentElement.scrollHeight-innerHeight)));
    const scroll=await page.evaluate(()=>({x:scrollX,y:scrollY}));
    await row.click();
    const outer=page.locator('#workspaceNoteDialog[open]'),reader=page.frameLocator('#workspaceNoteFrame');
    await reader.locator('#noteDialog[open]').waitFor();
    const box=await outer.boundingBox();assert.equal(box.x,0);assert.equal(box.y,0);assert.equal(box.width,1440);assert.equal(box.height,620);
    assert.match(await reader.locator('#noteSummaryReader').innerText(),/菲涅耳公式/);
    assert.equal(page.url(),workspaceUrl);assert.equal(await page.locator('#workspaceFrame').isVisible(),false);
    await reader.locator('#toggleNoteSource').click();await reader.locator('#noteSummaryEditor').fill('# 全屏编辑保留\n\n刚才修改的课堂笔记。');
    await page.getByRole('button',{name:'关闭并返回课程',exact:true}).click();await saveStarted;
    assert.equal(await outer.isVisible(),true,'save must finish before destroying the reader');
    releaseSave();await outer.waitFor({state:'hidden'});
    assert.equal(page.url(),workspaceUrl);assert.equal(await page.locator('#workspaceCourse').inputValue(),record.category);
    assert.deepEqual(await page.evaluate(()=>({x:scrollX,y:scrollY})),scroll);
    assert.equal(await row.evaluate(button=>document.activeElement===button),true);
    assert.equal(await page.locator('#workspaceTabs [aria-current="page"]').getAttribute('data-tab'),'overview');
    await row.click();await reader.locator('#noteDialog[open]').waitFor();
    await page.getByRole('button',{name:'关闭并返回课程',exact:true}).focus();await page.keyboard.press('Escape');await outer.waitFor({state:'hidden'});
    await page.setViewportSize({width:390,height:844});await row.click();await reader.locator('#noteDialog[open]').waitFor();
    const mobile=await outer.boundingBox();assert.equal(mobile.width,390);assert.equal(mobile.height,844);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await reader.locator('#closeNoteDialog').click();await outer.waitFor({state:'hidden'});
  });
  await scenario('已保存课堂的速记增强会自动更新，原手记保留',async(page,context,origin)=>{
    let polls=0;
    await context.route('**/api/recordings/smoke-class/scribbles',async route=>{
      polls++;
      const note={id:'smoke-note',raw:'合成原始手记',content:'合成原始手记',revision:1,start:0,end:30,
        status:polls===1?'pending':'enhanced',locked:false,job:{status:polls===1?'pending':'completed'},history:[]};
      if(polls>1)note.suggestion={id:'smoke-suggestion',input_revision:1,content:'合成课堂依据与补充',evidence:[],claims:{},validation:{}};
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({notes:[note]})});
    });
    await page.goto(origin+'/?chat='+record.id+'&panel=scribbles');
    await page.getByRole('button',{name:'采纳建议',exact:true}).waitFor();
    assert.ok(polls>=2);
    assert.equal(await page.locator('.scribble-raw-text').innerText(),'合成原始手记');
    assert.match(await page.locator('.suggestion-content').innerText(),/合成课堂依据与补充/);
  });
 } finally {fs.writeFileSync(path.join(output,'smoke-results.json'),JSON.stringify({synthetic:true,results},null,2));await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
