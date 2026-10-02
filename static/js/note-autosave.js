// One ordered save queue per note. Editor text and acknowledged text are separate:
// an older response must never replace characters typed while it was in flight.
export function createNoteAutosave({onStatus, onSaved}) {
  const documents = new Map();
  const key = id => `qwen-asr-note-draft-v1:${id}`;
  const dirty = doc => doc.text !== doc.savedInput;
  function persist(doc) {
    try {
      if (dirty(doc) || doc.pending || doc.uncertain) localStorage.setItem(key(doc.id), JSON.stringify({text: doc.text, base: doc.base}));
      else localStorage.removeItem(key(doc.id));
      doc.localSafe = true;
    } catch { doc.localSafe = false; }
  }
  function status(doc, phase, message) {
    doc.phase = phase;
    doc.message = message;
    onStatus(doc.id, phase, message, doc);
  }
  function schedule(doc, delay = 900) {
    clearTimeout(doc.timer);
    doc.timer = setTimeout(() => flush(doc.id), delay);
  }
  function open(id, markdown, {preserve = false} = {}) {
    let doc = documents.get(id);
    if (doc && (dirty(doc) || doc.pending || doc.uncertain || preserve)) {
      onStatus(id, doc.phase, doc.message || "修改会自动保存", doc);
      return doc;
    }
    doc = {id, text: markdown, savedInput: markdown, base: markdown, retries: 0, localSafe: true, phase: 'saved'};
    try {
      const draft = JSON.parse(localStorage.getItem(key(id)) || 'null');
      if (draft && typeof draft.text === 'string' && draft.text.trim() !== markdown.trim()) {
        doc.text = draft.text; doc.base = String(draft.base ?? markdown);
      }
    } catch { /* A malformed cache never replaces the saved note. */ }
    documents.set(id, doc);
    if (dirty(doc)) { status(doc, 'pending', '已恢复本机草稿，正在自动保存…'); schedule(doc); }
    else { persist(doc); status(doc, 'saved', '修改会自动保存'); }
    return doc;
  }
  function change(id, text) {
    const doc = documents.get(id); if (!doc) return;
    doc.text = text; doc.retries = 0; persist(doc);
    if (dirty(doc)) {
      status(doc, 'pending', doc.localSafe ? '草稿已保留，稍后自动保存…' : '正在编辑，等待自动保存…');
      schedule(doc);
    } else { clearTimeout(doc.timer); status(doc, 'saved', '已自动保存'); }
  }
  async function drain(doc, keepalive) {
    while (dirty(doc) || doc.uncertain) {
      if (!doc.text.trim()) { status(doc, 'error', '内容为空，已保留原总结；输入内容后会自动保存'); return false; }
      const snapshot = doc.text;
      const body = JSON.stringify({summary: snapshot, expected_summary: doc.base});
      status(doc, 'saving', '正在自动保存…');
      try {
        const response = await fetch(`/api/recordings/${encodeURIComponent(doc.id)}/summary`, {
          method: 'PUT', headers: {'Content-Type': 'application/json'}, body,
          keepalive: keepalive && new Blob([body]).size < 60000,
        });
        const payload = await response.json();
        if (!response.ok) {
          const error = new Error(typeof payload.detail === 'string' ? payload.detail : '自动保存未完成');
          error.status = response.status;
          const retryAfter = Number(response.headers.get('Retry-After'));
          if (response.status === 409 && retryAfter > 0 && Number.isFinite(retryAfter)) error.retryAfter = retryAfter;
          if (typeof payload.current_summary === 'string') doc.conflictBase = payload.current_summary;
          throw error;
        }
        doc.base = payload.summary.summary; doc.savedInput = snapshot; doc.retries = 0; doc.uncertain = false;
        delete doc.conflictBase; persist(doc);
        onSaved(doc.id, payload, doc);
      } catch (error) {
        if (!error.status || error.status >= 500) doc.uncertain = true;
        persist(doc);
        if (error.retryAfter && typeof doc.conflictBase !== 'string') {
          status(doc, 'pending', `后台任务正在处理，完成后自动保存；${doc.localSafe ? '草稿已保存在本机' : '请保留页面，草稿尚未落盘'}`);
          schedule(doc, Math.min(30, Math.max(1, error.retryAfter)) * 1000);
          return false;
        }
        status(doc, 'error', `${error.message}；${doc.localSafe ? '草稿已保存在本机' : '请保留页面，草稿尚未落盘'}`);
        if ((!error.status || error.status >= 500) && doc.retries++ < 3) schedule(doc, 5000 * doc.retries);
        return false;
      }
    }
    status(doc, 'saved', '已自动保存');
    return true;
  }
  async function flush(id, {keepalive = false, replaceConflict = false} = {}) {
    const doc = documents.get(id); if (!doc) return true;
    clearTimeout(doc.timer);
    if (replaceConflict && doc.conflictBase !== undefined) doc.base = doc.conflictBase;
    if (!doc.pending) {
      doc.pending = drain(doc, keepalive);
      try { return await doc.pending; } finally { doc.pending = null; persist(doc); }
    }
    return await doc.pending;
  }
  return {open, change, flush, get: id => documents.get(id),
    isDirty: id => { const doc = documents.get(id); return !!doc && (dirty(doc) || doc.uncertain); }};
}
