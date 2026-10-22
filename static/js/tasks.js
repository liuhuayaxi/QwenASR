import {taskGroups, filterTaskGroups, groupCounts, actionableTasks, completedTasks, taskMatchesType, activeStatuses, errorStatuses, leadingTask, taskProgressView, progressGroups} from './task-center-view.js?v=20261018-recovery-1';
import {createTaskPdfDialog} from './task-pdf-dialog.js?v=20261018-recovery-1';
import {aiTaskPayload} from './task-config.js?v=20260913-tasks-1';
import {mergeLiveCourseProgress} from './course-progress.js?v=20261018-recovery-1';
import {mineruEstimateLabel, legacyMineruProgress} from './mineru-progress-view.js?v=20261013-time-2';
import {resourceStatus} from './resource-status.js?v=20261013-1';
import {aiActivityView} from './ai-activity-view.js?v=20261019-live-model-activity';
import {asrResourceView} from './asr-resource-view.js?v=20261022-local-main-1';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const active = activeStatuses;
const attention = errorStatuses;
const labels = {running:'进行中', queued:'排队中', waiting_retry:'等待自动重试', stopping:'正在停止', paused:'已暂停', failed:'失败', error:'失败', interrupted:'已中断', pending:'待处理', stale:'原件已更新', completed_with_errors:'部分失败', completed:'已完成', cancelled:'已取消', recovered:'已恢复', active:'进行中', attention:'需要处理'};
const actionLabels = {continue:'继续', retry:'重试', regenerate:'重新生成', pause:'暂停', cancel:'取消', download:'仅恢复下载', new_recognition:'新建识别', new_local:'新建本地任务'};
const filterLabels = {daily:'当前进展', all:'全部课堂与资料', active:'进行中', attention:'需要处理', review:'待核对', pending:'尚未开始', paused:'已暂停', completed:'已完成', cancelled:'已取消', recovered:'有恢复记录'};
const state = {tasks: [], groups: [], counts: {}, history: {}, filter: 'daily', selected: new Set(), busy: false, loading: false, ready: false, reloadRequested: false, revision: 0, detailState: new Map()};
let timer, toastTimer, lastPayload = '', loadFailed = false;
let asrResources = {}, resourceRequest = null, lastResourceCheck = 0;
const asrResourceText = view => view.text.replace(/\b(CPU|GPU) /g, '$1\u00a0');

function applyAsrResources() {
  for (const node of $('taskList').querySelectorAll('[data-asr-resource]')) {
    const phase = {state: 'running', execution: {provider: node.dataset.asrProvider,
      fallback: node.dataset.asrFallback === 'true', updated_at: Number(node.dataset.asrUpdated || 0)}};
    const view = asrResourceView(phase, asrResources[node.dataset.asrResource]);
    node.textContent = asrResourceText(view); node.title = view.title;
  }
}
function refreshAsrResources() {
  const nodes = [...$('taskList').querySelectorAll('[data-asr-resource]')];
  if (document.hidden || resourceRequest || !nodes.length || Date.now() - lastResourceCheck < 5000) return;
  lastResourceCheck = Date.now();
  resourceRequest = (async () => {
    try {
      const response = await fetch('/course/api/resources', {cache: 'no-store', signal: AbortSignal.timeout(4500)});
      if (!response.ok) throw Error('资源状态暂不可用');
      const data = await response.json();
      asrResources = data.lessons || {};
    } catch { asrResources = {}; }
    if (document.hidden) return;
    applyAsrResources();
  })().finally(() => { resourceRequest = null; });
}

function notice(id, message) { $(id).textContent = message; $(id).hidden = !message; }
function toast(message) { notice('toast', message); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 5000); }
async function api(path, body) {
  const response = await fetch(path, {cache: 'no-store', signal: AbortSignal.timeout(30000), ...(body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-QwenASR-Client': 'task-center'}, body: JSON.stringify(body),
  })});
  let data;
  try { data = await response.json(); } catch { throw new Error('任务服务响应不完整，正在尝试重新连接'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}）`);
  return data;
}
function filteredGroups(filter = state.filter) {
  const type = $('typeFilter').value;
  let groups = filterTaskGroups(state.groups, {filter:'all', type,
    category:$('categoryFilter').value, query:$('taskSearch').value});
  if (type) groups = taskGroups({groups:groups.map(group => ({...group,
    tasks:group.tasks.filter(task => taskMatchesType(task, type)),
    current_tasks:group.current_tasks.filter(task => taskMatchesType(task, type)),
    history:group.history.filter(task => taskMatchesType(task, type)),
    context_tasks:(group.context_tasks || []).filter(task => taskMatchesType(task, type)),
    status:undefined, errors:undefined, current_stage:'', review_count:undefined, recovered_count:undefined,
  }))});
  return filterTaskGroups(groups, {filter});
}
function filteredTasks() { return actionableTasks(filteredGroups(), $('typeFilter').value); }
function filteredCompleted() { return completedTasks(filteredGroups(), $('typeFilter').value); }
function failedTargets() {
  return filteredTasks().filter(task => !task.is_context && attention.has(task.status) &&
    (task.actions || []).some(action => ['retry', 'continue'].includes(action)));
}
function dateLabel(value) {
  if (!value) return '';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'});
}
function friendlyError(task) {
  if (!task.error) return '';
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
    material_tool_violation: 'AGY 使用了本次请求未允许的工具，本次结果未采用；已完成检查点保留，可从此处继续。',
    service_restart: '服务曾退出，已完成检查点保留；继续后会复用已保存内容。',
    invalid_json: '模型返回的 JSON 结构不完整，可继续重新生成。',
    invalid_segment_mapping: '模型返回的段落编号不可靠，可继续重新处理。',
  };
  if (messages[task.error_kind]) return messages[task.error_kind];
  const raw = String(task.error);
  if (/Broken pipe|Errno 32/i.test(raw)) return '与 AI 通道的连接中断，可继续处理；已有内容已保留。';
  if (/EMBEDDING_INPUT_TOO_LONG|maximum.*tokens|input.*too.*long/i.test(raw)) return '索引片段超过模型长度限制，需要拆分后继续。';
  if (/timeout|timed out|超时/i.test(raw)) return '等待处理结果超时，可继续检查或重试。';
  if (/connection|connect|network|连接|网络/i.test(raw)) return '处理服务暂时无法连接，可在连接恢复后继续。';
  return raw.length <= 180 && !/Traceback|File "|\\Users\\/.test(raw) ? raw : '本步骤尚未完成，请展开处理详情查看原因并继续。';
}
function operationLabel(inference = {}, receipt = {}, mode, task) {
  if (task?.type === 'knowledge_index') return task.execution_location === 'local' ? '处理位置：Mac 本机' : '';
  // Only inference tasks have ASR/MinerU receipts. A cloud note or local index
  // cannot establish its execution location from an ASR configuration value.
  if (task && !['asr', 'mineru'].includes(task.family)) return '';
  if (task?.type === 'course_queue' && !inference.provider && !receipt.job_id) return '';
  return [({local:'配置：本机', remote_preferred:'配置：优先 Windows', remote_only:'配置：仅 Windows'}[mode] || ''),
    inference.provider ? `实际：${inference.provider === 'remote' ? 'Windows' : 'Mac'}` : '实际来源待确认',
    inference.fallback ? `已回退：${inference.fallback_reason || '远端确认未接受'}` : '',
    (inference.job_id || receipt.job_id) ? `Job ID：${inference.job_id || receipt.job_id}` : '',
    receipt.state ? `操作阶段：${receipt.state}` : ''].filter(Boolean).join(' · ');
}
function courseQueueDetails(task) {
  const items = task.course_item ? [task.course_item] : task.course_items || [];
  if (!items.length) return '';
  return `<details class="task-details course-queue-details" data-detail-key="course-progress" open><summary>${task.course_item ? '本课回放进度' : `未完成课次（${items.length}）`}</summary><div class="course-queue-items">${items.map(item => {
    const phases = (item.phases || []).map(phase => {
      const percent = Math.max(0, Math.min(100, Number(phase.percent) || 0));
      const phaseState = phase.state === 'completed' ? 'completed' : phase.state === 'running' ? 'running' : 'pending';
      const execution = phase.key === 'asr' ? asrResourceView({...phase, state: item.state === 'running' ? phase.state : 'paused'}, asrResources[item.id]) : null;
      const monitor = phase.key === 'asr' && phase.state === 'running' && item.state === 'running';
      return `<div class="course-phase" data-state="${esc(phaseState)}"><div class="course-phase-head"><span ${execution ? 'class="course-asr-resource"' : ''} ${monitor ? `data-asr-resource="${esc(item.id)}" data-asr-provider="${esc(phase.execution?.provider || '')}" data-asr-fallback="${!!phase.execution?.fallback}" data-asr-updated="${esc(phase.execution?.updated_at || 0)}"` : ''} title="${esc(execution?.title || '')}">${esc(execution ? asrResourceText(execution) : phase.label)}</span><b>${percent.toFixed(percent % 1 ? 1 : 0)}%</b></div><progress max="100" value="${percent}"></progress><small>${esc(phase.detail || (phase.state === 'completed' ? '已完成' : '等待处理'))}</small></div>`;
    }).join('');
    const window = item.start && item.stop ? ` · ${item.start}–${item.stop}` : '';
    return `<article class="course-queue-item"><div class="course-queue-item-head"><b>${esc(item.title)}</b><span>${esc(item.state === 'running' ? '进行中' : item.state === 'paused' ? '已暂停' : '排队中')}</span></div><p>${esc(item.course || '')}${esc(window)}</p><div class="course-phases">${phases || `<small>${esc(item.message || '等待课程回放处理')}</small>`}</div></article>`;
  }).join('')}</div></details>`;
}
function taskStatus(task) { return task.recovered || task.display_status === 'recovered' ? '已恢复' : task.attention_kind === 'auto_review' ? '等待 AI 复核' : task.status === 'pending' && (task.attention_kind === 'review' || Number(task.review_required_blocks) > 0) ? '待核对' : labels[task.status] || task.status; }
function taskActionLabel(task, action) { return task.type === 'course_queue' && ['continue', 'retry', 'pause', 'cancel'].includes(action) ? `${actionLabels[action]}课程队列` : actionLabels[action]; }
function isReviewTask(task) { return task.status === 'pending' && task.attention_kind === 'review'; }
function defaultBulkTasks(tasks) { return tasks.filter(task => !isReviewTask(task) && !task.is_context); }
function card(task, historical = false) {
  const recovered = !!task.recovered || task.display_status === 'recovered';
  const done = historical || task.status === 'completed' || recovered;
  const actions = done ? [] : (task.actions || []);
  const review = !done && isReviewTask(task);
  const activity = aiActivityView(task);
  const phaseProgress = taskProgressView(task);
  const resultClass = done ? 'task-result-link' : 'ui-button';
  const legacyProgress = legacyMineruProgress(task);
  const progress = task.status === 'completed' ? 100 : legacyProgress !== null ? legacyProgress : Object.hasOwn(task, 'progress') ? task.progress : (task.total ? Math.round((task.completed || 0) / task.total * 100) : null);
  const bounded = review ? null : task.type === 'summary' ? phaseProgress.percent : progress === null ? null : Math.max(0, Math.min(done ? 100 : 99, Number(progress) || 0));
  const estimate = task.family === 'mineru' ? task.progress_estimate : null;
  const estimateLabel = mineruEstimateLabel(estimate, task.status) || (legacyProgress !== null ? '按已完成页段显示 · 整体耗时预估尚未就绪' : task.family === 'mineru' && task.status === 'running' ? '正在计算整体进度' : '');
  const resource = resourceStatus(task);
  const reviewCount = Number(task.review_required_blocks) || Number(task.total) || 0;
  const count = (task.type === 'summary' || task.type === 'course_queue' || task.type === 'refinement') && !done ? phaseProgress.count : review ? (reviewCount ? `${reviewCount} 段待核对` : '等待人工核对') : task.total ? `${task.family === 'ai' ? '已完成 ' : ''}${task.completed || 0}/${task.total}${task.type === 'course_queue' ? ' 堂课' : task.family === 'mineru' ? ' 个文件' : task.family === 'ai' ? ` ${task.progress_unit || '批'}` : ' 段'}` : '';
  const wait = task.status === 'waiting_retry' && task.retry_in_seconds ? ` · 约 ${task.retry_in_seconds} 秒后自动重试` : '';
  const stage = (activity ? [task.stage, activity.label].filter(Boolean).join(' · ')
    : task.stage || (task.status === 'pending' ? '等待手动开始处理' : labels[task.status])) + wait;
  const error = historical ? '' : friendlyError(task);
  const inference = task.inference || {}; // Mixed batches report each file separately.
  const inferMetrics = inference.metrics || {};
  const mode = task.configured_provider;
  const providerName = inference.provider ? (inference.provider === 'remote' ? 'Windows 远程' : 'Mac 本机')
    : ({local: '配置为 Mac 本机', remote_preferred: '配置为优先 Windows', remote_only: '配置为仅 Windows'}[mode] || '');
  const tier = inferMetrics.tier || task.mineru_settings?.tier;
  const recognition = task.family === 'mineru' ? [providerName,
    inferMetrics.mineru_version ? `MinerU ${inferMetrics.mineru_version}` : '',
    tier ? `${tier === 'advanced' ? 'Advanced' : tier} 档位` : '', inference.fallback ? '已回退本机' : ''].filter(Boolean).join(' · ') : task.family === 'asr' && task.type !== 'course_queue' ? [providerName, inferMetrics.model || inference.model || '', inferMetrics.dtype || inference.dtype || '', inferMetrics.device || inference.device || '', inference.fallback ? '已回退本机' : ''].filter(Boolean).join(' · ') : '';
  const aiProvider = task.configured_ai_model ? `AI 通道：${task.configured_ai_transport === 'agy_cli' ? 'AGY CLI' : 'OpenAI 兼容 API'} · ${task.configured_ai_model}${task.configured_ai_effort ? ` · ${task.configured_ai_effort} 思考` : ''}` : '';
  const details = [operationLabel(inference, {job_id: task.remote_job_id, state: task.remote_operation_state}, mode, task), aiProvider, recognition ? `识别环境：${recognition}` : '', resource?.detail, task.error && task.error !== error ? task.error : '',
    task.previous_error ? `上次处理失败：${task.previous_error}` : '',
    recovered ? `恢复说明：${task.recovery_reason || '后续处理已完成，旧错误保留供核对。'}` : '',
    task.range_end ? `片段范围：${Math.floor(task.range_start / 60)}–${Math.ceil(task.range_end / 60)} 分钟` : '',
    task.attempts > 1 ? `历史尝试 ${task.attempts} 次` : '', ...(task.warnings || [])].filter(Boolean);
  const files = (task.files || []).map(file => `<li class="task-file"><div><b>${esc(file.source_name)}</b><span>${esc(labels[file.status] || file.status)}${file.status === 'running' && file.progress != null ? ` · ${Math.round(file.progress || 0)}%` : ''}</span></div><p>${esc(file.error || file.stage)}</p><p>${esc(operationLabel(file.inference, file.remote_operation, task.configured_provider, task))}</p><p>${esc(file.output_path || file.source_path)}</p>${file.download_url ? `<a href="${esc(file.download_url)}" download>下载 AI 友好 PDF</a>` : ''}</li>`).join('');
  const courseDetails = task.type === 'course_queue' ? courseQueueDetails(task) : '';
  const select = `<input class="task-select" type="checkbox" aria-label="选择${review ? '重新处理' : ''} ${esc(task.label)}：${esc(task.title)}" ${state.selected.has(task.id) ? 'checked' : ''} ${state.busy ? 'disabled' : ''}>`;
  const buttons = actions.map(action => `<button data-action="${action}" class="ui-button ${action === 'cancel' ? 'danger' : !review && action === 'continue' ? 'primary' : ''}" ${state.busy ? 'disabled' : ''}>${review ? action === 'cancel' ? '取消重新处理任务' : `重新处理（${actionLabels[action]}）` : taskActionLabel(task, action)}</button>`).join('');
  return `<article class="task-card" data-id="${esc(task.id)}" data-status="${esc(recovered ? 'recovered' : task.status)}"><div class="task-card-top">
    ${actions.length && !review ? select : ''}
    <div class="task-main"><div class="task-title"><h3>${esc(task.label || task.title)}</h3><span class="task-status">${esc(taskStatus(task))}</span></div>
    <div class="task-meta"><span>${esc(task.source || task.category)}</span>${recognition ? `<span>${esc(recognition)}</span>` : ''}${resource ? `<span class="task-resource ${resource.tone === 'error' ? 'error' : ''}" title="${esc(resource.detail)}">${esc(resource.text)}</span>` : ''}${dateLabel(task.updated_at) ? `<span>更新于 ${esc(dateLabel(task.updated_at))}</span>` : ''}</div>
    <div class="task-stage"><span>${esc(stage)}</span><span>${esc(count)}${bounded !== null ? `${count ? ' · ' : ''}${estimate?.estimated && !done ? '整体预估 ' : legacyProgress !== null ? '已完成页段 ' : ''}${bounded}%` : ''}</span></div>
    ${bounded !== null ? `<progress class="task-progress" max="100" value="${bounded}" aria-label="${esc(task.label)}${estimate?.estimated && !done ? '整体预估' : legacyProgress !== null ? '已完成页段' : ''}进度"></progress>` : active.has(task.status) ? `<div class="task-progress-indeterminate" role="progressbar" aria-label="${esc(task.label)}进度待计算"></div>` : ''}
    ${activity ? `<p class="task-ai-activity" role="status">${esc(activity.detail)}</p>` : ''}
    ${estimateLabel ? `<p class="task-estimate">${esc(estimateLabel)}</p>` : ''}
    ${error ? `<div class="task-error">${esc(error)}</div>` : ''}
    ${!review && actions.length || task.result_url || task.download_url ? `${task.type === 'course_queue' && actions.length ? '<p class="task-queue-scope">队列操作会影响全部课次。</p>' : ''}<div class="task-actions">${review ? '' : buttons}
    ${task.result_url ? `<a href="${esc(task.result_url)}" class="${resultClass}${review ? ' primary' : ''}">${review ? '查看并核对' : task.type === 'course_queue' ? '打开课程回放' : task.family === 'ai' || task.type === 'upload' ? '查看笔记' : '查看课程文件'}</a>` : ''}${task.download_url ? `<a href="${esc(task.download_url)}" class="${resultClass}" download>下载 AI 友好 PDF</a>` : ''}</div>` : ''}
    ${review && actions.length ? `<details class="task-details task-reprocess" data-detail-key="reprocess"><summary>重新处理选项</summary><p>重新调用 AI 处理这些片段会使用当前已保存的配置，不会标记为已人工核对。</p><label>${select} 选择此步骤参与批量重新处理</label><div class="task-actions">${buttons}</div></details>` : ''}
    ${courseDetails || files || details.length ? `${courseDetails || `<details class="task-details" data-detail-key="technical"><summary>${files ? `文件明细（${task.files.length}）` : '处理详情'}</summary>${details.map(text => `<p>${esc(text)}</p>`).join('')}${files ? `<ul>${files}</ul>` : ''}</details>`}` : ''}
    </div></div></article>`;
}
function contextQueueCard(task) {
  return `<article class="task-card task-context-card" data-id="${esc(task.id)}">${task.actions?.length && taskMatchesType(task, $('typeFilter').value) ? `<p class="task-queue-scope">队列操作会影响全部课次。</p><label><input class="task-select" type="checkbox" aria-label="选择整个课程队列" ${state.selected.has(task.id) ? 'checked' : ''} ${state.busy ? 'disabled' : ''}> 选择整个课程队列参与批量处理</label><div class="task-actions">${task.actions.map(action => `<button data-action="${action}" class="ui-button ${action === 'cancel' ? 'danger' : ''}" ${state.busy ? 'disabled' : ''}>${taskActionLabel(task, action)}</button>`).join('')}</div>` : ''}${courseQueueDetails(task)}</article>`;
}
function groupCard(group) {

  const current=group.current_tasks, history=group.history, task=leadingTask(group);
  const running=current.some(t=>active.has(t.status));
  const review=!running && task.attention_kind!=='auto_review' && (task.attention_kind==='review' || (task.status==='pending' && task.review_required_blocks));
  const view=taskProgressView(task), activity=aiActivityView(task);
  const tone=running?'active':group.errors.length?'attention':review?'review':'quiet';
  const status=running?'处理中':review?'待核对':labels[group.status] || group.status;
  const primary=task.actions?.includes('pause')?'pause':task.actions?.includes('continue')?'continue':task.actions?.includes('retry')?'retry':null;
  const elapsed=task.ai_activity?.elapsed_seconds;
  const time=elapsed!=null?`已用时 ${Math.floor(elapsed/60)} 分 ${Math.floor(elapsed%60)} 秒`:'';
  const count=view.count || (view.steps?`步骤 ${view.index+1}/${view.steps.length}`:'');
  const phaseRail=view.steps?`<ol class="task-phase-rail" aria-label="笔记生成步骤">${view.steps.map((label,i)=>`<li data-state="${i<view.index?'done':i===view.index?'current':'next'}" ${i===view.index?'aria-current="step"':''}><i>${i<view.index?'✓':i+1}</i><span>${esc(label)}</span></li>`).join('')}</ol>`:'';
  const meter=view.percent!==null?`<progress class="task-progress focus-progress" value="${view.percent}" max="100" aria-label="${esc(task.label || '任务')}本阶段进度"></progress>`:'';
  const error=group.errors.length?`<p class="group-alert">${esc(friendlyError(current.find(t=>t.error) || {})) || `${group.errors.length} 个步骤需要处理，展开详情查看`}</p>`:'';
  return `<section class="task-group task-group-card" data-group-id="${esc(group.id)}" data-group-status="${esc(group.status)}" data-tone="${tone}">
    <header class="task-group-head"><div><p class="task-group-course">${esc(group.category || '本地资料')}</p><h3>${esc(group.title)}</h3></div><span class="group-status" data-tone="${tone}">${running?'<i class="live-dot"></i>':''}${esc(status)}</span></header>
    <div class="group-focus" data-id="${esc(task.id || '')}"><div class="focus-heading"><div><span class="focus-kind">${esc(task.label || '')}</span><p class="focus-stage">${esc(view.label)}</p></div><b class="focus-count">${esc(count)}</b></div>
    ${phaseRail}${meter}
    ${view.detail?`<p class="focus-note">${esc(view.detail)}</p>`:''}
    ${activity?.detail?`<p class="focus-note focus-live-detail" role="status">${esc(activity.detail)}</p>`:''}
    <div class="focus-footer"><p class="focus-activity">${activity?`<i class="live-dot"></i>${esc(activity.label)}`:group.status==='paused'?'进度已保存，可随时继续':group.status==='completed'?'结果已保存':''}${time?`<span>${esc(time)}</span>`:''}</p><div class="focus-actions">
    ${primary && !review?`<button data-action="${primary}" class="ui-button ${primary==='continue'?'primary':''}" ${state.busy?'disabled':''}>${esc(actionLabels[primary])}</button>`:''}
    ${group.result_url?`<a class="ui-button ${review?'primary':''}" href="${esc(group.result_url)}">${review?'去核对':group.recording_id?'查看笔记':'查看结果'}</a>`:''}</div></div></div>
    ${error}${running && group.review_count?`<p class="group-secondary">另有 ${group.review_count} 个校对步骤待核对，不影响当前处理</p>`:''}
    <details class="group-details" data-detail-key="steps"><summary>任务详情<span>${current.length} 个当前步骤${history.length?` · ${history.length} 条历史`:''}</span></summary>
      ${current.map(t=>card(t)).join('')}
      ${group.context_tasks?.length?`<details class="task-details group-context" data-detail-key="queue-context"><summary>课程队列控制</summary>${group.context_tasks.map(contextQueueCard).join('')}</details>`:''}
      ${history.length?`<details class="group-history" data-detail-key="history"><summary>已完成与历史 · ${history.length} 条记录</summary>${history.map(t=>card(t,true)).join('')}</details>`:''}
    </details></section>`;

}

function groupSection(groups, title, key, collapsed = false) {
  if (!groups.length) return '';
  const content = groups.map(groupCard).join('');
  return collapsed ? `<details class="task-collection" data-detail-key="collection:${key}"><summary>${esc(title)} <span>${groups.length} 项</span></summary><div class="task-list-section">${content}</div></details>` : `<div class="task-list-section">${content}</div>`;
}
function selectionControls() {
  const selectable = filteredTasks();
  const selected = selectable.filter(task => state.selected.has(task.id));
  const defaults = defaultBulkTasks(selectable);
  const defaultSelected = defaults.filter(task => state.selected.has(task.id));
  // A shared course queue can appear under several lessons. Its checkboxes
  // represent the same durable owner and must reflect one choice immediately.
  $('taskList').querySelectorAll('.task-select').forEach(input => {
    input.checked = state.selected.has(input.closest('[data-id]').dataset.id);
    input.disabled = state.busy;
  });
  document.querySelectorAll('[data-bulk]').forEach(button => button.hidden = !selected.length);
  $('selectionCount').textContent = selected.length ? `已选 ${selected.length} 个处理步骤` : '可选择具体步骤批量处理';
  $('selectAll').checked = defaults.length > 0 && defaultSelected.length === defaults.length;
  $('selectAll').indeterminate = defaultSelected.length > 0 && defaultSelected.length < defaults.length;
  $('selectAll').disabled = !defaults.length || state.busy;
  document.querySelectorAll('[data-bulk]').forEach(button => {
    const count = selected.filter(task => task.actions.includes(button.dataset.bulk)).length;
    button.disabled = state.busy || !count;
    button.textContent = `${actionLabels[button.dataset.bulk]}${count ? ` (${count})` : ''}`;
  });
  const completed = filteredCompleted();
  $('clearCompleted').disabled = state.busy || !completed.length;
  $('restoreCompleted').hidden = state.busy || !state.history.can_restore;
  $('restoreCompleted').disabled = state.busy;
  $('retryFailed').disabled = state.busy || !failedTargets().length;
}
function detailKey(node) {
  return `${node.closest('[data-group-id]')?.dataset.groupId || ''}|${node.closest('[data-id]')?.dataset.id || ''}|${node.dataset.detailKey || ''}`;
}
function render() {
  $('taskList').querySelectorAll('details').forEach(node => state.detailState.set(detailKey(node), node.open));
  const focused = document.activeElement;
  const focusedDetail = focused?.matches('summary') ? detailKey(focused.parentElement) : null;
  const focusedGroup = focused?.closest('[data-group-id]')?.dataset.groupId;
  const focusedCard = focused?.closest('#taskList article.task-card');
  const focusedId = focusedCard ? focusedCard.dataset.id : focused?.closest('#taskList [data-id]')?.dataset.id;
  const focusedHref = focused?.matches('a[href]') ? focused.getAttribute('href') : null;
  const focusSelector = focused?.matches('.task-select') ? '.task-select' : focused?.matches('summary') ? 'summary' : focused?.dataset.action ? `[data-action="${focused.dataset.action}"]` : null;
  const groups = progressGroups(filteredGroups());
  $('listTitle').textContent = `${filterLabels[state.filter]} · ${groups.length} 项`;
  let html = '';
  if (state.filter === 'daily' || state.filter === 'all') {
    const current = groups.filter(group => !['pending', 'completed', 'cancelled'].includes(group.status) || group.errors.length || group.review_count);
    const pending = groups.filter(group => group.status === 'pending' && !group.errors.length && !group.review_count);
    const completed = groups.filter(group => ['completed', 'cancelled'].includes(group.status) && !group.errors.length);
    html = groupSection(current, '', 'current') + groupSection(pending, '尚未开始', 'pending', true) + groupSection(completed, '已完成与已取消', 'completed', true);
  } else html = groupSection(groups, '', 'filtered');
  $('taskList').innerHTML = html || `<div class="task-empty"><b>${state.groups.length ? '这里暂时没有需要处理的项目' : '目前没有任务'}</b>${state.groups.length ? '可以切换状态、类型或课程，查看其他课堂与历史记录。' : '课堂转写、复习笔记、资料识别和索引会按课堂或资料汇总到这里。'}</div>`;
  $('taskList').querySelectorAll('details').forEach(node => { if (state.detailState.has(detailKey(node))) node.open = state.detailState.get(detailKey(node)); });
  if (focusedDetail) [...$('taskList').querySelectorAll('details')].find(node => detailKey(node) === focusedDetail)?.querySelector('summary')?.focus({preventScroll:true});
  else if (focusedId && focusSelector) {
    const candidates = [...$('taskList').querySelectorAll('[data-id]')];
    candidates.find(node => node.dataset.id === focusedId && node.closest('[data-group-id]')?.dataset.groupId === focusedGroup && node.querySelector(focusSelector))?.querySelector(focusSelector)?.focus({preventScroll:true});
  }
  if (focusedHref !== null && focusedGroup) {
    const group = [...$('taskList').querySelectorAll('[data-group-id]')].find(node => node.dataset.groupId === focusedGroup);
    const owner = focusedCard ? [...(group?.querySelectorAll('article.task-card[data-id]') || [])].find(node => node.dataset.id === focusedId)
      : focusedId ? [...(group?.querySelectorAll('[data-id]') || [])].find(node => node.dataset.id === focusedId) : group;
    [...(owner?.querySelectorAll('a[href]') || [])].find(node => node.getAttribute('href') === focusedHref)?.focus({preventScroll:true});
  }
  document.querySelectorAll('[data-count]').forEach(node => node.textContent = state.counts[node.dataset.count] || 0);
  document.querySelectorAll('#statusFilters [data-status]').forEach(node => node.setAttribute('aria-current', node.dataset.status === state.filter ? 'page' : 'false'));
  document.querySelectorAll('[data-summary]').forEach(node => node.setAttribute('aria-pressed', String(node.dataset.summary === state.filter)));
  $('taskOverview').textContent = `${state.counts.active || 0} 项正在处理 · ${state.counts.review || 0} 项待核对 · ${state.counts.completed || 0} 项已完成`;
  selectionControls();
}
function applyFilters() { state.selected.clear(); render(); }
function setFilter(filter) { state.filter = filter; applyFilters(); }
async function load() {
  if (state.busy) return;
  if (state.loading) { state.reloadRequested = true; return; }
  clearTimeout(timer); state.loading = true; $('taskList').setAttribute('aria-busy', 'true'); const revision = state.revision, hadReady = state.ready;
  try {
    let data = await api('/api/tasks');
    if (!Array.isArray(data.tasks) && !Array.isArray(data.groups)) throw new Error('任务列表响应不完整');
    loadFailed = false;
    let courseWarning = '';
    if (!Array.isArray(data.groups) && (data.tasks || []).some(task => task.type === 'course_queue')) {
      try {
        const response = await fetch('/course/api/status', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
        if (!response.ok) throw new Error('课程服务暂时不可用');
        data = mergeLiveCourseProgress(data, await response.json());
      } catch {
        courseWarning = '课程回放实时进度暂时不可用，当前显示已保存的进度，正在自动重连。';
      }
    }
    if (revision !== state.revision || state.busy) return;
    state.tasks = data.tasks || []; state.groups = taskGroups(data); state.counts = groupCounts(state.groups); state.history = data.history || {}; state.ready = true;
    const actionable = new Set(actionableTasks(state.groups).map(task => task.id));
    state.selected = new Set([...state.selected].filter(id => actionable.has(id)));
    const categoryValue = hadReady ? $('categoryFilter').value : new URLSearchParams(location.search).get('category') || '';
    const categories = [...new Set([...state.groups.map(group => group.category), categoryValue === '__local__' ? '' : categoryValue].filter(Boolean))].sort((a,b) => a.localeCompare(b,'zh-CN'));
    const categoryOptions = '<option value="">全部课程</option>' + categories.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('') + '<option value="__local__">本地 PDF</option>';
    if ($('categoryFilter').innerHTML !== categoryOptions) $('categoryFilter').innerHTML = categoryOptions;
    if ([...$('categoryFilter').options].some(option => option.value === categoryValue)) $('categoryFilter').value = categoryValue;
    const signature = JSON.stringify(data);
    if (signature !== lastPayload) { render(); lastPayload = signature; }
    refreshAsrResources();
    notice('loadError', ''); notice('sourceWarning', [...(data.warnings || []), courseWarning].filter(Boolean).join('\n'));
    $('lastUpdated').textContent = `已同步 ${new Date().toLocaleTimeString('zh-CN')} · ${state.counts.active ? '每 3 秒自动更新' : '空闲时每分钟校验，返回页面立即更新'}`;
  } catch (error) {
    loadFailed = true;
    if (revision !== state.revision || state.busy) return;
    asrResources = {}; applyAsrResources();
    notice('loadError', `${error.message}。${state.ready ? '当前保留上次读取的任务。' : ''}`);
    if (!state.ready) $('taskList').innerHTML = '<div class="task-empty">暂时无法读取任务，请稍后刷新。</div>';
    $('lastUpdated').textContent = '连接中断，正在自动重连';
  } finally {
    state.loading = false; $('taskList').setAttribute('aria-busy', 'false');
    if (state.reloadRequested && !state.busy) { state.reloadRequested = false; queueMicrotask(load); }
    else if (!document.hidden) timer = setTimeout(load, loadFailed ? 5000 : state.counts.active ? 3000 : 60000);
  }
}
function actionFocus() {
  const element = document.activeElement;
  return {element, group: element?.closest('[data-group-id]')?.dataset.groupId,
    id: element?.closest('[data-id]')?.dataset.id, action: element?.dataset.action};
}
function restoreActionFocus(origin) {
  // Do not take focus from an input the user moved to while IO was pending.
  if (document.activeElement !== document.body) return;
  if (origin.element?.isConnected && !origin.element.disabled && origin.element.getClientRects().length) {
    origin.element.focus({preventScroll:true}); return;
  }
  const group = [...$('taskList').querySelectorAll('[data-group-id]')].find(node => node.dataset.groupId === origin.group);
  const card = [...(group?.querySelectorAll('[data-id]') || [])].find(node => node.dataset.id === origin.id);
  const button = [...(card?.querySelectorAll('[data-action]') || [])].find(node => node.dataset.action === origin.action && !node.disabled);
  const target = button || group?.querySelector('.task-group-head h3') || $('listTitle');
  if (!button) target.tabIndex = -1;
  target.focus({preventScroll:true});
}

async function perform(targets, action, bulk = false) {
  if (state.busy || !targets.length) return;
  if (action === 'cancel' && !confirm(`确认取消 ${targets.length} 个处理步骤？已完成的结果仍保留。${targets.some(task => task.type === 'course_queue') ? '课程队列操作会影响全部课次。' : ''}`)) return;
  const newOperation = ['new_local', 'new_recognition'].includes(action);
  if (newOperation && !confirm('旧远端任务可能仍在运行。新任务可能重复计算，旧收据与结果会保留。确认创建新任务？')) return;
  if (state.busy || !targets.length) return;
  const focus = actionFocus();
  state.busy = true; state.revision += 1; render(); notice('actionResult', '');
  try {
    // The backend rebuilds AI settings from its local durable store. Do not
    // fetch or pass browser-local provider values during task actions.
    const items = targets.map(task => ({...aiTaskPayload(task, action, {}, {}), ...(newOperation ? {acknowledge_duplicate:true} : {})}));
    if (bulk) {
      const result = await api('/api/tasks/bulk-action', {action, items});
      for (const item of result.results || []) if (item.ok) state.selected.delete(item.id);
      const failures = result.results.filter(item => !item.ok).map(item => `${targets.find(task => task.id === item.id)?.title || '任务'}：${item.error}`);
      notice('actionResult', `已提交${actionLabels[action]} ${result.accepted}/${result.requested} 项任务${failures.length ? '\n' + failures.join('\n') : ''}`);
    } else {
      await api('/api/tasks/action', items[0]);
      toast(action === 'pause' || action === 'cancel' ? `已请求${actionLabels[action]}，正在保存进度` : '任务已加入后台队列');
      state.selected.delete(targets[0].id);
    }
  } catch (error) { notice('actionResult', error.message); }
  finally { state.busy = false; lastPayload = ''; await load(); render(); restoreActionFocus(focus); }
}
$('statusFilters').addEventListener('click', event => { const button = event.target.closest('[data-status]'); if (button) setFilter(button.dataset.status); });
document.querySelectorAll('[data-summary]').forEach(button => {if(!button.closest('#statusFilters'))button.addEventListener('click', () => setFilter(button.dataset.summary));});
for (const id of ['typeFilter', 'categoryFilter']) $(id).addEventListener('change', applyFilters);
$('taskSearch').addEventListener('input', applyFilters);
$('selectAll').addEventListener('change', () => { state.selected = new Set($('selectAll').checked ? defaultBulkTasks(filteredTasks()).map(task => task.id) : []); render(); });
$('taskList').addEventListener('change', event => { if (!event.target.matches('.task-select')) return; const id = event.target.closest('[data-id]').dataset.id; if (event.target.checked) state.selected.add(id); else state.selected.delete(id); selectionControls(); });
$('taskList').addEventListener('click', event => { const button = event.target.closest('[data-action]'); if (!button) return; const task = actionableTasks(state.groups).find(task => task.id === button.closest('[data-id]').dataset.id); if (task) perform([task], button.dataset.action); });
document.querySelectorAll('[data-bulk]').forEach(button => button.addEventListener('click', () => {
  const targets = filteredTasks().filter(task => state.selected.has(task.id) && task.actions.includes(button.dataset.bulk));
  if (targets.length > 200) { toast('每次最多操作 200 项任务，请减少选择'); return; }
  perform(targets, button.dataset.bulk, true);
}));
$('refreshTasks').addEventListener('click', load);
$('retryFailed').addEventListener('click', async () => {
  const targets = failedTargets();
  if (state.busy || !targets.length) return;
  if (targets.length > 200) { toast('每次最多操作 200 项任务，请缩小筛选范围'); return; }
  state.busy = true; state.revision += 1;
  state.selected = new Set(targets.map(task => task.id)); render(); notice('actionResult', '');
  const results = [];
  try {
    // Freeze the visible identities, not the server-wide failure collection.
    // Use each step's supported action; resumed steps need not support retry.
    for (const action of ['retry', 'continue']) {
      const batch = targets.filter(task => (task.actions.includes('retry') ? 'retry' : 'continue') === action);
      if (!batch.length) continue;
      const result = await api('/api/tasks/bulk-action', {action, items: batch.map(task => aiTaskPayload(task, action))});
      results.push(...result.results);
      for (const item of result.results) if (item.ok) state.selected.delete(item.id);
    }
    const failures = results.filter(item => !item.ok).map(item => `${item.id}：${item.error}`);
    notice('actionResult', `已提交失败任务重试 ${results.filter(item => item.ok).length}/${targets.length} 项${failures.length ? '\n' + failures.join('\n') : ''}`);
  } catch (error) { notice('actionResult', `已确认提交 ${results.filter(item => item.ok).length}/${targets.length} 项。${error.message}；未确认的步骤仍保留选择，请先核对状态。`); }
  finally { state.busy = false; lastPayload = ''; await load(); render(); }
});
$('clearCompleted').addEventListener('click', async () => {
  const completed = filteredCompleted();
  if (!completed.length || state.busy) return;
  state.busy = true; state.revision += 1; render(); notice('actionResult', '');
  try {
    const result = await api('/api/tasks/clear-completed', {items: completed.map(task => ({id: task.id, generation: task.history_generation}))});
    toast(`已收起 ${result.cleared} 条已完成记录，原文件和结果仍保留`);
    lastPayload = '';
  } catch (error) { notice('actionResult', error.message); }
  finally { state.busy = false; await load(); render(); }
});
$('restoreCompleted').addEventListener('click', async () => {
  if (state.busy || !state.history.can_restore) return;
  state.busy = true; state.revision += 1; render(); notice('actionResult', '');
  try {
    const result = await api('/api/tasks/restore-completed', {});
    toast(`已恢复 ${result.restored} 条刚清理的任务记录`);
    lastPayload = '';
  } catch (error) { notice('actionResult', error.message); }
  finally { state.busy = false; await load(); render(); }
});
const pdfDialog = createTaskPdfDialog({api, toast, onCreated: async ({showCreated}) => {
  if (showCreated) {
    state.filter = 'active'; $('typeFilter').value = ''; $('categoryFilter').value = ''; $('taskSearch').value = '';
    state.selected.clear();
  }
  lastPayload = ''; await load(); render();
}});
document.addEventListener('visibilitychange', () => { clearTimeout(timer); if (!document.hidden) load(); });
window.addEventListener('online', load);
load();
if (new URLSearchParams(location.search).get('new') === 'pdf') {
  pdfDialog.open();
  const url = new URL(location.href); url.searchParams.delete('new');
  history.replaceState(history.state, '', url.pathname + url.search + url.hash);
}
