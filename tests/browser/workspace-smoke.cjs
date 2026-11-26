/* Offline browser acceptance: static source, fake microphone/WebSocket, synthetic
   API fixtures. Never import app.py, read user settings, or contact services. */
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'../..');
const output = path.resolve(process.env.QWEN_ASR_BROWSER_OUTPUT || path.join(root,'output/playwright'));
fs.mkdirSync(output,{recursive:true});
const record = {id:'smoke-class',title:'合成课堂第 3 讲',category:'合成课程',status:'completed',kind:'upload',started_at:'2026-10-10T08:00:00+08:00',duration:60,
 segments:[{id:1,start:0,end:60,raw:'合成原文：菲涅耳公式与相位差。',corrected:'合成校对内容：菲涅耳公式与相位差。'}],ai_reviews:[],ai_summaries:[],
 ai_note_summary:{summary:'# 合成课堂笔记\n\n菲涅耳公式与相位差。',abstract:'合成摘要'},downloads:{audio:'/fake.aac',transcript:'/fake.txt',raw_transcript:'/fake.txt',corrected_transcript:'/fake.txt',bundle:'/fake.zip',ai_summary:'/fake.md'},inference:{provider:'remote',job_id:'fake-job'}};
const baseTask = {id:'summary:smoke-class',recording_id:record.id,recording_title:record.title,title:record.title,category:record.category,type:'summary',family:'ai',label:'课后复习笔记',status:'interrupted',stage:'笔记生成中断',actions:['continue','retry'],result_url:'/library?recording=smoke-class',inference:{provider:'remote'}};
let tasks,uploaded,reviewUploaded,activeSession,liveCourse,optimizedReady,syntheticRecords,unknownSync,linkedSync,courseFixtureFiles,noteReviewEnabled=false,requests=[];
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
 else if(p==='/api/ai/settings'){
   if(request.method()==='PUT')noteReviewEnabled=request.postDataJSON().settings.note_review_enabled===true;
   data={settings:{function_providers:{},note_review_enabled:noteReviewEnabled},has_api_key:false};
 }
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
 const browser=await chromium.launch({...(process.env.QWEN_ASR_BROWSER_EXECUTABLE ? {executablePath:process.env.QWEN_ASR_BROWSER_EXECUTABLE} : process.env.QWEN_ASR_BROWSER_CHANNEL === 'chromium' ? {} : {channel:process.env.QWEN_ASR_BROWSER_CHANNEL || 'chrome'}),headless:true,args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream']});
 const results=[];
 async function scenario(name,run) {
  if(process.env.QWEN_ASR_SMOKE_FILTER && !name.includes(process.env.QWEN_ASR_SMOKE_FILTER))return;
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
  await scenario('笔记审核默认关闭并保存明确开启选择',async(page,context,origin)=>{
    noteReviewEnabled=false;
    await page.goto(origin+'/?settings=ai');
    await page.locator('[data-ai-tab="summary"]').click();
    const toggle=page.locator('#noteReviewEnabled');
    await toggle.waitFor({state:'visible'});assert.equal(await toggle.isChecked(),false);
    await toggle.locator('..').click();
    await page.waitForFunction(()=>document.querySelector('#apiSaveStatus')?.textContent?.includes('已保存') || document.querySelector('#noteReviewEnabled')?.checked);
    await page.waitForResponse(response=>response.url().endsWith('/api/ai/settings') && response.request().method()==='PUT');
    assert.equal(noteReviewEnabled,true);
    await page.reload();await page.locator('[data-ai-tab="summary"]').click();
    assert.equal(await page.locator('#noteReviewEnabled').isChecked(),true);
    await page.locator('#noteReviewEnabled').locator('..').click();
    await page.waitForResponse(response=>response.url().endsWith('/api/ai/settings') && response.request().method()==='PUT');
    assert.equal(noteReviewEnabled,false);
    await page.screenshot({path:path.join(output,'note-review-switch.png'),fullPage:true});
  });
  await scenario('课程任务使用实时进度并自动刷新，断线明确提示',async(page,context,origin)=>{
    tasks=[{id:'course_queue',type:'course_queue',family:'asr',label:'课程回放队列',title:'课程回放 · 1 堂课',status:'running',progress:10.7,total:1,completed:0,actions:['pause'],course_items:[{id:'course-a',title:'合成课堂 A',state:'running',progress:10.7,phases:[{key:'asr',label:'Mac 转写',execution:{provider:'local'},percent:12.7,state:'running'}]}]}];
    liveCourse={queue:{paused:false,items:[{id:'course-a',title:'合成课堂 A',state:'running'}]},imports:[{id:'course-a',progress:62,phases:{download:{label:'获取回放音频',percent:100,state:'completed'},asr:{label:'Mac 转写',execution:{provider:'local'},percent:48.8,detail:'45.9/94.0 分钟',state:'running'}}}]};
    await page.goto(origin+'/tasks');
    const groupDetails=page.locator('.group-details');
    await groupDetails.first().waitFor({state:'attached'});
    if(!(await groupDetails.first().evaluate(e=>e.open))) await page.locator('.group-details > summary').first().click();
    const phase=page.locator('.course-phase').filter({hasText:'Mac 转写'}).locator('progress');
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
    await page.goto(origin+'/tasks');
    const groupDetails=page.locator('.group-details');
    await groupDetails.first().waitFor({state:'attached'});
    for(let i=0;i<await groupDetails.count();i++){
      if(!(await groupDetails.nth(i).evaluate(e=>e.open))) await groupDetails.nth(i).locator('> summary').click();
    }
    await page.locator('article.task-card[data-id="knowledge:local"]').waitFor();
    for(const id of ['knowledge:local','classroom-note:synthetic','ai-synthetic']){
      const details=page.locator(`article.task-card[data-id="${id}"] .task-details`);
      if(await details.count())await details.locator('summary').click();
      assert.doesNotMatch(await page.locator(`article.task-card[data-id="${id}"]`).innerText(),/实际来源待确认/);
    }
    assert.match(await page.locator('article.task-card[data-id="knowledge:local"]').innerText(),/处理位置：Mac 本机/);
    await page.locator('article.task-card[data-id="mineru-unknown"] .task-details summary').click();
    assert.match(await page.locator('article.task-card[data-id="mineru-unknown"]').innerText(),/实际来源待确认/);
    await page.locator('article.task-card[data-id="asr-confirmed"] .task-details summary').click();
    assert.match(await page.locator('article.task-card[data-id="asr-confirmed"]').innerText(),/实际：Windows/);
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
  await scenario('工作台三栏占满可用高度并分别滚动',async(page,context,origin)=>{
    activeSession={...record,status:'active',kind:'live',segments:Array.from({length:60},(_,i)=>({id:i+1,start:i*20,end:(i+1)*20,raw:'合成课堂的长段原文。'.repeat(35),corrected:'合成精修内容。'.repeat(35)}))};
    await page.setViewportSize({width:2048,height:1220});await page.goto(origin);await page.locator('#transcript .segment').first().waitFor();
    async function inspect(label){
      const proof=await page.evaluate(()=>({viewport:{width:innerWidth,height:innerHeight},documentRange:document.scrollingElement.scrollHeight-document.scrollingElement.clientHeight,
        boxes:Object.fromEntries(['.workspace','.control-panel','.classroom-workspace','.transcript-panel','.classroom-side-rail','#transcriptView','#transcript'].map(selector=>{const e=document.querySelector(selector),b=e.getBoundingClientRect();return [selector,{top:b.top,bottom:b.bottom,height:b.height,range:e.scrollHeight-e.clientHeight,overflowY:getComputedStyle(e).overflowY}]}))}));
      fs.writeFileSync(path.join(output,`workbench-height-${label}.json`),JSON.stringify(proof,null,2));
      assert.ok(proof.documentRange<=1,`workbench document must not scroll: ${JSON.stringify(proof)}`);
      const values=['.control-panel','.transcript-panel','.classroom-side-rail'].map(s=>proof.boxes[s]);
      for(const b of values){assert.ok(b.bottom<=proof.viewport.height+1);assert.ok(proof.viewport.height-b.bottom<42);}
      assert.ok(Math.max(...values.map(b=>b.bottom))-Math.min(...values.map(b=>b.bottom))<=1,'three panels share available height');
      return proof;
    }
    await inspect('large');await page.screenshot({path:path.join(output,'workbench-height-large.png')});
    const left=page.locator('.control-panel');await left.hover();await page.mouse.wheel(0,900);await page.waitForFunction(()=>document.querySelector('.control-panel').scrollTop>0);
    const main=page.locator('#transcript');await main.hover();await page.mouse.wheel(0,-1500);await page.waitForFunction(()=>document.querySelector('#transcript').scrollTop<document.querySelector('#transcript').scrollHeight-document.querySelector('#transcript').clientHeight);
    await inspect('scrolled');
    await page.setViewportSize({width:1280,height:640});await inspect('short');
    await page.locator('#sideTabChat').click();await page.locator('#chatQuestion').waitFor({state:'visible'});await inspect('chat');
    const composer=await page.locator('#chatForm').boundingBox();assert.ok(composer.y+composer.height<=640,'chat composer stays in viewport');
    await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'narrow workbench has no horizontal clipping');
  });
  await scenario('工作台中文翻译和精修公式安全渲染且原文保留',async(page,context,origin)=>{
    const translated=String.raw`相位延迟为 $\phi = \frac{2\pi}{\lambda} n L$，其中 $\lambda$ 是波长，$n$ 是折射率，$L$ 是长度。`;
    const raw='The phase delay equals two pi n L over lambda. Keep $literal$ and <b>spoken text</b> as recognized.';
    const math={...record,segments:[{id:2,start:32,end:77,language:'English',raw,corrected:String.raw`The phase delay is $\phi = \frac{2\pi}{\lambda} n L$.`,translation:translated+'<script>window.__transcriptXss=1</script><a href="javascript:window.__transcriptXss=2">unsafe link</a>'}],
      ai_reviews:[{id:'math-review',start:32,end:77,segment_ids:[2],corrected:String.raw`精修：$\lambda$ 与 $n$。`,translation:translated,manual_refinement:true}]};
    await context.route('**/api/recordings/smoke-class',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({session:math})}));
    await context.route('**/api/recordings/smoke-class/summary-status',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({job:{running:false,status:'completed'},session:math})}));
    await page.goto(origin+'/?chat='+record.id);await page.locator('.segment[data-id="2"]').waitFor();
    const card=page.locator('.segment[data-id="2"]'),translation=card.locator('.translation-text');
    await translation.locator('.katex').first().waitFor();assert.equal(await translation.locator('.katex').count(),4);
    assert.doesNotMatch(await translation.innerText(),/\$|\\frac|\\lambda/);assert.equal(await card.locator('.corrected-text .katex').count(),1);
    assert.equal(await card.locator('.raw-text .katex').count(),0);assert.ok((await card.locator('.raw-text').innerText()).endsWith(raw));
    assert.equal(await translation.locator('script,[onerror],a[href^="javascript:"]').count(),0);assert.equal(await page.evaluate(()=>window.__transcriptXss),undefined);
    assert.match(await card.locator('.segment-meta').innerText(),/00:00:32–00:01:17/);
    assert.equal(await page.locator('.periodic-review .translation-text .katex').count(),4);
    await page.goto(origin+'/library?recording='+record.id);await page.locator('#noteDialog[open]').waitFor();
    assert.equal(await page.locator('#noteTranscript .translation-text .katex').count(),4);
    await page.locator('#noteTranscriptMode').selectOption('raw');assert.equal(await page.locator('#noteTranscript .katex').count(),0);assert.match(await page.locator('#noteTranscript').innerText(),/\$literal\$/);
  });
  await scenario('结束保存期间丢弃录音必须两次确认，取消不发请求',async(page,context,origin)=>{
    activeSession={...record,status:'processing',kind:'microphone',finalization:{status:'running'}};
    let posts=0,polls=0,done=false;
    await context.route('**/api/recordings/smoke-class',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({session:activeSession || record})}));
    await context.route('**/api/recordings/smoke-class/discard',route=>{
      const request=route.request();
      if(request.method()==='POST'){
        posts++;assert.deepEqual(request.postDataJSON(),{confirmed:true,expected_session_id:'smoke-class'});
        return route.fulfill({status:202,contentType:'application/json',body:JSON.stringify({session_id:'smoke-class',status:'discarding',recoverable:true})});
      }
      polls++;done=polls>=2;if(done)activeSession=null;
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({session_id:'smoke-class',status:done?'discarded':'discarding',recoverable:true})});
    });
    await page.goto(origin);const button=page.locator('#discardRecordingButton'),dialog=page.locator('#discardRecordingDialog');
    await button.waitFor({state:'visible'});assert.equal(await page.locator('#endButton').isDisabled(),true);
    await button.click();await dialog.locator('[data-discard-cancel]').click();assert.equal(posts,0);
    await button.click();await dialog.locator('[data-discard-continue]').click();await dialog.locator('[data-discard-cancel]').click();assert.equal(posts,0);
    await button.click();await dialog.locator('[data-discard-continue]').click();
    const firstPoll=page.waitForResponse(r=>r.url().endsWith('/smoke-class/discard')&&r.request().method()==='GET');
    await dialog.locator('[data-discard-confirm]').click();await firstPoll;await page.reload();
    await button.waitFor({state:'hidden'});assert.equal(posts,1);assert.ok(polls>=2);assert.equal(done,true);
    assert.match(await page.locator('#toast').innerText(),/废纸篓/);
  });
  await scenario('丢弃录音结果核对发现已恢复时保留资料并解除旧等待状态',async(page,context,origin)=>{
    activeSession={...record,status:'processing',kind:'microphone',finalization:{status:'running'}};
    let posts=0,polls=0;
    await context.addInitScript(()=>{
      localStorage.setItem('qwen-asr-pending-discard-v1',JSON.stringify(['smoke-class']));
      localStorage.setItem('qwen-asr-pending-end-v1','smoke-class');
    });
    await context.addInitScript(old=>{
      class SyntheticSocket {
        static OPEN=1;
        constructor(){this.readyState=0;window.__restoredSocket=this;setTimeout(()=>{this.readyState=1;this.onopen?.();},10);}
        send(value){if(typeof value!=='string')return;const message=JSON.parse(value);if(message.type==='start')setTimeout(()=>this.onmessage?.({data:JSON.stringify({type:'session_started',session:{...old,id:'smoke-successor',title:'恢复之后的新录音',status:'active',kind:'microphone'}})}),10);}
        close(){this.readyState=3;this.onclose?.();}
      }
      window.WebSocket=SyntheticSocket;
      window.__lateRestoredEnd=()=>window.__restoredSocket.onmessage?.({data:JSON.stringify({type:'ended',session:old})});
    },record);
    await context.route('**/api/recordings/smoke-class/discard',route=>{
      if(route.request().method()==='POST')posts++;else polls++;
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({session_id:'smoke-class',status:'restored',recoverable:true})});
    });
    await page.goto(origin);
    await page.locator('#toast').filter({hasText:'已从废纸篓恢复'}).waitFor();
    assert.equal(posts,0);assert.equal(polls,1);
    assert.equal(await page.locator('#discardRecordingButton').isVisible(),false);
    assert.equal(await page.locator('#recordButton').isDisabled(),false);
    assert.match(await page.locator('#recordLabel').innerText(),/新建并开始/);
    assert.match(await page.locator('#listeningLabel').innerText(),/已从废纸篓恢复/);
    assert.doesNotMatch(await page.locator('#toast').innerText(),/丢弃未完成|未知|移入废纸篓/);
    assert.equal(await page.evaluate(()=>localStorage.getItem('qwen-asr-pending-end-v1')),null);
    assert.deepEqual(await page.evaluate(()=>JSON.parse(localStorage.getItem('qwen-asr-pending-discard-v1'))),[]);
    await page.locator('#recordButton').click();
    await page.waitForFunction(()=>document.getElementById('recordButton').classList.contains('recording'));
    await page.evaluate(()=>window.__lateRestoredEnd());
    assert.match(await page.locator('#recordName').inputValue(),/恢复之后的新录音/);
    assert.equal(await page.locator('#recordButton').evaluate(el=>el.classList.contains('recording')),true);
    activeSession=null;
    await page.goto(origin+'/library');
    await page.locator('.library-card').first().waitFor();
    assert.ok((await page.locator('body').innerText()).includes(record.title));
    assert.equal(posts,0);
  });
  await scenario('课堂记录直接打开笔记',async(page,context,origin)=>{await page.goto(origin+'/library?recording='+record.id);await page.locator('#noteDialog[open]').waitFor();assert.match(await page.locator('#noteSummaryReader').innerText(),/菲涅耳公式/);});
  await scenario('实测回归：活动录音恢复不抢占课堂记录与嵌入笔记入口',async(page,context,origin)=>{
    activeSession={...record,id:'active-other',title:'另一条未结束录音',category:'第二课程',status:'active',kind:'microphone'};
    const query='?category='+encodeURIComponent(record.category)+'&embedded=1&recording='+record.id;
    await page.goto(origin+'/library'+query);
    await page.locator('#noteDialog[open]').waitFor();
    assert.equal(new URL(page.url()).pathname,'/library');
    assert.equal(new URL(page.url()).searchParams.get('embedded'),'1');
    assert.equal(await page.locator('#libraryCategory').inputValue(),record.category);
    assert.equal(await page.locator('#recordCategory').inputValue(),'第二课程');
    assert.match(await page.locator('#noteSummaryReader').innerText(),/菲涅耳公式/);
    await page.reload();await page.locator('#noteDialog[open]').waitFor();
    assert.equal(new URL(page.url()).searchParams.get('recording'),record.id);
    await page.goto(origin+'/library?category='+encodeURIComponent(record.category)+'&embedded=1');
    await page.locator('.library-card').first().waitFor();
    assert.equal(await page.locator('body').evaluate(el=>el.classList.contains('library-page')),true);
    assert.equal(await page.locator('#libraryCategory').inputValue(),record.category);
  });
  await scenario('实测回归：PDF暂忙保留笔记页面并允许重试下载',async(page,context,origin)=>{
    noteReviewEnabled=false;
    const pdfRecord={...record,ai_note_summary:{...record.ai_note_summary,pdf_ready:true,review_enabled:false,review_policy:'disabled_by_settings'},downloads:{...record.downloads,ai_summary:'/api/recordings/smoke-class/download/ai-summary'}};
    await context.route('**/api/recordings/smoke-class',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({session:pdfRecord})}));
    await context.route('**/api/recordings/smoke-class/summary-status',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({job:{running:false,status:'completed'},session:pdfRecord})}));
    let downloads=0;
    await context.route('**/api/recordings/smoke-class/download/ai-summary',route=>++downloads===1
      ? route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'这条录音正在处理，请完成后再操作'})})
      : route.fulfill({status:200,contentType:'application/pdf',headers:{'Content-Disposition':"attachment; filename*=UTF-8''%E8%AF%BE%E5%A0%82%E7%AC%94%E8%AE%B0.pdf"},body:'%PDF-1.4\nsynthetic download\n%%EOF'}));
    await page.goto(origin+'/library?recording='+record.id);await page.locator('#noteDialog[open]').waitFor();
    const before=page.url();await page.locator('#downloadNoteSummary').click();
    await page.locator('#toast').filter({hasText:'请稍后重试下载'}).waitFor();
    assert.equal(page.url(),before);assert.equal(await page.locator('#noteDialog').evaluate(el=>el.open),true);
    const received=page.waitForEvent('download');await page.locator('#downloadNoteSummary').click();
    assert.equal((await received).suggestedFilename(),'课堂笔记.pdf');
    assert.equal(page.url(),before);assert.equal(downloads,2);
  });
  await scenario('实测回归：任务状态中文显示且计数跟随筛选范围',async(page,context,origin)=>{
    tasks=[{id:'knowledge:A',type:'knowledge_index',family:'knowledge',title:'课程A索引',label:'课程知识索引',category:'课程A',status:'queued',stage:'waiting_asr',progress:0,actions:['cancel']},
      {id:'knowledge:B',type:'knowledge_index',family:'knowledge',title:'课程B索引',label:'课程知识索引',category:'课程B',status:'completed',stage:'completed',progress:100,actions:[]}];
    await page.goto(origin+'/tasks');await page.locator('#lastUpdated').filter({hasText:'已同步'}).waitFor();
    await page.locator('#categoryFilter').selectOption('课程A');
    assert.equal(await page.locator('[data-count="all"]').innerText(),'1');
    assert.equal(await page.locator('[data-count="active"]').innerText(),'1');
    assert.equal(await page.locator('[data-count="completed"]').innerText(),'0');
    assert.match(await page.locator('#taskList').innerText(),/等待本机转写完成后建立知识索引/);
    assert.doesNotMatch(await page.locator('#taskList').innerText(),/waiting_asr/);
    await page.locator('#taskSearch').fill('不存在');
    await page.waitForFunction(()=>document.querySelector('[data-count="all"]').textContent==='0');
    assert.equal(await page.locator('[data-count="active"]').innerText(),'0');
  });
  await scenario('任务默认过滤分组与恢复',async(page,context,origin)=>{await page.goto(origin+'/tasks');await page.locator('.task-group').waitFor();assert.equal(await page.locator('.task-group').count(),1);assert.equal(await page.locator('.task-card').count(),2);const gd=page.locator('.group-details');if(await gd.count()&&!(await gd.first().evaluate(e=>e.open)))await page.locator('.group-details > summary').first().click();await page.locator('article.task-card[data-id="summary:smoke-class"] [data-action="continue"]').click();await page.getByText('任务已加入后台队列',{exact:true}).waitFor();assert.ok(requests.some(request=>request.path==='/api/tasks/action'));await page.locator('[data-status="completed"]').click();assert.equal(await page.locator('.task-card').count(),1);await page.goto(origin+'/tasks?category='+encodeURIComponent('空课程'));await page.locator('#lastUpdated').filter({hasText:'已同步'}).waitFor();assert.equal(await page.locator('.task-card').count(),0);});
  await scenario('MinerU 两端稳定耗时进度、刷新与旧任务兼容',async(page,context,origin)=>{
    const task={id:'mineru-remote',type:'course_pdf',family:'mineru',label:'课程资料识别',title:'合成讲义 · Windows',category:'合成课程',status:'running',
      stage:'Windows · MinerU · 版面分析 · 阶段 100% · 第 33–36 页/90 · 窗口 9/23',progress:42.5,actions:['pause','cancel'],
      progress_estimate:{estimated:true,percent:42.5,phase:'pages',elapsed_seconds:195,remaining_seconds:260,sample_count:3,observed_windows:7},configured_provider:'remote_only'};
    tasks=[task,{...structuredClone(task),id:'mineru-local',title:'合成讲义 · Mac',configured_provider:'local',stage:'Mac · MinerU · 文字识别 · 阶段 50%',progress:21.4,
      progress_estimate:{...task.progress_estimate,percent:21.4,sample_count:0,observed_windows:0}},
      {...structuredClone(task),id:'mineru-legacy',title:'尚未启用耗时预估的旧任务',progress:null,progress_estimate:null},
      {...structuredClone(task),id:'mineru-loading',title:'刚开始准备的任务',progress:null,progress_estimate:null,stage:'正在加载模型'}];
    await page.goto(origin+'/tasks');
    const gd=page.locator('.group-details');
    for(let i=0;i<await gd.count();i++){if(!(await gd.nth(i).evaluate(e=>e.open)))await gd.nth(i).locator('> summary').click();}
    const card=page.locator('article.task-card[data-id="mineru-remote"]');await card.waitFor();
    assert.equal(await card.locator('progress').getAttribute('value'),'42.5');
    assert.match(await card.innerText(),/整体预估 42.5%/);
    assert.match(await card.innerText(),/预计剩余约 4 分 20 秒/);
    assert.match(await page.locator('article.task-card[data-id="mineru-local"]').innerText(),/初步估算/);
    assert.equal(await page.locator('article.task-card[data-id="mineru-legacy"] progress').getAttribute('value'),'35.6');
    assert.match(await page.locator('article.task-card[data-id="mineru-legacy"]').innerText(),/已完成页段/);
    assert.equal(await page.locator('article.task-card[data-id="mineru-loading"] .task-progress-indeterminate').evaluate(el=>getComputedStyle(el).animationName),'none');
    await page.screenshot({path:path.join(output,'mineru-time-progress.png'),fullPage:true});
    tasks[0]={...tasks[0],progress:62.1,progress_estimate:{...task.progress_estimate,percent:62.1,overrun:true,remaining_seconds:null}};
    await page.reload();
    const gdReload=page.locator('.group-details');
    for(let i=0;i<await gdReload.count();i++){if(!(await gdReload.nth(i).evaluate(e=>e.open)))await gdReload.nth(i).locator('> summary').click();}
    await card.waitFor();
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
    assert.doesNotMatch(await page.locator('.group-focus[data-id="course_queue"]').innerText(),/遥测|来源未验证|资源释放/);
    await page.locator('[data-detail-key="collection:completed"] > summary').click();
    await page.locator('[data-group-id="task:remote-asr"] .group-details > summary').click();
    await page.locator('[data-group-id="task:remote-asr"] .group-history > summary').click();
    await page.locator('article.task-card[data-id="remote-asr"]').waitFor({state:'visible'});
    assert.match(await page.locator('article.task-card[data-id="remote-asr"]').innerText(),/显存释放已确认/);
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
  await scenario('旧复习生成工具已移除，资料上传统一使用课程入口',async(page,context,origin)=>{
    await page.goto(origin+'/notes');
    await page.locator('#filesInput').waitFor({state:'attached'});
    assert.equal(await page.locator('.external-import, #review-lessons, #review-noteFile').count(),0);
    assert.equal(await page.locator('#uploadKind').count(),1);
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
  await scenario('资料 PDF 标题点击后请求 Chrome 查看并保留下载',async(page,context,origin)=>{
    courseFixtureFiles=[
      {id:'original-pdf',name:'合成课程-第01章-课件.pdf',kind:'课件',size:20,created_at:'2026-10-20',conversion:{status:'completed'},sync:[]},
      {id:'converted-office',name:'合成课程-第02章-课件.pptx',kind:'课件',size:20,created_at:'2026-10-20',conversion:{status:'completed'},sync:[]},
      {id:'unconverted-office',name:'合成课程-作业.docx',kind:'作业',size:20,created_at:'2026-10-20',conversion:{status:'none'},sync:[]},
    ];
    await page.goto(origin+'/course-files?category='+encodeURIComponent(record.category));
    const original=page.locator('[data-id="original-pdf"] .file-title-link');
    await original.waitFor();
    await original.click();
    await page.waitForFunction(()=>document.getElementById('toast')?.textContent.includes('Chrome'));
    const request=requests.find(item=>item.path==='/api/course-files/original-pdf/open-pdf');
    assert.equal(request.method,'POST');assert.equal(JSON.parse(request.body).variant,'original');
    await page.locator('[data-id="converted-office"] .file-title-link').click();
    await page.waitForTimeout(100);
    const converted=requests.find(item=>item.path==='/api/course-files/converted-office/open-pdf');
    assert.equal(JSON.parse(converted.body).variant,'pdf');
    assert.equal(await page.locator('[data-id="unconverted-office"] .file-title-link').count(),0);
    assert.equal(await page.locator('[data-id="original-pdf"] a[download]').count(),2);
    await page.locator('[data-review-id="review-old"] .file-title-link').click();
    await page.waitForTimeout(100);
    assert.ok(requests.some(item=>item.path==='/api/notes/review-old/open-pdf' && item.method==='POST'));
    await page.screenshot({path:path.join(output,'course-pdf-title-chrome.png'),fullPage:true});
  });
  await scenario('课件笔记无转写时自动预览正文和插图',async(page,context,origin)=>{
    const old=structuredClone(record);
    try {
      record.segments=[];record.has_transcript=false;
      record.ai_note_summary={summary:'# 合成课件笔记\n\nFM 与频分复用。',abstract:'课件摘要',review_package:'.review-notes/'+'a'.repeat(24)};
      const asset='fig-'+'b'.repeat(16)+'.png';
      await page.route('**/ideashell/preview',async route=>{
        const body=route.request().postDataJSON();
        assert.equal(body.content_mode,'summary');
        await route.fulfill({contentType:'application/json',body:JSON.stringify({mode:'summary',content:'# 合成课件笔记\n\nFM 与频分复用。\n\n![课件原图](assets/'+asset+')',note_count:1,characters:30,digest:'material-only',audio_parts:[],audio_error:'本次按课件整理，录音附件已略过'})});
      });
      await page.goto(origin+'/library?recording='+record.id);
      await page.locator('#noteDialog[open]').waitFor();
      await page.locator('#pushNoteSummary').click();
      await page.waitForFunction(()=>document.getElementById('pushPreviewStatus').textContent.includes('1 篇笔记'));
      assert.equal(await page.locator('#pushContentMode').inputValue(),'summary');
      assert.equal(await page.locator('#pushContentMode option[value=combined]').isDisabled(),true);
      assert.equal(await page.locator('#pushContentMode option[value=transcript]').isDisabled(),true);
      assert.match(await page.locator('#pushPreview').innerText(),/FM 与频分复用/);
      assert.equal(await page.locator('#pushPreview img').getAttribute('src'),`/api/recordings/${record.id}/note-assets/${'a'.repeat(24)}/${asset}`);
      assert.match(await page.locator('#pushFooterHint').innerText(),/本次不附带录音/);
      assert.equal(await page.locator('#confirmPush').isEnabled(),true);
      assert.equal(requests.some(request=>request.path.endsWith('/ideashell/push')),false);
      await page.screenshot({path:path.join(output,'ideashell-material-only-preview.png'),fullPage:true});
    } finally {
      for(const key of Object.keys(record))delete record[key];
      Object.assign(record,old);
    }
  });
  await scenario('图文笔记预览包含图片附件与 PDF 并校验附件摘要',async(page,context,origin)=>{
    const diagram='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="640" height="180" viewBox="0 0 640 180"><rect width="640" height="180" rx="12" fill="#e9f4ef"/><path d="M80 90H560" stroke="#317b5c" stroke-width="6"/><circle cx="120" cy="90" r="30" fill="#317b5c"/><rect x="430" y="55" width="70" height="70" fill="#317b5c"/><text x="220" y="70" font-size="24" fill="#204331">Synthetic figure</text></svg>');
    await page.route('**/ideashell/preview',async route=>{
      await route.fulfill({contentType:'application/json',body:JSON.stringify({content:'# 图文笔记\n\n【配图 1：光路；图片附件：配图-01.png】',characters:40,digest:'figures',assets_digest:'bound-figures',images:[{name:'配图-01.png',bytes:100,sha256:'figure-hash'}],documents:[{name:'课后复习笔记.pdf',bytes:200,sha256:'pdf-hash'}],image_previews:[{name:'配图-01.png',url:diagram}],image_note:'图片保存在笔记附件中，PDF 保留原图文排版',audio_parts:[]})});
    });
    await page.route('**/ideashell/push',async route=>{
      const body=route.request().postDataJSON();assert.equal(body.assets_digest,'bound-figures');
      await route.fulfill({contentType:'application/json',body:JSON.stringify({session:record,images_attached:1,documents_attached:1})});
    });
    await page.goto(origin+'/library?recording='+record.id);
    await page.locator('#noteDialog[open]').waitFor();
    await page.locator('#pushNoteSummary').click();
    await page.waitForFunction(()=>document.getElementById('pushPreviewStatus').textContent.includes('图片附件'));
    assert.match(await page.locator('#pushPreviewStatus').innerText(),/1 张图片附件＋图文 PDF/);
    assert.equal(await page.locator('#pushPreview figure img').getAttribute('alt'),'配图-01.png');
    await page.waitForFunction(()=>document.querySelector('#pushPreview figure img')?.naturalWidth===640);
    await page.screenshot({path:path.join(output,'ideashell-figure-attachments-preview.png'),fullPage:true});
    await page.locator('#confirmPush').click();
    await page.waitForFunction(()=>!document.getElementById('pushDialog').open);
  });
  await scenario('已存笔记 PDF 在对应课堂提供下载',async(page,context,origin)=>{
    const previous=record.note_pdfs;
    try {
      record.note_pdfs=[{id:'a'.repeat(32),title:'图文复习笔记',filename:'note.pdf',download_url:'/api/recordings/smoke-class/note-pdfs/'+'a'.repeat(32)}];
      await page.goto(origin+'/library?recording='+record.id);
      await page.locator('#noteDialog[open]').waitFor();
      const link=page.locator('#noteSavedPdfs a');
      assert.match(await link.innerText(),/图文复习笔记/);
      assert.equal(await link.getAttribute('href'),record.note_pdfs[0].download_url);
      assert.equal(await link.isVisible(),true);
    } finally {if(previous===undefined)delete record.note_pdfs;else record.note_pdfs=previous;}
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
  await scenario('课程内笔记与任务抽屉',async(page,context,origin)=>{await page.goto(origin+'/workspace?category='+encodeURIComponent(record.category));await page.locator('[data-recording="smoke-class"]').waitFor();await page.screenshot({path:path.join(output,'workspace.png'),fullPage:true});await page.locator('[data-recording="smoke-class"]').click();await page.frameLocator('#workspaceNoteFrame').locator('#noteDialog[open]').waitFor();await page.frameLocator('#workspaceNoteFrame').locator('#closeNoteDialog').click();await page.locator('#workspaceNoteDialog').waitFor({state:'hidden'});await page.locator('#workspaceTasks').click();await page.locator('#timelineDialog[open]').waitFor();const timeline=page.frameLocator('#timelineFrame');await timeline.locator('.task-group').first().waitFor();await timeline.locator('.group-details > summary').first().click();await timeline.locator('.task-card').first().waitFor();});
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
    await reader.locator('#closeNoteDialog').click();await saveStarted;
    assert.equal(await outer.isVisible(),true,'save must finish before destroying the reader');
    releaseSave();await outer.waitFor({state:'hidden'});
    await page.waitForFunction(pos=>scrollX===pos.x && scrollY===pos.y,scroll);
    assert.equal(page.url(),workspaceUrl);assert.equal(await page.locator('#workspaceCourse').inputValue(),record.category);
    assert.deepEqual(await page.evaluate(()=>({x:scrollX,y:scrollY})),scroll);
    assert.equal(await row.evaluate(button=>document.activeElement===button),true);
    assert.equal(await page.locator('#workspaceTabs [aria-current="page"]').getAttribute('data-tab'),'overview');
    await row.click();await reader.locator('#noteDialog[open]').waitFor();
    await outer.evaluate(dialog=>{dialog.setAttribute('tabindex','-1');dialog.focus();});await page.keyboard.press('Escape');await outer.waitFor({state:'hidden'});
    await page.setViewportSize({width:390,height:844});await row.click();await reader.locator('#noteDialog[open]').waitFor();
    const mobile=await outer.boundingBox();assert.equal(mobile.width,390);assert.equal(mobile.height,844);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await reader.locator('#closeNoteDialog').click();await outer.waitFor({state:'hidden'});
  });
  await scenario('全屏笔记保存后进入本堂课问答，拒绝其他窗口的跳转消息',async(page,context,origin)=>{
    let failSave=true;
    await context.route('**/api/recordings/smoke-class/summary',async route=>{
      if(failSave)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'合成保存失败'})});
      const body=route.request().postDataJSON();
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({summary:{summary:body.summary},session:{...record,ai_note_summary:{summary:body.summary}}})});
    });
    await page.goto(origin+'/workspace?category='+encodeURIComponent(record.category));
    await page.locator('#workspaceRecent [data-recording="smoke-class"]').click();
    const reader=page.frameLocator('#workspaceNoteFrame'),outer=page.locator('#workspaceNoteDialog[open]');
    await reader.locator('#noteDialog[open]').waitFor();
    await page.evaluate(()=>window.postMessage({type:'qwenasr:open-classroom-chat',recording:'smoke-class'},location.origin));
    assert.equal(await outer.isVisible(),true,'parent window cannot impersonate the reader');
    const frame=await (await page.locator('#workspaceNoteFrame').elementHandle()).contentFrame();
    await frame.evaluate(()=>parent.postMessage({type:'qwenasr:open-classroom-chat',recording:'different-class'},location.origin));
    assert.equal(await outer.isVisible(),true,'message must identify the currently open recording');
    await reader.locator('#toggleNoteSource').click();await reader.locator('#noteSummaryEditor').fill('# 进入问答前需要保存\n\n人工笔记。');
    await reader.locator('#askNoteClass').click();
    await reader.locator('#noteSaveStatus').getByText('失败',{exact:false}).waitFor();
    assert.equal(await outer.isVisible(),true);assert.equal(await page.locator('#workspaceFrame').isVisible(),false);
    failSave=false;await reader.locator('#askNoteClass').click();await outer.waitFor({state:'hidden'});
    await page.waitForFunction(()=>document.querySelector('#workspaceTabs [aria-current="page"]')?.dataset.tab==='review'
      && new URL(document.getElementById('workspaceFrame').src).searchParams.get('chat')==='smoke-class');
    assert.equal(await page.locator('#workspaceTabs [aria-current="page"]').getAttribute('data-tab'),'review');
    const url=new URL(await page.locator('#workspaceFrame').getAttribute('src'),origin);
    assert.equal(url.searchParams.get('chat'),'smoke-class');
    await page.frameLocator('#workspaceFrame').locator('#classroomChat').waitFor();
  });
  await scenario('普通课堂记录全屏笔记只保留内容滚动',async(page,context,origin)=>{
    const long={...record,title:'完整课堂笔记与较长标题，用于验证全屏阅读的滚动边界',
      segments:Array.from({length:90},(_,i)=>({id:i+1,start:i*60,end:(i+1)*60,raw:'合成课堂原文。'.repeat(35),corrected:'合成课堂精修原文。'.repeat(35)})),
      ai_note_summary:{summary:Array.from({length:65},(_,i)=>`## 第 ${i+1} 节\n\n${'合成课堂笔记的完整段落。'.repeat(45)}`).join('\n\n')},ai_note_proposal:{task_id:'synthetic-proposal'}};
    syntheticRecords=Array.from({length:35},(_,i)=>({...record,id:i?`synthetic-${i}`:record.id,title:`第 ${i+1} 节合成课堂`}));
    await context.route('**/api/recordings/smoke-class',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({session:long})}));
    await context.route('**/api/recordings/smoke-class/summary-status',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({job:{running:false,status:'completed'},session:long})}));
    await context.route('**/api/recordings/smoke-class/note-proposal',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({task_id:'synthetic-proposal',current_summary:long.ai_note_summary.summary,note:{pdf_ready:true,summary:'# 合成候选版本\n\n只供预览。'}})}));
    await page.setViewportSize({width:1440,height:650});await page.goto(origin+'/library');
    await page.locator('.library-open').first().waitFor();
    await page.evaluate(()=>scrollTo(0,300));
    await page.locator('.library-open').first().click();await page.locator('#noteDialog[open]').waitFor();
    async function inspect(label){
      const proof=await page.evaluate(()=>{
        const range=el=>el.scrollHeight-el.clientHeight;
        const list=[document.documentElement,document.body,...document.querySelectorAll('#noteDialog,#noteColumns,.note-summary-pane,.reader-layout,#noteSummaryReader,#noteSummaryEditor,#noteTranscript')];
        return {viewport:{width:innerWidth,height:innerHeight},scrollY,
          scrollHosts:list.filter(el=>el.getClientRects().length).map(el=>({id:el===document.documentElement?'HTML':el===document.body?'BODY':el.id||el.className||el.tagName,range:range(el),overflowY:getComputedStyle(el).overflowY,right:el.getBoundingClientRect().right,bottom:el.getBoundingClientRect().bottom,height:el.clientHeight})),
          backgroundVisible:!!document.querySelector('.shell')?.getClientRects().length};
      });
      fs.writeFileSync(path.join(output,`note-scroll-${label}.json`),JSON.stringify(proof,null,2));
      const scrolling=proof.scrollHosts.filter(el=>el.range>1 && /auto|scroll/.test(el.overflowY));
      const right=scrolling.filter(el=>Math.abs(el.right-proof.viewport.width)<3);
      assert.deepEqual(right.map(el=>el.id),[await page.locator('#noteSummaryEditor').isVisible()?'noteSummaryEditor':'noteSummaryReader'],JSON.stringify(proof));
      for(const id of ['HTML','BODY','noteDialog','noteColumns','note-summary-pane','reader-layout']){
        const host=proof.scrollHosts.find(el=>el.id===id);if(host)assert.ok(host.range<=1,`${id} has nested overflow: ${JSON.stringify(proof)}`);
      }
      return proof;
    }
    await inspect('desktop');await page.screenshot({path:path.join(output,'note-scroll-desktop.png')});
    await page.locator('#previewNoteProposal').click();await page.getByRole('button',{name:'保留原文并关闭',exact:true}).click();
    assert.equal(await page.evaluate(()=>document.documentElement.classList.contains('note-reader-open')),true,'nested preview close preserves main reader lock');
    await inspect('preview-closed');
    const reader=page.locator('#noteSummaryReader');await reader.hover();await page.mouse.wheel(0,900);await page.waitForFunction(()=>document.getElementById('noteSummaryReader').scrollTop>0);
    await reader.evaluate(el=>el.scrollTop=el.scrollHeight);await reader.hover();await page.mouse.wheel(0,900);await inspect('desktop-end');
    await page.locator('#toggleNoteFocus').click();await inspect('focus');
    await page.locator('#toggleNoteSource').click();await inspect('source');await page.locator('#toggleNoteSource').click();
    await page.setViewportSize({width:390,height:540});await inspect('mobile');await page.screenshot({path:path.join(output,'note-scroll-mobile.png')});
    await page.locator('#toggleNoteMobilePane').click();assert.equal(await page.locator('.note-summary-pane').isVisible(),false);
    const transcript=page.locator('#noteTranscript');await transcript.hover();await page.mouse.wheel(0,700);await page.waitForFunction(()=>document.getElementById('noteTranscript').scrollTop>0);
    await page.locator('#toggleNoteMobilePane').click();
    await page.locator('#closeNoteDialog').click();await page.locator('#noteDialog').waitFor({state:'hidden'});
    await page.waitForFunction(()=>!document.documentElement.classList.contains('note-reader-open'));
    assert.equal(await page.locator('.shell').isVisible(),true);
    assert.equal(await page.evaluate(()=>document.documentElement.classList.contains('note-reader-open')),false);
    await page.setViewportSize({width:1440,height:650});assert.ok(await page.evaluate(()=>document.scrollingElement.scrollHeight>innerHeight),'library scroll restored');
  });
  await scenario('长课堂笔记仅一个标题，内容独立滚动且无外层裁切',async(page,context,origin)=>{
    const fixturePath=process.env.QWEN_ASR_LONG_NOTE_FIXTURE || path.join(output,'real-long-note-fixture.json');
    const real=fs.existsSync(fixturePath)?JSON.parse(fs.readFileSync(fixturePath,'utf8')).session:null;
    const fallbackSummary=Array.from({length:90},(_,i)=>`## 第 ${i+1} 节\n\n较长的课堂笔记段落，用于检查实际内容滚动。${'公式含义、条件和结论。'.repeat(30)}\n`).join('\n');
    const long={...record,...real,id:record.id,category:record.category,
      title:(real?.title || '长课堂笔记滚动检查')+'（标题放大、换行与长课堂阅读布局验证）',
      segments:real?.segments || Array.from({length:90},(_,i)=>({id:i+1,start:i*60,end:(i+1)*60,raw:'长段录音原文。'.repeat(40),corrected:'长段录音精修原文。'.repeat(40)})),
      ai_note_summary:{...real?.ai_note_summary,summary:real?.ai_note_summary?.summary || fallbackSummary}};
    await context.route('**/api/recordings/smoke-class',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({session:long})}));
    await context.route('**/api/recordings/smoke-class/summary-status',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({job:{running:false,status:'completed'},session:long})}));
    await page.setViewportSize({width:1440,height:900});await page.goto(origin+'/workspace?category='+encodeURIComponent(record.category));
    await page.locator('#workspaceRecent [data-recording="smoke-class"]').click();
    const frame=await (await page.locator('#workspaceNoteFrame').elementHandle()).contentFrame();
    await frame.locator('#noteDialog[open]').waitFor();await page.locator('#workspaceNoteReturn').waitFor({state:'hidden'});
    async function noOuterScroll() {
      assert.equal(await page.locator('#workspaceNoteDialog h2').count(),0);
      assert.equal(await page.locator('#workspaceNoteReturn').isVisible(),false);
      assert.equal(await frame.locator('#noteDialogTitle:visible').count(),1);
      const parent=await page.evaluate(()=>({height:innerHeight,range:document.scrollingElement.scrollHeight-document.scrollingElement.clientHeight,y:scrollY}));
      assert.equal(parent.range,0);assert.equal(parent.y,0);
      const layout=await frame.evaluate(()=>{
        const range=el=>el.scrollHeight-el.clientHeight;
        const d=document.getElementById('noteDialog'),c=document.getElementById('noteColumns'),r=document.querySelector('.reader-layout');
        return {document:range(document.scrollingElement),body:range(document.body),dialog:range(d),columns:range(c),readerLayout:range(r),
          headerBottom:d.querySelector('.note-dialog-header').getBoundingClientRect().bottom,columnsTop:c.getBoundingClientRect().top,
          columnsBottom:c.getBoundingClientRect().bottom,viewport:innerHeight,
          content:[...d.querySelectorAll('#noteSummaryReader,#noteSummaryEditor,#noteTranscript')].filter(el=>el.getClientRects().length).map(el=>({id:el.id,height:el.clientHeight,bottom:el.getBoundingClientRect().bottom,range:range(el)}))};
      });
      for(const name of ['document','body','dialog','columns','readerLayout'])assert.ok(layout[name]<=1,`${name} must not scroll: ${JSON.stringify(layout)}`);
      assert.ok(Math.abs(layout.headerBottom-layout.columnsTop)<=1);
      assert.ok(Math.abs(layout.columnsBottom-layout.viewport)<=1);
      for(const content of layout.content){assert.ok(content.height>100,`${content.id} has usable remaining space`);assert.ok(content.bottom<=layout.viewport+1);assert.ok(content.range>0);}
    }
    async function wheelContent(id) {
      const content=frame.locator('#'+id);
      await content.evaluate(el=>el.scrollTop=0);await content.hover();await page.mouse.wheel(0,720);
      await frame.waitForFunction(name=>document.getElementById(name).scrollTop>0,id);
      await noOuterScroll();
      await content.evaluate(el=>el.scrollTop=el.scrollHeight);
      await content.hover();await page.mouse.wheel(0,720);await noOuterScroll();
    }
    await noOuterScroll();await wheelContent('noteSummaryReader');await wheelContent('noteTranscript');
    const last=await frame.locator('#noteSummaryReader .markdown-block').last().boundingBox();
    const readerBounds=await frame.locator('#noteSummaryReader').boundingBox();
    assert.ok(last.y+last.height<=readerBounds.y+readerBounds.height+1,'last note block can be reached without clipping');
    await frame.locator('#toggleNoteToc').click();await frame.locator('#noteToc:not(.hidden)').waitFor();await noOuterScroll();
    await frame.locator('#toggleNoteFocus').click();assert.equal(await frame.locator('.note-transcript-pane').isVisible(),false);await noOuterScroll();
    await frame.locator('#toggleNoteSource').click();assert.equal(await frame.locator('.reader-layout').isVisible(),false);await wheelContent('noteSummaryEditor');
    await frame.locator('#toggleNoteSource').click();await frame.locator('#toggleNoteFocus').click();
    // Increase text size so the title and controls wrap; remaining space follows
    // their actual height, rather than a guessed 66 px header.
    await frame.addStyleTag({content:'.note-reader-mode #noteDialogTitle{font-size:25px}.note-reader-mode .note-dialog-header small{font-size:18px}.note-reader-mode .mini-button{font-size:17px}'});
    await noOuterScroll();await wheelContent('noteSummaryReader');
    await page.screenshot({path:path.join(output,'note-reader-long-desktop.png')});
    await page.setViewportSize({width:390,height:844});
    await noOuterScroll();assert.equal(await frame.locator('.note-transcript-pane').isVisible(),false);
    await wheelContent('noteSummaryReader');
    await frame.locator('#toggleNoteMobilePane').click();assert.equal(await frame.locator('.note-summary-pane').isVisible(),false);assert.equal(await frame.locator('.note-transcript-pane').isVisible(),true);
    await wheelContent('noteTranscript');await frame.locator('#noteTranscriptMode').selectOption('raw');await wheelContent('noteTranscript');
    await frame.locator('#toggleNoteMobilePane').click();await frame.locator('#toggleNoteSource').click();await wheelContent('noteSummaryEditor');await frame.locator('#toggleNoteSource').click();
    await page.screenshot({path:path.join(output,'note-reader-long-mobile.png')});
    fs.writeFileSync(path.join(output,'note-reader-content-proof.json'),JSON.stringify({fixture_is_real:!!real,summary_characters:long.ai_note_summary.summary.length,segments:long.segments.length,transport:'offline fixture; no production writes',desktop_and_mobile_passed:true},null,2));
    await frame.locator('#closeNoteDialog').click();await page.locator('#workspaceNoteDialog').waitFor({state:'hidden'});
  });
  await scenario('全屏笔记保存失败且草稿不可落盘时禁止关闭',async(page,context,origin)=>{
    let failSave=true;
    await context.addInitScript(()=>{
      const original=Storage.prototype.setItem;
      Storage.prototype.setItem=function(key,value){if(String(key).startsWith('qwen-asr-note-draft-v1:'))throw new Error('synthetic draft storage unavailable');return original.call(this,key,value);};
    });
    await context.route('**/api/recordings/smoke-class/summary',route=>{
      const body=route.request().postDataJSON();
      return route.fulfill({status:failSave?503:200,contentType:'application/json',body:JSON.stringify(failSave?{detail:'synthetic save unavailable'}:{summary:{summary:body.summary},session:{...record,ai_note_summary:{summary:body.summary}}})});
    });
    await page.goto(origin+'/workspace?category='+encodeURIComponent(record.category));await page.locator('#workspaceRecent [data-recording="smoke-class"]').click();
    const reader=page.frameLocator('#workspaceNoteFrame'),outer=page.locator('#workspaceNoteDialog[open]');await reader.locator('#noteDialog[open]').waitFor();
    await reader.locator('#toggleNoteSource').click();await reader.locator('#noteSummaryEditor').fill('# 未保存保护\n\n尚未成功保存的手动修改。');
    await reader.locator('#closeNoteDialog').click();await reader.locator('#toast').filter({hasText:'保存失败，请保留页面'}).waitFor();
    assert.equal(await outer.isVisible(),true);assert.equal(await reader.locator('#noteDialog').isVisible(),true);assert.match(await reader.locator('#noteSummaryEditor').inputValue(),/未保存保护/);
    failSave=false;await reader.locator('#closeNoteDialog').click();await outer.waitFor({state:'hidden'});
  });
  await scenario('后台笔记完成后摘要列表刷新不覆盖阅读正文',async(page,context,origin)=>{
    let finish, completed=false;
    const ready=new Promise(resolve=>finish=resolve);
    const full={...record,ai_note_summary:{...record.ai_note_summary,summary:'# 后台完整笔记\n\n正文必须保留在阅读器中。'}};
    await context.route('**/api/library?*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({
      categories:[record.category],storage_root:'/synthetic',total:1,next_offset:null,calendar:{},
      records:[{...record,segments:undefined,has_note:true,summary_job:{status:completed?'completed':'running'},ai_note_summary:{abstract:'只用于列表的摘要'}}]})}));
    await context.route('**/api/recordings/smoke-class',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({session:full})}));
    await context.route('**/api/recordings/smoke-class/summary-status',async route=>{
      await ready;completed=true;
      await route.fulfill({status:200,contentType:'application/json',body:'{"running":false,"status":"completed"}'});
    });
    await page.goto(origin+'/library?recording=smoke-class');
    await page.locator('#noteDialog[open]').waitFor();
    finish();
    await page.locator('#toast').filter({hasText:'课后复习笔记已保存'}).waitFor();
    await page.waitForTimeout(350);
    assert.match(await page.locator('#noteSummaryReader').innerText(),/正文必须保留在阅读器中/);
    assert.equal(await page.locator('#downloadNoteSummary').evaluate(el=>el.classList.contains('disabled-download')),false);
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
