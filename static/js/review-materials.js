// Review metadata and prompts retain their existing endpoints and files.
export function initReviewMaterials(onData, reload) {
const $ = id => document.getElementById(id === 'toast' ? id : 'review-' + id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = { category: new URLSearchParams(location.search).get('category') || '', categories: [], lessons: [], notes: [], selected: new Set(), template: '', epoch: 0 };
let toastTimer;
function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 4200); }
function notice(id, text, error = false) { const node = $(id); node.textContent = text; node.hidden = !text; node.classList.toggle('error', error); }
async function api(path, options = {}) { const response = await fetch(path, {cache:'no-store', ...options}); let data; try { data = await response.json(); } catch { throw new Error('服务未返回有效结果，请刷新页面'); } if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `操作未完成（${response.status}）`); return data; }
function renderLessons() { $('lessonCount').textContent = state.lessons.length; $('lessons').innerHTML = state.lessons.map(item => `<label class="lesson-item"><input type="checkbox" data-lesson="${esc(item.id)}" ${state.selected.has(item.id) ? 'checked' : ''}><span><span class="lesson-title">${esc(item.title)}</span><span class="lesson-meta">${esc(item.date)}${item.ideashell_note_id ? ' · 已关联闪念贝壳' : ''}</span></span></label>`).join(''); $('lessonEmpty').hidden = state.lessons.length !== 0; updateSelection(); }
function updateSelection() { const count = state.selected.size; $('selectionText').textContent = count ? `已选择 ${count} 节课` : '尚未选择课次'; $('selectedForUpload').textContent = count ? `将关联 ${count} 节课` : '未指定来源课次'; $('generatePrompt').disabled = !count; $('copyPrompt').disabled = !$('promptOutput').value.trim(); }
function renderNotes() { onData({notes: state.notes, categories: state.categories}); }
async function load(category = state.category) {
  const epoch = ++state.epoch;
  if (category !== state.category) { state.selected.clear(); $('promptOutput').value = ''; onData({notes: [], categories: []}); }
  state.category = category;
  try {
    const data = await api(`/api/notes?${new URLSearchParams({category})}`);
    if (epoch !== state.epoch) return;
    state.categories = data.categories || [];
    const sameCourse = data.category === category;
    state.lessons = sameCourse ? data.lessons || [] : [];
    state.notes = sameCourse ? data.notes || [] : [];
    state.template = data.prompt_template || state.template;
    if (document.activeElement !== $('promptTemplate')) $('promptTemplate').value = state.template;
    $('storagePath').textContent = data.storage_root || '';
    renderLessons(); renderNotes(); notice('loadError', '');
  } catch (error) { if (epoch === state.epoch) notice('loadError', `复习资料读取失败：${error.message}`, true); }
}
$('lessons').addEventListener('change', event => { const input = event.target.closest('[data-lesson]'); if (!input) return; if (input.checked) state.selected.add(input.dataset.lesson); else state.selected.delete(input.dataset.lesson); updateSelection(); });
$('clearSelection').addEventListener('click', () => { state.selected.clear(); renderLessons(); });
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
$('uploadForm').addEventListener('submit', async event => { event.preventDefault(); const file = $('noteFile').files[0]; if (!file) return; const body = new FormData(); body.append('category', state.category); body.append('title', $('noteTitle').value.trim()); body.append('source_ids', JSON.stringify([...state.selected])); body.append('file', file); $('uploadStatus').textContent = '正在保存…'; try { const result = await api('/api/notes/upload', {method:'POST', body}); $('noteTitle').value = ''; $('noteFile').value = ''; $('uploadStatus').textContent = ''; toast(`已保存“${result.note.title}”`); await reload(); } catch (error) { $('uploadStatus').textContent = error.message; } });
return {load, selected: state.selected};
}
