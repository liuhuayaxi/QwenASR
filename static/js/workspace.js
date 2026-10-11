import {escapeHtml as esc, recordingId, activeStatuses, attentionStatuses} from './workbench/continuation.js?v=20261011-ui-final';
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
let category = params.get('category') || '', records = [], tasks = [], categories = [], timer, loading = false, recordsLoaded = false, tasksLoaded = false;
const routes = {records:'/library', notes:'/library', materials:'/course-files', review:'/notes'};
const query = values => new URLSearchParams({category, ...values}).toString();
const hasNote = record => Boolean(record.ai_note_summary?.summary || record.has_ai_summary);
const taskLabels = {running:'进行中', queued:'排队中', waiting_retry:'等待重试', stopping:'正在停止', paused:'已暂停', failed:'失败', error:'失败', interrupted:'已中断', pending:'待处理', stale:'原件已更新', completed_with_errors:'部分失败', completed:'已完成', cancelled:'已取消'};
// Avoid replacing focused controls on every poll when the rendered data is unchanged.
const rendered = new Map();
function replace(id, html) {
  if (rendered.get(id) === html) return;
  const target = $(id), focused = target.contains(document.activeElement) ? document.activeElement : null;
  const identity = focused?.dataset.recording || focused?.dataset.chat;
  const focusedValue = focused && 'value' in focused ? focused.value : '';
  const focusedId = focused?.id || '';
  target.innerHTML = html;
  rendered.set(id, html);
  if (identity) [...target.querySelectorAll('button')].find(button => (button.dataset.recording || button.dataset.chat) === identity)?.focus({preventScroll:true});
  else if (focusedId) { const next = document.getElementById(focusedId); if (next) { next.focus({preventScroll:true}); if ('value' in next && focusedValue) next.value = focusedValue; } }
}
function dateParts(record) {
  const date = new Date(record.recorded_at || record.started_at || '');
  if (!Number.isFinite(date.getTime())) return {day:'—', month:'课堂', full:''};
  const parts = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit'}).formatToParts(date);
  const value = name => parts.find(part => part.type === name)?.value;
  return {day:value('day'), month:`${Number(value('month'))} 月`, full:`${value('year')}/${value('month')}/${value('day')}`};
}
function recordRow(record, chat = false) {
  const date = dateParts(record);
  const duration = record.duration > 0 ? `${Math.max(1, Math.round(record.duration / 60))} 分钟` : '';
  const detail = [category ? '' : record.category, date.full, duration, hasNote(record) ? '笔记已就绪' : record.status === 'completed' ? '转写已完成' : '处理中'].filter(Boolean).join(' · ');
  return `<button class="workspace-record" ${chat ? 'data-chat' : 'data-recording'}="${esc(record.id)}" aria-label="${chat ? '提问' : '打开课堂记录'}：${esc(record.title || '未命名课堂')}"><span class="workspace-record-date" aria-hidden="true"><strong>${esc(date.day)}</strong><span>${esc(date.month)}</span></span><span class="workspace-record-body"><span class="workspace-record-title">${esc(record.title || '未命名课堂')}</span><span class="workspace-record-meta">${esc(detail)}</span></span><span class="workspace-record-arrow" aria-hidden="true">${chat ? '提问 →' : '↗'}</span></button>`;
}
function render() {
  $('workspaceTitle').textContent = category || '全部课程';
  const lessons = records.filter(record => !category || record.category === category).sort((a,b) => String(b.recorded_at || b.started_at || '').localeCompare(String(a.recorded_at || a.started_at || '')));
  const ids = new Set(lessons.map(record => record.id));
  const related = tasks.filter(task => !category || task.category === category || ids.has(recordingId(task)));
  const pending = related.filter(task => activeStatuses.has(task.status) || attentionStatuses.has(task.status));
  $('workspaceMeta').textContent = `${lessons.length} 堂课堂 · ${lessons.filter(hasNote).length} 份笔记${tasksLoaded ? ` · ${pending.length} 项待处理` : ''}`;
  replace('workspaceRecent', lessons.slice(0,5).map(record => recordRow(record)).join('') || '<p class="workspace-empty">暂无课堂记录</p>');
  const shown = (pending.length ? pending : related.filter(task => task.status === 'completed')).slice(0, pending.length ? 4 : 3);
  $('workspaceTaskHeading').textContent = pending.length ? '待处理任务' : '最近任务';
  $('workspaceTaskCount').textContent = pending.length ? String(pending.length) : '';
  replace('workspaceTimeline', shown.map(task => `<article class="workspace-task"><b>${esc(task.title || task.label || '任务')}</b><p><span>${esc(task.label || '')}</span><span class="workspace-task-status" data-attention="${attentionStatuses.has(task.status)}">${esc(taskLabels[task.status] || task.stage || '处理中')}</span></p></article>`).join('') || `<p class="workspace-empty">${tasksLoaded ? '暂无任务' : '任务状态暂不可用'}</p>`);
  replace('workspaceQuestions', lessons.map(record => recordRow(record,true)).join('') || '<p class="workspace-empty">暂无课堂记录</p>');
  $('microphoneSource').href = '/?' + query({});
  $('uploadSource').href = '/?' + query({source:'upload'});
}
function tab(name, recording) {
  if (!['overview','review',...Object.keys(routes)].includes(name)) return;
  $('workspaceOverview').hidden = name !== 'overview';
  $('workspaceReview').hidden = name !== 'review';
  $('workspaceFrame').hidden = name === 'overview' || name === 'review';
  $('workspaceFrame').dataset.view = name;
  document.querySelectorAll('#workspaceTabs [data-tab]').forEach(button => {
    if (button.dataset.tab === name) button.setAttribute('aria-current','page');
    else button.removeAttribute('aria-current');
  });
  if (name !== 'overview' && name !== 'review') $('workspaceFrame').src = routes[name] + '?' + query({embedded:'1', ...(recording ? {recording} : {})});
}
function timeline() {
  $('timelineFrame').src = '/tasks?' + query({embedded:'1'});
  $('timelineDialog').showModal();
}
async function load() {
  if (loading) return;
  loading = true;
  try {
    const result = await Promise.allSettled(['/api/library','/api/tasks'].map(async url => {
      const response = await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(15000)});
      if (!response.ok) throw new Error('读取失败');
      return response.json();
    }));
    if (result[0].status === 'fulfilled') {
      records = result[0].value.records || [];
      recordsLoaded = true;
      categories = [...new Set([...(result[0].value.categories || []), ...records.map(record => record.category), ...(category ? [category] : [])].filter(Boolean))];
      replace('workspaceCourse', [`<option value="" ${!category ? 'selected' : ''}>全部课程</option>`, ...categories.map(name => `<option value="${esc(name)}" ${name===category ? 'selected' : ''}>${esc(name)}</option>`)].join(''));
      $('workspaceCourse').disabled = false;
    }
    if (result[1].status === 'fulfilled') { tasks = result[1].value.tasks || []; tasksLoaded = true; }
    if (recordsLoaded) render();
    const failures = result.map((value, index) => value.status === 'rejected' ? (index ? '任务' : '课堂记录') : '').filter(Boolean);
    $('workspaceError').hidden = !failures.length;
    $('workspaceError').textContent = failures.length ? `${failures.join('、')}读取失败，正在重试。` : '';
    if (!recordsLoaded) {
      $('workspaceMeta').textContent = '暂时无法读取课程';
      replace('workspaceRecent','<p class="workspace-empty">课堂记录暂不可用</p>');
      replace('workspaceTimeline','<p class="workspace-empty">任务状态暂不可用</p>');
    }
  } finally {
    loading = false;
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(load,5000);
  }
}
$('workspaceCourse').addEventListener('change', () => {
  category = $('workspaceCourse').value;
  try { localStorage.setItem('qwen-asr-last-course-v1',category); } catch { /* optional preference */ }
  history.replaceState(null,'','/workspace?' + query({}));
  render(); tab('overview');
});
document.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.tab) tab(button.dataset.tab);
  if (button.dataset.recording) tab('notes',button.dataset.recording);
  if (button.dataset.chat) {
    tab('review'); $('workspaceReview').hidden = true; $('workspaceFrame').hidden = false;
    $('workspaceFrame').src = '/?' + query({embedded:'1',chat:button.dataset.chat});
  }
  if (button.hasAttribute('data-external-review')) {
    tab('review'); $('workspaceReview').hidden = true; $('workspaceFrame').hidden = false;
    $('workspaceFrame').src = '/notes?' + query({embedded:'1'});
  }
  if (button.hasAttribute('data-timeline')) timeline();
  if (button.dataset.close) $(button.dataset.close).close();
});
$('workspaceTasks').addEventListener('click',timeline);
$('newClass').addEventListener('click', () => $('newClassDialog').showModal());
document.addEventListener('visibilitychange', () => { if (!document.hidden) void load(); else clearTimeout(timer); });
void load();
