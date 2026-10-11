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
let tasks,uploaded,activeSession,requests=[];
const pages={'/':'index.html','/library':'index.html','/tasks':'tasks.html','/notes':'notes.html','/workspace':'workspace.html','/course-files':'course-files.html','/course/':'smartclass.html'};
const server=http.createServer((req,res)=>{
 const url=new URL(req.url,'http://localhost');
 const file=pages[url.pathname] ? path.join(root,'static',pages[url.pathname]) : path.join(root,url.pathname);
 if(!file.startsWith(root+path.sep) || !fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}
 res.setHeader('Content-Type',(/\.m?js$/.test(file))?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html':'application/octet-stream');res.end(fs.readFileSync(file));
});
async function fixtures(route) {
 const request=route.request(),url=new URL(request.url()),p=url.pathname;
 requests.push({path:p,method:request.method(),body:request.postData()});
 let data={};
 if(p==='/course/api/status')data={state:'idle',message:'合成课程回放',identity:null,courses:[],options:{},imports:[{state:'completed',record_id:record.id,title:record.title}],queue:{remaining:0}};
 else if(p==='/api/config')data={feature_version:'library-upgrade-v15-classroom-chat'};
 else if(p==='/api/health')data={status:'idle',detail:'合成服务就绪',model:'fake',device:'fake',asr_provider:'remote_only',local_model_loaded:false};
 else if(p==='/api/library')data={records:[record],categories:['未分类',record.category],category_profiles:{},storage_root:'/synthetic'};
 else if(p==='/api/session')data={active:!!activeSession,session:activeSession};
 else if(p==='/api/tasks')data={tasks,counts:{all:tasks.length,attention:1,active:1,completed:1},history:{},warnings:[]};
 else if(p==='/api/tasks/action'){const intent=request.postDataJSON();assert.equal(intent.use_saved_ai_settings,true);tasks=tasks.map(task=>task.id===intent.id?{...task,status:'queued',stage:'已加入队列'}:task);data={ok:true};}
 else if(p==='/api/ai-tasks')data={tasks:[]};
 else if(p==='/api/inference/settings')data={settings:{asr_provider:'remote_only',mineru_provider:'local',base_url:'http://synthetic.invalid'}};
 else if(p==='/api/ai/settings')data={settings:{function_providers:{}},has_api_key:false};
 else if(p==='/api/ai/classroom-chat/settings')data={provider:{transport:'api',model:'fake-model',base_url:'http://synthetic.invalid',context_window:131072,max_output_tokens:1000},context:{},system_prompt:'合成',include_context:true,include_terminology:true,include_summary:true,include_history:true,include_partial:false};
 else if(p==='/api/ai/classroom-chat/models')data={models:[]};
 else if(p==='/api/ideashell/status')data={status:'disconnected',connected:false};
 else if(p==='/api/library/events'){await route.fulfill({status:200,contentType:'text/event-stream',body:': offline\n\n'});return;}
 else if(p===`/api/recordings/${record.id}`)data={session:record};
 else if(p.endsWith('/summary-status'))data={job:{running:false,status:'completed'},session:record};
 else if(p.endsWith('/chat'))data={turns:[],conversation_id:'synthetic',context:{},session_id:record.id};
 else if(p==='/api/upload-jobs' && request.method()==='POST')data={job:{id:'synthetic-upload',recording_id:record.id},id:'synthetic-upload'};
 else if(p==='/api/upload-jobs/synthetic-upload/events'){await route.fulfill({status:200,contentType:'application/x-ndjson',body:JSON.stringify({type:'complete',progress:100,stage:'完成',session:record})+'\n'});return;}
 else if(p==='/api/course-files')data={category:record.category,categories:[record.category],files:uploaded?[{id:'synthetic-file',name:'合成讲义.txt',kind:'lecture',size:20,created_at:'2026-10-10',conversion:{status:'none'},sync:[]}]:[],directory:'/synthetic',capabilities:{office:false,mineru:false}};
 else if(p==='/api/course-files/upload'){uploaded=true;data={duplicate:false};}
 else if(p==='/api/notes')data={category:record.category,categories:[record.category],lessons:[],notes:[],prompt_template:'合成',storage_root:'/synthetic'};
 else if(p==='/api/mineru/settings')data={settings:{tier:'standard',engine:'pipeline'},defaults:{}};
 await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
}
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const origin=`http://127.0.0.1:${server.address().port}`;
 const browser=await chromium.launch({channel:'chrome',headless:true,args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream']});
 const results=[];
 async function scenario(name,run) {
  tasks=[{...baseTask},{...baseTask,id:'refinement:smoke-class',type:'refinement',label:'转写校对',status:'running',stage:'正在校对',actions:['pause']},{...baseTask,id:'summary:old',recording_id:'old',title:'已完成合成课堂',status:'completed',actions:[]}];
  uploaded=false;activeSession=null;requests=[];
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
  await scenario('首页与来源证据',async(page,context,origin)=>{await page.goto(origin);await page.locator('#continuationList [data-recording]').waitFor();await page.locator('.recognition-details summary').click();await page.waitForFunction(()=>document.getElementById('recognitionStatus').textContent.includes('识别来源'));assert.match(await page.locator('#recognitionStatus').innerText(),/来源：待确认/);assert.match(await page.locator('#recognitionStatus').innerText(),/仅远程，不自动回退/);await page.screenshot({path:path.join(output,'workbench.png'),fullPage:true});});
  await scenario('新建课堂来源入口',async(page,context,origin)=>{await page.goto(origin+'/workspace');await page.locator('#workspaceRecent [data-recording]').waitFor();assert.equal(await page.locator('#workspaceCourse').inputValue(),'');await page.locator('#newClass').click();await page.locator('#newClassDialog[open]').waitFor();assert.equal(await page.locator('#newClassDialog a').count(),3);});
  await scenario('课堂记录直接打开笔记',async(page,context,origin)=>{await page.goto(origin+'/library?recording='+record.id);await page.locator('#noteDialog[open]').waitFor();assert.match(await page.locator('#noteSummaryReader').innerText(),/菲涅耳公式/);});
  await scenario('任务默认过滤分组与恢复',async(page,context,origin)=>{await page.goto(origin+'/tasks');await page.locator('.task-group').waitFor();assert.equal(await page.locator('.task-group').count(),1);assert.equal(await page.locator('.task-card').count(),2);await page.locator('[data-id="summary:smoke-class"] [data-action="continue"]').click();await page.getByText('任务已加入后台队列',{exact:true}).waitFor();assert.ok(requests.some(request=>request.path==='/api/tasks/action'));await page.locator('[data-status="completed"]').click();assert.equal(await page.locator('.task-card').count(),1);await page.goto(origin+'/tasks?category='+encodeURIComponent('空课程'));await page.locator('#lastUpdated').filter({hasText:'已同步'}).waitFor();assert.equal(await page.locator('.task-card').count(),0);});
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
    await page.locator('.external-import > summary').click();await page.locator('#noteFile').waitFor();
  });
  await scenario('课程内笔记与任务抽屉',async(page,context,origin)=>{await page.goto(origin+'/workspace?category='+encodeURIComponent(record.category));await page.locator('[data-recording="smoke-class"]').waitFor();await page.screenshot({path:path.join(output,'workspace.png'),fullPage:true});await page.locator('[data-recording="smoke-class"]').click();await page.frameLocator('#workspaceFrame').locator('#noteDialog[open]').waitFor();await page.locator('#workspaceTasks').click();await page.locator('#timelineDialog[open]').waitFor();await page.frameLocator('#timelineFrame').locator('.task-card').first().waitFor();});
 } finally {fs.writeFileSync(path.join(output,'smoke-results.json'),JSON.stringify({synthetic:true,results},null,2));await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
