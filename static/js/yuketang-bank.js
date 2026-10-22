// 雨课堂题库 panel on the course page: bind classrooms, sync, browse questions.
// The bank itself lives in the private app data folder; this view only talks to
// /api/yuketang and never contacts Yuketang directly.
import {createVisiblePoll} from './visible-poll.js';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
// Math delimiters used by Yuketang text and by vision transcripts. Text
// outside formulas stays escaped; formulas go through the vendored KaTeX
// (trust off), or stay escaped source when KaTeX is unavailable.
const MATH = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$((?:\\.|[^$\n\\]|\\)+?)\$/g;
export function mathText(value, katex = globalThis.katex) {
  const text = String(value ?? '');
  let html = '', last = 0;
  for (const match of text.matchAll(MATH)) {
    const [raw, block, bracket, paren, inline] = match;
    const tex = (block ?? bracket ?? paren ?? inline).trim();
    html += esc(text.slice(last, match.index));
    last = match.index + raw.length;
    if (!tex || !katex?.renderToString) { html += esc(raw); continue; }
    try {
      html += katex.renderToString(tex, {displayMode: block !== undefined || bracket !== undefined, throwOnError: false, trust: false, strict: 'ignore', maxExpand: 500});
    } catch { html += esc(raw); }
  }
  return html + esc(text.slice(last));
}

const SOURCES = {exam: '课前/课后测试与考试', lesson: '课上习题', courseware: '课件习题'};
const STATUS = {running: '运行中', completed: '已完成', failed: '失败', login_required: '需要重新登录', stopped: '已停止', idle: '空闲'};
const state = {category: '', status: null, questions: [], searchTimer: null};

export function currentCategory(doc = document) {
  return doc.querySelector('#courses button.selected')?.dataset.category || new URLSearchParams(location.search).get('category') || '';
}

async function api(path, body) {
  const options = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-QwenASR-Client': 'course-files'}, body: JSON.stringify(body)};
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败（${response.status}）`);
  return data;
}

export function countsText(counts = {}) {
  if (!counts.total) return '还没有题目';
  const parts = Object.entries(SOURCES).filter(([key]) => counts[key]).map(([key, label]) => `${label} ${counts[key]}`);
  const extra = [];
  if (counts.wrong) extra.push(`答错 ${counts.wrong}`);
  if (counts.needs_vision) extra.push(`图片转写 ${counts.vision_done || 0}/${counts.needs_vision}`);
  return `共 ${counts.total} 题：${parts.join('，')}${extra.length ? `（${extra.join('，')}）` : ''}`;
}

function when(value) {
  if (!value) return '从未同步';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', {hour12: false});
}

function setNotice(text, error = false) {
  const node = $('ykt-notice');
  node.hidden = !text; node.textContent = text || ''; node.classList.toggle('error', error);
}

function renderStatus() {
  const status = state.status || {};
  const job = status.job || {};
  const running = Boolean(status.running);
  $('ykt-classrooms').innerHTML = (status.classrooms || []).map(item => `
    <li><div><b>${esc(item.course_name || item.name || '雨课堂班级')}</b> <span class="muted">班级 ${esc(item.classroom_id)} · 学校 ${esc(item.university_id ?? '?')}</span>
      <p class="muted">上次同步：${esc(when(item.last_sync))} · ${esc(countsText(item.counts))}</p>
      ${(item.pending_activities || []).length ? `<details><summary>${item.pending_activities.length} 个活动待更新</summary><ul>${item.pending_activities.map(p => `<li>${esc(p.title)}：${esc(p.reason || p.status)}</li>`).join('')}</ul></details>` : ''}</div>
      <button class="ui-button" type="button" data-unbind="${esc(item.classroom_id)}" ${running ? 'disabled' : ''}>解除绑定</button></li>`).join('') || '<li class="muted">这门课还没有绑定雨课堂班级。</li>';
  $('ykt-summary').textContent = countsText(status.counts);
  const login = status.login || {};
  const needsLogin = job.status === 'login_required' || login.logged_in === false;
  $('ykt-login').classList.toggle('primary', needsLogin || !login.profile_exists);
  for (const id of ['ykt-login', 'ykt-sync', 'ykt-transcribe', 'ykt-bind']) $(id).disabled = running || (id !== 'ykt-login' && id !== 'ykt-bind' && !(status.classrooms || []).length);
  $('ykt-stop').hidden = !running;
  const label = `${{sync: '同步', login: '登录', transcribe: '转写'}[job.kind] || '任务'}${STATUS[job.status] || job.status || ''}`;
  $('ykt-job').textContent = job.status && job.status !== 'idle' ? `${label}${job.error ? `：${job.error}` : ''}` : '';
  $('ykt-log').textContent = (job.log || []).join('\n');
  $('ykt-log').hidden = !(job.log || []).length;
  if (!status.playwright) setNotice('需要先安装 Playwright：.venv/bin/pip install playwright（使用本机 Chrome 登录）', true);
  else if (needsLogin) setNotice('雨课堂登录已过期或尚未登录，请点击“登录雨课堂”并用微信扫码。', true);
  else setNotice('');
}

export function questionHtml(q, imageUrl = image => `/api/yuketang/image/${encodeURIComponent(q.classroom_id)}/${image.path.split('/').map(encodeURIComponent).join('/')}`) {
  const mark = q.correct === true ? '<span class="ykt-ok">答对</span>' : q.correct === false ? '<span class="ykt-wrong">答错</span>' : q.graded === false && (q.student_answer || []).length ? '<span class="muted">未批改</span>' : '';
  const body = q.body || q.vision?.text || (q.images?.length ? '（图片题，见下方图片）' : '');
  const options = (q.options || []).map(o => `<li><b>${esc(o.key)}</b> ${mathText(o.value)}</li>`).join('');
  const answer = (q.answer || []).join('、') || q.vision?.answer_text || '';
  const images = (q.images || []).filter(image => image.path).map(image => `<img loading="lazy" alt="${esc(image.kind === 'remark' ? '参考答案图片' : '题目图片')}" src="${esc(imageUrl(image))}">`).join('');
  return `<article class="ykt-question">
    <header><span class="count-badge">${esc(q.source_label)}</span> <b>${esc(q.activity_title)}</b> <span class="muted">${esc(q.date || '')}${q.page ? ` · 第 ${esc(q.page)} 页` : ''} · ${esc(q.type_text)}</span> ${mark}</header>
    <p class="ykt-body">${mathText(body)}</p>${options ? `<ul class="ykt-options">${options}</ul>` : ''}
    <p><b>正确答案：</b>${answer ? mathText(answer) : '未公布'}${q.remark ? `<br><b>解析：</b>${mathText(q.remark)}` : ''}</p>
    ${(q.student_answer || []).length ? `<p class="ykt-mine"><b>我的答案：</b>${mathText(q.student_answer.join('、'))}${q.score != null ? `（${esc(q.score)}/${esc(q.max_score ?? '?')} 分）` : ''}</p>` : ''}
    ${images ? `<div class="ykt-images">${images}</div>` : ''}</article>`;
}

async function loadQuestions() {
  if (!state.category) return;
  const params = new URLSearchParams({category: state.category, q: $('ykt-search').value.trim(), source: $('ykt-source').value, limit: '300'});
  try {
    const data = await api(`/api/yuketang/questions?${params}`);
    state.questions = data.questions || [];
    $('ykt-questions').innerHTML = state.questions.map(q => questionHtml(q)).join('') || '<p class="muted">没有符合条件的题目。</p>';
  } catch (error) { $('ykt-questions').innerHTML = `<p class="notice error">${esc(error.message)}</p>`; }
}

async function refresh() {
  const category = currentCategory();
  if (!category || category === '未分类') { $('yuketangSection').hidden = true; return; }
  $('yuketangSection').hidden = false;
  const changed = category !== state.category;
  state.category = category;
  const wasRunning = state.status?.running;
  state.status = await api(`/api/yuketang/status?category=${encodeURIComponent(category)}`);
  renderStatus();
  if ((changed || (wasRunning && !state.status.running)) && $('yuketangSection').open) await loadQuestions();
}

async function action(path, body) {
  try { setNotice(''); await api(path, {category: state.category, ...body}); await poll.refresh(); }
  catch (error) { setNotice(error.message, true); }
}

const poll = createVisiblePoll({run: refresh, delay: () => state.status?.running ? 2000 : 30000});

function init() {
  if (!$('yuketangSection')) return;
  $('ykt-bindForm').addEventListener('submit', event => {
    event.preventDefault();
    const classroom = $('ykt-classroom').value.trim();
    if (!/^\d+$/.test(classroom)) { setNotice('请填写雨课堂班级编号（网址 /v2/web/studentLog/ 后面的数字）', true); return; }
    void action('/api/yuketang/bind', {classroom_id: classroom, university_id: $('ykt-university').value.trim() || null}).then(() => { $('ykt-classroom').value = ''; $('ykt-university').value = ''; });
  });
  $('ykt-classrooms').addEventListener('click', event => {
    const id = event.target.closest('[data-unbind]')?.dataset.unbind;
    if (id && confirm('解除绑定不会删除已下载的题目，确定吗？')) void action('/api/yuketang/unbind', {classroom_id: id});
  });
  $('ykt-login').addEventListener('click', () => action('/api/yuketang/login', {}));
  $('ykt-sync').addEventListener('click', () => action('/api/yuketang/sync', {refresh: $('ykt-refresh').checked}));
  $('ykt-transcribe').addEventListener('click', () => action('/api/yuketang/transcribe', {}));
  $('ykt-stop').addEventListener('click', () => action('/api/yuketang/stop', {}));
  $('ykt-search').addEventListener('input', () => { clearTimeout(state.searchTimer); state.searchTimer = setTimeout(loadQuestions, 250); });
  $('ykt-source').addEventListener('change', loadQuestions);
  $('yuketangSection').addEventListener('toggle', () => { if ($('yuketangSection').open) void loadQuestions(); });
  const courses = $('courses');
  if (courses) new MutationObserver(() => { if (currentCategory() !== state.category) void poll.refresh(); }).observe(courses, {subtree: true, attributes: true, attributeFilter: ['class'], childList: true});
  void poll.refresh().catch(error => setNotice(error.message, true));
}

if (typeof document !== 'undefined' && document.getElementById) {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
}
