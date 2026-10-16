import {cachedJson} from './cached-json.js';
import {escapeHtml as esc, recordingId, activeStatuses, attentionStatuses} from './workbench/continuation.js?v=20261011-ui-final';
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
let category = params.get('category') || '', records = [], tasks = [], categories = [], timer, recordsLoaded = false, tasksLoaded = false;
const PAGE_SIZE = 20;
let activeRequest = null, refreshQueued = false, moreQueued = false;
let loadedPages = 1, loadingMore = false, paginationError = '';
let currentTab = 'overview';
const routes = {records:'/library', notes:'/library', materials:'/course-files', review:'/course-files'};
const query = values => new URLSearchParams({category, ...values}).toString();
const hasNote = record => Boolean(record.has_note || record.ai_note_summary?.summary || record.has_ai_summary);
let recordTotal = 0, noteTotal = 0, nextOffset = null;
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
  $('workspaceMeta').textContent = `${recordTotal} 堂课堂 · ${noteTotal} 份笔记${tasksLoaded ? ` · ${pending.length} 项待处理` : ''}`;
  replace('workspaceRecent', lessons.slice(0,5).map(record => recordRow(record)).join('') || '<p class="workspace-empty">暂无课堂记录</p>');
  const shown = (pending.length ? pending : related.filter(task => task.status === 'completed')).slice(0, pending.length ? 4 : 3);
  $('workspaceTaskHeading').textContent = pending.length ? '待处理任务' : '最近任务';
  $('workspaceTaskCount').textContent = pending.length ? String(pending.length) : '';
  replace('workspaceTimeline', shown.map(task => `<article class="workspace-task"><b>${esc(task.title || task.label || '任务')}</b><p><span>${esc(task.label || '')}</span><span class="workspace-task-status" data-attention="${attentionStatuses.has(task.status)}">${esc(taskLabels[task.status] || task.stage || '处理中')}</span></p></article>`).join('') || `<p class="workspace-empty">${tasksLoaded ? '暂无任务' : '任务状态暂不可用'}</p>`);
  const pagination = nextOffset !== null ? `<div class="workspace-pagination">${paginationError ? `<p id="workspacePaginationError" role="alert">${esc(paginationError)}</p>` : ''}<button id="workspaceLoadMore" class="secondary-button" data-more-records aria-controls="workspaceQuestions" aria-disabled="${loadingMore || moreQueued}" aria-busy="${loadingMore || moreQueued}">${loadingMore || moreQueued ? '正在加载…' : paginationError ? '重试加载' : '加载更多课堂'}</button></div>` : '';
  replace('workspaceQuestions', (lessons.map(record => recordRow(record,true)).join('') || '<p class="workspace-empty">暂无课堂记录</p>') + pagination);
  $('workspaceQuestions').setAttribute('aria-busy', String(loadingMore));
  if (!recordsLoaded) {
    $('workspaceMeta').textContent = '正在读取课程…';
    replace('workspaceRecent', '<p class="workspace-empty">正在读取…</p>');
    replace('workspaceQuestions', '<p class="workspace-empty">正在读取…</p>');
  }
  $('microphoneSource').href = '/?' + query({});
  $('uploadSource').href = '/?' + query({source:'upload'});
}
function tab(name, recording) {
  if (!['overview','review',...Object.keys(routes)].includes(name)) return;
  currentTab = name;
  const address = new URLSearchParams(location.search);
  if (category) address.set('category', category); else address.delete('category');
  if (name === 'overview') address.delete('tab'); else address.set('tab', name);
  history.replaceState(null, '', '/workspace' + (address.size ? '?' + address : ''));
  $('workspaceOverview').hidden = name !== 'overview';
  $('workspaceReview').hidden = name !== 'review';
  $('workspaceFrame').hidden = name === 'overview' || name === 'review';
  $('workspaceFrame').dataset.view = name;
  document.querySelectorAll('#workspaceTabs [data-tab]').forEach(button => {
    if (button.dataset.tab === name) button.setAttribute('aria-current','page');
    else button.removeAttribute('aria-current');
  });
  if (name !== 'overview' && name !== 'review') setFrameSource($('workspaceFrame'), routes[name] + '?' + query({embedded:'1', ...(recording ? {recording} : {})}));
}
// Avoid tearing down a live iframe when the selected destination is unchanged.
// dataset stores the requested URL; frame.src may be absolute in a browser.
function setFrameSource(frame, source) {
  if (frame.dataset.requestedSource === source) return;
  frame.dataset.requestedSource = source;
  frame.src = source;
}
let noteReturn = null;
let noteReaderObserver = null;
let noteChatTarget = null;
function openClassroomChat(recording) {
  tab('review'); $('workspaceReview').hidden = true; $('workspaceFrame').hidden = false;
  setFrameSource($('workspaceFrame'), '/?' + query({embedded:'1',chat:recording}));
}
function openClassroomNote(recording, opener) {
  const dialog = $('workspaceNoteDialog'), frame = $('workspaceNoteFrame');
  noteReturn = {recording, opener, x:window.scrollX, y:window.scrollY};
  noteChatTarget = null;
  const title = records.find(record => record.id === recording)?.title || '课堂笔记';
  frame.title = title;
  $('workspaceNoteReturn').hidden = false;
  frame.src = '/library?' + query({embedded:'1', note_reader:'1', recording});
  document.body.classList.add('workspace-note-open');
  document.documentElement.classList.add('workspace-note-open');
  dialog.showModal();
}
function requestNoteClose() {
  const frame = $('workspaceNoteFrame');
  try {
    const reader = frame.contentDocument?.getElementById('noteDialog');
    const closeButton = frame.contentDocument?.getElementById('closeNoteDialog');
    if (reader?.open && closeButton) {
      // Let the existing reader finish edits and flush its protected autosave.
      // It fires "close" only after saving or securing the recoverable draft.
      closeButton.click();
      return;
    }
  } catch { /* The reader has not loaded yet; there is no editable note. */ }
  $('workspaceNoteDialog').close();
}
$('workspaceNoteFrame').addEventListener('load', () => {
  if (!$('workspaceNoteDialog').open) return;
  try {
    const reader = $('workspaceNoteFrame').contentDocument?.getElementById('noteDialog');
    if (!reader) return;
    const ready = () => { $('workspaceNoteReturn').hidden = reader.open; };
    noteReaderObserver?.disconnect();
    noteReaderObserver = new MutationObserver(ready);
    noteReaderObserver.observe(reader, {attributes:true,attributeFilter:['open']});
    ready();
    reader.addEventListener('close', () => {
      if ($('workspaceNoteDialog').open) $('workspaceNoteDialog').close();
    }, {once:true});
  } catch { /* Keep the explicit return control available if loading fails. */ }
});
$('workspaceNoteDialog').addEventListener('cancel', event => {
  event.preventDefault();
  requestNoteClose();
});
$('workspaceNoteDialog').addEventListener('close', () => {
  noteReaderObserver?.disconnect();
  noteReaderObserver = null;
  document.body.classList.remove('workspace-note-open');
  document.documentElement.classList.remove('workspace-note-open');
  $('workspaceNoteFrame').src = 'about:blank';
  const back = noteReturn;
  const chat = noteChatTarget;
  noteReturn = null;
  noteChatTarget = null;
  if (!back) return;
  // The workspace remains mounted; a poll may have replaced the original row.
  const opener = back.opener?.isConnected ? back.opener : [...document.querySelectorAll('#workspaceRecent [data-recording]')].find(button => button.dataset.recording === back.recording);
  opener?.focus({preventScroll:true});
  window.scrollTo(back.x, back.y);
  // Removing the fullscreen document lock and the native dialog's focus
  // restoration settle during layout; restore again after that layout.
  if (!chat) requestAnimationFrame(() => {
    if (!noteReturn && !$('workspaceNoteDialog').open) window.scrollTo(back.x, back.y);
  });
  if (chat) openClassroomChat(chat);
});
window.addEventListener('message', event => {
  if (event.origin !== location.origin || event.source !== $('workspaceNoteFrame').contentWindow
      || !$('workspaceNoteDialog').open || !noteReturn
      || event.data?.type !== 'qwenasr:open-classroom-chat' || event.data.recording !== noteReturn.recording) return;
  noteChatTarget = noteReturn.recording;
  requestNoteClose();
});
function timeline() {
  $('timelineFrame').src = '/tasks?' + query({embedded:'1'});
  $('timelineDialog').showModal();
}
function beginRequest() {
  clearTimeout(timer);
  const request = {controller: new AbortController(), category};
  // Bound the whole operation, including all retained pages, to avoid a stuck
  // spinner and allow the user to retry. Category switches abort immediately.
  request.timeout = setTimeout(() => request.controller.abort(new Error('读取超时')), 15000);
  activeRequest = request;
  return request;
}
function finishRequest(request) {
  clearTimeout(request.timeout);
  if (activeRequest !== request) return;
  activeRequest = null;
  if (moreQueued) {
    moreQueued = false;
    if (nextOffset !== null) { void loadMore(); return; }
    render();
  }
  if (!document.hidden) {
    const delay = refreshQueued ? 0 : tasks.some(task => activeStatuses.has(task.status)) ? 15000 : 60000;
    refreshQueued = false;
    timer = setTimeout(load, delay);
  }
}
function libraryPage(request, offset = 0) {
  return cachedJson('/api/library?' + new URLSearchParams({category:request.category, limit:String(PAGE_SIZE), offset:String(offset), include_settings:'false'}), {signal:request.controller.signal});
}
async function libraryWindow(request) {
  let metadata, page, offset = 0, combined = [];
  // Refresh the already visible window, not just page one. Stage the whole
  // window and commit atomically so a failure never discards loaded pages.
  for (let index = 0; index < loadedPages; index++) {
    page = await libraryPage(request, offset);
    metadata ||= page;
    combined.push(...(page.records || []));
    const next = page.next_offset;
    if (next == null) break;
    if (!Number.isInteger(next) || next <= offset) throw new Error('课堂分页数据无效');
    offset = next;
  }
  return {...metadata, records:[...new Map(combined.map(record => [record.id,record])).values()], next_offset:page.next_offset ?? null};
}
async function load() {
  if (activeRequest) { refreshQueued = true; return; }
  const request = beginRequest();
  try {
    const result = await Promise.allSettled([
      libraryWindow(request),
      cachedJson('/api/tasks?' + new URLSearchParams({category:request.category}), {signal:request.controller.signal}),
    ]);
    if (activeRequest !== request) return;
    if (result[0].status === 'fulfilled') {
      const data = result[0].value;
      records = data.records;
      recordTotal = data.total ?? records.length;
      noteTotal = data.note_count ?? records.filter(hasNote).length;
      nextOffset = data.next_offset;
      recordsLoaded = true;
      categories = [...new Set([...(data.categories || []), ...records.map(record => record.category), ...(category ? [category] : [])].filter(Boolean))];
      replace('workspaceCourse', [`<option value="" ${!category ? 'selected' : ''}>全部课程</option>`, ...categories.map(name => `<option value="${esc(name)}" ${name===category ? 'selected' : ''}>${esc(name)}</option>`)].join(''));
      $('workspaceCourse').disabled = false;
    }
    if (result[1].status === 'fulfilled') { tasks = result[1].value.tasks || []; tasksLoaded = true; }
    render();
    const failures = result.map((value, index) => value.status === 'rejected' ? (index ? '任务' : '课堂记录') : '').filter(Boolean);
    $('workspaceError').hidden = !failures.length;
    $('workspaceError').textContent = failures.length ? `${failures.join('、')}读取失败，正在重试。` : '';
    if (!recordsLoaded) {
      $('workspaceMeta').textContent = '暂时无法读取课程';
      replace('workspaceRecent','<p class="workspace-empty">课堂记录暂不可用</p>');
      replace('workspaceQuestions','<p class="workspace-empty">课堂记录暂不可用</p>');
    }
  } finally { finishRequest(request); }
}
async function loadMore() {
  if (nextOffset === null || loadingMore || moreQueued) return;
  if (activeRequest) { moreQueued = true; render(); return; }
  const request = beginRequest(), offset = nextOffset;
  const focused = document.activeElement?.id === 'workspaceLoadMore';
  const oldIds = new Set(records.map(record => record.id));
  let appended = false;
  loadingMore = true; paginationError = ''; render();
  try {
    const data = await libraryPage(request, offset);
    if (activeRequest !== request) return;
    if (data.next_offset != null && (!Number.isInteger(data.next_offset) || data.next_offset <= offset)) throw new Error('课堂分页数据无效');
    records = [...new Map([...records, ...(data.records || [])].map(record => [record.id, record])).values()];
    nextOffset = data.next_offset ?? null;
    recordTotal = data.total ?? recordTotal;
    noteTotal = data.note_count ?? noteTotal;
    appended = true;
    loadedPages++;
  } catch {
    if (activeRequest === request) paginationError = '更多课堂读取失败，请重试。';
  } finally {
    if (activeRequest === request) {
      loadingMore = false; render();
      // Continue reading at the first new row, not at the document root or a
      // load-more button now below the fold. Do not steal focus moved elsewhere.
      if (appended && focused && (document.activeElement === document.body || document.activeElement?.id === 'workspaceLoadMore')) {
        [...$('workspaceQuestions').querySelectorAll('[data-chat]')].find(button => !oldIds.has(button.dataset.chat))?.focus({preventScroll:true});
      }
    }
    finishRequest(request);
  }
}
$('workspaceCourse').addEventListener('change', () => {
  const nextCategory = $('workspaceCourse').value;
  if (nextCategory === category) return;
  if (activeRequest) {
    clearTimeout(activeRequest.timeout);
    activeRequest.controller.abort();
    activeRequest = null;
  }
  clearTimeout(timer);
  category = nextCategory;
  refreshQueued = moreQueued = loadingMore = false;
  loadedPages = 1; paginationError = '';
  records = []; tasks = []; recordsLoaded = tasksLoaded = false;
  recordTotal = noteTotal = 0; nextOffset = null;
  $('workspaceError').hidden = true;
  try { localStorage.setItem('qwen-asr-last-course-v1',category); } catch { /* optional preference */ }
  render(); tab(currentTab); void load();
});
document.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.hasAttribute('data-more-records')) { void loadMore(); return; }
  if (button.dataset.tab) tab(button.dataset.tab);
  if (button.dataset.recording) openClassroomNote(button.dataset.recording,button);
  if (button.dataset.chat) {
    openClassroomChat(button.dataset.chat);
  }
  if (button.hasAttribute('data-external-review')) {
    tab('review'); $('workspaceReview').hidden = true; $('workspaceFrame').hidden = false;
    setFrameSource($('workspaceFrame'), '/course-files?' + query({embedded:'1'}));
  }
  if (button.hasAttribute('data-timeline')) timeline();
  if (button.hasAttribute('data-close-note')) requestNoteClose();
  if (button.dataset.close) $(button.dataset.close).close();
});
$('workspaceTasks').addEventListener('click',timeline);
$('newClass').addEventListener('click', () => $('newClassDialog').showModal());
document.addEventListener('visibilitychange', () => { if (!document.hidden) void load(); else clearTimeout(timer); });
tab(params.get('tab') || 'overview');
void load();

const libraryEvents = new EventSource('/api/library/events');
let libraryRefresh;
libraryEvents.onmessage = () => {
  clearTimeout(libraryRefresh);
  libraryRefresh = setTimeout(() => { if (!document.hidden) void load(); }, 200);
};
