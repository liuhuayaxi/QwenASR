const $ = id => document.getElementById(id);
let currentGroups = [], courseVersion = '', optionsLoaded = false;
let busy = false, loadingCourse = '', loadSerial = 0, accountCollapsed = false;
function notice(message = '') { $('notice').textContent = message; }
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
  $('import').disabled = busy || !!loadingCourse || !document.querySelector('#lessons input:checked:not(:disabled)');
  $('loadLessons').disabled = busy || !!loadingCourse || !$('courses').value;
}
function clearLessons() {
  loadSerial++;
  loadingCourse = '';
  currentGroups = [];
  $('lessons').replaceChildren();
  updateSelection();
}
function renderLessons() {
  const chosen = new Set([...document.querySelectorAll('#lessons input:checked')].map(e => e.value));
  $('lessons').replaceChildren();
  for (const g of currentGroups) {
    const row = document.createElement('label');
    row.className = 'lesson';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.value = g.id;
    checkbox.disabled = g.videos.length === 1 && !$('allowSingle').checked;
    checkbox.checked = chosen.has(g.id) && !checkbox.disabled;
    checkbox.onchange = updateSelection;
    const text = document.createElement('div'), title = document.createElement('strong'), detail = document.createElement('small');
    title.textContent = g.course_name + ' · ' + g.date + ' ' + g.start.slice(11,16) + '–' + g.stop.slice(11,16);
    detail.textContent = g.videos.length === 2
      ? `第 ${g.periods.join('、')} 节 · 合计约 ${Math.round(g.duration / 60)} 分钟 → 一份笔记`
      : `第 ${g.periods[0]} 节 · 暂无相邻回放配对`;
    text.append(title, detail); row.append(checkbox, text); $('lessons').append(row);
  }
  if (!currentGroups.length) $('lessons').textContent = '此课程暂无可用回放。';
  updateSelection();
}
async function loadLessons() {
  const key = $('courses').value;
  if (!key) return;
  const serial = ++loadSerial;
  loadingCourse = key;
  $('lessons').textContent = '正在获取回放列表…';
  updateSelection(); notice();
  try {
    const response = await fetch('/api/lessons?course=' + encodeURIComponent(key));
    const result = await response.json();
    if (serial !== loadSerial) return;
    if (!response.ok) throw Error(result.detail);
    currentGroups = result.groups;
    renderLessons();
  } catch (e) {
    if (serial === loadSerial) { currentGroups = []; $('lessons').textContent = e.message; }
  } finally {
    if (serial === loadSerial) { loadingCourse = ''; updateSelection(); }
  }
}
function renderCourseState(s) {
  busy = ['connecting','syncing','running'].includes(s.state);
  $('refresh').disabled = busy || !s.identity;
  $('courses').disabled = busy;
  $('stop').hidden = s.state !== 'running';
  $('resumeQueue').hidden = busy || !(s.queue?.remaining);
  $('resumeQueue').disabled = !s.identity;
  const version = JSON.stringify([s.course_revision, s.courses.map(c => [c.key, c.VideoTotal])]);
  if (version !== courseVersion) {
    courseVersion = version;
    const selected = $('courses').value;
    clearLessons();
    $('courses').replaceChildren(new Option('选择课程…', ''));
    for (const c of s.courses) {
      const duplicate = s.courses.filter(v => v.CourseName === c.CourseName && v.TeacherName === c.TeacherName).length > 1;
      const label = c.CourseName + ' · ' + (c.TeacherName || '') + ' · ' + c.VideoTotal + ' 节'
        + (duplicate ? ' · 教学班 ' + c.ClassID : '');
      $('courses').add(new Option(label, c.key));
    }
    if (s.courses.some(c => c.key === selected)) {
      $('courses').value = selected;
      if (!busy) loadLessons();
    }
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
  for (const j of [...s.imports].reverse()) {
    const box = document.createElement('div'); box.className = 'job';
    const head = document.createElement('div'); head.className = 'job-head';
    const title = document.createElement('strong'), badge = document.createElement('span'), message = document.createElement('p');
    title.textContent = j.title; badge.className = 'job-percent'; badge.textContent = Math.round(Number(j.progress) || 0) + '%';
    message.textContent = j.message; head.append(title, badge); box.append(head, message);
    if (j.metrics?.first_text_seconds != null) { const timing = document.createElement('p'); timing.textContent = '首段文字 ' + j.metrics.first_text_seconds.toFixed(1) + ' 秒 · ASR 排队 ' + (j.metrics.asr_queue_seconds || 0).toFixed(1) + ' 秒 · 推理 ' + (j.metrics.asr_inference_seconds || 0).toFixed(1) + ' 秒'; box.append(timing); }
    if (j.ai_errors) { const warning = document.createElement('p'); warning.textContent = j.ai_errors + ' 段精修未完成，原文已保留，可在顶部任务中心重试'; box.append(warning); }
    if (j.state === 'running') {
      const meter = document.createElement('progress'); meter.className = 'overall-progress'; meter.max = 100; meter.value = j.progress || 0; meter.setAttribute('aria-label', '课次总体进度'); box.append(meter);
    }
    if (j.phases && Object.keys(j.phases).length) {
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
    if (j.state === 'completed') {
      for (const [name, url] of [['下载笔记',j.summary], ['临时音频与转写 ZIP',j.bundle]]) {
        if (!url) continue;
        const a = document.createElement('a'); a.href = url; a.textContent = name;
        a.target = '_blank'; a.rel = 'noopener'; box.append(a);
      }
    }
    $('result').append(box);
  }
  updateSelection();
}
async function refresh() {
  try {
    const r = await fetch('/api/status'), s = await r.json();
    $('state').textContent = s.message;
    $('identity').textContent = s.identity?.name ? '已连接：' + s.identity.name + ' · ' + s.term + ' 学期' : '';
    $('reconnect').hidden = !s.credentials_saved;
    $('connect').disabled = $('reconnect').disabled = ['connecting','syncing','running'].includes(s.state);
    if (s.identity && !accountCollapsed) {
      $('accountDetails').open = false;
      accountCollapsed = true;
    }
    if (s.state === 'error') $('accountDetails').open = true;
    $('accountLabel').textContent = s.identity?.name ? '账号已连接 · ' + s.identity.name : '统一身份认证';
    window.courseStatus = s;
    renderCourseState(s);
  } catch { $('state').textContent = '本机课程同步服务尚未连接'; }
}
$('login').addEventListener('submit', async e => {
  e.preventDefault(); notice();
  try {
    await post('/api/connect', {username:$('username').value,password:$('password').value,save:$('save').checked});
    $('password').value = ''; accountCollapsed = false; await refresh();
  } catch (e) { notice(e.message); }
});
$('reconnect').onclick = async () => {
  notice();
  try { await post('/api/reconnect'); await refresh(); } catch (e) { notice(e.message); }
};
$('resumeQueue').onclick = async () => { try { await post('/api/resume'); await refresh(); } catch (e) { notice(e.message); } };
$('courses').onchange = () => { clearLessons(); if ($('courses').value) loadLessons(); };
$('loadLessons').onclick = loadLessons;
$('allowSingle').onchange = () => { if (!loadingCourse) renderLessons(); };
$('refresh').onclick = async () => {
  notice();
  try { await post('/api/refresh'); await refresh(); } catch (e) { notice(e.message); }
};
$('import').onclick = async () => {
  const ids = [...document.querySelectorAll('#lessons input:checked:not(:disabled)')].map(e => e.value);
  $('import').disabled = true; notice();
  try {
    const p = {...(window.courseStatus?.options?.summary_provider || {}), base_url:$('summaryBase').value.trim(), model:$('summaryModel').value.trim()};
    const correction = {...(window.courseStatus?.options?.correction_provider || p), base_url:$('correctionBase').value.trim() || p.base_url, model:$('correctionModel').value.trim() || p.model};
    await post('/api/options', {generate_summary:$('generateSummary').checked, ai_correction:$('aiCorrection').checked, summary_provider:p, correction_provider:correction});
    await post('/api/import', {ids}); await refresh();
  } catch (e) { notice(e.message); updateSelection(); }
};
$('stop').onclick = async () => {
  try { await post('/api/stop'); notice('正在停止，已保存的转写进度会保留…'); } catch (e) { notice(e.message); }
};
let refreshTimer = 0;
async function scheduledRefresh() {
  await refresh();
  refreshTimer = window.setTimeout(scheduledRefresh, document.hidden ? 15000 : 2000);
}
document.addEventListener('visibilitychange', () => {
  window.clearTimeout(refreshTimer);
  scheduledRefresh();
});
scheduledRefresh();
