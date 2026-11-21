// Real Chromium, complete production HTML/CSS/JS.
// Run: PLAYWRIGHT_MODULE=... QWEN_ASR_BROWSER_EXECUTABLE=... node tests/browser/frontend-lifecycle.cjs [repo] [output]
// All HTTP API, WebSocket, media input and playback boundaries are synthetic.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo=path.resolve(process.argv[2] || path.join(__dirname,'../..')),out=path.resolve(process.argv[3] || 'output/playwright/frontend-lifecycle');
fs.mkdirSync(out,{recursive:true});
const browserPath=process.env.QWEN_ASR_BROWSER_EXECUTABLE;
const selection=new RegExp(process.argv[4] || '');
const provider={transport:'api',model:'fixture-model',base_url:'http://fixture.invalid',context_window:131072,max_output_tokens:1000,system_prompt:'合成提示词',extra_params:{}};
const record=id=>({id,title:'合成课堂 '+id,category:'合成课程',status:'completed',kind:'upload',started_at:'2026-10-10T08:00:00+08:00',duration:600,
  segments:[{id:1,start:0,end:600,raw:'合成课堂原文',corrected:'合成课堂原文'}],ai_reviews:[],ai_summaries:[],
  ai_note_summary:{summary:'# 合成课堂笔记\n\n课堂内容。',abstract:'合成摘要'},downloads:{audio:'/fake.aac',transcript:'/fake.txt',raw_transcript:'/fake.txt',corrected_transcript:'/fake.txt',bundle:'/fake.zip',ai_summary:'/fake.pdf'},inference:{provider:'remote',job_id:'fixture-job'}});
const server=http.createServer((req,res)=>{
  const u=new URL(req.url,'http://localhost');
  const pages={'/':'static/index.html','/library':'static/index.html','/course':'static/smartclass.html','/course-files':'static/course-files.html'};
  const file=path.join(repo,pages[u.pathname] || u.pathname);
  if(!file.startsWith(repo+'/')||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}
  res.setHeader('Content-Type',/\.m?js$/.test(file)?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html':file.endsWith('.woff2')?'font/woff2':'application/octet-stream');res.end(fs.readFileSync(file));
});
function gate(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
const results=[];
async function main(){
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const origin='http://127.0.0.1:'+server.address().port;
  const browser=await chromium.launch({executablePath:browserPath,headless:true,args:['--no-sandbox']});
  async function scenario(name,run){
    if(!selection.test(name))return;
    const state={records:{A:record('A'),B:record('B')},notes:{A:[],B:[]},requests:[],gates:{},related:[],errors:[],unknown:[],files:[],knowledgeJobs:[],knowledgeSources:[],
      course:{state:'connected',message:'合成课程已就绪',credentials_saved:false,identity:{name:'合成用户'},authenticated:true,term:'2026',courses:[],options:{},imports:[],queue:{remaining:0},course_revision:0}};
    const context=await browser.newContext({viewport:{width:1600,height:1000}});
    await context.tracing.start({screenshots:true,snapshots:true,sources:true});
    await context.addInitScript(()=>{
      window.__mediaPlay=[];
      Object.defineProperty(HTMLMediaElement.prototype,'readyState',{configurable:true,get:()=>1});
      Object.defineProperty(HTMLMediaElement.prototype,'duration',{configurable:true,get:()=>600});
      HTMLMediaElement.prototype.play=function(){window.__mediaPlay.push({id:this.id,src:this.getAttribute('src'),time:this.currentTime});return Promise.resolve();};
      localStorage.setItem('qwenasr-theme','light');
    });
    await context.route('**/*',async route=>{
      const req=route.request(),url=new URL(req.url()),p=url.pathname,method=req.method();
      if(url.origin!==origin){state.unknown.push(req.url());await route.abort();return;}
      if(!p.startsWith('/api/')&&!p.startsWith('/course/api/'))return route.continue();
      const body=method==='GET'?null:(req.headers()['content-type']||'').includes('json')?req.postDataJSON():null;
      state.requests.push({p,method,body,query:url.search});
      let data={},status=200;
      if(state.gates[p+':'+method]){
        const g=state.gates[p+':'+method];g.reached=true;const spec=await g.promise;
        if(spec.status)status=spec.status;data=spec.data;
        await route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});return;
      }
      if(p==='/api/yuketang/status')data={playwright:true,running:false,job:{status:'idle'},classrooms:[],counts:{total:0}};
      else if(p==='/api/yuketang/courses')data={courses:[]};
      else if(p==='/api/yuketang/questions')data={questions:[]};
      else if(p==='/course/api/status')data=state.course;
      else if(p==='/course/api/ensure-session')data={started:true};
      else if(p==='/api/course-files')data={category:url.searchParams.get('category')||'未分类',categories:['未分类','合成课程'],shared_folder:true,files:state.files,directory:'/synthetic',capabilities:{office:false,mineru:true}};
      else if(p==='/api/course-files/from-vga' && method==='POST'){
        if(body.video_path==='/missing.mp4'){status=400;data={detail:'视频文件不存在或不是文件，请检查本机路径'};}
        else{assert.equal(body.category,'合成课程');state.vga={id:'vga-fixture',category:body.category,status:'running',progress:25,stage:'正在提取最终稳定画面'};data=state.vga;}
      }
      else if(p==='/api/course-files/from-vga')data={job:state.vga||null};
      else if(p==='/api/course-files/from-vga/vga-fixture/cancel'){state.vga={...state.vga,status:'cancelled',stage:'已取消，未导入课件'};data=state.vga;}
      else if(p==='/api/course-files/from-vga/vga-fixture')data=state.vga;
      else if(p==='/api/notes')data={category:url.searchParams.get('category')||'未分类',categories:['未分类','合成课程'],notes:[],lessons:[{id:'A',title:'合成课堂 A',date:'2026-10-10'}],prompt_template:'合成提示词',storage_root:'/synthetic'};
      else if(p==='/api/config')data={feature_version:'library-upgrade-v15-classroom-chat'};
      else if(p==='/api/health')data={status:'idle',detail:'合成服务就绪',model:'fixture',device:'fixture',asr_provider:'remote_only'};
      else if(p==='/api/session')data={active:false,session:null};
      else if(p==='/api/ai-tasks'||p==='/api/tasks')data={tasks:[],history:{},warnings:[]};
      else if(p==='/api/library')data={records:Object.values(state.records).map(({segments,ai_note_summary,...rest})=>({...rest,has_note:true,ai_note_summary:{abstract:ai_note_summary.abstract}})),total:2,note_count:2,next_offset:null,calendar:{'2026-10-10':{count:2,duration:1200}},categories:['未分类','合成课程'],chapter_counts:state.chapterCounts||[],category_profiles:{},storage_root:'/synthetic'};
      else if(p==='/api/ai/settings')data={settings:{function_providers:{},ai_correction:false,translation:'off',summary_enabled:false},has_api_key:false};
      else if(p==='/api/inference/settings')data={settings:{asr_provider:'remote_only',mineru_provider:'local',knowledge_provider:'local',base_url:'http://fixture.invalid',classroom_power_saving:false}};
      else if(p==='/api/mineru/settings')data={settings:{tier:'standard',engine:'pipeline'},defaults:{}};
      else if(p==='/api/ai/classroom-chat/settings')data={provider,context:{}};
      else if(p==='/api/ai/classroom-chat/models')data={models:[]};
      else if(p==='/api/intelligence/settings'){
        if(method==='GET')data={provider};
        else if(method==='PUT')data={provider:body.provider};
        else {status=405;data={detail:'Method Not Allowed'};}
      }
      else if(p==='/api/knowledge/settings')data={settings:{model_id:'Qwen/Qwen3-Embedding-0.6B',device:'cpu',batch_size:2,max_length:2048,idle_seconds:30,timeout_seconds:180},models:[],prepared:false};
      else if(p==='/api/knowledge/status')data={sources:state.knowledgeSources,active_sources_count:state.knowledgeSources.length,active_chunks_count:0};
      else if(p==='/api/knowledge/jobs')data={jobs:state.knowledgeJobs};
      else if(p==='/api/intelligence/related')data={results:state.related};
      else if(/^\/api\/section-page-links\/[^/]+$/.test(p))data={};
      else if(p==='/api/ideashell/status')data={status:'disconnected'};
      else if(p==='/api/library/drive')data={options:{pdf:true,transcripts:true,notes:true,automatic:false,interval_minutes:60},connected:false,configured:false,busy:false,authorizing:false,account:{},job:null};
      else if(p==='/api/library/backup')data={backup:null};
      else if(p==='/api/library/events'){await route.fulfill({status:200,contentType:'text/event-stream',body:': offline\n\n'});return;}
      else if(/^\/api\/recordings\/[^/]+$/.test(p))data={session:state.records[p.split('/')[3]]};
      else if(p.endsWith('/summary-status'))data={job:{running:false,status:'completed'},session:state.records[p.split('/')[3]]};
      else if(p.endsWith('/chat'))data={turns:[],conversation_id:'fixture',context:{}};
      else if(p.endsWith('/intelligence/context'))data={source_hint:body || {}};
      else if(p.endsWith('/scribbles'))data={notes:state.notes[p.split('/')[3]]||[],context:{}};
      else if(p.endsWith('/correction-review'))data={candidates:[]};
      else if(p.endsWith('/enhance')){data={note:null};}
      else if(p.endsWith('/playback')){await route.fulfill({status:200,contentType:'audio/wav',body:Buffer.alloc(0)});return;}
      else{state.unknown.push(method+' '+p);status=404;data={detail:'Unexpected fixture endpoint: '+p};}
      await route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
    });
    const page=await context.newPage();page.setDefaultTimeout(12000);page.on('pageerror',e=>state.errors.push(e.message));
    page.on('dialog',d=>d.dismiss());
    try{
      await run(page,state,context,origin);
      assert.deepEqual(state.errors,[],'Unhandled browser script errors');
      assert.deepEqual(state.unknown,[],'Unexpected external or API request');
      const detail={name,passed:true,browser:browser.version(),surface:'complete production page: '+new URL(page.url()).pathname,api:'synthetic; no models or account services',unknownRequests:state.unknown,pageErrors:state.errors};
      results.push({...detail,...state.proof});
      await page.screenshot({path:path.join(out,name+'.png'),fullPage:true});
      console.log('PASS',name,JSON.stringify(state.proof));
    }catch(error){results.push({name,passed:false,error:error.stack,pageErrors:state.errors,unknownRequests:state.unknown});await page.screenshot({path:path.join(out,name+'-error.png'),fullPage:true});console.error(name,error.message);}
    finally{
      fs.writeFileSync(path.join(out,name+'-requests.json'),JSON.stringify(state.requests,null,2));
      await context.tracing.stop({path:path.join(out,name+'-trace.zip')});await context.close();
      fs.writeFileSync(path.join(out,'browser-results.json'),JSON.stringify(results,null,2));
    }
  }
  async function until(predicate, message) {
    const deadline=Date.now()+10000;
    while(!predicate()) { if(Date.now()>deadline) throw Error(message); await new Promise(r=>setTimeout(r,20)); }
  }
  async function scribbles(page,origin) {
    await page.goto(origin+'/?chat=A&panel=scribbles');
    await page.waitForFunction(()=>!document.getElementById('scribbleInput').disabled);
  }
  async function captureFixture(context) {
    await context.addInitScript(()=>{
      const f=window.__capture={mics:[],sockets:[],contexts:[],holdResume:false};
      const node=()=>({connect(){},disconnect(){this.disconnected=true;}});
      Object.defineProperty(navigator.mediaDevices,'getUserMedia',{value:()=>new Promise(resolve=>{
        const mic={id:'mic-'+(f.mics.length+1),stopped:false,getTracks(){return [{stop:()=>mic.stopped=true}];},release:()=>resolve(mic)};
        f.mics.push(mic);
      })});
      window.AudioContext=class {
        constructor(){this.sampleRate=48000;this.destination={};f.contexts.push(this);}
        resume(){return f.holdResume ? new Promise(resolve=>this.release=resolve) : Promise.resolve();}
        close(){this.closed=true;return Promise.resolve();}
        createMediaStreamSource(stream){this.stream=stream;return node();}
        createScriptProcessor(){return this.processor=node();}
        createAnalyser(){return {...node(),frequencyBinCount:128,getByteFrequencyData:a=>a.fill(0)};}
        createGain(){return {...node(),gain:{value:1}};}
      };
      window.WebSocket=class {
        static OPEN=1;
        constructor(){this.readyState=0;this.sent=[];f.sockets.push(this);}
        send(value){this.sent.push(value);}
        open(){this.readyState=1;this.onopen();}
        receive(data){this.onmessage({data:JSON.stringify(data)});}
        close(){this.readyState=3;queueMicrotask(()=>this.onclose?.());}
      };
    });
  }
  async function connectCapture(page, index=0) {
    await page.waitForFunction(n=>__capture.mics.length>n,index);
    await page.evaluate(n=>__capture.mics[n].release(),index);
    await page.waitForFunction(n=>__capture.sockets.length>n,index);
    await page.evaluate(({index,session})=>{
      __capture.sockets[index].open();
      __capture.sockets[index].receive({type:'session_started',session});
    },{index,session:{...record('C'),title:'合成录音',status:'active',kind:'live'}});
  }
  try {
    await scenario('01-create-keeps-newer-draft',async(page,s,context,origin)=>{
      await scribbles(page,origin); await page.locator('#scribbleAutoEnhance').uncheck();
      const g=gate();s.gates['/api/recordings/A/scribbles:POST']=g;
      await page.locator('#scribbleInput').fill('第一条速记');await page.locator('#saveScribbleBtn').click();
      await until(()=>g.reached,'Scribble POST was not submitted');
      const draft='第一条仍在保存时继续输入的第二条速记';await page.locator('#scribbleInput').fill(draft);
      s.notes.A=[{id:'note-A',raw:'第一条速记',content:'第一条速记',revision:1,status:'saved'}];
      g.resolve({data:{note:s.notes.A[0]}});
      await page.locator('.scribble-card[data-note-id="note-A"]').waitFor();
      assert.equal(await page.locator('#scribbleInput').inputValue(),draft);
      assert.equal(await page.locator('#saveScribbleBtn').isEnabled(),true);
      s.proof={submitted:'第一条速记',preservedDraft:draft};
    });
    await scenario('02-create-response-stays-with-original-lesson',async(page,s,context,origin)=>{
      await scribbles(page,origin);await page.locator('#scribbleAutoEnhance').check();
      const g=gate();s.gates['/api/recordings/A/scribbles:POST']=g;
      await page.locator('#scribbleInput').fill('A 的速记');await page.locator('#saveScribbleBtn').click();
      await until(()=>g.reached,'Scribble POST was not submitted');
      await page.locator('#libraryTab').click();
      await page.locator('.library-card[data-id="B"] .library-open').click();
      await page.locator('#askNoteClass').click();await page.locator('#sideTabScribbles').click();
      const draft='B 的未保存草稿';await page.locator('#scribbleInput').fill(draft);
      g.resolve({data:{note:{id:'note-A',raw:'A 的速记',content:'A 的速记',revision:1,status:'saved'}}});
      await until(()=>s.requests.some(r=>r.p.endsWith('/note-A/enhance')),'Enhancement request was not submitted');
      assert.equal(await page.locator('#scribbleInput').inputValue(),draft);
      assert.equal(await page.locator('.scribble-card[data-note-id="note-A"]').count(),0);
      const enhancement=s.requests.find(r=>r.p.endsWith('/note-A/enhance'));
      assert.equal(enhancement.p,'/api/recordings/A/scribbles/note-A/enhance');
      s.proof={preservedDraft:draft,enhancement:enhancement.p};
    });
    await scenario('03-citation-opens-correct-recording-and-releases-old-audio',async(page,s,context,origin)=>{
      s.related=[{lesson_id:'A',start:120,title:'A 的课堂原文',text:'A 的第 120 秒',kind:'audio',open_url:'/?recording=A#t=120'}];
      await page.goto(origin+'/?chat=A');await page.locator('#sessionState').filter({hasText:'合成课堂 A'}).waitFor();
      await page.locator('#libraryTab').click();await page.locator('.library-card[data-id="B"] .library-open').click();
      await page.locator('#notePlayToggle').click();assert.equal(await page.locator('#noteAudio').getAttribute('src'),'/api/recordings/B/playback');
      await page.locator('#closeNoteDialog').click();
      await page.waitForFunction(()=>document.getElementById('noteAudio').getAttribute('src')===null);
      assert.equal(await page.locator('#noteAudio').getAttribute('src'),null);
      await page.locator('#workbenchNav').click();await page.locator('#sideTabRelated').click();
      await page.locator('.related-open-link').waitFor();await page.evaluate(()=>window.__mediaPlay=[]);
      const popupPromise=context.waitForEvent('page');await page.locator('.related-open-link').click();const popup=await popupPromise;
      await popup.waitForFunction(()=>document.getElementById('noteAudio')?.getAttribute('src')==='/api/recordings/A/playback' && document.getElementById('noteAudio').currentTime===120);
      await popup.locator('#notePlayToggle').click();
      assert.deepEqual(await page.evaluate(()=>__mediaPlay),[]);
      const played=await popup.evaluate(()=>__mediaPlay);assert.equal(played.at(-1).src,'/api/recordings/A/playback');assert.equal(played.at(-1).time,120);
      s.proof={oldAudioReleased:true,playCalls:played};await popup.close();
    });
    await scenario('04-double-start-owns-one-microphone',async(page,s,context,origin)=>{
      await captureFixture(context);await page.goto(origin);await page.waitForFunction(()=>!document.getElementById('recordButton').disabled);
      await page.locator('#recordName').fill('双击测试');await page.locator('#recordButton').dblclick();
      await page.waitForFunction(()=>__capture.mics.length===1);assert.equal(await page.locator('#recordButton').isDisabled(),true);
      await connectCapture(page);await page.waitForFunction(()=>document.getElementById('recordLabel').textContent==='暂停收音');
      let proof=await page.evaluate(()=>({mics:__capture.mics.length,sockets:__capture.sockets.length,contexts:__capture.contexts.length,source:__capture.contexts[0].stream.id}));
      assert.deepEqual(proof,{mics:1,sockets:1,contexts:1,source:'mic-1'});
      // Synthetic visibility changes may stop UI work, but never audio capture.
      await page.evaluate(()=>{
        Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));
        __capture.contexts[0].processor.onaudioprocess({inputBuffer:{getChannelData:()=>new Float32Array(4096)}});
      });
      assert.equal(await page.evaluate(()=>__capture.sockets[0].sent.some(x=>x instanceof ArrayBuffer)),true);
      assert.equal(await page.evaluate(()=>__capture.mics[0].stopped),false);
      await page.locator('#recordButton').click();
      assert.equal(await page.evaluate(()=>__capture.mics[0].stopped && __capture.contexts[0].closed),true);
      s.proof={...proof,hiddenCaptureContinues:true,resourcesReleasedOnPause:true};
    });
    await scenario('05-inline-save-failure-keeps-editor-through-poll',async(page,s,context,origin)=>{
      s.records.A.status='active';s.notes.A=[{id:'note-A',raw:'原始速记',content:'旧正文',status:'saved',revision:1}];
      await scribbles(page,origin);const card=page.locator('.scribble-card[data-note-id="note-A"]');
      await card.getByRole('button',{name:'编辑',exact:true}).click();
      const edit=page.locator('.inline-edit-textarea'),handle=await edit.elementHandle(),draft='编辑后的新正文';await edit.fill(draft);
      const g=gate();s.gates['/api/recordings/A/scribbles/note-A:PATCH']=g;
      await card.getByRole('button',{name:'保存修改',exact:true}).click();await until(()=>g.reached,'PATCH was not submitted');
      const gets=()=>s.requests.filter(r=>r.p==='/api/recordings/A/scribbles'&&r.method==='GET').length,before=gets();
      await until(()=>gets()>before,'Active scribbles did not poll at the normal cadence');await page.waitForTimeout(80);
      assert.equal(await handle.evaluate(e=>e.isConnected),true);assert.equal(await edit.inputValue(),draft);
      g.resolve({status:503,data:{detail:'合成保存失败'}});
      await page.waitForFunction(()=>!document.querySelector('.inline-edit-actions .primary').disabled);
      assert.equal(await edit.inputValue(),draft);assert.equal(await handle.evaluate(e=>e.isConnected),true);
      s.proof={draftPreserved:draft,pollsDuringSave:gets()-before,sameEditorAfterFailure:true};
    });
    await scenario('06-inline-success-keeps-newer-edit-and-advances-revision',async(page,s,context,origin)=>{
      s.notes.A=[{id:'note-A',raw:'原始速记',content:'旧正文',status:'saved',revision:1}];await scribbles(page,origin);
      const card=page.locator('.scribble-card[data-note-id="note-A"]');await card.getByRole('button',{name:'编辑',exact:true}).click();
      const edit=page.locator('.inline-edit-textarea');await edit.fill('先提交的修改');const g=gate();s.gates['/api/recordings/A/scribbles/note-A:PATCH']=g;
      await card.getByRole('button',{name:'保存修改',exact:true}).click();await until(()=>g.reached,'PATCH was not submitted');await edit.fill('提交之后继续修改');
      s.notes.A=[{...s.notes.A[0],content:'先提交的修改',revision:2}];g.resolve({data:{note:s.notes.A[0]}});
      await page.waitForFunction(()=>!document.querySelector('.inline-edit-actions .primary').disabled);assert.equal(await edit.inputValue(),'提交之后继续修改');
      const g2=gate();s.gates['/api/recordings/A/scribbles/note-A:PATCH']=g2;
      await card.getByRole('button',{name:'保存修改',exact:true}).click();await until(()=>g2.reached,'Second PATCH was not submitted');
      const updates=s.requests.filter(r=>r.method==='PATCH');assert.equal(updates.at(-1).body.expected_revision,2);
      s.notes.A=[{...s.notes.A[0],content:'提交之后继续修改',revision:3}];g2.resolve({data:{note:s.notes.A[0]}});
      await edit.waitFor({state:'detached'});assert.match(await card.textContent(),/提交之后继续修改/);s.proof={revisions:updates.map(r=>r.body.expected_revision),newerDraftSaved:true};
    });
    await scenario('07-cancelled-start-cannot-clean-up-a-new-capture',async(page,s,context,origin)=>{
      await captureFixture(context);await page.goto(origin);await page.waitForFunction(()=>!document.getElementById('recordButton').disabled);
      await page.evaluate(()=>__capture.holdResume=true);await page.locator('#recordButton').click();await connectCapture(page);
      await page.waitForFunction(()=>typeof __capture.contexts[0]?.release==='function');
      await page.evaluate(()=>{__capture.sockets[0].receive({type:'ownership_lost',message:'合成接管'});__capture.holdResume=false;});
      await page.waitForFunction(()=>!document.getElementById('recordButton').disabled);await page.locator('#recordButton').click();await connectCapture(page,1);
      await page.waitForFunction(()=>document.getElementById('recordLabel').textContent==='暂停收音');
      await page.evaluate(()=>__capture.contexts[0].release());await page.waitForTimeout(80);
      const proof=await page.evaluate(()=>({firstStopped:__capture.mics[0].stopped,firstClosed:__capture.contexts[0].closed,secondStopped:__capture.mics[1].stopped,secondSocket:__capture.sockets[1].readyState,label:document.getElementById('recordLabel').textContent}));
      assert.deepEqual(proof,{firstStopped:true,firstClosed:true,secondStopped:false,secondSocket:1,label:'暂停收音'});s.proof=proof;
    });
    await scenario('08-idle-and-hidden-stop-polling-resume-immediately',async(page,s,context,origin)=>{
      await page.goto(origin+'/?chat=A');await page.waitForFunction(()=>!document.getElementById('chatQuestion').disabled);await page.waitForTimeout(300);
      const polls=()=>s.requests.filter(r=>['/api/health','/api/ai-tasks','/api/library/drive','/api/recordings/A/chat','/api/recordings/A/scribbles'].includes(r.p));
      const idleStart=polls().length;await page.waitForTimeout(5500);assert.equal(polls().length-idleStart,0);
      await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));});
      const hiddenStart=polls().length;await page.waitForTimeout(5500);assert.equal(polls().length-hiddenStart,0);
      await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'));});
      await until(()=>polls().length>hiddenStart,'Visibility restoration did not refresh immediately');
      s.proof={idleWindowMs:5500,idlePolls:0,hiddenWindowMs:5500,hiddenPolls:0,visibleRecovery:true};
    });
    await scenario('09-real-settings-path-uses-put',async(page,s,context,origin)=>{
      await scribbles(page,origin);await page.locator('[data-knowledge-settings="intelligence"]').click();
      await page.locator('#knowledgeModelDialog [name="model"]').fill('new-fixture-model');await page.locator('#knowledgeModelDialog [type="submit"]').click();
      await page.waitForFunction(()=>document.querySelector('#knowledgeModelDialog [data-status]').textContent.includes('已保存'));
      assert.equal(s.requests.find(r=>r.p==='/api/intelligence/settings'&&r.method!=='GET').method,'PUT');s.proof={method:'PUT'};
    });
    await scenario('10-course-status-idle-active-hidden-cadences',async(page,s,context,origin)=>{
      await page.goto(origin+'/course');await page.waitForFunction(()=>window.courseStatus?.state==='connected');await page.waitForTimeout(150);
      const count=()=>s.requests.filter(r=>r.p==='/course/api/status').length;
      const idle=count();await page.waitForTimeout(2300);assert.equal(count(),idle);
      s.course={...s.course,state:'running',imports:[{id:'import-A',state:'running',title:'合成回放',progress:20,message:'正在转写'}]};
      await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));await page.locator('#result .job').waitFor();
      const row=await page.locator('#result .job').elementHandle(),active=count();
      await until(()=>count()>active,'Running course imports did not retain fast polling');await page.waitForTimeout(80);
      assert.equal(await row.evaluate(e=>e.isConnected),true);
      await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));});
      const hidden=count();await page.waitForTimeout(2300);assert.equal(count(),hidden);
      s.course={...s.course,state:'connected',imports:[]};
      await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'));});
      await page.waitForFunction(()=>window.courseStatus?.state==='connected');assert.equal(await page.locator('#result .job').count(),0);
      s.proof={idleRequests:0,unchangedActiveRowPreserved:true,hiddenRequests:0,completionRecoveredOnReturn:true};
    });
    await scenario('11-course-files-retain-dom-and-pause-hidden-work',async(page,s,context,origin)=>{
      s.files=[{id:'file-A',name:'合成课件.pdf',kind:'课件',size:1024,created_at:'2026-10-10',conversion:{status:'running',stage:'正在识别',progress:20},sync:[]}];
      s.knowledgeJobs=[{id:'index-A',category:'合成课程',status:'running',progress:.2}];
      await page.goto(origin+'/course-files?category='+encodeURIComponent('合成课程'));
      await page.locator('.file-card[data-id="file-A"]').waitFor();assert.equal(await page.locator('#review-lessons').count(),0,'obsolete review-builder controls must stay removed');
      const card=await page.locator('.file-card[data-id="file-A"]').elementHandle();
      const count=()=>s.requests.filter(r=>r.p==='/api/course-files').length,before=count();
      await until(()=>count()>before,'Active conversion did not retain fast polling');await page.waitForTimeout(100);
      assert.equal(await card.evaluate(e=>e.isConnected),true);
      await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));});
      const hidden=s.requests.length;await page.waitForTimeout(2800);
      assert.deepEqual(s.requests.slice(hidden).filter(r=>['/api/course-files','/api/notes','/api/knowledge/jobs'].includes(r.p)),[]);
      s.files[0].conversion={status:'completed',pages:2};s.knowledgeJobs=[];
      await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'));});
      await page.locator('.file-card[data-id="file-A"] .file-status').filter({hasText:'已就绪'}).waitFor();
      s.proof={sameFileCardAcrossUnchangedPoll:true,hiddenDataRequests:0,completedConversionRecovered:true};
    });
    await scenario('11b-course-files-vga-extraction',async(page,s,context,origin)=>{
      await page.goto(origin+'/course-files?category='+encodeURIComponent('合成课程'));
      await page.locator('#courseTitle').filter({hasText:'合成课程'}).waitFor();
      await page.locator('#openVga').click();
      await until(()=>s.requests.some(r=>r.p==='/api/course-files/from-vga'),'VGA recovery request missing');
      assert.equal(await page.locator('#vgaProgress').isHidden(),true);
      await page.locator('#vgaPath').fill('/missing.mp4');
      await page.locator('#startVga').click();
      await page.locator('#vgaStatus').filter({hasText:'视频文件不存在'}).waitFor();
      await page.locator('#vgaPath').fill('/synthetic/lecture.mp4');
      await page.locator('#startVga').click();
      await page.locator('#vgaStatus').filter({hasText:'25%'}).waitFor();
      assert.equal(await page.locator('#vgaProgress').evaluate(e=>e.position),0.25);
      assert.equal(await page.locator('#startVga').isDisabled(),true);
      await page.locator('#cancelVga').click();
      await page.locator('#vgaStatus').filter({hasText:'已取消'}).waitFor();
      await page.locator('#startVga').click();
      await page.locator('#vgaStatus').filter({hasText:'25%'}).waitFor();
      s.files=[{id:'vga-pdf',name:'lecture(VGA课件).pdf',kind:'课件',size:1024,created_at:'2026-10-10',conversion:{status:'none'},sync:[]}];
      s.vga={...s.vga,status:'completed',progress:100,stage:'课件已保存，可用于笔记与复习'};
      await page.locator('#vgaStatus').filter({hasText:'100%'}).waitFor();
      const progressGeometry=await page.locator('#vgaProgress').evaluate(e=>{
        const panel=e.parentElement,style=getComputedStyle(panel);
        return {position:e.position,width:e.getBoundingClientRect().width,
          available:panel.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)};
      });
      assert.equal(progressGeometry.position,1);
      assert.ok(Math.abs(progressGeometry.width-progressGeometry.available)<2,'Completed VGA progress must span the panel content width');
      await page.locator('.file-card[data-id="vga-pdf"]').waitFor();
      assert.equal(await page.locator('#cancelVga').isHidden(),true);
      s.proof={invalidPathShown:true,progressShown:true,cancelWorks:true,completedPdfRefreshed:true,idleProgressHidden:true,completedProgressFullWidth:true};
    });
    await scenario('12-drive-settings-are-lazy-and-poll-only-while-visible',async(page,s,context,origin)=>{
      await page.goto(origin);await page.waitForFunction(()=>!document.getElementById('recordButton').disabled);
      const count=()=>s.requests.filter(r=>r.p==='/api/library/drive').length;
      assert.equal(count(),0);
      await page.locator('#openSettingsDialog').click();await page.locator('[data-settings-tab="library"]').click();
      await until(()=>count()===1,'Opening Drive settings did not fetch its status');
      await page.waitForFunction(()=>document.getElementById('driveBackupStatus').textContent.includes('连接 Google'));
      const status=await page.locator('#driveBackupStatus').textContent();assert.match(status,/连接 Google/);
      await page.locator('#closeSettingsDialog').click();await page.waitForTimeout(1700);assert.equal(count(),1);
      s.proof={requestsBeforeOpening:0,requestsAfterOpening:1,requestsAfterClosing:0};
    });
    await scenario('13-related-refresh-discovers-new-course-materials',async(page,s,context,origin)=>{
      await page.goto(origin+'/?chat=A&panel=related');
      await page.locator('#relatedPanel').waitFor({state:'visible'});
      await until(()=>s.requests.some(r=>r.p==='/api/knowledge/status'),'Initial sources were not loaded');await page.waitForTimeout(100);
      s.knowledgeSources=[{source_id:'cf_added',title:'新索引的课件',state:'active',status:'已就绪'}];
      await page.locator('#relatedRefreshBtn').click();
      await page.locator('#relatedSourceSelect option[value="cf_added"]').waitFor({state:'attached'});
      assert.equal(s.requests.filter(r=>r.p==='/api/knowledge/status').length,2);
      s.proof={sameLessonRefreshDiscoversSource:'cf_added',relatedDeepLinkOpensRelatedPanel:true};
    });
    await scenario('14-manual-anchor-validation-and-save-status',async(page,s,context,origin)=>{
      await scribbles(page,origin);await page.locator('#scribbleAutoEnhance').uncheck();
      await page.locator('#anchorManual').click();await page.locator('#scribbleInput').fill('保留这份时间草稿');
      for(const invalid of ['abc','-1','1:99','Infinity']) {
        await page.locator('#manualStartTime').fill(invalid);await page.locator('#saveScribbleBtn').click();
        assert.match(await page.locator('#scribblesStatus').textContent(),/时间格式无效/);
        assert.equal(await page.locator('#scribbleInput').inputValue(),'保留这份时间草稿');
      }
      assert.equal(s.requests.filter(r=>r.p.endsWith('/scribbles')&&r.method==='POST').length,0);
      await page.locator('#manualStartTime').fill('00:05');await page.locator('#manualEndTime').fill('00:10');
      const g=gate();s.gates['/api/recordings/A/scribbles:POST']=g;
      await page.locator('#saveScribbleBtn').click();await until(()=>g.reached,'Valid manual note was not posted');
      const req=s.requests.find(r=>r.p.endsWith('/scribbles')&&r.method==='POST');assert.equal(req.body.start,5);assert.equal(req.body.end,10);
      g.resolve({data:{note:{id:'manual',raw:'保留这份时间草稿',content:'保留这份时间草稿',revision:1,status:'saved'}}});
      await page.waitForFunction(()=>document.getElementById('scribblesStatus').textContent==='速记已保存');
      s.proof={invalidInputsPreserveDraftAndDoNotPost:true,validAnchor:[5,10],savedStatus:'速记已保存'};
    });
    await scenario('15-scribble-conflict-preserves-specific-server-reason',async(page,s,context,origin)=>{
      s.notes.A=[{id:'note-conflict',raw:'原文',content:'原文',revision:1,status:'suggestion_ready',suggestion:{id:'suggestion',input_revision:1,content:'增强内容'}}];
      const g=gate();g.resolve({status:409,data:{detail:'来源版本已改变，请重新增强后核对'}});s.gates['/api/recordings/A/scribbles/note-conflict/action:POST']=g;
      await scribbles(page,origin);await page.getByRole('button',{name:'采纳建议',exact:true}).click();
      await page.locator('#toast').filter({hasText:'来源版本已改变，请重新增强后核对'}).waitFor();
      s.proof={conflictReasonVisible:true};
    });
    await scenario('16-chapter-edit-preview-save-and-url-scope',async(page,s,context,origin)=>{
      s.records.A.chapter='第六章 数字基带';s.records.A.chapters=['第六章 数字基带'];
      await page.goto(origin+'/library?category='+encodeURIComponent('合成课程')+'&chapter='+encodeURIComponent('第六章 数字基带')+'&date=2026-10-10');
      await page.locator('.library-card[data-id="A"] .library-card-chapter').filter({hasText:'第六章 数字基带'}).waitFor();
      assert.equal(await page.locator('#libraryChapter').inputValue(),'第六章 数字基带');
      assert(s.requests.filter(r=>r.p==='/api/library').some(r=>new URLSearchParams(r.query).get('chapter')==='第六章 数字基带'&&new URLSearchParams(r.query).get('date')==='2026-10-10'));
      await page.locator('.library-card[data-id="A"] .library-open').click();await page.locator('#editNoteChapter').click();
      const suggest=gate();s.gates['/api/recordings/A/chapter-suggestion:POST']=suggest;
      await page.locator('#suggestNoteChapter').click();await until(()=>suggest.reached,'No suggestion request');
      await page.locator('#noteChapterInput').fill('手动修改的章节');suggest.resolve({data:{chapter:'过期建议',chapters:['过期建议']}});
      await page.locator('#noteChapterStatus').filter({hasText:'保留你刚修改'}).waitFor();
      assert.equal(await page.locator('#noteChapterInput').inputValue(),'手动修改的章节');
      assert.equal(s.requests.filter(r=>r.method==='PATCH').length,0);
      const save=gate();s.gates['/api/recordings/A:PATCH']=save;
      await page.locator('#noteChapterInput').fill('第六章；第七章');await page.locator('#saveNoteChapter').click();await until(()=>save.reached,'No chapter PATCH');
      assert.equal(s.requests.find(r=>r.p==='/api/recordings/A'&&r.method==='PATCH').body.chapter,'第六章；第七章');
      s.records.A.chapter='第六章；第七章';s.records.A.chapters=['第六章','第七章'];save.resolve({data:{session:s.records.A}});
      await page.locator('#noteDialogChapter').filter({hasText:'第六章；第七章'}).waitFor();
      await page.waitForFunction(()=>document.getElementById('noteChapterEditor').hidden);
      s.proof={urlScopePreserved:true,staleSuggestionDoesNotOverwrite:true,suggestionDoesNotSave:true,manualMultiChapterSaved:true};
    });
    await scenario('17-mineru-success-clears-initial-placeholder',async(page,s,context,origin)=>{
      const settings={tier:'advanced',ocr_mode:'auto',image_analysis:true,small_backend:'torch',vlm_engine:'llama_cpp',model_source:'huggingface',processing_window_size:8,vlm_concurrency:2,virtual_vram_gb:8};
      const g=gate();g.resolve({data:{settings,defaults:settings,tier_choices:['auto','advanced'],runtime:{compatible:true,version:'4.0.10'}}});s.gates['/api/mineru/settings:GET']=g;
      await page.goto(origin+'/?settings=mineru');
      await page.waitForFunction(()=>document.getElementById('mineruSettingsStatus').textContent==='配置已读取');
      assert.equal(await page.locator('#saveMineruSettings').isEnabled(),true);
      s.proof={loadStatus:'配置已读取'};
    });
    await scenario('18-bulk-chapters-refresh-counts-and-preserve-course-url',async(page,s,context,origin)=>{
      await page.goto(origin+'/library?category='+encodeURIComponent('合成课程'));
      await page.getByRole('button',{name:'识别课程章节',exact:true}).waitFor();
      const g=gate();s.gates['/api/library/chapters/detect:POST']=g;
      await page.locator('#detectLibraryChapters').click();await until(()=>g.reached,'No bulk chapter request');
      assert.equal(await page.locator('#detectLibraryChapters').isDisabled(),true);
      assert.deepEqual(s.requests.find(r=>r.p==='/api/library/chapters/detect').body,{category:'合成课程'});
      s.chapterCounts=[{chapter:'第六章',records:25,notes:24},{chapter:'',records:1,notes:0}];
      s.records.A.chapter='第六章';s.records.A.chapters=['第六章'];
      g.resolve({data:{updated:1,unchanged:0,unassigned:1,busy:0}});
      await page.locator('#libraryChapterStatus').filter({hasText:'章节已更新 1 节'}).waitFor();
      assert.match(await page.locator('#libraryChapter option[value="第六章"]').textContent(),/25节 \/ 24份笔记/);
      await page.locator('#libraryChapter').selectOption('第六章');
      await page.waitForFunction(()=>new URL(location.href).searchParams.get('chapter')==='第六章');
      await page.locator('#workbenchNav').click();await page.locator('#libraryTab').click();
      assert.equal(new URL(page.url()).searchParams.get('chapter'),'第六章');
      await page.reload();await page.waitForFunction(()=>document.getElementById('libraryChapter').value==='第六章');
      s.proof={courseScopedBulkRequest:true,globalCountsNotCurrentPage:true,chapterSurvivesNavigationAndReload:true};
    });
    await scenario('19-closing-deeplink-note-keeps-filters-without-reopening',async(page,s,context,origin)=>{
      await page.goto(origin+'/library?recording=A&category='+encodeURIComponent('合成课程')+'&chapter='+encodeURIComponent('第六章'));
      await page.locator('#noteDialog[open]').waitFor();await page.locator('#closeNoteDialog').click();
      await page.waitForFunction(()=>!document.getElementById('noteDialog').open);
      assert.equal(new URL(page.url()).searchParams.has('recording'),false);
      assert.equal(new URL(page.url()).searchParams.get('chapter'),'第六章');
      await page.reload();await page.locator('.library-card').first().waitFor();
      assert.equal(await page.locator('#noteDialog').evaluate(e=>e.open),false);
      s.proof={closedNoteStaysClosedAfterReload:true,chapterFilterPreserved:true};
    });
  } finally {await browser.close();await new Promise(r=>server.close(r));}
  console.log(JSON.stringify(results,null,2));
  if(results.some(r=>!r.passed))process.exitCode=1;
}
main().catch(error=>{console.error(error);server.close();process.exitCode=1;});
