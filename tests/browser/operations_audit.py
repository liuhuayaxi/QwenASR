"""Chromium UI operations regression, with explicitly controlled API fixtures.
No microphone, GPU, remote gateway, Finder, or production writes are exercised.
"""
import sys,json,base64,traceback,time,hashlib
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
import argparse, os
HERE=Path(__file__).resolve().parent
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root',type=Path,default=HERE.parents[1])
parser.add_argument('--output',type=Path,default=Path('output/operations-audit'))
parser.add_argument('--match',default='')
parser.add_argument('--chromium',default=os.environ.get('CHROMIUM_EXECUTABLE'))
args=parser.parse_args()
ROOT=args.source_root.resolve(); MODE='checkout'; OUT=args.output.resolve();OUT.mkdir(parents=True,exist_ok=True)
expect.set_options(timeout=1400)
INF={'mineru_provider':'local','asr_provider':'local','knowledge_provider':'local','classroom_power_saving':False,'base_url':'http://127.0.0.1:8765','token_env':'EXAMPLE_TOKEN'}
MIN={'tier':'standard','ocr_mode':'auto','image_analysis':True,'small_backend':'auto','vlm_engine':'llama-cpp','model_source':'modelscope','processing_window_size':0,'vlm_concurrency':4,'virtual_vram_gb':0}
RUNTIME={'compatible':True,'available':True,'version':'4.0.10'}
NET=r'''initial=>{
 const n=window.__net={requests:[],pending:{},rules:[],inference:initial.inference,mineru:initial.mineru,defaults:initial.mineru,runtime:initial.runtime,tasks:initial.tasks||{tasks:[]},backup:null};
 n.hold=(method,path)=>n.rules.push({method,path,hold:true});
 n.fail=(method,path,status=500,data={detail:'fixture failure'})=>n.rules.push({method,path,status,data});
 n.reply=(id,data,status=200)=>{const item=n.pending[id];if(!item)throw Error('No deferred request '+id);delete n.pending[id];item.resolve(new Response(typeof data==='string'?data:JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}}));};
 window.fetch=async (path,options={})=>{
  const method=options.method||'GET',url=String(path),id=n.requests.length;
  const item={id,path:url,method,headers:Object.fromEntries(new Headers(options.headers)),body:options.body?JSON.parse(options.body):null};n.requests.push(item);
  const index=n.rules.findIndex(r=>r.method===method&&r.path===url),rule=index<0?null:n.rules.splice(index,1)[0];
  if(rule?.hold)return await new Promise((resolve,reject)=>{n.pending[id]={resolve,reject};options.signal?.addEventListener('abort',()=>{delete n.pending[id];item.aborted=true;reject(options.signal.reason);},{once:true});});
  if(rule)return new Response(JSON.stringify(rule.data),{status:rule.status,headers:{'Content-Type':'application/json'}});
  let data;
  if(url==='/api/inference/settings') {if(method==='PUT')n.inference=item.body.settings; data={settings:n.inference};}
  else if(url==='/api/inference/test') data={capabilities:{version:'fixture-1'}};
  else if(url==='/api/mineru/settings'){if(method==='PUT')n.mineru=item.body.settings;data={settings:n.mineru,defaults:n.defaults,tier_choices:['auto','flash','basic','standard','advanced'],runtime:n.runtime};}
  else if(url==='/api/library/backup'){data={backup:n.backup};}
  else if(url==='/api/tasks')data=n.tasks;
  else if(url==='/api/pdf-ai/choose-files')data={cancelled:false,paths:['/tmp/picked.pdf']};
  else if(url.startsWith('/api/tasks/')){
   if(item.headers['x-qwenasr-client']!=='task-center')return new Response(JSON.stringify({detail:'Task center client required'}),{status:403});
   if(url==='/api/tasks/pdf')data={tasks:[],accepted:item.body.paths.length};
   else if(url==='/api/tasks/clear-completed')data={cleared:item.body.items.length};
   else if(url==='/api/tasks/restore-completed')data={restored:1};
   else {
    const items=url.endsWith('/bulk-action')?item.body.items:url.endsWith('/retry-failed')?n.tasks.tasks.filter(t=>['failed','interrupted'].includes(t.status)): [item.body];
    n.applied=(n.applied||[]).concat(items.map(t=>t.id));
    const results=items.map(t=>({id:t.id,ok:!(n.bulkFailureIds||[]).includes(t.id),error:(n.bulkFailureIds||[]).includes(t.id)?'fixture conflict':''}));
    data={results,accepted:results.filter(r=>r.ok).length,requested:results.length};
   }
  }
  else throw Error('Unmapped fixture endpoint: '+method+' '+url);
  return new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}});
 };
}'''

def source(rel):
 return (ROOT/rel).read_text(encoding='utf-8')

def module_url(rel,seen=None):
 # A data module evaluates the exact source. Import specifiers alone are
 # resolved offline; external navigation is blocked at the browser boundary.
 import re,posixpath
 text=source(rel);seen=set() if seen is None else seen
 if rel=='static/js/tasks.js':text='const location=window.__locationFixture||window.location; const history=window.__historyFixture||window.history;\n'+text
 if rel in seen:raise RuntimeError('Circular import unsupported by offline loader')
 seen=seen|{rel}
 def replace(m):
  path=m.group(2).split('?')[0]
  if not path.startswith('.'):raise RuntimeError('Unexpected external import '+path)
  child=posixpath.normpath(posixpath.join(posixpath.dirname(rel),path))
  return m.group(1)+json.dumps(module_url(child,seen))
 text=re.sub(r'(\bfrom\s*|\bimport\s*)([\'"][^\'\"]+[\'\"])',lambda m:m.group(0),text) if False else text
 pattern=r'(\bfrom\s+)[\'\"]([^\'\"]+)[\'\"]'
 text=re.sub(pattern,replace,text)
 return 'data:text/javascript;base64,'+base64.b64encode(text.encode()).decode()

def load_module(page,rel,factory):
 page.evaluate('async arg=>{const m=await import(arg.url);window.__ctl=m[arg.factory]();}',{'url':module_url(rel),'factory':factory})

def setup(page,kind='inference'):
 page.set_content((HERE/'fixtures/operations-settings.html').read_text(encoding='utf-8'))
 page.add_style_tag(content=source('static/ui.css'))
 page.evaluate(NET,{'inference':INF,'mineru':MIN,'runtime':RUNTIME})
 load_module(page,'static/js/'+kind+'-settings.js','createInferenceSettings' if kind=='inference' else 'createMineruSettings')
 page.evaluate('()=>window.__ctl.load()')

def hold(page,method,path):page.evaluate('x=>__net.hold(...x)',[method,path])
def reply(page,payload,status=200):
 ids=page.evaluate('Object.keys(__net.pending)')
 assert ids,'No pending request'
 page.evaluate('x=>__net.reply(...x)',[int(ids[0]),payload,status])
def reqs(page,method,path):return page.evaluate('x=>__net.requests.filter(r=>r.method===x[0]&&r.path===x[1])',[method,path])
def check(condition,msg):
 if not condition:raise AssertionError(msg)
TESTS=[]
def test(name):
 def wrap(f):TESTS.append((name,f));return f
 return wrap

@test('inference_save_does_not_erase_newer_edit')
def inf_save_edit(p):
 setup(p);hold(p,'PUT','/api/inference/settings')
 p.locator('#inferenceRemoteUrl').fill('http://127.0.0.1:9001');p.locator('#saveInferenceSettings').click()
 p.locator('#inferenceRemoteUrl').fill('http://127.0.0.1:9002')
 reply(p,{'settings':{**INF,'base_url':'http://127.0.0.1:9001'}})
 expect(p.locator('#saveInferenceSettings')).to_be_enabled()
 expect(p.locator('#inferenceRemoteUrl')).to_have_value('http://127.0.0.1:9002')
 check(p.evaluate('__ctl.current().base_url')=='http://127.0.0.1:9001','Unconfirmed edit entered server-owned settings')
 p.locator('#saveInferenceSettings').click()
 expect(p.locator('#inferenceSettingsStatus')).to_have_text('\u5df2\u4fdd\u5b58')
 check(reqs(p,'PUT','/api/inference/settings')[-1]['body']['settings']['base_url'].endswith(':9002'),'Second save did not submit draft')

@test('inference_test_does_not_validate_a_different_url')
def inf_test_stale(p):
 setup(p);hold(p,'POST','/api/inference/test');p.locator('#testInferenceConnection').click()
 p.locator('#inferenceRemoteUrl').fill('http://127.0.0.1:9003');reply(p,{'capabilities':{'version':'fixture-1'}})
 expect(p.locator('#testInferenceConnection')).to_be_enabled()
 check('\u91cd\u65b0\u6d4b\u8bd5' in p.locator('#inferenceSettingsStatus').inner_text(),'Stale test reported the current URL as verified')

@test('inference_save_and_test_are_serialized')
def inf_serial(p):
 setup(p);hold(p,'PUT','/api/inference/settings');p.locator('#saveInferenceSettings').click()
 expect(p.locator('#testInferenceConnection')).to_be_disabled()
 reply(p,{'settings':INF});expect(p.locator('#testInferenceConnection')).to_be_enabled()

@test('inference_failed_reload_disables_stale_actions')
def inf_reload(p):
 setup(p);p.evaluate("__net.fail('GET','/api/inference/settings')");p.evaluate('()=>__ctl.load().catch(()=>{})')
 expect(p.locator('#saveInferenceSettings')).to_be_disabled();expect(p.locator('#testInferenceConnection')).to_be_disabled()
 check(not p.evaluate('__ctl.isReady()'),'Stale readiness after failed reload')
 p.evaluate('()=>__ctl.load()');expect(p.locator('#saveInferenceSettings')).to_be_enabled()

@test('inference_invalid_url_does_not_submit')
def inf_invalid(p):
 setup(p);p.locator('#inferenceRemoteUrl').fill('not a URL');p.locator('#saveInferenceSettings').click()
 check(not reqs(p,'PUT','/api/inference/settings'),'Invalid URL submitted')

@test('inference_power_saving_changes_only_after_save')
def inf_power(p):
 setup(p);p.locator('#uploadUseMac').check();p.locator('#inferencePowerSaving').select_option('on')
 expect(p.locator('#uploadUseMac')).to_be_enabled();check(not p.evaluate('__ctl.current().classroom_power_saving'),'Unsaved power state leaked')
 p.locator('#saveInferenceSettings').click();expect(p.locator('#uploadUseMac')).to_be_disabled();expect(p.locator('#uploadUseMac')).not_to_be_checked()

@test('inference_save_error_preserves_draft_and_allows_retry')
def inf_failure(p):
 setup(p);p.evaluate("__net.fail('PUT','/api/inference/settings')");p.locator('#inferenceRemoteUrl').fill('http://127.0.0.1:9004');p.locator('#saveInferenceSettings').click()
 expect(p.locator('#saveInferenceSettings')).to_be_enabled();expect(p.locator('#inferenceRemoteUrl')).to_have_value('http://127.0.0.1:9004')
 p.locator('#saveInferenceSettings').click();expect(p.locator('#inferenceSettingsStatus')).to_have_text('\u5df2\u4fdd\u5b58')
 check(len(reqs(p,'PUT','/api/inference/settings'))==2,'Retry did not issue one new request')

@test('inference_initial_load_protects_form')
def inf_initial(p):
 p.set_content((HERE/'fixtures/operations-settings.html').read_text(encoding='utf-8'));p.evaluate(NET,{'inference':INF,'mineru':MIN,'runtime':RUNTIME})
 hold(p,'GET','/api/inference/settings');load_module(p,'static/js/inference-settings.js','createInferenceSettings')
 p.evaluate('()=>{void __ctl.load()}');expect(p.locator('#inferenceRemoteUrl')).to_be_disabled();reply(p,{'settings':INF});expect(p.locator('#inferenceRemoteUrl')).to_be_enabled()


def task_fixture():
 def t(id,category,recording,type='summary',status='failed',actions=None):
  return dict(id=id,title=recording+' lesson',category=category,recording_id=recording,type=type,family='asr' if type=='upload' else 'ai',status=status,actions=actions if actions is not None else ['retry','cancel'],label=type,stage='Fixture stage',error='Fixture error' if status=='failed' else '',result_url='/?recording='+recording,history_generation='generation-'+id)
 a=[t('a-summary','Optics','a'),t('a-upload','Optics','a','upload'),t('a-done','Optics','a',status='completed',actions=[]),t('a-done-upload','Optics','a','upload','completed',[])]
 b=[t('b-summary','Electronics','b'),t('b-done','Electronics','b',status='completed',actions=[])]
 groups=[dict(id='recording:'+r,recording_id=r,title=r+' lesson',category=c,tasks=ts) for r,c,ts in [('a','Optics',a),('b','Electronics',b)]]
 return {'tasks':a+b,'groups':groups,'history':{'can_restore':True}}

def setup_tasks(page,query='',tasks=None):
 from bs4 import BeautifulSoup
 soup=BeautifulSoup(source('static/tasks.html'),'html.parser')
 for node in soup.find_all('script'):node.decompose()
 for node in soup.find_all('link'):
  if node.get('rel')==['stylesheet']:
   css=source(node['href'].split('?')[0].lstrip('/'));style=soup.new_tag('style');style.string=css;node.replace_with(style)
  else:node.decompose()
 page.set_content(str(soup));page.add_script_tag(content=source('static/js/theme.js'))
 page.evaluate(NET,{'inference':INF,'mineru':MIN,'runtime':RUNTIME,'tasks':tasks or task_fixture()})
 if query:
  page.evaluate('query=>{window.__locationFixture=new URL("http://localhost/tasks"+query);window.__historyFixture={state:{from:"fixture"},replaceState(state,unused,url){window.__historyResult={state,url};window.__locationFixture=new URL(url,"http://localhost");}}}',query)
 page.evaluate('async url=>{await import(url)}',module_url('static/js/tasks.js'))
 expect(page.locator('#taskList')).to_have_attribute('aria-busy','false')

def task_filter(page,category='',type=''):
 page.locator('#categoryFilter').select_option(category);page.locator('#typeFilter').select_option(type)
def history_tools(page):
 if not page.locator('.task-management').evaluate('e=>e.open'):page.locator('.task-management > summary').click()
 if not page.locator('.task-history-tools').evaluate('e=>e.open'):page.locator('.task-history-tools > summary').click()
def select_all(page):
 if not page.locator('.task-management').evaluate('e=>e.open'):page.locator('.task-management > summary').click()
 page.locator('#selectAll').check()
def open_steps(page):
 details=page.locator('.group-details')
 for i in range(details.count()):
  if not details.nth(i).evaluate('e=>e.open'):details.nth(i).locator('> summary').click()

def post_items(page):
 return [item['id'] for r in page.evaluate('__net.requests') if r['path']=='/api/tasks/bulk-action' for item in r['body']['items']]

@test('tasks_bulk_respects_course_and_step_type')
def task_scope(p):
 setup_tasks(p);task_filter(p,'Optics','summary');select_all(p);p.locator('[data-bulk="retry"]').click()
 check(post_items(p)==['a-summary'],'Bulk action escaped selected course/type: '+repr(post_items(p)))

@test('tasks_retry_failed_respects_current_filters')
def task_retry_scope(p):
 setup_tasks(p);task_filter(p,'Optics','summary');history_tools(p);p.locator('#retryFailed').click()
 p.wait_for_function('(__net.applied||[]).length>0')
 check(p.evaluate('__net.applied')==['a-summary'],'Retry-current operation touched hidden steps: '+str(p.evaluate('__net.applied')))

@test('tasks_clear_completed_respects_course_and_type')
def task_clear_scope(p):
 setup_tasks(p);p.locator('#statusFilters [data-status="all"]').click();task_filter(p,'Optics','summary');history_tools(p);p.locator('#clearCompleted').click()
 sent=reqs(p,'POST','/api/tasks/clear-completed')
 check(len(sent)==1,'Clear action did not submit once')
 check(sent[0]['body']['items']==[{'id':'a-done','generation':'generation-a-done'}],'Clear-completed escaped current filters')

@test('tasks_partial_failure_preserves_failed_selection')
def task_partial(p):
 setup_tasks(p);task_filter(p,type='summary');p.evaluate("__net.bulkFailureIds=['b-summary']")
 select_all(p);p.locator('[data-bulk="retry"]').click();open_steps(p)
 expect(p.locator('article.task-card[data-id="b-summary"] .task-select')).to_be_checked()
 expect(p.locator('article.task-card[data-id="a-summary"] .task-select')).not_to_be_checked()
 expect(p.locator('#actionResult')).to_contain_text('fixture conflict')

@test('tasks_refresh_keeps_a_disappearing_course_filter')
def task_disappear(p):
 setup_tasks(p);task_filter(p,'Optics');p.evaluate("__net.tasks.groups=__net.tasks.groups.filter(g=>g.category!=='Optics');__net.tasks.tasks=__net.tasks.tasks.filter(t=>t.category!=='Optics')")
 p.locator('#refreshTasks').click();expect(p.locator('#categoryFilter')).to_have_value('Optics')
 expect(p.locator('#taskList .task-group-card')).to_have_count(0)

@test('tasks_live_refresh_preserves_result_link_focus')
def task_link_focus(p):
 setup_tasks(p);open_steps(p);link=p.locator('article.task-card[data-id="a-summary"] .task-actions a');link.focus()
 p.evaluate("__net.tasks.groups[0].tasks[0].stage='New stage';window.dispatchEvent(new Event('online'))")
 open_steps(p)
 expect(p.locator('article.task-card[data-id="a-summary"] .task-stage')).to_contain_text('New stage');expect(link).to_be_focused()

@test('tasks_malformed_snapshot_does_not_erase_previous_list')
def task_malformed(p):
 setup_tasks(p);p.evaluate("__net.fail('GET','/api/tasks',200,{})");p.locator('#refreshTasks').click()
 expect(p.locator('#loadError')).to_be_visible();expect(p.locator('#taskList .task-group-card')).to_have_count(2)

@test('tasks_network_error_preserves_list_then_recovers')
def task_network(p):
 setup_tasks(p);p.evaluate("__net.fail('GET','/api/tasks')");p.locator('#refreshTasks').click();expect(p.locator('#loadError')).to_be_visible();expect(p.locator('#taskList .task-group-card')).to_have_count(2)
 p.locator('#refreshTasks').click();expect(p.locator('#loadError')).not_to_be_visible()

@test('tasks_old_refresh_cannot_overwrite_an_action_refresh')
def task_old_refresh(p):
 setup_tasks(p);hold(p,'GET','/api/tasks');p.locator('#refreshTasks').click();p.locator('.focus-actions [data-action="retry"]').first.click()
 reply(p,{'tasks':[],'groups':[]});expect(p.locator('#taskList .task-group-card')).to_have_count(2)

@test('tasks_selection_clears_when_filter_changes')
def task_change_filter(p):
 setup_tasks(p);select_all(p);task_filter(p,'Electronics');expect(p.locator('#selectAll')).not_to_be_checked();expect(p.locator('[data-bulk="retry"]')).not_to_be_visible()

@test('tasks_pdf_delayed_submit_cannot_close_reopened_dialog')
def task_pdf_reopen(p):
 setup_tasks(p);hold(p,'POST','/api/tasks/pdf');p.locator('#newPdf').click();p.locator('#pdfPaths').fill('/tmp/old.pdf');p.locator('#submitPdf').click();p.keyboard.press('Escape')
 task_filter(p,'Optics','summary');p.locator('#newPdf').click();p.locator('#pdfPaths').fill('/tmp/new.pdf');reply(p,{'tasks':[]})
 expect(p.locator('#pdfDialog')).to_be_visible();expect(p.locator('#pdfPaths')).to_have_value('/tmp/new.pdf');expect(p.locator('#categoryFilter')).to_have_value('Optics');expect(p.locator('#typeFilter')).to_have_value('summary')

@test('tasks_pdf_late_picker_does_not_erase_typed_path')
def task_pdf_picker_edit(p):
 setup_tasks(p);hold(p,'GET','/api/pdf-ai/choose-files');p.locator('#newPdf').click();p.locator('#choosePdf').click();p.locator('#pdfPaths').fill('/tmp/typed.pdf');reply(p,{'paths':['/tmp/picked.pdf']})
 expect(p.locator('#choosePdf')).to_be_enabled();expect(p.locator('#pdfPaths')).to_have_value('/tmp/typed.pdf')

@test('tasks_pdf_late_picker_does_not_cross_dialog_sessions')
def task_pdf_picker_reopen(p):
 setup_tasks(p);hold(p,'GET','/api/pdf-ai/choose-files');p.locator('#newPdf').click();p.locator('#choosePdf').click();p.keyboard.press('Escape');p.locator('#newPdf').click();p.locator('#pdfPaths').fill('/tmp/new.pdf');reply(p,{'paths':['/tmp/old-choice.pdf']})
 expect(p.locator('#choosePdf')).to_be_enabled();expect(p.locator('#pdfPaths')).to_have_value('/tmp/new.pdf')

@test('tasks_pdf_cannot_submit_during_file_picker')
def task_pdf_picker_guard(p):
 setup_tasks(p);hold(p,'GET','/api/pdf-ai/choose-files');p.locator('#newPdf').click();p.locator('#pdfPaths').fill('/tmp/old.pdf');p.locator('#choosePdf').click();expect(p.locator('#submitPdf')).to_be_disabled();reply(p,{'cancelled':True});expect(p.locator('#submitPdf')).to_be_enabled()

@test('tasks_pdf_whitespace_is_not_submitted')
def task_pdf_empty(p):
 setup_tasks(p);p.locator('#newPdf').click();p.locator('#pdfPaths').fill('  \n  ');p.locator('#submitPdf').click()
 check(not reqs(p,'POST','/api/tasks/pdf'),'Whitespace-only PDF request submitted');expect(p.locator('#pdfError')).to_be_visible();expect(p.locator('#pdfPaths')).to_be_focused()

@test('tasks_pdf_twenty_file_limit_before_submission')
def task_pdf_limit(p):
 setup_tasks(p);p.locator('#newPdf').click();p.locator('#pdfPaths').fill('\n'.join('/tmp/'+str(i)+'.pdf' for i in range(21)));p.locator('#submitPdf').click()
 check(not reqs(p,'POST','/api/tasks/pdf'),'21-file request submitted');expect(p.locator('#pdfError')).to_be_visible()

@test('tasks_pdf_deduplicates_and_trims_paths')
def task_pdf_dedup(p):
 setup_tasks(p);p.locator('#newPdf').click();p.locator('#pdfPaths').fill(' /tmp/a.pdf\n/tmp/a.pdf\n\n /tmp/b.pdf ');p.locator('#submitPdf').click()
 check(reqs(p,'POST','/api/tasks/pdf')[0]['body']['paths']==['/tmp/a.pdf','/tmp/b.pdf'],'Duplicate paths submitted')
 expect(p.locator('#pdfDialog')).not_to_be_visible();expect(p.locator('#statusFilters [data-status="active"]')).to_have_attribute('aria-current','page')

@test('tasks_pdf_failure_keeps_draft_and_retry_works')
def task_pdf_error(p):
 setup_tasks(p);p.evaluate("__net.fail('POST','/api/tasks/pdf',400,{detail:'File unavailable'})");p.locator('#newPdf').click();p.locator('#pdfPaths').fill('/tmp/a.pdf');p.locator('#submitPdf').click()
 expect(p.locator('#pdfError')).to_contain_text('File unavailable');expect(p.locator('#pdfPaths')).to_have_value('/tmp/a.pdf');expect(p.locator('#submitPdf')).to_be_enabled();p.locator('#submitPdf').click();expect(p.locator('#pdfDialog')).not_to_be_visible()

@test('tasks_pdf_picker_cancel_preserves_draft')
def task_pdf_cancel(p):
 setup_tasks(p);p.evaluate("__net.fail('GET','/api/pdf-ai/choose-files',200,{cancelled:true})");p.locator('#newPdf').click();p.locator('#pdfPaths').fill('/tmp/keep.pdf');p.locator('#choosePdf').click();expect(p.locator('#pdfPaths')).to_have_value('/tmp/keep.pdf')

@test('tasks_pdf_entry_query_keeps_course_and_embedding')
def task_pdf_query(p):
 setup_tasks(p,query='?category=Optics&embedded=1&new=pdf#files');expect(p.locator('#pdfDialog')).to_be_visible()
 out=p.evaluate('window.__historyResult');check('category=Optics' in out['url'] and 'embedded=1' in out['url'] and '#files' in out['url'],'PDF entry stripped unrelated URL state')
 check('new=' not in out['url'],'Entry trigger not removed');expect(p.locator('#categoryFilter')).to_have_value('Optics')

@test('tasks_narrow_layout_and_keyboard_dialog')
def task_mobile(p):
 p.set_viewport_size({'width':390,'height':844});setup_tasks(p)
 check(p.evaluate('document.documentElement.scrollWidth<=innerWidth'),'Page overflows narrow viewport')
 p.locator('#newPdf').focus();p.keyboard.press('Enter');expect(p.locator('#pdfDialog')).to_be_visible();p.keyboard.press('Escape');expect(p.locator('#pdfDialog')).not_to_be_visible()
 p.locator('[data-theme-toggle]').click();expect(p.locator('html')).to_have_attribute('data-theme','dark')

@test('mineru_save_does_not_erase_newer_edits')
def min_draft(p):
 setup(p,'mineru');hold(p,'PUT','/api/mineru/settings');p.locator('#mineruTier').select_option('advanced');p.locator('#saveMineruSettings').click();p.locator('#mineruTier').select_option('basic')
 reply(p,{'settings':{**MIN,'tier':'advanced'},'runtime':RUNTIME});expect(p.locator('#saveMineruSettings')).to_be_enabled();expect(p.locator('#mineruTier')).to_have_value('basic')
 p.locator('#saveMineruSettings').click();check(reqs(p,'PUT','/api/mineru/settings')[-1]['body']['settings']['tier']=='basic','Second edit not saved')

@test('mineru_empty_numeric_field_is_not_silently_zero')
def min_empty(p):
 setup(p,'mineru');p.locator('#mineruSettingsGroup details > summary').click();p.locator('#mineruWindowSize').fill('');p.locator('#saveMineruSettings').click()
 check(not reqs(p,'PUT','/api/mineru/settings'),'Blank numeric value silently submitted as zero');expect(p.locator('#mineruWindowSize')).to_be_focused()

@test('mineru_invalid_hidden_field_is_revealed')
def min_hidden(p):
 setup(p,'mineru');p.locator('#mineruSettingsGroup details > summary').click();p.locator('#mineruConcurrency').fill('99');p.locator('#mineruSettingsGroup details > summary').click();p.locator('#saveMineruSettings').click()
 expect(p.locator('#mineruConcurrency')).to_be_visible();expect(p.locator('#mineruConcurrency')).to_be_focused();check(not reqs(p,'PUT','/api/mineru/settings'),'Invalid concurrency sent')

@test('mineru_failed_reload_is_not_ready')
def min_reload(p):
 setup(p,'mineru');p.evaluate("__net.fail('GET','/api/mineru/settings')");p.evaluate('()=>__ctl.load()');check(not p.evaluate('__ctl.isReady()'),'Failed reload retained stale readiness');expect(p.locator('#saveMineruSettings')).to_be_disabled()
 p.evaluate('()=>__ctl.load()');expect(p.locator('#saveMineruSettings')).to_be_enabled()

@test('mineru_reset_is_a_draft_until_saved')
def min_reset(p):
 setup(p,'mineru');p.locator('#mineruTier').select_option('advanced');p.locator('#resetMineruSettings').click();expect(p.locator('#mineruTier')).to_have_value('standard');check(not reqs(p,'PUT','/api/mineru/settings'),'Reset unexpectedly persisted')
 p.locator('#saveMineruSettings').click();check(reqs(p,'PUT','/api/mineru/settings')[-1]['body']['settings']==MIN,'Recommended defaults changed')

@test('mineru_tier_controls_preserve_inactive_values')
def min_tier(p):
 setup(p,'mineru');p.locator('#mineruSettingsGroup details > summary').click();p.locator('#mineruConcurrency').fill('7');p.locator('#mineruTier').select_option('flash');expect(p.locator('#mineruConcurrency')).to_be_disabled();p.locator('#mineruTier').select_option('advanced');expect(p.locator('#mineruConcurrency')).to_be_enabled();expect(p.locator('#mineruConcurrency')).to_have_value('7')

@test('mineru_opening_settings_during_save_does_not_reset_draft')
def min_load_save(p):
 setup(p,'mineru');hold(p,'PUT','/api/mineru/settings');p.locator('#mineruTier').select_option('advanced');p.locator('#saveMineruSettings').click();p.locator('#mineruTier').select_option('basic');p.evaluate('()=>__ctl.load()')
 expect(p.locator('#mineruTier')).to_have_value('basic');reply(p,{'settings':{**MIN,'tier':'advanced'},'runtime':RUNTIME});expect(p.locator('#mineruTier')).to_have_value('basic')

def setup_backup(p,before=None):
 p.set_content('<html lang="zh-CN"><body><main><h1>Library backup component fixture</h1><button data-settings-tab="library">Library settings</button><p><button id="exportLibraryBackup">Export backup</button><a id="downloadLibraryBackup" class="hidden">Download ZIP</a></p><progress id="libraryBackupProgress" class="hidden" max="100"></progress><p id="libraryBackupStatus" role="status"></p></main></body></html>');p.add_style_tag(content=source('static/ui.css'))
 p.evaluate(NET,{'inference':INF,'mineru':MIN,'runtime':RUNTIME})
 if before:p.evaluate(before)
 load_module(p,'static/js/library-backup.js','createLibraryBackup')

def backup_ready(p):expect(p.locator('#exportLibraryBackup')).to_be_enabled()

@test('backup_initial_loading_blocks_duplicate_creation')
def backup_initial(p):
 setup_backup(p,"__net.hold('GET','/api/library/backup')");expect(p.locator('#exportLibraryBackup')).to_be_disabled();reply(p,{'backup':None});backup_ready(p)

@test('backup_read_error_retry_reads_instead_of_creating')
def backup_read_retry(p):
 setup_backup(p,"__net.fail('GET','/api/library/backup')");backup_ready(p);p.locator('#exportLibraryBackup').click();check(not reqs(p,'POST','/api/library/backup'),'Re-read button created a backup')
 check(len(reqs(p,'GET','/api/library/backup'))==2,'Status was not reconciled')

@test('backup_unknown_post_outcome_is_reconciled_before_retry')
def backup_unknown(p):
 setup_backup(p);backup_ready(p);p.evaluate("__net.fail('POST','/api/library/backup')");p.locator('#exportLibraryBackup').click();backup_ready(p);p.locator('#exportLibraryBackup').click()
 check(len(reqs(p,'POST','/api/library/backup'))==1,'Unknown POST outcome duplicated backup')
 check(len(reqs(p,'GET','/api/library/backup'))==2,'Missing GET reconciliation')

@test('backup_tab_refresh_cannot_unlock_pending_export')
def backup_refresh_race(p):
 setup_backup(p);backup_ready(p);hold(p,'POST','/api/library/backup');p.locator('#exportLibraryBackup').click();p.locator('[data-settings-tab="library"]').click();expect(p.locator('#exportLibraryBackup')).to_be_disabled();check(len(reqs(p,'GET','/api/library/backup'))==1,'Parallel refresh raced with export')
 reply(p,{'backup':{'status':'scanning'}});expect(p.locator('#exportLibraryBackup')).to_be_disabled()

@test('backup_empty_result_clears_old_progress_message')
def backup_empty(p):
 setup_backup(p,"__net.backup={status:'scanning'}");p.evaluate('__net.backup=null');p.locator('[data-settings-tab="library"]').click();backup_ready(p)
 check('\u6682\u65e0' in p.locator('#libraryBackupStatus').inner_text(),'Empty result retained obsolete packing message')

@test('backup_malformed_status_cannot_enable_creation')
def backup_malformed(p):
 setup_backup(p,"__net.fail('GET','/api/library/backup',200,{})");backup_ready(p);p.locator('#exportLibraryBackup').click();check(not reqs(p,'POST','/api/library/backup'),'Malformed response enabled a new export')

@test('backup_completed_then_released_clears_download_target')
def backup_released(p):
 setup_backup(p,"__net.backup={status:'completed',filename:'fixture.zip',download_url:'/fixture.zip',file_count:1,archive_bytes:123}")
 expect(p.locator('#downloadLibraryBackup')).to_be_visible();p.evaluate("__net.backup={status:'released'}");p.locator('[data-settings-tab="library"]').click();expect(p.locator('#downloadLibraryBackup')).not_to_be_visible();check(p.locator('#downloadLibraryBackup').get_attribute('href') is None,'Released ZIP retained stale download URL')

def setup_discard(p):
 p.set_content('<html lang="zh-CN"><body><main><h1>Recording discard component fixture</h1><button id="discard">Discard recording</button><dialog id="dialog"><h2 data-discard-title></h2><b data-discard-target></b><p data-discard-explain></p><button data-discard-cancel>Cancel</button><button data-discard-continue>Continue</button><button data-discard-confirm>Confirm discard</button></dialog><p id="notice" role="status"></p></main></body></html>');p.add_style_tag(content=source('static/ui.css'))
 p.evaluate(NET,{'inference':INF,'mineru':MIN,'runtime':RUNTIME})
 p.evaluate('async url=>{const m=await import(url);window.__session={id:"rec1",title:"Fixture recording 1"};window.__events=[];const event=(type)=>(id,value)=>__events.push({type,id,value});window.__ctl=m.createRecordingDiscard({button:document.querySelector("#discard"),dialog:document.querySelector("#dialog"),getSession:()=>__session,onAccepted:event("accepted"),onDiscarded:event("discarded"),onRestored:event("restored"),onFailed:(id,error)=>event("failed")(id,error.message),toast:message=>document.querySelector("#notice").textContent=message});__ctl.update();}',module_url('static/js/recording-discard.js'))

def confirm_discard(p):
 p.locator('#discard').click();p.locator('[data-discard-continue]').click();p.locator('[data-discard-confirm]').click()

def ws_done(p):p.evaluate('__ctl.receive({session_id:"rec1",type:"recording_discarded",status:"discarded"})')

@test('discard_late_post_cannot_resurrect_completed_operation')
def discard_post_stale(p):
 setup_discard(p);hold(p,'POST','/api/recordings/rec1/discard');confirm_discard(p);ws_done(p);reply(p,{'session_id':'rec1','status':'discarding'})
 p.wait_for_timeout(30);check(not p.evaluate('__ctl.isPending("rec1")'),'Completed discard resurrected by old POST');check(p.evaluate('__events.filter(e=>e.type==="accepted").length')==1,'Old POST repeated accepted callback')

@test('discard_late_poll_cannot_resurrect_completed_operation')
def discard_poll_stale(p):
 setup_discard(p);hold(p,'POST','/api/recordings/rec1/discard');hold(p,'GET','/api/recordings/rec1/discard');confirm_discard(p);reply(p,{'session_id':'rec1','status':'discarding'});p.wait_for_function('Object.keys(__net.pending).length===1');ws_done(p);reply(p,{'session_id':'rec1','status':'discarding'});p.wait_for_timeout(30)
 check(not p.evaluate('__ctl.isPending("rec1")'),'Completed discard resurrected by old poll')

@test('discard_late_poll_error_cannot_report_false_failure')
def discard_poll_error(p):
 setup_discard(p);hold(p,'POST','/api/recordings/rec1/discard');hold(p,'GET','/api/recordings/rec1/discard');confirm_discard(p);reply(p,{'session_id':'rec1','status':'discarding'});p.wait_for_function('Object.keys(__net.pending).length===1');ws_done(p);reply(p,{'detail':'stale error'},500);p.wait_for_timeout(30)
 check(not p.evaluate('__events.some(e=>e.type==="failed")'),'Late error reported an already completed discard as failed')

@test('discard_malformed_post_response_reconciles_status')
def discard_malformed(p):
 setup_discard(p);hold(p,'POST','/api/recordings/rec1/discard');hold(p,'GET','/api/recordings/rec1/discard');confirm_discard(p);reply(p,'{bad-json');p.wait_for_timeout(30)
 check(bool(reqs(p,'GET','/api/recordings/rec1/discard')),'Malformed POST response treated as definite failure without checking status');reply(p,{'session_id':'rec1','status':'discarded'});p.wait_for_function('__ctl.isDiscarded("rec1")')

@test('discard_two_confirmations_and_cancel_do_not_mutate')
def discard_cancel(p):
 setup_discard(p);p.locator('#discard').click();expect(p.locator('[data-discard-cancel]')).to_be_focused();p.keyboard.press('Escape');check(not p.evaluate('__net.requests.length'),'Escape sent mutation');p.locator('#discard').click();p.locator('[data-discard-continue]').click();p.locator('[data-discard-cancel]').click();check(not p.evaluate('__net.requests.length'),'Second-stage cancel sent mutation')

@test('discard_confirmed_target_survives_session_switch')
def discard_target(p):
 setup_discard(p);hold(p,'POST','/api/recordings/rec1/discard');p.locator('#discard').click();p.evaluate('__session={id:"rec2",title:"Fixture recording 2"}');p.locator('[data-discard-continue]').click();p.locator('[data-discard-confirm]').click()
 check(reqs(p,'POST','/api/recordings/rec1/discard')[0]['body']['expected_session_id']=='rec1','Confirmation changed target');reply(p,{'session_id':'rec1','status':'discarded'});expect(p.locator('#discard')).to_be_enabled();check(p.evaluate('__session.id')=='rec2','Successor recording was replaced')

@test('discard_old_attempt_cannot_complete_a_new_retry')
def discard_attempt(p):
 setup_discard(p);hold(p,'POST','/api/recordings/rec1/discard');hold(p,'GET','/api/recordings/rec1/discard');confirm_discard(p)
 p.evaluate('__ctl.receive({type:"recording_discarding",session_id:"rec1"})');p.wait_for_function('Object.keys(__net.pending).length===2')
 ids=p.evaluate('Object.keys(__net.pending).map(Number)');p.evaluate('args=>__net.reply(...args)',[ids[-1],{'detail':'fixture retryable failure'},500]);expect(p.locator('#discard')).to_be_enabled()
 hold(p,'POST','/api/recordings/rec1/discard');confirm_discard(p);p.evaluate('args=>__net.reply(...args)',[ids[0],{'session_id':'rec1','status':'discarded'},200]);p.wait_for_timeout(30)
 check(p.evaluate('__ctl.isPending("rec1")'),'An older attempt completed the new retry');check(not p.evaluate('__events.some(e=>e.type==="discarded")'),'Old attempt fired terminal callback')
 reply(p,{'session_id':'rec1','status':'discarded'});p.wait_for_function('__ctl.isDiscarded("rec1")')

@test('tasks_type_filter_updates_group_error_count')
def task_scoped_count(p):
 setup_tasks(p);task_filter(p,'Optics','summary')
 expect(p.locator('[data-group-id="recording:a"] .group-status')).to_contain_text('失败')
 expect(p.locator('[data-group-id="recording:a"] .group-details > .task-card')).to_have_count(1)

@test('tasks_type_and_status_filters_intersect')
def task_scoped_status(p):
 data=task_fixture();t=data['tasks'][1];t.update(status='running',error='',actions=['pause'])
 setup_tasks(p,tasks=data);task_filter(p,'Optics','summary');p.locator('[data-status="active"]').click()
 expect(p.locator('.task-group-card')).to_have_count(0)

def setup_continuation(p,action='retry'):
 p.set_content('<main><h1>Workbench continuation component fixture</h1><div id="continuationList"></div><p id="continuationError" hidden></p><p id="feedback" role="status"></p></main>')
 p.add_style_tag(content=source('static/ui.css'))
 data=task_fixture();data['tasks']=[{**data['tasks'][0],'actions':[action]}]
 p.evaluate(NET,{'inference':INF,'mineru':MIN,'runtime':RUNTIME,'tasks':data})
 p.evaluate('''()=>{const fetcher=window.fetch;window.fetch=async(path,options)=>String(path).startsWith('/api/library?')?new Response(JSON.stringify({records:[{id:'a',title:'Fixture lesson',category:'Optics',status:'completed'}]}),{status:200}):fetcher(path,options);window.__opened=[];}''')
 p.evaluate('''async url=>{const m=await import(url);window.__ctl=m.createContinuation({openRecord:async id=>__opened.push(id),toast:message=>document.getElementById('feedback').textContent=message});await __ctl.refresh();}''',module_url('static/js/workbench/continuation.js'))

@test('continuation_retry_is_accepted_by_task_client_guard')
def continuation_retry(p):
 setup_continuation(p);p.locator('[data-action="retry"]').click()
 expect(p.locator('#feedback')).to_have_text('\u5df2\u63d0\u4ea4\u540e\u53f0\u5904\u7406')
 check(reqs(p,'POST','/api/tasks/action')[0]['headers'].get('x-qwenasr-client')=='task-center','Task client header missing')
 check(p.evaluate('__net.applied')==['a-summary'],'Wrong task owner')

@test('continuation_continue_keeps_saved_settings_intent')
def continuation_continue(p):
 setup_continuation(p,'continue');p.locator('[data-action="continue"]').click()
 expect(p.locator('#feedback')).to_have_text('\u5df2\u63d0\u4ea4\u540e\u53f0\u5904\u7406')
 check(reqs(p,'POST','/api/tasks/action')[0]['body']=={'id':'a-summary','action':'continue','use_saved_ai_settings':True},'Provider settings leaked or intent changed')

@test('continuation_failure_keeps_retry_usable')
def continuation_failure(p):
 setup_continuation(p);p.evaluate("__net.fail('POST','/api/tasks/action',409,{detail:'Fixture conflict'})")
 p.locator('[data-action="retry"]').click();expect(p.locator('#feedback')).to_have_text('Fixture conflict');expect(p.locator('[data-action="retry"]')).to_be_enabled()
 p.locator('[data-action="retry"]').click();expect(p.locator('#feedback')).to_have_text('\u5df2\u63d0\u4ea4\u540e\u53f0\u5904\u7406')

@test('continuation_open_record_does_not_start_processing')
def continuation_read(p):
 setup_continuation(p);p.locator('[data-recording="a"]').click();check(p.evaluate('__opened')==['a'],'Wrong record opened');check(not reqs(p,'POST','/api/tasks/action'),'Reading caused task mutation')

@test('continuation_pending_action_cannot_be_submitted_twice')
def continuation_pending(p):
 setup_continuation(p);hold(p,'POST','/api/tasks/action');p.locator('[data-action="retry"]').click();expect(p.locator('[data-action="retry"]')).to_be_disabled();check(len(reqs(p,'POST','/api/tasks/action'))==1,'Duplicate submission');reply(p,{});expect(p.locator('[data-action="retry"]')).to_be_enabled()


def main():
 selected=[(n,f) for n,f in TESTS if args.match in n]
 results=[]
 with sync_playwright() as pw:
  browser=pw.chromium.launch(executable_path=args.chromium,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
  for name,f in selected:
   context=browser.new_context(viewport={'width':1365,'height':1000});context.tracing.start(screenshots=True,snapshots=True,sources=True)
   page=context.new_page();page.set_default_timeout(1800);page.route('**/*',lambda route:route.abort());errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
   t=time.monotonic();result={'name':name}
   try:
    f(page)
    if errors:raise AssertionError('Browser script errors: '+str(errors))
    result['status']='PASS'
   except Exception as exc:
    result.update(status='FAIL',error=str(exc),traceback=traceback.format_exc())
   finally:
    result['seconds']=round(time.monotonic()-t,3)
    try:
     page.screenshot(path=str(OUT/(name+'.png')),full_page=True, animations="disabled", timeout=4000)
     requests=page.evaluate('window.__net?.requests||[]');(OUT/(name+'.requests.json')).write_text(json.dumps(requests,ensure_ascii=False,indent=2))
    except Exception as exc:result['capture_error']=str(exc)
    context.tracing.stop(path=str(OUT/(name+'.trace.zip')));context.close()
   results.append(result);print(result['status'],name,result.get('error','')[:160],flush=True)
  browser.close()
 summary={'mode':MODE,'browser':'Chromium','transport':'controlled API fixtures; external network blocked','results':results,'passed':sum(x['status']=='PASS' for x in results),'failed':sum(x['status']=='FAIL' for x in results)}
 (OUT/'results.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print('TOTAL',summary['passed'],summary['failed'])
 return summary['failed']
if __name__=='__main__':sys.exit(bool(main()))
