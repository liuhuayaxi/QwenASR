import {groupTasks, attentionStatuses} from './workbench/continuation.js?v=20261011';
import {aiTaskPayload} from './task-config.js?v=20260913-tasks-1';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const active = new Set(['running', 'queued', 'waiting_retry', 'stopping']);
const attention = attentionStatuses;
const labels = {running:'进行中', queued:'排队中', waiting_retry:'等待自动重试', stopping:'正在停止', paused:'已暂停', failed:'失败', error:'失败', interrupted:'已中断', pending:'待处理', stale:'原件已更新', completed_with_errors:'部分失败', completed:'已完成', cancelled:'已取消'};
const actionLabels = {continue:'继续', retry:'重试', pause:'暂停', cancel:'取消', download:'仅恢复下载', new_recognition:'新建识别', new_local:'新建本地任务'};
const filterLabels = {daily:'需要继续', all:'全部任务', active:'进行中', attention:'待处理', paused:'已暂停', completed:'已完成', cancelled:'已取消'};
const state = {tasks: [], counts: {}, history: {}, filter: 'daily', selected: new Set(), busy: false, loading: false, ready: false};
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
  return state.tasks.filter(task => (state.filter === 'all' || (state.filter === 'daily' ? active.has(task.status) || attention.has(task.status) : state.filter === 'active' ? active.has(task.status) : state.filter === 'attention' ? attention.has(task.status) : task.status === state.filter))
    && (!type || task.type === type || task.family === type) && (!category || (category === '__local__' ? !task.category : task.category === category || task.type === 'course_queue' && !task.category))
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
    provider_auth: '模型服务认证失败，请检查该功能已保存的接口和密钥。',
    provider_region: 'AGY/Gemini 当前账号或地区不可用，请在 AGY CLI 中确认登录状态与地区。',
    agy_cli_unavailable: '本机没有可用的 agy CLI，请检查安装路径。',
    agy_cli_config: 'AGY CLI 配置不完整，请选择模型后保存设置。',
    invalid_json: '模型返回的 JSON 结构不完整，可继续重新生成。',
    invalid_segment_mapping: '模型返回的段落编号不可靠，可继续重新处理。',
  };
  return messages[task.error_kind] || task.error || '';
}
function operationLabel(inference = {}, receipt = {}, mode) {
  return [({local:'配置：本机', remote_preferred:'配置：优先 Windows', remote_only:'配置：仅 Windows'}[mode] || ''),
    inference.provider ? `实际：${inference.provider === 'remote' ? 'Windows' : 'Mac'}` : '实际来源待确认',
    inference.fallback ? `已回退：${inference.fallback_reason || '远端确认未接受'}` : '',
    (inference.job_id || receipt.job_id) ? `Job ID：${inference.job_id || receipt.job_id}` : '',
    receipt.state ? `操作阶段：${receipt.state}` : ''].filter(Boolean).join(' · ');
}
function card(task) {
  const done = task.status === 'completed';
  const actions = done ? [] : (task.actions || []);
  const resultClass = done ? 'task-result-link' : 'ui-button';
  const progress = task.status === 'completed' ? 100 : Object.hasOwn(task, 'progress') ? task.progress : (task.total ? Math.round((task.completed || 0) / task.total * 100) : null);
  const bounded = progress === null ? null : Math.max(0, Math.min(100, Number(progress) || 0));
  const count = task.total ? `${task.completed || 0}/${task.total}${task.family === 'mineru' ? ' 个文件' : ' 段'}` : '';
  const wait = task.status === 'waiting_retry' && task.retry_in_seconds ? ` · 约 ${task.retry_in_seconds} 秒后自动重试` : '';
  const stage = (task.stage || (task.status === 'pending' ? '等待手动开始处理' : labels[task.status])) + wait;
  const error = friendlyError(task);
  const inference = task.inference || {}; // Mixed batches report each file separately.
  const inferMetrics = inference.metrics || {};
  const mode = task.configured_provider;
  const providerName = inference.provider ? (inference.provider === 'remote' ? 'Windows 远程' : 'Mac 本机')
    : ({local: '配置为 Mac 本机', remote_preferred: '配置为优先 Windows', remote_only: '配置为仅 Windows'}[mode] || '');
  const tier = inferMetrics.tier || task.mineru_settings?.tier;
  const recognition = task.family === 'mineru' ? [providerName,
    inferMetrics.mineru_version ? `MinerU ${inferMetrics.mineru_version}` : '',
    tier ? `${tier === 'advanced' ? 'Advanced' : tier} 档位` : '', inference.fallback ? '已回退本机' : ''].filter(Boolean).join(' · ') : task.family === 'asr' ? [providerName || '来源未验证', inferMetrics.model || inference.model || '', inferMetrics.dtype || inference.dtype || '', inferMetrics.device || inference.device || '', inference.fallback ? '已回退本机' : '', inferMetrics.gpu_release_state === 'verified' ? '显存释放已验证' : '资源遥测未验证'].filter(Boolean).join(' · ') : '';
  const aiProvider = task.configured_ai_model ? `AI 通道：${task.configured_ai_transport === 'agy_cli' ? 'AGY CLI' : 'OpenAI 兼容 API'} · ${task.configured_ai_model}${task.configured_ai_effort ? ` · ${task.configured_ai_effort} 思考` : ''}` : '';
  const details = [operationLabel(inference, {job_id: task.remote_job_id, state: task.remote_operation_state}, mode), aiProvider, recognition ? `识别环境：${recognition}` : '', task.error && task.error !== error ? task.error : '',
    task.range_end ? `片段范围：${Math.floor(task.range_start / 60)}–${Math.ceil(task.range_end / 60)} 分钟` : '',
    task.attempts > 1 ? `历史尝试 ${task.attempts} 次` : '', ...(task.warnings || [])].filter(Boolean);
  const files = (task.files || []).map(file => `<li class="task-file"><div><b>${esc(file.source_name)}</b><span>${esc(labels[file.status] || file.status)}${file.status === 'running' && file.progress != null ? ` · ${Math.round(file.progress || 0)}%` : ''}</span></div><p>${esc(file.error || file.stage)}</p><p>${esc(operationLabel(file.inference, file.remote_operation, task.configured_provider))}</p><p>${esc(file.output_path || file.source_path)}</p>${file.download_url ? `<a href="${esc(file.download_url)}" download>下载 AI 友好 PDF</a>` : ''}</li>`).join('');
  return `<article class="task-card" data-id="${esc(task.id)}" data-status="${esc(task.status)}"><div class="task-card-top">
    ${done ? '' : `<input class="task-select" type="checkbox" aria-label="选择 ${esc(task.label)}：${esc(task.title)}" ${state.selected.has(task.id) ? 'checked' : ''} ${state.busy ? 'disabled' : ''}>`}
    <div class="task-main"><div class="task-title"><h3>${esc(task.title)}</h3><span class="task-status">${esc(labels[task.status] || task.status)}</span></div>
    <div class="task-meta"><span class="task-type">${esc(task.label)}</span><span>${esc(task.category || task.source)}</span>${recognition ? `<span>${esc(recognition)}</span>` : ''}${dateLabel(task.updated_at) ? `<span>更新于 ${esc(dateLabel(task.updated_at))}</span>` : ''}</div>
    <div class="task-stage"><span>${esc(stage)}</span><span>${esc(count)}${bounded !== null ? `${count ? ' · ' : ''}${bounded}%` : ''}</span></div>
    ${bounded !== null || active.has(task.status) ? `<progress class="task-progress" max="100" ${bounded !== null ? `value="${bounded}"` : ''} aria-label="${esc(task.label)}进度"></progress>` : ''}
    ${error ? `<div class="task-error">${esc(error)}</div>` : ''}
    ${actions.length || task.result_url || task.download_url ? `<div class="task-actions">${actions.map(action => `<button data-action="${action}" class="ui-button ${action === 'cancel' ? 'danger' : action === 'continue' ? 'primary' : ''}" ${state.busy ? 'disabled' : ''}>${actionLabels[action]}</button>`).join('')}
    ${task.result_url ? `<a href="${esc(task.result_url)}" class="${resultClass}">${task.type === 'course_queue' ? '打开课程回放' : task.family === 'ai' || task.type === 'upload' ? '查看笔记' : '查看课程文件'}</a>` : ''}${task.download_url ? `<a href="${esc(task.download_url)}" class="${resultClass}" download>下载 AI 友好 PDF</a>` : ''}</div>` : ''}
    ${files || details.length ? `<details class="task-details"><summary>${files ? `文件明细（${task.files.length}）` : '任务详情'}</summary>${details.map(text => `<p>${esc(text)}</p>`).join('')}${files ? `<ul>${files}</ul>` : ''}</details>` : ''}
    </div></div></article>`;
}
function selectionControls() {
  const visible = filteredTasks();
  const selectable = visible.filter(task => (task.actions || []).length);
  const selected = selectable.filter(task => state.selected.has(task.id));
  document.querySelectorAll('[data-bulk]').forEach(button => button.hidden = !selected.length);
  $('selectionCount').textContent = selected.length ? `已选 ${selected.length} 项` : '未选择任务';
  $('selectAll').checked = selectable.length > 0 && selected.length === selectable.length;
  $('selectAll').indeterminate = selected.length > 0 && selected.length < selectable.length;
  $('selectAll').disabled = !selectable.length || state.busy;
  document.querySelectorAll('[data-bulk]').forEach(button => {
    const count = selected.filter(task => task.actions.includes(button.dataset.bulk)).length;
    button.disabled = state.busy || !count;
    button.textContent = `${actionLabels[button.dataset.bulk]}${count ? ` (${count})` : ''}`;
  });
  const completed = state.tasks.filter(task => task.status === 'completed');
  $('clearCompleted').disabled = state.busy || !completed.length;
  $('restoreCompleted').hidden = state.busy || !state.history.can_restore;
  $('restoreCompleted').disabled = state.busy;
  $('retryFailed').disabled = state.busy || !state.tasks.some(task => attention.has(task.status) && (task.actions || []).some(action => ['retry', 'continue'].includes(action)));
}
function render() {
  const opened = new Set([...$('taskList').querySelectorAll('details[open]')].map(node => node.closest('[data-id]').dataset.id));
  const focused = document.activeElement;
  const focusedId = focused?.closest('#taskList [data-id]')?.dataset.id;
  const focusSelector = focused?.matches('.task-select') ? '.task-select' : focused?.matches('summary') ? 'summary' : focused?.dataset.action ? `[data-action="${focused.dataset.action}"]` : null;
  const tasks = filteredTasks();
  $('listTitle').textContent = `${filterLabels[state.filter]} · ${tasks.length}`;
  $('taskList').innerHTML = tasks.length ? groupTasks(tasks).map(group => `<section class="task-group"><h3>${esc(group.title)}${group.category && group.category !== group.title ? ` · ${esc(group.category)}` : ''}</h3>${group.tasks.map(card).join('')}</section>`).join('') : `<div class="task-empty"><b>${state.tasks.length ? '没有符合条件的任务' : '目前没有任务'}</b>${state.tasks.length ? '可以切换状态、类型或课程，或清空搜索。' : '在工作台生成 AI 笔记，或添加 PDF 识别任务，进度会显示在这里。'}</div>`;
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
    state.tasks = data.tasks || []; state.counts = data.counts || {}; state.history = data.history || {}; state.ready = true;
    state.selected = new Set([...state.selected].filter(id => state.tasks.some(task => task.id === id)));
    const categories = [...new Set([...state.tasks.map(task => task.category), new URLSearchParams(location.search).get('category')].filter(Boolean))].sort((a,b) => a.localeCompare(b,'zh-CN'));
    const categoryValue = state.ready && lastPayload ? $('categoryFilter').value : new URLSearchParams(location.search).get('category') || '';
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
  const newOperation = ['new_local', 'new_recognition'].includes(action);
  if (newOperation && !confirm('旧远端任务可能仍在运行。新任务可能重复计算，旧收据与结果会保留。确认创建新任务？')) return;
  if (state.busy || !targets.length) return;
  state.busy = true; render(); notice('actionResult', '');
  try {
    // The backend rebuilds AI settings from its local durable store. Do not
    // fetch or pass browser-local provider values during task actions.
    const items = targets.map(task => ({...aiTaskPayload(task, action, {}, {}), ...(newOperation ? {acknowledge_duplicate:true} : {})}));
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
$('selectAll').addEventListener('change', () => { state.selected = new Set($('selectAll').checked ? filteredTasks().filter(task => (task.actions || []).length).map(task => task.id) : []); render(); });
$('taskList').addEventListener('change', event => { if (!event.target.matches('.task-select')) return; const id = event.target.closest('[data-id]').dataset.id; if (event.target.checked) state.selected.add(id); else state.selected.delete(id); selectionControls(); });
$('taskList').addEventListener('click', event => { const button = event.target.closest('[data-action]'); if (!button) return; const task = state.tasks.find(task => task.id === button.closest('[data-id]').dataset.id); if (task) perform([task], button.dataset.action); });
document.querySelectorAll('[data-bulk]').forEach(button => button.addEventListener('click', () => {
  const targets = filteredTasks().filter(task => state.selected.has(task.id) && task.actions.includes(button.dataset.bulk));
  if (targets.length > 200) { toast('每次最多操作 200 项任务，请减少选择'); return; }
  perform(targets, button.dataset.bulk, true);
}));
$('refreshTasks').addEventListener('click', load);
$('retryFailed').addEventListener('click', async () => {
  if (state.busy) return;
  state.busy = true; render(); notice('actionResult', '');
  try {
    const result = await api('/api/tasks/retry-failed', {});
    const failures = (result.results || []).filter(item => !item.ok).map(item => `${item.id}：${item.error}`);
    notice('actionResult', `已提交失败任务重试 ${result.accepted}/${result.requested} 项${failures.length ? '\n' + failures.join('\n') : ''}`);
    state.selected.clear(); lastPayload = '';
  } catch (error) { notice('actionResult', error.message); }
  finally { state.busy = false; await load(); render(); }
});
$('clearCompleted').addEventListener('click', async () => {
  const completed = state.tasks.filter(task => task.status === 'completed');
  if (!completed.length || state.busy) return;
  state.busy = true; render(); notice('actionResult', '');
  try {
    const result = await api('/api/tasks/clear-completed', {items: completed.map(task => ({id: task.id, generation: task.history_generation}))});
    toast(`已清理 ${result.cleared} 条已完成任务记录，原文件和结果仍保留`);
    lastPayload = '';
  } catch (error) { notice('actionResult', error.message); }
  finally { state.busy = false; await load(); render(); }
});
$('restoreCompleted').addEventListener('click', async () => {
  if (state.busy || !state.history.can_restore) return;
  state.busy = true; render(); notice('actionResult', '');
  try {
    const result = await api('/api/tasks/restore-completed', {});
    toast(`已恢复 ${result.restored} 条刚清理的任务记录`);
    lastPayload = '';
  } catch (error) { notice('actionResult', error.message); }
  finally { state.busy = false; await load(); render(); }
});
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
