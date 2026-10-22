// 雨课堂习题 panel on the course page: bind via dropdown, one-click sync,
// optional question browser. Talks only to /api/yuketang.
import {createVisiblePoll} from './visible-poll.js';
import {esc, countsText, questionHtml, headerLine, bindView, currentCategory} from './yuketang-render.js?v=20261022-ykt-4';

const $ = id => document.getElementById(id);
const STATUS = {running: '进行中', completed: '已完成', failed: '失败', login_required: '需要登录', interrupted: '已中断'};
const KIND = {sync: '同步', login: '登录', transcribe: '图片转写', courses: '读取课程列表'};
const state = {category: '', status: null, courses: {courses: []}, rebinding: false, searchTimer: null, loaded: ''};

async function api(path, body) {
  const options = body === undefined ? {cache: 'no-store'} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-QwenASR-Client': 'course-files'}, body: JSON.stringify(body)};
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}）`);
  return data;
}

function setNotice(text, {error = false, login = false} = {}) {
  const node = $('ykt-notice');
  node.hidden = !text; node.classList.toggle('error', error);
  node.innerHTML = text ? `${esc(text)}${login ? ' <button class="ghost-button mini-button" type="button" data-ykt="login">登录雨课堂</button>' : ''}` : '';
}

function renderBind() {
  const view = bindView(state.status || {}, state.courses, state.rebinding);
  const area = $('ykt-bind');
  area.hidden = view.mode === 'hidden';
  if (view.mode === 'login') {
    area.innerHTML = `<span class="muted">登录雨课堂后即可从课程列表中选择要绑定的课程。</span><button class="ui-button primary mini-button" type="button" data-ykt="login">登录雨课堂</button>${state.rebinding ? '<button class="ghost-button mini-button" type="button" data-ykt="cancel">取消</button>' : ''}`;
  } else if (view.mode === 'select') {
    area.innerHTML = `<label class="sr-only" for="ykt-course">雨课堂课程</label><select id="ykt-course" class="ui-field">${view.options.map(option => `<option value="${esc(option.value)}" data-university="${esc(option.university)}"${option.value === view.selected ? ' selected' : ''}>${esc(option.label)}</option>`).join('')}</select><button class="ui-button primary mini-button" type="button" data-ykt="bind">绑定</button>${state.rebinding ? '<button class="ghost-button mini-button" type="button" data-ykt="cancel">取消</button>' : ''}<button class="ghost-button mini-button" type="button" data-ykt="courses" title="重新读取雨课堂课程列表">刷新列表</button>`;
  }
}

function render() {
  const status = state.status || {};
  const job = status.job || {};
  const running = Boolean(status.running);
  const bound = (status.classrooms || []).length > 0;
  $('ykt-line').textContent = headerLine(status);
  $('ykt-rebind').hidden = !bound || state.rebinding;
  $('ykt-sync').hidden = !bound || running;
  $('ykt-stop').hidden = !running;
  $('ykt-more').hidden = !bound;
  for (const node of $('ykt-menu').querySelectorAll('button')) node.disabled = running;
  $('ykt-job').textContent = running ? `${KIND[job.kind] || '任务'}${job.message ? `：${job.message}` : '中…'}` : '';
  renderBind();
  const pending = (status.classrooms || []).flatMap(room => room.pending_activities || []);
  const log = job.log || [];
  $('ykt-details').hidden = !bound || (!pending.length && !log.length && !job.error);
  $('ykt-detailsSummary').textContent = `同步详情${pending.length ? ` · ${pending.length} 个活动待更新` : ''}`;
  $('ykt-pending').innerHTML = pending.map(item => `<li>${esc(item.title)}：${esc(item.reason || item.status)}</li>`).join('');
  $('ykt-log').textContent = log.join('\n');
  $('ykt-log').hidden = !log.length;
  $('ykt-browse').hidden = !bound || !(status.counts || {}).total;
  $('ykt-browseSummary').textContent = `浏览题目 · ${countsText(status.counts)}`;
  const needsLogin = job.status === 'login_required';
  if (!status.playwright) setNotice('需要先安装 Playwright：.venv/bin/pip install playwright', {error: true});
  else if (needsLogin) setNotice('雨课堂登录已过期或尚未登录。', {error: true, login: true});
  else if (!running && job.status === 'failed' && job.error) setNotice(`${KIND[job.kind] || '任务'}失败：${job.error}`, {error: true});
  else setNotice('');
}

async function loadQuestions(force = false) {
  if (!state.category || $('ykt-browse').hidden || !$('ykt-browse').open) return;
  const params = new URLSearchParams({category: state.category, q: $('ykt-search').value.trim(), source: $('ykt-source').value, limit: '300'});
  if (!force && state.loaded === params.toString()) return;
  state.loaded = params.toString();
  try {
    const data = await api(`/api/yuketang/questions?${params}`);
    $('ykt-questions').innerHTML = (data.questions || []).map(q => questionHtml(q)).join('') || '<p class="muted">没有符合条件的题目。</p>';
  } catch (error) { $('ykt-questions').innerHTML = `<p class="notice error">${esc(error.message)}</p>`; }
}

async function refresh() {
  const category = currentCategory();
  const section = $('yuketangSection');
  if (!category || category === '未分类') { section.hidden = true; return; }
  section.hidden = false;
  if (category !== state.category) { state.category = category; state.rebinding = false; state.loaded = ''; }
  const wasRunning = state.status?.running;
  const [status, courses] = await Promise.all([
    api(`/api/yuketang/status?category=${encodeURIComponent(category)}`),
    api(`/api/yuketang/courses?category=${encodeURIComponent(category)}`).catch(() => ({courses: []})),
  ]);
  if (category !== currentCategory()) return;
  state.status = status; state.courses = courses;
  render();
  await loadQuestions(Boolean(wasRunning && !status.running));
}

async function action(path, body = {}) {
  try { setNotice(''); await api(path, {category: state.category, ...body}); }
  catch (error) { setNotice(error.message, {error: true}); }
  await poll.refresh();
}

const poll = createVisiblePoll({run: refresh, delay: () => state.status?.running ? 2000 : 30000});

function init() {
  const section = $('yuketangSection');
  if (!section) return;
  section.addEventListener('click', event => {
    const button = event.target.closest('[data-ykt]');
    if (!button) return;
    const kind = button.dataset.ykt;
    if (button.closest('#ykt-menu')) $('ykt-more').open = false;
    if (kind === 'login') void action('/api/yuketang/login');
    else if (kind === 'courses') void action('/api/yuketang/courses/refresh');
    else if (kind === 'rebind') { state.rebinding = true; render(); }
    else if (kind === 'cancel') { state.rebinding = false; render(); }
    else if (kind === 'bind') {
      const select = $('ykt-course');
      const option = select?.selectedOptions[0];
      if (!option) return;
      state.rebinding = false;
      void action('/api/yuketang/bind', {classroom_id: option.value, university_id: option.dataset.university || null, name: option.textContent, replace: true});
    }
    else if (kind === 'sync') void action('/api/yuketang/sync', {transcribe: true});
    else if (kind === 'refresh-all') void action('/api/yuketang/sync', {refresh: true, transcribe: true});
    else if (kind === 'transcribe') void action('/api/yuketang/transcribe');
    else if (kind === 'stop') void action('/api/yuketang/stop');
    else if (kind === 'unbind') {
      const room = state.status?.classrooms?.[0];
      if (room && confirm('解除绑定不会删除已下载的题目，确定吗？')) void action('/api/yuketang/unbind', {classroom_id: room.classroom_id});
    }
  });
  $('ykt-search').addEventListener('input', () => { clearTimeout(state.searchTimer); state.searchTimer = setTimeout(() => loadQuestions(), 250); });
  $('ykt-source').addEventListener('change', () => loadQuestions());
  $('ykt-browse').addEventListener('toggle', () => loadQuestions());
  const courses = $('courses');
  if (courses) new MutationObserver(() => { if (currentCategory() !== state.category) void poll.refresh(); }).observe(courses, {subtree: true, attributes: true, attributeFilter: ['class'], childList: true});
  void poll.refresh().catch(error => setNotice(error.message, {error: true}));
}

if (typeof document !== 'undefined' && document.getElementById) {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
}
