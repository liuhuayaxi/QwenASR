import { IDEASHELL_SERVERS as servers } from './ideashell-config.js?v=20261007';

const $ = (id) => document.getElementById(id);
const storageKey = 'qwen-asr-ideashell-settings-v1';
const state = {category: new URLSearchParams(location.search).get('category') || '未分类', files: [], categories: [], selected: null, preview: null, sharedFolder: false, fileAction: null, replacing: null, uploading: false, syncing: false, managing: false, categoryAction: null, epoch: 0, modalEpoch: 0};
let pollTimer, connectionTimer, toastTimer, previewTimer;
let previewEpoch = 0, matchesEpoch = 0;
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const size = bytes => bytes < 1024 ** 2 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
const query = (values) => new URLSearchParams(values).toString();
const server = () => servers[$('region').value];
function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 4200); }
function notice(id, text, error = false) { const node = $(id); node.textContent = text; node.hidden = !text; node.classList.toggle('error', error); }
async function api(path, body) {
  const options = {cache: 'no-store'};
  if (body !== undefined) {
    options.method = 'POST'; options.headers = {'X-QwenASR-Client': 'course-files'};
    if (body instanceof FormData) options.body = body;
    else { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
  }
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch { throw new Error('服务未返回有效结果，请刷新页面或重新启动工作台'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `操作未完成（${response.status}）`);
  return data;
}
async function busy(button, action) {
  button.disabled = true;
  try { await action(); } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}
function renderCourses() {
  $('courses').innerHTML = state.categories.map(category => `<button class="${category === state.category ? 'selected' : ''}" aria-current="${category === state.category ? 'page' : 'false'}" data-category="${esc(category)}">${esc(category)}</button>`).join('');
  updateCategoryControls();
}
function updateCategoryControls() {
  const working = state.uploading || state.syncing || state.managing;
  const fixed = state.category === '未分类';
  for (const id of ['renameCategory', 'deleteCategory']) {
    $(id).disabled = fixed || working || !state.categories.includes(state.category);
    $(id).title = fixed ? '“未分类”是默认分类，不能重命名或删除' : working ? '请等待当前操作完成' : '';
  }
}
function renderFiles() {
  const filter = $('search').value.trim().toLocaleLowerCase();
  const files = state.files.filter(file => file.name.toLocaleLowerCase().includes(filter) && (!$('kindFilter').value || file.kind === $('kindFilter').value));
  $('fileCount').textContent = state.files.length;
  if (!files.length) {
    $('files').innerHTML = `<div class="empty"><b>${state.files.length ? '没有符合筛选条件的文件' : '这门课的资料，从第一份文件开始'}</b>${state.files.length ? '试试其他文件名或类型。' : '添加课件、讲义或作业，再按需转换、复制与同步。'}</div>`;
    return;
  }
  $('files').innerHTML = files.map(file => {
    const conversion = file.conversion || {};
    const ready = conversion.status === 'completed';
    const needsMineru = ready && file.needs_mineru_upgrade;
    const settingsChanged = ready && file.mineru_settings_changed;
    const tier = {flash: '极速', basic: '基础', standard: '标准', advanced: '高级'}[conversion.mineru_settings?.tier];
    const working = ['queued', 'running', 'stopping'].includes(conversion.status);
    const failed = ['error', 'interrupted', 'paused', 'cancelled'].includes(conversion.status);
    const latest = (file.sync || []).at(-1);
    const pending = latest && ['sending', 'unknown'].includes(latest.status);
    const syncText = latest ? latest.status === 'synced' ? `已上传到 ${latest.title}` : '上次上传结果待核对，请先在闪念贝壳查看附件。' : '尚未上传到闪念贝壳';
    const original = `/api/course-files/${file.id}/download?${query({category: state.category, variant: 'original'})}`;
    const pdf = `/api/course-files/${file.id}/download?${query({category: state.category, variant: 'pdf'})}`;
    const fileControls = state.sharedFolder ? `<button class="ui-button" data-action="rename">重命名</button><button class="ui-button" data-action="replace">替换原件</button><button class="ui-button danger" data-action="delete">移到废纸篓</button>` : '';
    return `<article class="file-card" data-id="${esc(file.id)}"><div class="file-icon">${esc(file.name.split('.').at(-1).slice(0,6).toUpperCase())}</div><div class="file-main"><h3 class="file-title">${esc(file.name)}</h3><div class="file-meta"><span class="badge">${esc(file.kind)}</span><span>${size(file.size)}</span>${file.relative_path && file.relative_path !== file.name ? `<span>${esc(file.relative_path)}</span>` : ''}<span>${esc(file.created_at.slice(0,10))}</span><span>${esc(syncText)}</span></div><p class="file-status ${failed || pending ? 'error' : ''}">${esc(ready ? `PDF 已就绪 · ${conversion.pages ? `${conversion.pages} 页` : '已关联文件夹版本'} · ${needsMineru ? '旧版识别结果，可升级为 MinerU 4' : conversion.external ? '与本机 AI 友好型文件同步' : `识别完成${tier ? ` · ${tier}档` : ''}${settingsChanged ? ' · 参数已更新，可重新识别' : ''}`}` : `${conversion.stage || '原件已保存，可生成 AI 友好 PDF'}${working ? ` · ${conversion.progress || 0}%` : ''}`)}${(conversion.warnings || []).map(w => `<br>${esc(w)}`).join('')}</p><div class="actions">${conversion.status && conversion.status !== 'none' ? `<a class="ui-button" href="/tasks">查看识别任务</a>` : ''}<button class="ui-button" data-action="convert" ${working || (ready && !needsMineru && !settingsChanged) ? 'disabled' : ''}>${needsMineru ? '升级识别版本' : settingsChanged ? '按新参数重新识别' : ready ? '✓ PDF 已就绪' : working ? '正在转换 PDF…' : failed ? '重新转换 PDF' : '生成 AI 友好 PDF'}</button><a class="ui-button" href="${esc(original)}" download>下载原件</a><button class="ui-button" data-action="copy-original">复制原件</button>${ready ? `<a class="ui-button" href="${esc(pdf)}" download>下载 PDF</a><button class="ui-button" data-action="copy-pdf">复制 PDF</button>` : ''}${fileControls}<button class="ui-button sync-action" data-action="sync">匹配笔记并上传 ↗</button></div></div></article>`;
  }).join('');
}
async function load() {
  const epoch = ++state.epoch;
  clearTimeout(pollTimer);
  try {
    const data = await api(`/api/course-files?${query({category: state.category})}`);
    if (epoch !== state.epoch) return;
    state.files = data.files; state.categories = data.categories; state.sharedFolder = Boolean(data.shared_folder);
    if (state.category !== data.category) {
      state.category = data.category;
      history.replaceState(null, '', `/course-files?${query({category: state.category})}`);
    }
    $('courseTitle').textContent = state.category;
    $('courseMeta').textContent = state.sharedFolder ? '与本机课程文件夹实时同步，原件在外层，AI 友好型文件单独保存。' : '原件与 PDF 成对保存，和课堂笔记建立关联。';
    $('directory').textContent = `保存位置：${data.directory}`;
    $('capabilities').textContent = `Word / PPT / Excel 转 PDF：${data.capabilities.office ? '已具备转换工具' : '需安装 LibreOffice'} · MinerU 识别：${data.capabilities.mineru ? '可用' : '需配置 MinerU'}`;
    notice('loadError', ''); renderCourses(); renderFiles();
    const asset = new URLSearchParams(location.search).get('asset');
    if (asset && !state.revealedAsset) {
      const card = [...$('files').querySelectorAll('[data-id]')].find(node => node.dataset.id === asset);
      if (card) { card.scrollIntoView({block: 'center'}); card.tabIndex = -1; card.focus({preventScroll: true}); state.revealedAsset = asset; }
    }
    if (state.files.some(f => ['queued', 'running', 'stopping'].includes(f.conversion?.status))) pollTimer = setTimeout(load, 2500);
  } catch (error) { if (epoch === state.epoch) notice('loadError', error.message, true); }
}
$('courses').addEventListener('click', event => {
  const button = event.target.closest('[data-category]');
  if (!button || state.uploading || state.syncing || state.managing) return;
  state.category = button.dataset.category;
  history.replaceState(null, '', `/course-files?${query({category: state.category})}`);
  $('search').value = ''; notice('uploadStatus', ''); load();
});
$('newCourseForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (state.uploading || state.syncing || state.managing) return;
  const name = $('newCourse').value.trim(); if (!name) return;
  await busy(event.submitter, async () => {
    const data = await api('/api/library/categories', {name});
    state.category = data.category; $('newCourse').value = '';
    history.replaceState(null, '', `/course-files?${query({category: state.category})}`); await load();
  });
});
function openCategoryDialog(action) {
  if (state.uploading || state.syncing || state.managing || state.category === '未分类') return;
  const deleting = action === 'delete';
  state.categoryAction = {action, category: state.category};
  $('categoryDialogTitle').textContent = deleting ? '删除分类' : '重命名分类';
  $('categoryDescription').textContent = deleting
    ? `删除“${state.category}”后，其中的课程资料和录音会移入“未分类”，文件和笔记内容都会保留。`
    : `为“${state.category}”设置新名称，课程资料、录音库分类和本地课程配置会一起更新。`;
  $('categoryNameField').hidden = deleting;
  $('categoryName').disabled = deleting;
  $('categoryName').value = state.category;
  $('saveCategory').textContent = deleting ? '删除并移入未分类' : '保存名称';
  $('saveCategory').classList.toggle('danger', deleting);
  $('saveCategory').classList.toggle('primary', !deleting);
  notice('categoryError', '');
  $('categoryDialog').showModal();
  if (deleting) $('cancelCategory').focus();
  else { $('categoryName').focus(); $('categoryName').select(); }
}
function closeCategoryDialog() {
  if (!state.managing) $('categoryDialog').close();
}
$('renameCategory').addEventListener('click', () => openCategoryDialog('rename'));
$('deleteCategory').addEventListener('click', () => openCategoryDialog('delete'));
$('closeCategory').addEventListener('click', closeCategoryDialog);
$('cancelCategory').addEventListener('click', closeCategoryDialog);
$('categoryDialog').addEventListener('cancel', event => { if (state.managing) event.preventDefault(); });
$('categoryDialog').addEventListener('close', () => { state.categoryAction = null; });
$('categoryForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (state.managing || !state.categoryAction) return;
  const {action, category} = state.categoryAction;
  const name = $('categoryName').value.trim();
  if (action === 'rename' && !name) { notice('categoryError', '请输入分类名称', true); return; }
  if (action === 'rename' && name === category) { closeCategoryDialog(); return; }
  state.managing = true;
  state.epoch++;
  clearTimeout(pollTimer);
  updateCategoryControls();
  const controls = [...$('categoryForm').querySelectorAll('button,input')];
  controls.forEach(node => node.disabled = true);
  $('saveCategory').textContent = '正在保存…';
  notice('categoryError', '');
  try {
    const data = await api(`/api/library/categories/${action}`, {category, ...(action === 'rename' ? {name} : {})});
    state.category = data.category;
    state.categories = data.categories;
    state.files = [];
    state.selected = null;
    invalidatePreview();
    $('search').value = ''; $('kindFilter').value = '';
    history.replaceState(null, '', `/course-files?${query({category: state.category})}`);
    notice('uploadStatus', '');
    renderCourses(); renderFiles();
    $('courseTitle').textContent = state.category;
    $('categoryDialog').close();
    toast(action === 'delete' ? '分类已删除，资料和录音已移入“未分类”' : '分类名称已更新');
    await load();
  } catch (error) {
    notice('categoryError', error.message, true);
    await load();
  } finally {
    state.managing = false;
    controls.forEach(node => node.disabled = false);
    $('categoryName').disabled = action === 'delete';
    $('saveCategory').textContent = action === 'delete' ? '删除并移入未分类' : '保存名称';
    updateCategoryControls();
  }
});
$('refresh').addEventListener('click', load);
$('search').addEventListener('input', renderFiles);
$('kindFilter').addEventListener('change', renderFiles);
$('reveal').addEventListener('click', event => busy(event.currentTarget, () => api('/api/course-files/reveal', {category: state.category})));
$('addFiles').addEventListener('click', () => $('filesInput').click());
$('filesInput').addEventListener('change', async () => { await upload([...$('filesInput').files]); $('filesInput').value = ''; });
for (const type of ['dragover', 'dragenter']) $('dropzone').addEventListener(type, event => { event.preventDefault(); $('dropzone').classList.add('drag'); });
for (const type of ['dragleave', 'drop']) $('dropzone').addEventListener(type, event => { event.preventDefault(); $('dropzone').classList.remove('drag'); });
$('dropzone').addEventListener('drop', event => upload([...event.dataTransfer.files]));
async function upload(files) {
  if (state.uploading || state.managing || !files.length) return;
  state.uploading = true; $('addFiles').disabled = true;
  updateCategoryControls();
  const category = state.category, kind = $('uploadKind').value;
  let saved = 0, duplicates = 0; const errors = [];
  try {
    for (const [index, file] of files.entries()) {
      notice('uploadStatus', `正在保存 ${index + 1}/${files.length}：${file.name}`);
      try {
        if (file.size > 200 * 1024 ** 2) throw new Error('超过 200 MB');
        const body = new FormData(); body.append('category', category); body.append('kind', kind); body.append('file', file);
        const result = await api('/api/course-files/upload', body);
        result.duplicate ? duplicates++ : saved++;
      } catch (error) { errors.push(`${file.name}：${error.message}`); }
    }
    notice('uploadStatus', `已添加 ${saved} 个文件${duplicates ? `，${duplicates} 个重复文件已复用` : ''}${errors.length ? `\n${errors.join('\n')}` : ''}`, !!errors.length);
    await load();
  } finally { state.uploading = false; $('addFiles').disabled = false; updateCategoryControls(); }
}
$('files').addEventListener('click', event => {
  const button = event.target.closest('button[data-action]'); if (!button) return;
  const file = state.files.find(f => f.id === button.closest('[data-id]').dataset.id); if (!file) return;
  const action = button.dataset.action;
  if (action === 'sync') { openSync(file); return; }
  if (action === 'rename' || action === 'delete') { openFileDialog(file, action); return; }
  if (action === 'replace') { state.replacing = {category: state.category, file}; $('replaceInput').accept = '.' + file.name.split('.').at(-1); $('replaceInput').click(); return; }
  busy(button, async () => {
    if (action === 'convert') {
      await api(`/api/course-files/${file.id}/convert`, {category: state.category});
      toast('已加入转换队列，关闭页面后会继续处理'); await load();
    } else {
      const data = await api(`/api/course-files/${file.id}/clipboard`, {category: state.category, variant: action === 'copy-pdf' ? 'pdf' : 'original'});
      toast(`已复制文件“${data.name}”，可到闪念贝壳粘贴`);
    }
  });
});
try { const saved = JSON.parse(localStorage.getItem(storageKey) || '{}'); if (servers[saved.region]) $('region').value = saved.region; } catch { /* Use the account region selector. */ }
function openFileDialog(file, action) {
  if (state.managing || state.uploading || state.syncing) return;
  state.fileAction = {file, action, category: state.category};
  const deleting = action === 'delete';
  $('fileDialogTitle').textContent = deleting ? '移到废纸篓' : '重命名文件';
  $('fileDescription').textContent = deleting
    ? `“${file.name}”的原件及关联 AI 友好型文件会从课程目录移到本机废纸篓，页面同步移除。`
    : '新名称会直接用于本机课程文件夹中的原件。';
  $('fileName').value = file.name;
  $('fileName').hidden = deleting; $('fileNameLabel').hidden = deleting; $('fileName').disabled = deleting;
  $('saveFile').textContent = deleting ? '移到废纸篓' : '保存名称';
  $('saveFile').classList.toggle('danger', deleting); $('saveFile').classList.toggle('primary', !deleting);
  notice('fileError', ''); $('fileDialog').showModal();
  if (deleting) $('cancelFile').focus(); else { $('fileName').focus(); $('fileName').select(); }
}
for (const id of ['closeFile', 'cancelFile']) $(id).addEventListener('click', () => { if (!state.managing) $('fileDialog').close(); });
$('fileDialog').addEventListener('cancel', event => { if (state.managing) event.preventDefault(); });
$('fileForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (state.managing || !state.fileAction) return;
  const {file, action, category} = state.fileAction;
  const name = $('fileName').value.trim();
  state.managing = true; updateCategoryControls();
  const controls = [...$('fileForm').querySelectorAll('button,input')]; controls.forEach(node => node.disabled = true);
  notice('fileError', '');
  try {
    await api(`/api/course-files/${file.id}/${action}`, {category, name});
    $('fileDialog').close(); invalidatePreview();
    toast(action === 'delete' ? '文件已移到废纸篓，本机文件夹已同步' : '文件已重命名，本机文件夹已同步');
    await load();
  } catch (error) { notice('fileError', error.message, true); }
  finally { state.managing = false; controls.forEach(node => node.disabled = false); $('fileName').disabled = action === 'delete'; updateCategoryControls(); }
});
$('replaceInput').addEventListener('change', async () => {
  const replacement = $('replaceInput').files[0], selection = state.replacing;
  $('replaceInput').value = '';
  if (!replacement || !selection || state.uploading || state.managing) return;
  state.uploading = true; updateCategoryControls();
  notice('uploadStatus', `正在替换“${selection.file.name}”…`);
  try {
    const body = new FormData(); body.append('category', selection.category); body.append('file', replacement);
    await api(`/api/course-files/${selection.file.id}/replace`, body);
    invalidatePreview(); notice('uploadStatus', '原件已替换并同步到文件夹，旧版本已保留在废纸篓。'); await load();
  } catch (error) { notice('uploadStatus', error.message, true); }
  finally { state.uploading = false; state.replacing = null; updateCategoryControls(); }
});

function validNoteId() { return /^[a-f0-9]{32}$/.test($('noteId').value.trim()); }
function updateSyncControls() {
  $('previewButton').disabled = state.syncing || state.previewing || !validNoteId();
  $('previewButton').textContent = state.previewing ? '正在预览…' : state.preview ? '重新预览上传目标' : '预览上传目标';
  $('syncButton').disabled = state.syncing || !state.preview || state.preview.duplicate;
  const hint = state.syncing ? '正在上传附件，请稍候…'
    : state.previewing ? '正在读取目标笔记并检查文件，预览成功后即可确认上传。'
    : state.preview?.duplicate ? '这个文件版本已上传到所选笔记，无需重复上传。'
    : state.preview ? '请核对上方文件和目标笔记，然后点击“确认上传附件”。'
    : validNoteId() ? '请先完成上传目标预览；若预览失败，按提示处理后点击“预览上传目标”重试。'
    : $('noteId').value.trim() ? '笔记编号须为完整的 32 位小写字母与数字编号，也可以直接选择上方笔记。'
    : '请选择目标笔记，预览成功后即可确认上传附件。';
  $('syncHint').textContent = hint;
  $('syncButton').title = $('syncButton').disabled ? hint : '';
}
function invalidatePreview() {
  previewEpoch++; clearTimeout(previewTimer);
  state.preview = null; state.previewing = false; $('preview').hidden = true;
  updateSyncControls();
}
function highlightNote() {
  const ident = $('noteId').value.trim();
  $('candidates').querySelectorAll('[data-note]').forEach(node => {
    const selected = node.dataset.note === ident;
    node.classList.toggle('selected', selected); node.setAttribute('aria-pressed', String(selected));
  });
}
function schedulePreview() {
  if (validNoteId()) previewTimer = setTimeout(previewTarget, 400);
}
async function openSync(file) {
  state.modalEpoch++; state.selected = file;
  $('syncFileName').textContent = file.name;
  $('variant').querySelector('[value="pdf"]').disabled = file.conversion?.status !== 'completed';
  $('variant').value = file.conversion?.status === 'completed' ? 'pdf' : 'original';
  $('noteQuery').value = `${state.category} ${file.name.replace(/\.[^.]+$/, '')}`;
  $('noteId').value = ''; $('candidates').innerHTML = ''; notice('syncStatus', ''); notice('matchStatus', ''); invalidatePreview();
  $('authorize').hidden = true; $('syncDialog').showModal();
  await Promise.all([matches(false), connectionStatus()]);
}
function closeSync() { if (state.syncing) return; state.modalEpoch++; clearTimeout(connectionTimer); invalidatePreview(); $('syncDialog').close(); }
$('closeSync').addEventListener('click', closeSync);
$('syncDialog').addEventListener('cancel', event => { if (state.syncing) event.preventDefault(); else { state.modalEpoch++; clearTimeout(connectionTimer); invalidatePreview(); } });
async function connectionStatus() {
  const epoch = state.modalEpoch;
  try {
    const status = await api(`/api/ideashell/status?${query({server_url: server()})}`);
    if (epoch !== state.modalEpoch) return;
    showConnection(status);
  } catch (error) { if (epoch === state.modalEpoch) $('connectionStatus').textContent = error.message; }
}
function showConnection(status) {
  const names = {connected: '已连接，凭证保存在本机', saved: '已保存凭证，将自动恢复连接', disconnected: '首次使用请连接', connecting: '正在恢复连接…', authorization_required: '需要授权，请打开授权页面', error: '连接暂未完成，凭证已保留'};
  $('connectionStatus').textContent = status.error || names[status.status] || status.status;
  $('authorize').hidden = !status.authorization_url;
  if (status.authorization_url) $('authorize').href = status.authorization_url;
  clearTimeout(connectionTimer);
  if ($('syncDialog').open && ['connecting', 'authorization_required'].includes(status.status)) connectionTimer = setTimeout(connectionStatus, 2500);
}
$('connect').addEventListener('click', event => busy(event.currentTarget, async () => {
  const epoch = state.modalEpoch;
  const status = await api('/api/ideashell/connect', {server_url: server(), callback_url: `${location.origin}/api/ideashell/oauth/callback`});
  if (epoch !== state.modalEpoch) return;
  showConnection(status);
  if (status.authorization_url) toast('请点击“打开授权页面”，登录并授权闪念贝壳');
}));
$('region').addEventListener('change', () => {
  state.modalEpoch++; clearTimeout(connectionTimer); $('noteId').value = ''; invalidatePreview(); notice('syncStatus', ''); notice('matchStatus', '');
  localStorage.setItem(storageKey, JSON.stringify({region: $('region').value})); matches(false); connectionStatus();
});
$('variant').addEventListener('change', () => { invalidatePreview(); notice('syncStatus', ''); schedulePreview(); });
$('noteId').addEventListener('input', () => { invalidatePreview(); notice('syncStatus', ''); highlightNote(); schedulePreview(); });
async function matches(search) {
  const epoch = state.modalEpoch, requestEpoch = ++matchesEpoch;
  if (search) notice('matchStatus', '正在搜索闪念贝壳…');
  try {
    const data = await api(`/api/course-files/${state.selected.id}/matches`, {category: state.category, server_url: server(), search, query: $('noteQuery').value});
    if (epoch !== state.modalEpoch || requestEpoch !== matchesEpoch) return;
    $('candidates').innerHTML = data.candidates.map(note => `<button class="ui-button candidate" data-note="${esc(note.note_id)}">${esc(note.title)}<small>${esc(note.reason)} · ${esc(note.note_id)}</small></button>`).join('') || '<p class="small muted">暂无候选笔记，可先连接并搜索。</p>';
    highlightNote();
    notice('matchStatus', data.warning || '', !!data.warning);
  } catch (error) { if (epoch === state.modalEpoch && requestEpoch === matchesEpoch) notice('matchStatus', error.message, true); }
  finally { if (epoch === state.modalEpoch && search) await connectionStatus(); }
}
$('searchNotes').addEventListener('click', event => busy(event.currentTarget, () => matches(true)));
$('candidates').addEventListener('click', event => {
  const button = event.target.closest('[data-note]'); if (!button || state.syncing) return;
  $('noteId').value = button.dataset.note; invalidatePreview(); highlightNote();
  previewTarget();
});
$('previewButton').addEventListener('click', previewTarget);
async function previewTarget() {
  if (!state.selected || state.syncing || state.previewing || !validNoteId()) return;
  invalidatePreview(); notice('syncStatus', '正在读取目标笔记与附件能力…');
  const epoch = state.modalEpoch, requestEpoch = previewEpoch, assetId = state.selected.id;
  const current = () => epoch === state.modalEpoch && requestEpoch === previewEpoch;
  const selection = {category: state.category, variant: $('variant').value, server_url: server(), note_id: $('noteId').value.trim()};
  state.previewing = true; updateSyncControls();
  try {
    const data = await api(`/api/course-files/${assetId}/preview`, selection);
    if (!current()) return;
    state.preview = data; $('preview').hidden = false;
    $('preview').innerHTML = `<b>${data.duplicate ? '这个版本已上传，无需重复发送' : '将追加以下附件'}</b><br>文件：${esc(data.filename)}（${size(data.size)}）<br>课程：${esc(data.category)}<br>目标笔记：${esc(data.title)}<br><span class="small">${esc(data.note_id)}</span>`;
    notice('syncStatus', '');
  } catch (error) { if (current()) notice('syncStatus', error.message, true); }
  finally { if (current()) { state.previewing = false; updateSyncControls(); await connectionStatus(); } }
}
$('syncButton').addEventListener('click', async () => {
  if (!state.preview || state.syncing) return;
  state.syncing = true;
  updateCategoryControls();
  const controls = [...$('syncDialog').querySelectorAll('button,input,select')];
  controls.forEach(node => node.disabled = true);
  notice('syncStatus', '正在上传附件，请稍候…');
  try {
    const result = await api(`/api/course-files/${state.selected.id}/sync`, {token: state.preview.token});
    notice('syncStatus', result.duplicate ? '该文件版本已上传，已跳过重复发送。' : '附件上传成功，已记录与目标笔记的关联。');
    toast('闪念贝壳附件已同步'); invalidatePreview(); await load();
  } catch (error) { notice('syncStatus', `${error.message}\n如果网络中断，请先在闪念贝壳查看附件是否已收到，再重新预览上传。`, true); invalidatePreview(); await load(); }
  finally { state.syncing = false; updateCategoryControls(); controls.forEach(node => node.disabled = false); updateSyncControls(); $('variant').querySelector('[value="pdf"]').disabled = state.selected.conversion?.status !== 'completed'; await connectionStatus(); }
});
let liveRefreshTimer;
const libraryEvents = new EventSource('/api/library/events');
libraryEvents.onmessage = () => {
  clearTimeout(liveRefreshTimer);
  liveRefreshTimer = setTimeout(() => {
    if (!state.managing && !state.uploading && !state.syncing) load();
  }, 150);
};
// EventSource reconnects on its own; a fallback also recovers missed changes.
setInterval(() => {
  if (document.visibilityState === 'visible' && libraryEvents.readyState !== EventSource.OPEN
      && !state.managing && !state.uploading && !state.syncing) load();
}, 3000);
document.addEventListener('visibilitychange', () => { if (!document.hidden && !state.managing) load(); });
load();
