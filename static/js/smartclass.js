const $ = id => document.getElementById(id);
let currentGroups = [], courseVersion = '', catalogRevision = null, optionsLoaded = false;
const selectedLessonIds = new Set();
let busy = false, submitting = false, loadingCourse = '', loadSerial = 0, accountCollapsed = false;
let replayLoginSubmitted = false;
let sessionCheckInFlight = false, lastSessionCheck = 0;
function notice(message = '', kind = 'error') { $('notice').textContent = message; $('notice').dataset.kind = kind; }
function pendingReplayURL() {
  const value = new URLSearchParams(location.search).get('returnTo');
  if (!value) return null;
  try {
    const target = new URL(value, location.origin);
    if (target.origin === location.origin && target.pathname === '/course/replay'
        && !target.username && !target.password) return target;
  } catch (_) {}
  return null;
}
function continuePendingReplay(status) {
  const target = pendingReplayURL();
  if (!target || !replayLoginSubmitted || !status.identity || !status.authenticated
      || ['checking','error'].includes(status.session_check)) return;
  location.replace(target.href);
  return true;
}
async function post(path, body) {
  const r = await fetch(path, {
    method: 'POST', headers: {'Content-Type':'application/json','X-QwenASR-Client':'smartclass'},
    body: JSON.stringify(body || {})
  });
  const value = await r.json();
  if (!r.ok) throw Error(typeof value.detail === 'string' ? value.detail : '提交失败');
  return value;
}
function updateSelection() {
  const count = selectedLessonIds.size;
  $('import').disabled = busy || submitting || !!loadingCourse || !count;
  $('import').textContent = count ? `将 ${count} 个选中课次加入队列` : '将选中课次加入队列';
  $('loadLessons').disabled = busy || !!loadingCourse || !$('courses').value;
  const message = count ? `已选择 ${count} 个课次` : '勾选课次后加入队列；右侧按钮可直接打开回放。';
  if ($('selectionStatus').textContent !== message) $('selectionStatus').textContent = message;
}
function clearLessons() {
  loadSerial++;
  loadingCourse = '';
  currentGroups = [];
  selectedLessonIds.clear();
  $('lessons').replaceChildren();
  $('lessonsStatus').hidden = true;
  updateSelection();
}
function renderLessonStatus(row, state) {
  const badge = row.querySelector('.lesson-badge');
  badge.textContent = state?.label || '';
  badge.hidden = !state?.label;
  badge.dataset.state = state?.state || 'new';
  badge.title = state?.detail || '';
  const detail = row.querySelector('.lesson-import-detail');
  detail.textContent = state?.detail || '';
  detail.hidden = !state?.label || state.state === 'completed';
}
function updateLessonStatuses(statuses) {
  for (const group of currentGroups) {
    if (statuses[group.id]) group.import_status = statuses[group.id];
  }
  for (const row of $('lessons').querySelectorAll('.lesson')) {
    renderLessonStatus(row, currentGroups.find(group => group.id === row.dataset.group)?.import_status);
  }
}
function renderLessons() {
  const available = new Set(currentGroups.filter(g => g.videos.length !== 1 || $('allowSingle').checked).map(g => g.id));
  for (const id of selectedLessonIds) if (!available.has(id)) selectedLessonIds.delete(id);
  $('lessons').replaceChildren();
  for (const g of currentGroups) {
    const row = document.createElement('div'), choice = document.createElement('label');
    row.className = 'lesson'; row.dataset.group = g.id;
    choice.className = 'lesson-choice';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.value = g.id;
    checkbox.disabled = g.videos.length === 1 && !$('allowSingle').checked;
    checkbox.checked = selectedLessonIds.has(g.id);
    checkbox.onchange = () => {
      if (checkbox.checked) selectedLessonIds.add(g.id);
      else selectedLessonIds.delete(g.id);
      updateSelection();
    };
    const text = document.createElement('div'), title = document.createElement('strong'), detail = document.createElement('small');
    title.textContent = g.course_name + ' · ' + g.date + ' ' + g.start.slice(11,16) + '–' + g.stop.slice(11,16);
    const periods = g.periods.filter(Boolean);
    detail.textContent = g.videos.length === 2
      ? `${periods.length === 2 ? '第 ' + periods.join('、') + ' 节' : '两节回放'} · 合计约 ${Math.round(g.duration / 60)} 分钟 → 一份笔记`
      : `${periods[0] ? '第 ' + periods[0] + ' 节' : '单节回放'} · 暂无相邻回放配对`;
    const heading = document.createElement('div'), badge = document.createElement('span');
    heading.className = 'lesson-heading'; badge.className = 'lesson-badge';
    const importDetail = document.createElement('small'); importDetail.className = 'lesson-import-detail';
    heading.append(title, badge); text.append(heading, detail, importDetail);
    choice.append(checkbox, text); row.append(choice);
    const replays = document.createElement('div'); replays.className = 'lesson-replays';
    for (const [index, video] of g.videos.entries()) {
      const link = document.createElement('a');
      link.className = 'ui-button lesson-replay';
      link.textContent = g.videos.length === 2 ? (index === 0 ? '上半节回放' : '下半节回放') : '课程回放';
      link.href = '/course/replay?' + new URLSearchParams({video:video.NewID});
      link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.title = g.periods[index] ? `打开第 ${g.periods[index]} 节学校回放` : '打开学校回放';
      replays.append(link);
    }
    row.append(replays); renderLessonStatus(row, g.import_status); $('lessons').append(row);
  }
  if (!currentGroups.length) $('lessons').textContent = '此课程暂无可用回放。';
  updateSelection();
}
async function loadLessons() {
  const key = $('courses').value;
  if (!key) return;
  const serial = ++loadSerial;
  loadingCourse = key;
  $('lessonsStatus').textContent = '正在获取回放列表…';
  $('lessonsStatus').hidden = false;
  updateSelection(); notice();
  try {
    const response = await fetch('/api/lessons?course=' + encodeURIComponent(key));
    const result = await response.json();
    if (serial !== loadSerial) return;
    if (!response.ok) throw Error(result.detail);
    currentGroups = result.groups;
    renderLessons();
    $('lessonsStatus').hidden = true;
  } catch (e) {
    if (serial === loadSerial) {
      $('lessonsStatus').textContent = currentGroups.length
        ? `刷新回放失败，当前列表和选择已保留：${e.message}` : e.message;
    }
  } finally {
    if (serial === loadSerial) { loadingCourse = ''; updateSelection(); }
  }
}
function renderCourseState(s) {
  busy = ['connecting','syncing'].includes(s.state);
  $('refresh').disabled = busy || !s.identity;
  $('courses').disabled = busy;
  $('stop').hidden = s.state !== 'running';
  $('resumeQueue').hidden = busy || s.state === 'running' || !(s.queue?.remaining);
  $('resumeQueue').disabled = !s.identity;
  const version = JSON.stringify(s.courses.map(c => [c.key, c.CourseName, c.TeacherName, c.ClassID, c.VideoTotal]));
  if (version !== courseVersion) {
    courseVersion = version;
    const selected = $('courses').value;
    const retained = s.courses.some(c => c.key === selected);
    if (!retained) clearLessons();
    $('courses').replaceChildren(new Option('选择课程…', ''));
    for (const c of s.courses) {
      const duplicate = s.courses.filter(v => v.CourseName === c.CourseName && v.TeacherName === c.TeacherName).length > 1;
      const label = c.CourseName + ' · ' + (c.TeacherName || '暂无主讲') + ' · ' + c.VideoTotal + ' 节'
        + (duplicate ? ' · 教学班 ' + c.ClassID : '');
      $('courses').add(new Option(label, c.key));
    }
    if (retained) {
      $('courses').value = selected;
    }
  }
  if (s.course_revision !== catalogRevision && !busy) {
    catalogRevision = s.course_revision;
    if ($('courses').value && !loadingCourse) loadLessons();
  }
  if (!optionsLoaded && s.options.summary_provider) {
    const p = s.options.summary_provider;
    $('summaryBase').value = p.base_url || '';
    $('summaryModel').value = p.model || '';
    $('generateSummary').checked = s.options.generate_summary !== false;
    $('aiCorrection').checked = s.options.ai_correction !== false;
    const correction = s.options.correction_provider || p;
    $('correctionBase').value = correction.base_url || '';
    $('correctionModel').value = correction.model || '';
    optionsLoaded = true;
  }
  $('result').replaceChildren();
  const activeImports = s.imports.filter(j => ['running', 'queued'].includes(j.state));
  if (!activeImports.length) {
    const empty = document.createElement('p'); empty.className = 'muted';
    empty.textContent = '暂无运行任务'; $('result').append(empty);
  }
  for (const j of activeImports) {
    const box = document.createElement('div'); box.className = 'job';
    const head = document.createElement('div'); head.className = 'job-head';
    const title = document.createElement('strong'), badge = document.createElement('span'), message = document.createElement('p');
    title.textContent = j.title; badge.className = 'job-percent'; badge.textContent = j.state === 'queued' ? '排队中' : Math.round(Number(j.progress) || 0) + '%';
    message.textContent = j.message; head.append(title, badge); box.append(head, message);
    if (j.metrics?.first_text_seconds != null) { const timing = document.createElement('p'); timing.textContent = '首段文字 ' + j.metrics.first_text_seconds.toFixed(1) + ' 秒 · ASR 排队 ' + (j.metrics.asr_queue_seconds || 0).toFixed(1) + ' 秒 · 推理 ' + (j.metrics.asr_inference_seconds || 0).toFixed(1) + ' 秒'; box.append(timing); }
    if (j.ai_errors) { const warning = document.createElement('p'); warning.textContent = j.ai_errors + ' 段精修未完成，原文已保留，可在顶部任务中心重试'; box.append(warning); }
    if (j.state === 'running') {
      const meter = document.createElement('progress'); meter.className = 'overall-progress'; meter.max = 100; meter.value = j.progress || 0; meter.setAttribute('aria-label', '课次总体进度'); box.append(meter);
    }
    if (j.state === 'running' && j.phases && Object.keys(j.phases).length) {
      const phases = document.createElement('div'); phases.className = 'phase-list';
      for (const key of ['download','asr','correction','summary']) {
        const phase = j.phases[key]; if (!phase) continue;
        const row = document.createElement('div'); row.className = 'phase-row'; row.dataset.state = phase.state || 'pending';
        const phaseHead = document.createElement('div'); phaseHead.className = 'phase-head';
        const label = document.createElement('b'), value = document.createElement('span');
        const percent = Math.max(0, Math.min(100, Number(phase.percent) || 0));
        label.textContent = phase.label || key; value.textContent = Math.round(percent) + '%'; phaseHead.append(label, value);
        const track = document.createElement('div'); track.className = 'phase-track'; track.setAttribute('role', 'progressbar'); track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', '100'); track.setAttribute('aria-valuenow', String(Math.round(percent)));
        const fill = document.createElement('i'); fill.style.width = percent + '%'; track.append(fill);
        const detail = document.createElement('small'); detail.className = 'phase-detail'; detail.textContent = phase.detail || '等待开始';
        row.append(phaseHead, track, detail); phases.append(row);
      }
      box.append(phases);
    }
    $('result').append(box);
  }
  const completedImports = (s.imports || []).filter(item => item.state === 'completed' && item.record_id).slice(-5).reverse();
  if (completedImports.length) {
    const heading = document.createElement('h3'); heading.textContent = '最近入库 · 课堂记录'; $('result').append(heading);
    for (const item of completedImports) {
      const link = document.createElement('a'); link.className = 'ui-button';
      link.href = '/library?' + new URLSearchParams({recording: item.record_id});
      link.textContent = `${item.title || '课堂记录'} · 阅读原文 / 打开课堂笔记`;
      $('result').append(link);
    }
  }
  if (s.lesson_course === $('courses').value) updateLessonStatuses(s.lesson_statuses || {});
  updateSelection();
}
async function refresh() {
  try {
    const course = $('courses').value;
    const r = await fetch('/api/status' + (course ? '?course=' + encodeURIComponent(course) : '')), s = await r.json();
    if (continuePendingReplay(s)) return;
    $('state').textContent = s.message;
    $('identity').textContent = s.identity?.name && s.authenticated ? '已连接：' + s.identity.name + ' · ' + s.term + ' 学期' : '';
    $('reconnect').hidden = !s.credentials_saved;
    $('connect').disabled = $('reconnect').disabled = ['connecting','syncing','running'].includes(s.state);
    if (s.identity && !accountCollapsed) {
      $('accountDetails').open = false;
      accountCollapsed = true;
    }
    if (s.state === 'error') $('accountDetails').open = true;
    if (new URLSearchParams(location.search).has('returnTo')) $('accountDetails').open = true;
    $('accountLabel').textContent = s.identity?.name && s.authenticated ? '账号已连接 · ' + s.identity.name : '统一身份认证';
    window.courseStatus = s;
    renderCourseState(s);
  } catch { $('state').textContent = '本机课程同步服务尚未连接'; }
}
async function checkSession() {
  if (sessionCheckInFlight || Date.now() - lastSessionCheck < 60000) return;
  sessionCheckInFlight = true;
  lastSessionCheck = Date.now();
  try {
    await post('/api/ensure-session', {replay_only:!!pendingReplayURL()});
    replayLoginSubmitted = true;
    await refresh();
  } catch (error) { notice(error.message); }
  finally { sessionCheckInFlight = false; }
}
$('login').addEventListener('submit', async e => {
  e.preventDefault(); notice();
  try {
    await post('/api/connect', {username:$('username').value,password:$('password').value,save:$('save').checked,
      replay_only:!!pendingReplayURL()});
    replayLoginSubmitted = true;
    $('password').value = ''; accountCollapsed = false; await refresh();
  } catch (e) { notice(e.message); }
});
$('reconnect').onclick = async () => {
  notice();
  try { await post('/api/reconnect', {replay_only:!!pendingReplayURL()}); replayLoginSubmitted = true; await refresh(); } catch (e) { notice(e.message); }
};
$('resumeQueue').onclick = async () => { try { await post('/api/resume'); await refresh(); } catch (e) { notice(e.message); } };
$('courses').onchange = () => { clearLessons(); if ($('courses').value) loadLessons(); };
$('loadLessons').onclick = loadLessons;
$('allowSingle').onchange = renderLessons;
$('refresh').onclick = async () => {
  notice();
  try { await post('/api/refresh'); await refresh(); } catch (e) { notice(e.message); }
};
let optionsSaveTimer = 0, optionsDirty = false;
function selectedOptions() {
  const existing = window.courseStatus?.options || {};
  const summary = {...(existing.summary_provider || {}), base_url:$('summaryBase').value.trim(), model:$('summaryModel').value.trim()};
  const correction = {...(existing.correction_provider || summary), base_url:$('correctionBase').value.trim() || summary.base_url, model:$('correctionModel').value.trim() || summary.model};
  return {generate_summary:$('generateSummary').checked, ai_correction:$('aiCorrection').checked,
    summary_provider:summary, correction_provider:correction};
}
async function saveOptions() {
  clearTimeout(optionsSaveTimer);
  const values = selectedOptions();
  $('optionsStatus').textContent = '正在保存到本机…';
  try {
    await post('/api/options', values);
    if (JSON.stringify(values) === JSON.stringify(selectedOptions())) {
      optionsDirty = false;
      if (window.courseStatus) window.courseStatus.options = {...window.courseStatus.options, ...values};
      $('optionsStatus').textContent = '已保存';
    }
  } catch (error) { $('optionsStatus').textContent = `尚未保存：${error.message}`; throw error; }
}
for (const id of ['summaryBase','summaryModel','correctionBase','correctionModel','generateSummary','aiCorrection']) {
  $(id).addEventListener('input', () => {
    optionsDirty = true;
    clearTimeout(optionsSaveTimer);
    $('optionsStatus').textContent = '配置已修改，等待保存…';
    optionsSaveTimer = window.setTimeout(() => saveOptions().catch(() => {}), 400);
  });
}
$('saveOptions').onclick = () => saveOptions().catch(error => notice(error.message));
window.addEventListener('pagehide', () => {
  if (!optionsDirty || !optionsLoaded) return;
  fetch('/api/options', {method:'POST', headers:{'Content-Type':'application/json','X-QwenASR-Client':'smartclass'},
    body:JSON.stringify(selectedOptions()), keepalive:true}).catch(() => {});
});
$('import').onclick = async () => {
  if (busy || submitting || loadingCourse) return;
  const ids = [...selectedLessonIds];
  const course = $('courses').value;
  if (!ids.length) return;
  submitting = true; updateSelection(); notice();
  try {
    await saveOptions();
    const result = await post('/api/import', {ids});
    if ($('courses').value === course) {
      for (const id of ids) selectedLessonIds.delete(id);
      for (const input of $('lessons').querySelectorAll('input')) input.checked = selectedLessonIds.has(input.value);
    }
    notice(result.already_queued ? '所选课次已在队列中。' : `已添加 ${result.added?.length || ids.length} 个课次，可以继续选择其他课程。`, 'success');
    await refresh();
  } catch (e) { notice(e.message); }
  finally { submitting = false; updateSelection(); }
};
$('stop').onclick = async () => {
  try { await post('/api/stop'); notice('正在暂停队列，已保存的转写进度会保留…', 'success'); } catch (e) { notice(e.message); }
};
let refreshTimer = 0;
async function scheduledRefresh() {
  await refresh();
  refreshTimer = window.setTimeout(scheduledRefresh, document.hidden ? 15000 : 2000);
}
document.addEventListener('visibilitychange', () => {
  window.clearTimeout(refreshTimer);
  if (!document.hidden) checkSession();
  scheduledRefresh();
});
window.addEventListener('pageshow', () => checkSession());
checkSession();
scheduledRefresh();
