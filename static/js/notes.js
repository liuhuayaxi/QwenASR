const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = { category: new URLSearchParams(location.search).get('category') || '', categories: [], lessons: [], notes: [], selected: new Set(), template: '', epoch: 0 };
let toastTimer;
function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 4200); }
function notice(id, text, error = false) { const node = $(id); node.textContent = text; node.hidden = !text; node.classList.toggle('error', error); }
async function api(path, options = {}) { const response = await fetch(path, {cache:'no-store', ...options}); let data; try { data = await response.json(); } catch { throw new Error('服务未返回有效结果，请刷新页面'); } if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `操作未完成（${response.status}）`); return data; }
function renderCourses() { $('courses').innerHTML = state.categories.map(category => `<button class="${category === state.category ? 'selected' : ''}" data-category="${esc(category)}">${esc(category)}</button>`).join(''); }
function renderLessons() { $('lessonCount').textContent = state.lessons.length; $('lessons').innerHTML = state.lessons.map(item => `<label class="lesson-item"><input type="checkbox" data-lesson="${esc(item.id)}" ${state.selected.has(item.id) ? 'checked' : ''}><span><span class="lesson-title">${esc(item.title)}</span><span class="lesson-meta">${esc(item.date)}${item.ideashell_note_id ? ' · 已关联闪念贝壳' : ''}</span></span></label>`).join(''); $('lessonEmpty').hidden = state.lessons.length !== 0; updateSelection(); }
function updateSelection() { const count = state.selected.size; $('selectionText').textContent = count ? `已选择 ${count} 节课` : '尚未选择课次'; $('selectedForUpload').textContent = count ? `将关联 ${count} 节课` : '未指定来源课次'; $('generatePrompt').disabled = !count; $('copyPrompt').disabled = !$('promptOutput').value.trim(); }
function renderNotes() { $('noteCount').textContent = state.notes.length; $('notes').innerHTML = state.notes.map(item => { const sources = (item.source_lessons || []).map(source => `${source.title}（${source.date}）`).join('、'); return `<article class="saved-item"><div><h3 class="saved-title">${esc(item.title)}</h3><p class="saved-meta">${esc(item.created_at || '')} · ${esc(item.filename || '')}${item.exists ? '' : ' · 文件已不存在'}</p>${sources ? `<p class="saved-sources">来源课次：${esc(sources)}</p>` : '<p class="saved-sources">未指定来源课次</p>'}</div><div class="saved-actions">${item.exists ? `<a class="ui-button" href="${esc(item.download_url)}">下载 PDF</a>` : ''}<button class="ui-button danger" data-delete="${esc(item.id)}" type="button">删除</button></div></article>`; }).join(''); $('notesEmpty').hidden = state.notes.length !== 0; }
async function load() { const epoch = ++state.epoch; try { const data = await api(`/api/notes?${new URLSearchParams(state.category ? {category:state.category} : {})}`); if (epoch !== state.epoch) return; state.categories = data.categories || []; if (!state.category || !state.categories.includes(state.category)) state.category = data.category || state.categories[0] || '未分类'; if (state.category !== data.category) { const next = `/notes?${new URLSearchParams({category:state.category})}`; history.replaceState(null, '', next); const refreshed = await api(`/api/notes?${new URLSearchParams({category:state.category})}`); data.lessons = refreshed.lessons; data.notes = refreshed.notes; data.prompt_template = refreshed.prompt_template; data.storage_root = refreshed.storage_root; } state.lessons = data.lessons || []; state.notes = data.notes || []; state.template = data.prompt_template || state.template; $('promptTemplate').value = state.template; $('rootInput').value = data.storage_root || ''; $('storagePath').textContent = data.storage_root || ''; $('courseTitle').textContent = state.category || '笔记管理'; renderCourses(); renderLessons(); renderNotes(); notice('loadError',''); } catch (error) { notice('loadError', error.message, true); } }
$('courses').addEventListener('click', event => { const button = event.target.closest('[data-category]'); if (!button) return; state.category = button.dataset.category; state.selected.clear(); history.replaceState(null, '', `/notes?${new URLSearchParams({category:state.category})}`); load(); });
$('lessons').addEventListener('change', event => { const input = event.target.closest('[data-lesson]'); if (!input) return; if (input.checked) state.selected.add(input.dataset.lesson); else state.selected.delete(input.dataset.lesson); updateSelection(); });
$('clearSelection').addEventListener('click', () => { state.selected.clear(); renderLessons(); });
$('refresh').addEventListener('click', load);
$('generatePrompt').addEventListener('click', async () => { try { const result = await api('/api/notes/generate-prompt', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({category:state.category, source_ids:[...state.selected], template:$('promptTemplate').value})}); $('promptOutput').value = result.prompt; updateSelection(); await copyPrompt(); notice('promptStatus', `已生成并复制 ${result.lessons.length} 节课的提示词`); } catch (error) { notice('promptStatus', error.message, true); } });
async function copyPrompt() {
  const value = $('promptOutput').value.trim();
  if (!value) return;
  // The workbench is a local macOS service. Ask it to write the system
  // pasteboard so copying still works when Chromium denies page clipboard
  // permissions or the page is running in an embedded browser.
  try {
    const response = await fetch('/api/notes/copy-prompt', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt: value}), cache: 'no-store'
    });
    if (response.ok) { toast('提示词已复制，可以直接粘贴给 AI'); return; }
  } catch { /* Try the browser clipboard below. */ }
  const area = $('promptOutput');
  area.focus();
  area.select();
  // On localhost Chromium, the selection-based path is the most reliable
  // way to publish plain text to the system pasteboard. The async Clipboard
  // API is retained as a fallback for browsers that disable execCommand.
  let copied = false;
  const onCopy = event => {
    event.preventDefault();
    event.clipboardData?.setData('text/plain', value);
  };
  document.addEventListener('copy', onCopy);
  try { copied = document.execCommand('copy'); } catch { copied = false; }
  document.removeEventListener('copy', onCopy);
  if (!copied) {
    try { await navigator.clipboard.writeText(value); copied = true; } catch { copied = false; }
  }
  if (!copied) throw new Error('浏览器未允许复制，请手动复制结果框中的文字');
  toast('提示词已复制，可以直接粘贴给 AI');
}
$('copyPrompt').addEventListener('click', async () => { try { await copyPrompt(); } catch (error) { notice('promptStatus', error.message, true); } });
$('saveTemplate').addEventListener('click', async () => { try { const result = await api('/api/notes/settings', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({prompt_template:$('promptTemplate').value})}); state.template = result.prompt_template; toast('提示词模板已保存'); } catch (error) { notice('promptStatus', error.message, true); } });
$('chooseRoot').addEventListener('click', async () => { try { const result = await api('/api/notes/choose-root'); if (!result.cancelled) $('rootInput').value = result.storage_root; } catch (error) { toast(error.message); } });
$('saveRoot').addEventListener('click', async () => { try { const result = await api('/api/notes/settings', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({storage_root:$('rootInput').value})}); $('storagePath').textContent = result.storage_root; $('rootInput').value = result.storage_root; toast('笔记保存路径已更新'); } catch (error) { toast(error.message); } });
$('uploadForm').addEventListener('submit', async event => { event.preventDefault(); const file = $('noteFile').files[0]; if (!file) return; const body = new FormData(); body.append('category', state.category); body.append('title', $('noteTitle').value.trim()); body.append('source_ids', JSON.stringify([...state.selected])); body.append('file', file); $('uploadStatus').textContent = '正在保存…'; try { const result = await api('/api/notes/upload', {method:'POST', body}); $('noteTitle').value = ''; $('noteFile').value = ''; $('uploadStatus').textContent = ''; toast(`已保存“${result.note.title}”`); await load(); } catch (error) { $('uploadStatus').textContent = error.message; } });
$('notes').addEventListener('click', async event => { const button = event.target.closest('[data-delete]'); if (!button) return; if (!window.confirm('删除这份笔记？文件会移入废纸篓。')) return; button.disabled = true; try { await api(`/api/notes/${button.dataset.delete}/delete`, {method:'POST'}); toast('笔记已移入废纸篓'); await load(); } catch (error) { toast(error.message); button.disabled = false; } });
load();
