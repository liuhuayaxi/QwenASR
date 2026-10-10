import {escapeHtml as esc, recordingId, recordStages} from './workbench/continuation.js?v=20261011';
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
let category = params.get('category') || '', records = [], tasks = [], categories = [], timer, loading = false;
const routes = {records:'/library', notes:'/library', materials:'/course-files', review:'/notes'};
const query = values => new URLSearchParams({category,...values}).toString();
function render() {
  $('workspaceTitle').textContent = category;
  const lessons = records.filter(record => record.category === category);
  $('workspaceRecent').innerHTML = '<h2>最近课堂与笔记</h2>' + lessons.slice(0,5).map(record => `<article class="continuation-card"><b>${esc(record.title)}</b><p>${recordStages(record,tasks.filter(task=>recordingId(task)===record.id)).map(esc).join(' · ')}</p><button class="ui-button" data-recording="${esc(record.id)}">阅读原文 / 打开课堂笔记 / 课堂问答</button></article>`).join('') + (!lessons.length ? '<p>这门课程还没有课堂记录。</p>' : '');
  $('workspaceTimeline').innerHTML = tasks.filter(task=>task.category===category || task.type==='course_queue').slice(0,10).map(task=>`<p>${esc(task.title)} · ${esc(task.label)} · ${esc(task.status === 'completed' ? '已完成' : task.stage || task.status)} <button class="ui-button" data-timeline>查看并处理</button></p>`).join('') || '<p>这门课程目前没有任务。</p>';
  $('workspaceQuestions').innerHTML = lessons.map(record => `<p>${esc(record.title)} <button class="ui-button" data-chat="${esc(record.id)}">提问这堂课</button></p>`).join('') || '<p>保存课堂记录后，可结合全文提问。</p>';
  $('microphoneSource').href = '/?' + query({});
  $('uploadSource').href = '/?' + query({source:'upload'});
}
function tab(name, recording) {
  $('workspaceOverview').hidden = name !== 'overview';
  $('workspaceReview').hidden = name !== 'review';
  $('workspaceFrame').hidden = name === 'overview' || name === 'review';
  document.querySelectorAll('#workspaceTabs [data-tab]').forEach(button=>button.setAttribute('aria-current',button.dataset.tab===name ? 'page':'false'));
  if (name !== 'overview' && name !== 'review') $('workspaceFrame').src = routes[name] + '?' + query({embedded:'1',...(recording ? {recording} : {})});
}
function timeline() { $('timelineFrame').src='/tasks?'+query({embedded:'1'});$('timelineDialog').showModal(); }
async function load() {
  if (loading) return;
  loading=true;
  try {
    const result = await Promise.all(['/api/library','/api/tasks'].map(async url=>{const response=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(15000)});if(!response.ok)throw new Error('课程状态暂时无法读取');return response.json();}));
    records=result[0].records || [];tasks=result[1].tasks || [];
    categories=[...new Set([...(result[0].categories || []),...records.map(record=>record.category),...(category ? [category] : [])].filter(Boolean))];
    if (!category) category=categories[0] || '未分类';
    $('workspaceCourse').innerHTML=categories.map(name=>`<option ${name===category?'selected':''}>${esc(name)}</option>`).join('');
    render();$('workspaceError').hidden=true;
  } catch (error) {$('workspaceError').hidden=false;$('workspaceError').textContent=error.message+'，稍后自动重试。';}
  finally {loading=false;clearTimeout(timer);if(!document.hidden)timer=setTimeout(load,5000);}
}
$('workspaceCourse').addEventListener('change',()=>{category=$('workspaceCourse').value;history.replaceState(null,'','/workspace?'+query({}));render();tab('overview');});
document.addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;if(button.dataset.tab)tab(button.dataset.tab);if(button.dataset.recording)tab('notes',button.dataset.recording);if(button.dataset.chat){tab('review');$('workspaceReview').hidden=true;$('workspaceFrame').hidden=false;$('workspaceFrame').src='/?'+query({embedded:'1',chat:button.dataset.chat});}if(button.hasAttribute('data-external-review')){$('workspaceReview').hidden=true;$('workspaceFrame').hidden=false;$('workspaceFrame').src='/notes?'+query({embedded:'1'});}if(button.hasAttribute('data-timeline'))timeline();if(button.dataset.close)$(button.dataset.close).close();});
$('workspaceTasks').addEventListener('click',timeline);
$('newClass').addEventListener('click',()=>$('newClassDialog').showModal());
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void load();else clearTimeout(timer);});
void load();
