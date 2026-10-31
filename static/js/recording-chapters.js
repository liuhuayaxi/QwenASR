export function chapterValue(record) {
  return (record?.chapters?.length ? record.chapters.join('；') : record?.chapter || '').trim();
}

export function chapterLabel(record) {
  return chapterValue(record) || '章节待确认';
}

export function createChapterControls({getSession, onSaved, refreshLibrary, toast, root = document, fetcher = fetch}) {
  const get = id => root.querySelector(`#${id}`);
  const filter = get('libraryChapter'), editor = get('noteChapterEditor');
  const input = get('noteChapterInput'), status = get('noteChapterStatus');
  const save = get('saveNoteChapter'), suggest = get('suggestNoteChapter');
  let sessionId = '', draftVersion = 0, requestVersion = 0;
  get('detectLibraryChapters').addEventListener('click', async () => {
    const button = get('detectLibraryChapters'), output = get('libraryChapterStatus');
    const category = get('libraryCategory').value;
    button.disabled = true; output.textContent = '正在读取课件并识别已有笔记的章节…';
    try {
      const response = await fetcher('/api/library/chapters/detect', {method:'POST',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({category})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '章节识别失败');
      await refreshLibrary();
      const errors = data.errors || [];
      const firstError = typeof errors[0] === 'string' ? errors[0] : errors[0]?.error || errors[0]?.message || errors[0]?.detail;
      output.textContent = `章节已更新 ${data.updated || 0} 节，保持 ${data.unchanged || 0} 节，待确认 ${data.unassigned || 0} 节${data.busy ? `，处理中跳过 ${data.busy} 节` : ''}${errors.length ? `，失败 ${errors.length} 节：${firstError || '请重试或查看对应课堂'}` : ''}`;
    } catch (error) {output.textContent = error.message;}
    finally {button.disabled = false;}
  });

  function render(session) {
    get('noteDialogChapter').textContent = chapterLabel(session);
    if (sessionId !== session?.id) {
      sessionId = session?.id || ''; requestVersion++; draftVersion++;
      editor.hidden = true; status.textContent = ''; save.disabled = suggest.disabled = false;
    }
    if (editor.hidden) input.value = chapterValue(session);
  }

  input.addEventListener('input', () => {draftVersion++; status.textContent = '章节尚未保存';});
  get('editNoteChapter').addEventListener('click', () => {
    render(getSession()); editor.hidden = !editor.hidden;
    if (!editor.hidden) {input.value = chapterValue(getSession()); input.focus();}
  });
  get('cancelNoteChapter').addEventListener('click', () => {
    requestVersion++; editor.hidden = true; status.textContent = ''; render(getSession());
    save.disabled = suggest.disabled = false;
  });

  async function request(suffix, body, method) {
    const response = await fetcher(`/api/recordings/${encodeURIComponent(sessionId)}${suffix}`, {
      method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '章节操作失败');
    return data;
  }

  suggest.addEventListener('click', async () => {
    const id = sessionId, version = ++requestVersion, draft = draftVersion;
    suggest.disabled = true; status.textContent = '正在从课件识别章节…';
    try {
      const data = await request('/chapter-suggestion', {}, 'POST');
      if (id !== sessionId || version !== requestVersion) return;
      if (draft !== draftVersion) {status.textContent = '识别已完成；保留你刚修改的章节，请再次识别或直接保存'; return;}
      const value = chapterValue(data);
      if (!value) {status.textContent = '课件中没有找到可确认的章节，请手动填写'; return;}
      input.value = value; draftVersion++;
      const evidence = (data.chapter_info?.evidence || []).slice(0, 3)
        .map(row => `${row.source_name || '课件'}${row.page ? ` 第${row.page}页` : ''}：${String(row.text || '').slice(0, 120)}`).join('；');
      status.textContent = `建议：${value}。请核对后保存${evidence ? `。依据：${evidence}` : ''}`;
    } catch (error) {
      if (id === sessionId && version === requestVersion) status.textContent = `${error.message}；当前输入已保留`;
    } finally {if (id === sessionId && version === requestVersion) suggest.disabled = false;}
  });

  editor.addEventListener('submit', async event => {
    event.preventDefault();
    if (save.disabled) return;
    const id = sessionId, version = ++requestVersion, draft = draftVersion;
    save.disabled = suggest.disabled = true; status.textContent = '正在保存章节…';
    try {
      const data = await request('', {chapter: input.value.trim()}, 'PATCH');
      if (id !== sessionId || version !== requestVersion) return;
      await onSaved(data.session || data);
      if (id !== sessionId || version !== requestVersion) return;
      if (draft === draftVersion) {editor.hidden = true; render(getSession()); toast('章节已保存');}
      else status.textContent = '此前章节已保存；当前输入还有未保存的修改';
    } catch (error) {
      if (id === sessionId && version === requestVersion) status.textContent = `${error.message}；当前输入已保留`;
    } finally {if (id === sessionId && version === requestVersion) save.disabled = suggest.disabled = false;}
  });

  function updateOptions(rows = []) {
    get('detectLibraryChapters').textContent = get('libraryCategory').value ? '识别课程章节' : '识别全部章节';
    const selected = filter.value;
    filter.replaceChildren(new Option('全部章节', ''));
    for (const row of rows) {
      const value = row.chapter || '__unassigned__';
      filter.add(new Option(`${value === '__unassigned__' ? '章节待确认' : value} · ${row.records || 0}节 / ${row.notes || 0}份笔记`, value));
    }
    if (selected && !Array.from(filter.options).some(option => option.value === selected)) {
      filter.add(new Option(`${selected === '__unassigned__' ? '章节待确认' : selected} · 0节`, selected));
    }
    filter.value = selected;
  }

  function restoreLibrary(category, state) {
    const params = new URLSearchParams(location.search);
    const course = params.get('category') || '';
    if (course && !Array.from(category.options).some(option => option.value === course)) category.add(new Option(course, course));
    category.value = course;
    const chapter = params.get('chapter') || '';
    if (chapter && !Array.from(filter.options).some(option => option.value === chapter)) filter.add(new Option(chapter, chapter));
    filter.value = chapter;
    const date = params.get('date') || '';
    state.selectedLibraryDate = /^\d{4}-\d{2}-\d{2}$/.test(date) ? date : null;
    state.libraryPage = Math.max(0, (Number.parseInt(params.get('page'), 10) || 1) - 1);
  }

  function syncLibraryUrl(category, state) {
    if (location.pathname !== '/library') return;
    const url = new URL(location.href);
    for (const [key, value] of Object.entries({category: category.value, chapter: filter.value,
      date: state.selectedLibraryDate || '', page: state.libraryPage ? String(state.libraryPage + 1) : ''})) {
      if (value) url.searchParams.set(key, value); else url.searchParams.delete(key);
    }
    history.replaceState(null, '', url);
  }
  return {render, updateOptions, restoreLibrary, syncLibraryUrl};
}
