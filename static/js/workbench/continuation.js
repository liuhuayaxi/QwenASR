import {cachedJson} from '../cached-json.js';
import {aiTaskPayload} from '../task-config.js';

export const activeStatuses = new Set(['running', 'queued', 'waiting_retry', 'stopping']);
export const attentionStatuses = new Set(['failed', 'error', 'interrupted', 'pending', 'stale', 'completed_with_errors', 'paused']);
const labels = {running:'进行中', queued:'排队中', waiting_retry:'等待重试', stopping:'正在停止', paused:'已暂停', failed:'失败', error:'失败', interrupted:'已中断', pending:'待处理', stale:'原件已更新', completed_with_errors:'部分失败', completed:'已完成', cancelled:'已取消'};
export const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));

export function recordingId(task) {
  if (task.recording_id) return task.recording_id;
  if (task.result_url) {
    try { return new URL(task.result_url, 'http://localhost').searchParams.get('recording'); } catch { /* malformed legacy link */ }
  }
  return null;
}
export function groupTasks(tasks) {
  const groups = new Map();
  for (const task of tasks) {
    const recording = recordingId(task);
    const key = recording ? `recording:${recording}` : task.category ? `course:${task.category}` : `task:${task.id}`;
    if (!groups.has(key)) groups.set(key, {key, title: recording ? task.title : task.category || task.title, category: task.category, tasks: []});
    groups.get(key).tasks.push(task);
  }
  return [...groups.values()];
}
export function recognitionText(health, inference = {}) {
  const strategy = {local:'本机识别', remote_only:'仅远程，不自动回退', remote_preferred:'远程确认未接受时使用 Mac'}[health.asr_provider] || '策略待确认';
  const actual = {remote:'Windows 远程', local:'Mac 本机'}[inference?.provider];
  return `识别来源：${actual || '待确认（尚无推理回执）'}${inference?.fallback ? ' · 已回退 Mac' : ''} · 当前状态：本地服务已连接${health.asr_provider !== 'local' ? '，远程连接未验证' : ''} · 失败策略：${strategy}`;
}
export function recordStages(record, tasks = []) {
  const note = record.ai_note_summary?.summary || record.has_ai_summary;
  const raw = record.status === 'completed' ? '原始转写已完成' : record.has_transcript || record.segment_count || record.segments?.length ? '原始转写可阅读 · 尚未完成' : '原始转写等待中';
  const stages = [raw, note ? '课后复习笔记已保存' : '课后复习笔记尚未生成', record.ideashell_sync?.note_id ? '已关联 IdeaShell' : 'IdeaShell 尚未同步'];
  for (const task of tasks.filter(task => task.status !== 'completed' && task.status !== 'cancelled')) {
    const progress = typeof task.progress === 'number' ? ` ${Math.max(0, Math.min(100, task.progress))}%` : '';
    const actual = {remote:'Windows',local:'Mac'}[task.inference?.provider] || '来源待确认';
    stages.push(`${task.label || '任务'}：${task.stage || labels[task.status] || task.status}${progress} · ${actual}${task.error ? ` · ${task.error}` : ''}`);
  }
  return stages;
}
const COURSE_KEY = 'qwen-asr-last-course-v1', RECORD_KEY = 'qwen-asr-last-recording-v1';
const read = key => { try { return localStorage.getItem(key); } catch { return null; } };
const write = (key, value) => { try { localStorage.setItem(key, value); } catch { /* browsing without storage */ } };

export function createContinuation({root = document, openRecord, toast, fetcher = fetch}) {
  const list = root.querySelector('#continuationList'), error = root.querySelector('#continuationError');
  let records = [], tasks = [], timer, loading = false, busy = false, loaded = false;
  function render() {
    const pending = tasks.filter(task => attentionStatuses.has(task.status));
    const running = tasks.filter(task => activeStatuses.has(task.status));
    const ordered = [...records].sort((a,b) => String(b.started_at || '').localeCompare(String(a.started_at || '')));
    const latest = records.find(record => record.id === read(RECORD_KEY)) || ordered[0];
    const items = [...new Set([latest?.id, ...tasks.filter(task => activeStatuses.has(task.status) || attentionStatuses.has(task.status)).map(recordingId)].filter(Boolean))]
      .map(id => records.find(record => record.id === id)).filter(Boolean).slice(0, 3);
    list.innerHTML = `<p><a href="/tasks">${loaded ? `待处理 ${pending.length} · 进行中 ${running.length}` : '任务状态读取中'} · 查看并继续 →</a></p>` + items.map(record => {
      const related = tasks.filter(task => recordingId(task) === record.id);
      const actions = related.flatMap(task => (task.actions || []).filter(action => ['continue','retry'].includes(action)).slice(0,1).map(action =>
        `<button class="secondary-button" data-task="${escapeHtml(task.id)}" data-action="${action}" ${busy ? 'disabled' : ''}>${action === 'retry' ? '重试' : '继续'}${escapeHtml(task.label || '处理')}</button>`));
      return `<article class="continuation-card"><b>${escapeHtml(record.title || '未命名课堂')}</b><ul>${recordStages(record,related).map(text => `<li>${escapeHtml(text)}</li>`).join('')}</ul><div class="continuation-actions"><button class="secondary-button" data-recording="${escapeHtml(record.id)}">阅读原文 / 打开课堂笔记</button>${actions.join('')}</div></article>`;
    }).join('') + (!items.length ? '<p>暂无待处理内容。</p>' : '');
  }
  async function refresh() {
    if (loading || busy) return;
    loading = true; clearTimeout(timer);
    try {
      const results = await Promise.allSettled(['/api/library?limit=20&include_settings=false','/api/tasks'].map(path => cachedJson(path, {signal:AbortSignal.timeout(15000)}, fetcher)));
      const problems = [];
      results.forEach((result,index) => {
        if (result.status === 'fulfilled') { if (index === 0) records = result.value.records || []; else { tasks = result.value.tasks || []; loaded = true; } }
        else problems.push(`${index === 0 ? '课堂记录' : '任务状态'}暂时无法读取`);
      });
      const wanted = [...new Set([read(RECORD_KEY), ...tasks.filter(task => activeStatuses.has(task.status) || attentionStatuses.has(task.status)).map(recordingId)].filter(Boolean))]
        .filter(id => !records.some(record => record.id === id)).slice(0, 100);
      if (wanted.length) {
        try {
          const linked = await cachedJson('/api/library?' + new URLSearchParams({ids:wanted.join(','), include_settings:'false'}), {}, fetcher);
          records = [...new Map([...records, ...(linked.records || [])].map(record => [record.id, record])).values()];
        } catch { problems.push('关联课堂暂时无法读取'); }
      }
      render();
      error.hidden = !problems.length;
      error.textContent = problems.length ? `${problems.join('；')}，显示上次状态。稍后自动重试。` : '';
      if (!loaded && problems.length) list.insertAdjacentHTML('afterbegin','<p>任务数量待确认</p>');
    } finally {
      loading = false;
      if (!document.hidden) timer = setTimeout(refresh, tasks.some(task => activeStatuses.has(task.status)) ? 15000 : 60000);
    }
  }
  list.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button || busy) return;
    if (button.dataset.recording) { await openRecord(button.dataset.recording); return; }
    const task = tasks.find(task => task.id === button.dataset.task);
    if (!task || !(task.actions || []).includes(button.dataset.action)) return;
    busy = true; render();
    try {
      const response = await fetcher('/api/tasks/action', {method:'POST',headers:{'Content-Type':'application/json','X-QwenASR-Client':'task-center'},signal:AbortSignal.timeout(30000),body:JSON.stringify(aiTaskPayload(task,button.dataset.action))});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '无法继续任务');
      toast('已提交后台处理');
    } catch (problem) { toast(problem.message); }
    finally { busy = false; await refresh(); }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) void refresh(); else clearTimeout(timer); });
  return {refresh, setRecords(value) { records = value; render(); }, lastCourse: () => read(COURSE_KEY),
    rememberCourse(category) { write(COURSE_KEY,category); },
    remember(session) { if (!session?.id) return; write(RECORD_KEY,session.id); if (session.category) write(COURSE_KEY,session.category); },
  };
}
