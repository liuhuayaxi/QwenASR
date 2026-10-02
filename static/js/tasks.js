import {aiTaskPayload} from './task-config.js?v=20260913-tasks-1';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const active = new Set(['running', 'queued', 'waiting_retry', 'stopping']);
const attention = new Set(['failed', 'interrupted', 'pending', 'stale', 'completed_with_errors']);
const labels = {running:'进行中', queued:'排队中', waiting_retry:'等待自动重试', stopping:'正在停止', paused:'已暂停', failed:'失败', error:'失败', interrupted:'已中断', pending:'待处理', stale:'原件已更新', completed_with_errors:'部分失败', completed:'已完成', cancelled:'已取消'};
const actionLabels = {continue:'继续', retry:'重试', pause:'暂停', cancel:'取消'};
const filterLabels = {all:'全部任务', active:'进行中', attention:'待处理', paused:'已暂停', completed:'已完成', cancelled:'已取消'};
const state = {tasks: [], counts: {}, filter: 'all', selected: new Set(), busy: false, loading: false, ready: false};
let timer, toastTimer, lastPayload = '';

function notice(id, message) { $(id).textContent = message; $(id).hidden = !message; }
function toast(message) { notice('toast', message); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 5000); }
async function api(path, body) {
  const response = await fetch(path, {cache: 'no-store', signal: AbortSignal.timeout(30000), ...(body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-QwenASR-Client': 'task-center'}, body: JSON.stringify(body),
  })});
  let data;
  try { data = await response.json(); } catch { throw new Error('后台尚未更新或连接已中断，请重启服务后刷新'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}）`);
  return data;
}
function filteredTasks() {
  const query = $('taskSearch').value.trim().toLocaleLowerCase();
  const type = $('typeFilter').value, category = $('categoryFilter').value;
  return state.tasks.filter(task => (state.filter === 'all' || (state.filter === 'active' ? active.has(task.status) : state.filter === 'attention' ? attention.has(task.status) : task.status === state.filter))
    && (!type || task.type === type || task.family === type) && (!category || (category === '__local__' ? !task.category : task.category === category))
    && (!query || [task.title, task.category, task.label, ...(task.files || []).map(file => file.source_name)].join(' ').toLocaleLowerCase().includes(query)));
}
function dateLabel(value) {
  if (!value) return '';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'});
}
function friendlyError(task) {
  const messages = {
    network_dns: '模型服务域名暂时无法解析，网络恢复后可继续。',
    output_truncated: '模型输出达到长度上限，可继续处理或重试。',
    input_too_long: '请求超过服务商窗口，继续后会拆成更小的片段。',
    rate_limit: '模型服务临时限流。', provider_busy: '模型服务暂时繁忙。',
    provider_quota: '服务商余额或额度不足，请补充额度或更换模型后重试。',
    invalid_json: '模型返回的 JSON 结构不完整，可继续重新生成。',
    invalid_segment_mapping: '模型返回的段落编号不可靠，可继续重新处理。',
  };
  return messages[task.error_kind] || task.error || '';
}
function card(task) {
  const progress = task.status === 'completed' ? 100 : task.progress ?? (task.total ? Math.round((task.completed || 0) / task.total * 100) : null);
  const bounded = progress === null ? null : Math.max(0, Math.min(100, Number(progress) || 0));
  const count = task.total ? `${task.completed || 0}/${task.total}${task.family === 'mineru' ? ' 个文件' : ' 段'}` : '';
  const wait = task.status === 'waiting_retry' && task.retry_in_seconds ? ` · 约 ${task.retry_in_seconds} 秒后自动重试` : '';
  const stage = (task.stage || (task.status === 'pending' ? '等待手动开始处理' : labels[task.status])) + wait;
  const error = friendlyError(task);
  const details = [task.error && task.error !== error ? task.error : '',
    task.range_end ? `片段范围：${Math.floor(task.range_start / 60)}–${Math.ceil(task.range_end / 60)} 分钟` : '',
    task.attempts > 1 ? `历史尝试 ${task.attempts} 次` : '', ...(task.warnings || [])].filter(Boolean);
  const files = (task.files || []).map(file => `<li class="task-file"><div><b>${esc(file.source_name)}</b><span>${esc(labels[file.status] || file.status)}${file.status === 'running' ? ` · ${Math.round(file.progress || 0)}%` : ''}</span></div><p>${esc(file.error || file.stage)}</p><p>${esc(file.output_path || file.source_path)}</p>${file.download_url ? `<a href="${esc(file.download_url)}" download>下载 AI 友好 PDF</a>` : ''}</li>`).join('');
  return `<article class="task-card" data-id="${esc(task.id)}" data-status="${esc(task.status)}"><div class="task-card-top">
    <input class="task-select" type="checkbox" aria-label="选择 ${esc(task.label)}：${esc(task.title)}" ${state.selected.has(task.id) ? 'checked' : ''} ${state.busy ? 'disabled' : ''}>
    <div class="task-main"><div class="task-title"><h3>${esc(task.title)}</h3><span class="task-status">${esc(labels[task.status] || task.status)}</span></div>
    <div class="task-meta"><span class="task-type">${esc(task.label)}</span><span>${esc(task.category || task.source)}</span>${dateLabel(task.updated_at) ? `<span>更新于 ${esc(dateLabel(task.updated_at))}</span>` : ''}</div>
    <div class="task-stage"><span>${esc(stage)}</span><span>${esc(count)}${bounded !== null ? `${count ? ' · ' : ''}${bounded}%` : ''}</span></div>
    ${bounded !== null || active.has(task.status) ? `<progress class="task-progress" max="100" ${bounded !== null ? `value="${bounded}"` : ''} aria-label="${esc(task.label)}进度"></progress>` : ''}
    ${error ? `<div class="task-error">${esc(error)}</div>` : ''}
    <div class="task-actions">${(task.actions || []).map(action => `<button data-action="${action}" class="ui-button ${action === 'cancel' ? 'danger' : action === 'continue' ? 'primary' : ''}" ${state.busy ? 'disabled' : ''}>${actionLabels[action]}</button>`).join('')}
    ${task.result_url ? `<a href="${esc(task.result_url)}" class="ui-button">${task.type === 'course_queue' ? '打开课程回放' : task.family === 'ai' || task.type === 'upload' ? '查看笔记' : '查看课程文件'}</a>` : ''}${task.download_url ? `<a href="${esc(task.download_url)}" class="ui-button" download>下载 AI 友好 PDF</a>` : ''}</div>
    ${files || details.length ? `<details class="task-details"><summary>${files ? `文件明细（${task.files.length}）` : '任务详情'}</summary>${details.map(text => `<p>${esc(text)}</p>`).join('')}${files ? `<ul>${files}</ul>` : ''}</details>` : ''}
    </div></div></article>`;
}
function selectionControls() {
  const visible = filteredTasks();
  const selected = visible.filter(task => state.selected.has(task.id));
  $('selectionCount').textContent = selected.length ? `已选 ${selected.length} 项` : '未选择任务';
  $('selectAll').checked = visible.length > 0 && selected.length === visible.length;
  $('selectAll').indeterminate = selected.length > 0 && selected.length < visible.length;
  $('selectAll').disabled = !visible.length || state.busy;
  document.querySelectorAll('[data-bulk]').forEach(button => {
    const count = selected.filter(task => task.actions.includes(button.dataset.bulk)).length;
    button.disabled = state.busy || !count;
    button.textContent = `${actionLabels[button.dataset.bulk]}${count ? ` (${count})` : ''}`;
  });
}
function render() {
  const opened = new Set([...$('taskList').querySelectorAll('details[open]')].map(node => node.closest('[data-id]').dataset.id));
  const focused = document.activeElement;
  const focusedId = focused?.closest('#taskList [data-id]')?.dataset.id;
  const focusSelector = focused?.matches('.task-select') ? '.task-select' : focused?.matches('summary') ? 'summary' : focused?.dataset.action ? `[data-action="${focused.dataset.action}"]` : null;
  const tasks = filteredTasks();
  $('listTitle').textContent = `${filterLabels[state.filter]} · ${tasks.length}`;
  $('taskList').innerHTML = tasks.length ? tasks.map(card).join('') : `<div class="task-empty"><b>${state.tasks.length ? '没有符合条件的任务' : '目前没有任务'}</b>${state.tasks.length ? '可以切换状态、类型或课程，或清空搜索。' : '在工作台生成 AI 笔记，或添加 PDF 识别任务，进度会显示在这里。'}</div>`;
  $('taskList').querySelectorAll('[data-id]').forEach(node => {
    if (opened.has(node.dataset.id) && node.querySelector('details')) node.querySelector('details').open = true;
    if (node.dataset.id === focusedId && focusSelector) node.querySelector(focusSelector)?.focus({preventScroll: true});
  });
  document.querySelectorAll('[data-count]').forEach(node => node.textContent = state.counts[node.dataset.count] || 0);
  document.querySelectorAll('[data-status]').forEach(node => { if (node.matches('button')) node.setAttribute('aria-current', node.dataset.status === state.filter ? 'page' : 'false'); });
  document.querySelectorAll('[data-summary]').forEach(node => node.setAttribute('aria-pressed', String(node.dataset.summary === state.filter)));
  selectionControls();
}
function applyFilters() { state.selected.clear(); render(); }
function setFilter(filter) { state.filter = filter; applyFilters(); }
async function load() {
  if (state.loading || state.busy) return;
  clearTimeout(timer); state.loading = true;
  try {
    const data = await api('/api/tasks');
    state.tasks = data.tasks || []; state.counts = data.counts || {}; state.ready = true;
    state.selected = new Set([...state.selected].filter(id => state.tasks.some(task => task.id === id)));
    const categories = [...new Set(state.tasks.map(task => task.category).filter(Boolean))].sort((a,b) => a.localeCompare(b,'zh-CN'));
    const categoryValue = $('categoryFilter').value;
    $('categoryFilter').innerHTML = '<option value="">全部课程</option>' + categories.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('') + '<option value="__local__">本地 PDF</option>';
    if ([...$('categoryFilter').options].some(option => option.value === categoryValue)) $('categoryFilter').value = categoryValue;
    const signature = JSON.stringify(data);
    if (signature !== lastPayload) { render(); lastPayload = signature; }
    notice('loadError', ''); notice('sourceWarning', (data.warnings || []).join('\n'));
    $('lastUpdated').textContent = `已同步 ${new Date().toLocaleTimeString('zh-CN')} · 每 3 秒自动更新`;
  } catch (error) {
    notice('loadError', `${error.message}。${state.ready ? '当前保留上次读取的任务。' : ''}`);
    if (!state.ready) $('taskList').innerHTML = '<div class="task-empty">暂时无法读取任务，请稍后刷新。</div>';
    $('lastUpdated').textContent = '连接中断，正在自动重连';
  } finally {
    state.loading = false; $('taskList').setAttribute('aria-busy', 'false');
    if (!document.hidden) timer = setTimeout(load, 3000);
  }
}
async function perform(targets, action, bulk = false) {
  if (state.busy || !targets.length) return;
  state.busy = true; render(); notice('actionResult', '');
  try {
    let defaults = {}, library = {};
    if (targets.some(task => task.family === 'ai') && ['continue','retry'].includes(action)) {
      [defaults, library] = await Promise.all([api('/api/config'), api('/api/library')]);
    }
    const items = targets.map(task => aiTaskPayload(task, action, defaults, library));
    if (bulk) {
      const result = await api('/api/tasks/bulk-action', {action, items});
      const failures = result.results.filter(item => !item.ok).map(item => `${targets.find(task => task.id === item.id)?.title || '任务'}：${item.error}`);
      notice('actionResult', `已提交${actionLabels[action]} ${result.accepted}/${result.requested} 项任务${failures.length ? '\n' + failures.join('\n') : ''}`);
    } else {
      await api('/api/tasks/action', items[0]);
      toast(action === 'pause' || action === 'cancel' ? `已请求${actionLabels[action]}，正在保存进度` : '任务已加入后台队列');
    }
    state.selected.clear();
  } catch (error) { notice('actionResult', error.message); }
  finally { state.busy = false; lastPayload = ''; await load(); render(); }
}
$('statusFilters').addEventListener('click', event => { const button = event.target.closest('[data-status]'); if (button) setFilter(button.dataset.status); });
document.querySelectorAll('[data-summary]').forEach(button => button.addEventListener('click', () => setFilter(button.dataset.summary)));
for (const id of ['typeFilter', 'categoryFilter']) $(id).addEventListener('change', applyFilters);
$('taskSearch').addEventListener('input', applyFilters);
$('selectAll').addEventListener('change', () => { state.selected = new Set($('selectAll').checked ? filteredTasks().map(task => task.id) : []); render(); });
$('taskList').addEventListener('change', event => { if (!event.target.matches('.task-select')) return; const id = event.target.closest('[data-id]').dataset.id; if (event.target.checked) state.selected.add(id); else state.selected.delete(id); selectionControls(); });
$('taskList').addEventListener('click', event => { const button = event.target.closest('[data-action]'); if (!button) return; const task = state.tasks.find(task => task.id === button.closest('[data-id]').dataset.id); if (task) perform([task], button.dataset.action); });
document.querySelectorAll('[data-bulk]').forEach(button => button.addEventListener('click', () => {
  const targets = filteredTasks().filter(task => state.selected.has(task.id) && task.actions.includes(button.dataset.bulk));
  if (targets.length > 200) { toast('每次最多操作 200 项任务，请减少选择'); return; }
  perform(targets, button.dataset.bulk, true);
}));
$('refreshTasks').addEventListener('click', load);
$('newPdf').addEventListener('click', () => $('pdfDialog').showModal());
$('closePdf').addEventListener('click', () => $('pdfDialog').close());
$('choosePdf').addEventListener('click', async () => {
  $('choosePdf').disabled = true; notice('pdfError', '');
  try { const result = await api('/api/pdf-ai/choose-files'); if (!result.cancelled) $('pdfPaths').value = result.paths.join('\n'); }
  catch (error) { notice('pdfError', error.message); }
  finally { $('choosePdf').disabled = false; }
});
$('pdfForm').addEventListener('submit', async event => {
  event.preventDefault(); $('submitPdf').disabled = true; notice('pdfError', '');
  try {
    await api('/api/tasks/pdf', {paths: $('pdfPaths').value.split('\n').map(path => path.trim()).filter(Boolean)});
    $('pdfDialog').close(); $('pdfPaths').value = ''; setFilter('active'); $('typeFilter').value = ''; $('categoryFilter').value = ''; $('taskSearch').value = '';
    toast('PDF 识别任务已加入队列'); lastPayload = ''; await load(); render();
  } catch (error) { notice('pdfError', error.message); }
  finally { $('submitPdf').disabled = false; }
});
document.addEventListener('visibilitychange', () => { clearTimeout(timer); if (!document.hidden) load(); });
window.addEventListener('online', load);
load();
if (new URLSearchParams(location.search).get('new') === 'pdf') {
  $('pdfDialog').showModal(); history.replaceState(null, '', '/tasks');
}
