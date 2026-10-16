/* Real Chromium, real production HTML/CSS/modules; API/storage/history fixtures.
 * This is offline frontend acceptance, NOT a microphone/model/backend E2E run.
 */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(process.argv[2]||'.'),out=path.resolve(process.argv[3]||'output/playwright/deep-ui');
const select=process.argv[4]||'';fs.mkdirSync(out,{recursive:true});
const tests=[];const test=(name,fn,options={})=>tests.push({name,fn,options});
const COURSE_A='光电子学',COURSE_B='数字电路';
function task(id,type='summary',status='failed',course=COURSE_A,recording='r1'){
 return {id,type,family:type==='upload'?'asr':type==='pdf'?'mineru':'ai',status,category:course,recording_id:recording,
 title:recording==='r1'?'激光器课堂':'逻辑电路课堂',label:type==='upload'?'上传录音转写':'课后复习笔记',history_generation:'g:'+id,
 actions:status==='completed'?[]:status==='running'?['pause','cancel']:['retry','continue','cancel'],error:status==='failed'?'连接失败':''};
}
function data(){return {tasks:[task('s1'),task('u1','upload'),task('s2','summary','failed',COURSE_B,'r2'),task('d1','summary','completed',COURSE_A,'r3'),task('d2','upload','completed',COURSE_A,'r3'),task('d3','summary','completed',COURSE_B,'r4')],plans:[]};}
function source(name){return fs.readFileSync(path.join(root,name),'utf8');}
function moduleURL(name,cache=new Map()){
 const full=path.resolve(root,name);if(cache.has(full))return cache.get(full);
 let text=fs.readFileSync(full,'utf8');
 text=text.replace(/(from\s+['"])(\.[^'"]+)(['"])/g,(_,a,url,z)=>a+moduleURL(path.relative(root,path.resolve(path.dirname(full),url.split('?')[0])),cache)+z);
 if(path.basename(full)==='tasks.js')text='const location=window.__fixture.location,history=window.__fixture.history;\n'+text;
 const url='data:text/javascript;base64,'+Buffer.from(text).toString('base64');cache.set(full,url);return url;
}
function html(name){return source(name).replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,node=>{
 const href=node.match(/href=['"]([^'"]+)/)?.[1];return /rel=['"]stylesheet/.test(node)?'<style>'+source(href.split('?')[0].replace(/^\//,''))+'</style>':'';
});}
const runtime=fs.readFileSync(path.join(__dirname,'runtime.js'),'utf8');
let browser;
async function setup(options={}){
 const context=await browser.newContext({viewport:options.viewport||{width:1440,height:1000},reducedMotion:'reduce'});context.setDefaultTimeout(1800);await context.tracing.start({screenshots:true,snapshots:true,sources:true});
 const p=await context.newPage(),errors=[],network=[],steps=[];p.on('pageerror',e=>errors.push(e.message));context.on('request',r=>network.push(r.url()));
 await context.route('**/*',route=>route.abort());
 if(options.component) await require('./components.cjs').mount(p,{root,options,source,moduleURL,runtime});
 else { await p.setContent(html('static/tasks.html'),{waitUntil:'domcontentloaded'});
 const fixture={...data(),...options};await p.evaluate(f=>window.__fixture=f,fixture);await p.addScriptTag({content:runtime});await p.addScriptTag({content:source('static/js/theme.js')});
 p.on('dialog',d=>{steps.push({dialog:d.message(),accepted:options.acceptConfirm!==false});options.acceptConfirm===false?d.dismiss():d.accept();});
 await p.evaluate(url=>import(url),moduleURL('static/js/tasks.js'));await p.waitForFunction(()=>document.querySelector('#taskList').getAttribute('aria-busy')==='false'); }
 return {context,p,errors,network,steps,
  async requests(){return p.evaluate(()=>__fixture.requests)},
  async posts(){return (await this.requests()).filter(x=>x.method==='POST')},
  async plan(value){await p.evaluate(v=>__fixture.plans.push(v),value)},
  async resolve(name,spec={}){await p.evaluate(([n,s])=>__fixture.resolve(n,s),[name,spec]);await p.waitForTimeout(25)},
  async waitGate(name){await p.waitForFunction(n=>!!__fixture.pending[n],name)},
  async refresh(){await p.locator('#refreshTasks').click();await p.waitForFunction(()=>document.querySelector('#taskList').getAttribute('aria-busy')==='false');await p.waitForTimeout(25)},
  async management(){const m=p.locator('.task-management');if(await m.count()>0&&!(await m.evaluate(el=>el.open)))await p.locator('.task-management > summary').click()},
  async history(){await this.management();const h=p.locator('.task-history-tools');if(await h.count()>0&&!(await h.evaluate(el=>el.open)))await p.locator('.task-history-tools > summary').click()},
  async openSteps(groupId=null){const loc=groupId?p.locator(`[data-group-id="${groupId}"] .group-details`):p.locator('.group-details');const count=await loc.count();for(let i=0;i<count;i++){const item=loc.nth(i);if(!(await item.evaluate(e=>e.open)))await item.locator('> summary').click();}},
  async selectAll(){await this.management();await p.locator('#selectAll').check()},
  async filter(type,category){if(type!==undefined)await p.locator('#typeFilter').selectOption(type);if(category!==undefined)await p.locator('#categoryFilter').selectOption(category)},
  async pdf(){await p.locator('#newPdf').click();await p.locator('#pdfPaths').fill('/fixture/first.pdf')},
 };
}
test('01-render-and-navigate-status',async t=>{const p=t.p;assert.equal(await p.locator('#taskList [data-group-id]').count(),2);await p.locator('#statusFilters [data-status=completed]').click();assert.equal(await p.locator('#taskList [data-group-id]').count(),2);await p.locator('[data-summary=attention]').click();assert.equal(await p.locator('#statusFilters [data-status=attention]').getAttribute('aria-current'),'page');});
test('02-type-filter-select-all-is-scoped',async t=>{await t.filter('summary',COURSE_A);await t.selectAll();await t.p.locator('[data-bulk=retry]').click();await t.p.waitForTimeout(40);assert.deepEqual((await t.posts()).find(x=>x.path.endsWith('bulk-action')).body.items.map(x=>x.id),['s1']);});
test('03-retry-failed-honors-course',async t=>{await t.filter('',COURSE_A);await t.history();await t.p.locator('#retryFailed').click();await t.p.waitForTimeout(40);const posts=await t.posts();assert(!posts.some(x=>x.path.endsWith('retry-failed')),'unscoped endpoint called');assert.deepEqual(posts.flatMap(x=>x.body?.items?.map(t=>t.id)||[]).sort(),['s1','u1']);});
require('./task-cases.cjs')({test,assert,task,data,COURSE_A,COURSE_B});
require('./component-cases.cjs')({test,assert,task,data,COURSE_A,COURSE_B});
require('./lifecycle-cases.cjs')({test,assert,task,COURSE_A});
async function main(){browser=await chromium.launch({headless:true,executablePath:process.env.QWEN_ASR_BROWSER_EXECUTABLE||undefined,args:['--no-sandbox']});const results=[];
 for(const item of tests.filter(t=>!select||new RegExp(select).test(t.name))){let t,error;const started=Date.now();try{t=await setup(item.options);await item.fn(t);assert.deepEqual(t.errors,[],'browser script errors');assert.deepEqual(t.network,[],'offline acceptance made a network request');}catch(e){error=e.stack||String(e)}
  const result={name:item.name,surface:item.options.component?'isolated-native-'+item.options.component+'-panel':'complete-task-page',passed:!error,duration_ms:Date.now()-started,...(error?{error}:{})};results.push(result);console.log((error?'FAIL ':'PASS ')+item.name+' '+result.duration_ms+'ms');if(error)console.log(error.split('\n').slice(0,5).join('\n'));fs.writeFileSync(path.join(out,'cases-progress.json'),JSON.stringify(results,null,2));
  if(t){await t.p.screenshot({path:path.join(out,item.name+'.png'),fullPage:false});fs.writeFileSync(path.join(out,item.name+'.requests.json'),JSON.stringify({requests:await t.requests(),steps:t.steps,errors:t.errors,network:t.network},null,2));await t.context.tracing.stop({path:path.join(out,item.name+'.trace.zip')});await t.context.close();}
 }
 const summary={mode:'offline-production-UI-with-synthetic-IO-see-case-surface',browser:browser.version(),total:results.length,passed:results.filter(x=>x.passed).length,failed:results.filter(x=>!x.passed).length,cases:results};fs.writeFileSync(path.join(out,'results.json'),JSON.stringify(summary,null,2));await browser.close();process.exitCode=summary.failed?1:0;
}
main().catch(e=>{console.error(e);process.exitCode=1});
