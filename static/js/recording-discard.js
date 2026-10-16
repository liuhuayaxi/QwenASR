// Discard always refers to the recording captured before either confirmation.
// Polls may finish after another recording starts; callbacks keep that id.
export function createRecordingDiscard({button, dialog, getSession, onAccepted, onDiscarded, onRestored, onFailed, onChange, toast}) {
  const storageKey = 'qwen-asr-pending-discard-v1';
  const pending = new Set(), accepted = new Set(), finished = new Set(), settled = new Set(), polling = new Map(), attempts = new Map();
  try { for (const id of JSON.parse(localStorage.getItem(storageKey) || '[]')) if (typeof id === 'string') pending.add(id); } catch {}
  for (const id of pending) attempts.set(id, Symbol(id));
  const current = (id, attempt) => pending.has(id) && !settled.has(id) && attempts.get(id) === attempt;
  const title = dialog.querySelector('[data-discard-title]'), target = dialog.querySelector('[data-discard-target]');
  const explain = dialog.querySelector('[data-discard-explain]'), first = dialog.querySelector('[data-discard-continue]');
  const final = dialog.querySelector('[data-discard-confirm]'), cancel = dialog.querySelector('[data-discard-cancel]');
  let candidate = null;
  function persist() { try { localStorage.setItem(storageKey, JSON.stringify([...pending])); } catch {} }
  function update() {
    const session = getSession();
    button.hidden = !session;
    button.disabled = !!session && pending.has(session.id);
    button.textContent = session && pending.has(session.id) ? '正在丢弃录音…' : '丢弃录音';
  }
  function changed() { persist(); update(); onChange?.(); }
  function accept(id, result) {
    if (settled.has(id)) return;
    pending.add(id);
    if (!accepted.has(id)) { accepted.add(id); onAccepted?.(id, result); }
    changed();
  }
  function complete(id, result) {
    if (finished.has(id)) return;
    finished.add(id); settled.add(id); pending.delete(id); accepted.delete(id);
    onDiscarded?.(id, result); changed();
  }
  function restored(id, result) {
    // An old end-save response can still arrive after restoration. Keep its
    // recording settled so it cannot clear a successor recording's controls.
    pending.delete(id); accepted.delete(id); finished.delete(id); settled.add(id);
    onRestored?.(id, result); changed();
    toast('这条录音已从废纸篓恢复，可以在课堂记录中查看和继续处理。', 6500);
  }
  function fail(id, error) {
    pending.delete(id); accepted.delete(id); changed();
    onFailed?.(id, error);
    toast(`丢弃未完成：${error.message}。录音资料仍保留，请核对后重试。`, 7000);
  }
  async function poll(id, attempt = attempts.get(id)) {
    if (!current(id, attempt) || polling.get(id) === attempt) return;
    polling.set(id, attempt);
    try {
      while (current(id, attempt)) {
        let response, result;
        try {
          response = await fetch(`/api/recordings/${encodeURIComponent(id)}/discard`, {cache:'no-store', signal:AbortSignal.timeout(15000)});
          result = await response.json();
        } catch {
          if (!current(id, attempt)) return;
          toast('正在等待丢弃结果；已确认的录音不会继续收音，连接恢复后继续核对。', 5000);
          await new Promise(resolve => setTimeout(resolve, 2000)); continue;
        }
        if (!current(id, attempt)) return;
        if (!response.ok) throw new Error(result.detail || '无法核对丢弃进度');
        if (result.session_id !== id) throw new Error('返回的录音编号不一致');
        if (result.status === 'failed') throw new Error(result.error || result.message || '回收处理未完成');
        if (result.status === 'discarded') { complete(id, result); return; }
        if (result.status === 'restored') { restored(id, result); return; }
        if (result.status !== 'discarding') throw new Error('丢弃进度状态无法识别');
        accept(id, result);
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    } catch (error) { if (current(id, attempt)) fail(id, error); }
    finally { if (polling.get(id) === attempt) polling.delete(id); }
  }
  button.addEventListener('click', () => {
    const session = getSession();
    if (!session || pending.has(session.id)) return;
    candidate = {id:session.id, title:session.title || '未命名录音'};
    title.textContent = '丢弃这条录音？';
    target.textContent = candidate.title;
    explain.textContent = '将停止这条录音及其转写、AI 和保存任务，并把已有音频、转写和笔记移入废纸篓。需要保留时可取消。';
    first.hidden = false; final.hidden = true; final.disabled = false;
    dialog.showModal(); cancel.focus();
  });
  cancel.addEventListener('click', () => dialog.close());
  first.addEventListener('click', () => {
    if (!candidate) return;
    title.textContent = '再次确认丢弃录音';
    explain.textContent = '确认后立即停止这条录音的处理，并移入可恢复的回收站；不会影响其他课堂录音。';
    first.hidden = true; final.hidden = false; cancel.focus();
  });
  dialog.addEventListener('close', () => { candidate = null; });
  final.addEventListener('click', async () => {
    if (!candidate || pending.has(candidate.id)) return;
    const id = candidate.id, attempt = Symbol(candidate.id);
    attempts.set(id, attempt); settled.delete(id); finished.delete(id);
    final.disabled = true; accept(id, {status:'requesting'}); dialog.close();
    try {
      let response, result;
      try {
        response = await fetch(`/api/recordings/${encodeURIComponent(id)}/discard`, {
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({confirmed:true, expected_session_id:id}),
          signal:AbortSignal.timeout(30000),
        });
        result = await response.json();
      } catch {
        if (!current(id, attempt)) return;
        toast('丢弃请求的结果尚未确认，正在核对这条录音的状态。', 6000);
        void poll(id, attempt); return;
      }
      if (!current(id, attempt)) return;
      if (!response.ok) throw new Error(result.detail || '无法丢弃录音');
      if (result.session_id !== id) throw new Error('返回的录音编号不一致');
      if (result.status === 'failed') throw new Error(result.error || result.message || '丢弃未完成');
      if (result.status === 'discarded') complete(id, result);
      else if (result.status === 'restored') restored(id, result);
      else if (result.status === 'discarding') { accept(id, result); void poll(id); }
      else throw new Error('丢弃进度状态无法识别');
    } catch (error) { if (current(id, attempt)) fail(id, error); }
  });
  function receive(message) {
    const id = message.session_id;
    if (!id || !pending.has(id)) return;
    if (message.type === 'recording_discarding') { accept(id, message); void poll(id); }
    if (message.type === 'recording_discarded') complete(id, message);
  }
  return {update, receive, isPending:id=>pending.has(id), isDiscarded:id=>finished.has(id),
    isSettled:id=>settled.has(id),
    resume:()=>{for(const id of pending)void poll(id);}};
}
