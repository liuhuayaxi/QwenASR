// Each opening owns its draft. A late picker/create response must not close
// a new dialog, overwrite subsequent edits, or change a user's new filter.
export function createTaskPdfDialog({api, toast, onCreated}) {
  const $ = id => document.getElementById(id);
  const dialog = $('pdfDialog'), paths = $('pdfPaths');
  let session = 0, edits = 0, submitting = null, picking = null;
  const notice = message => { $('pdfError').textContent = message; $('pdfError').hidden = !message; };
  function controls() {
    $('submitPdf').disabled = !!submitting || !!picking;
    $('choosePdf').disabled = !!submitting || !!picking;
    $('pdfForm').setAttribute('aria-busy', String(!!submitting || !!picking));
  }
  function open() {
    if (dialog.open) return;
    session += 1; notice(''); controls(); dialog.showModal(); paths.focus();
  }
  const ownsDraft = ticket => ticket.session === session && ticket.edits === edits;
  paths.addEventListener('input', () => { edits += 1; notice(''); });
  $('newPdf').addEventListener('click', open);
  $('closePdf').addEventListener('click', () => dialog.close());
  $('choosePdf').addEventListener('click', async () => {
    if (picking || submitting) return;
    const ticket = {session, edits}; picking = ticket; controls(); notice('');
    try {
      const result = await api('/api/pdf-ai/choose-files');
      if (!dialog.open || ticket.session !== session || result.cancelled) return;
      if (!ownsDraft(ticket)) { notice('文件选择已完成，但输入已修改，未覆盖当前路径。请重新选择或手动补充。'); return; }
      if (!Array.isArray(result.paths)) throw new Error('文件选择结果不完整，请重试');
      paths.value = result.paths.join('\n'); edits += 1;
    } catch (error) {
      if (dialog.open && ticket.session === session) notice(error.message);
    } finally { if (picking === ticket) picking = null; controls(); }
  });
  $('pdfForm').addEventListener('submit', async event => {
    event.preventDefault();
    if (submitting || picking) return;
    const selected = [...new Set(paths.value.split('\n').map(path => path.trim()).filter(Boolean))];
    if (!selected.length) { notice('请至少填写一个 PDF 文件的绝对路径'); paths.focus(); return; }
    if (selected.length > 20) { notice('每次最多选择 20 个不同的 PDF 文件，请减少后重试'); paths.focus(); return; }
    const ticket = {session, edits}; submitting = ticket; controls(); notice('');
    try {
      await api('/api/tasks/pdf', {paths:selected});
      const showCreated = ownsDraft(ticket) && dialog.open;
      if (ownsDraft(ticket)) {
        if (dialog.open) dialog.close();
        paths.value = ''; edits += 1;
      }
      toast('PDF 识别任务已加入队列');
      await onCreated?.({showCreated, paths:selected});
    } catch (error) {
      const message = error instanceof TypeError || error.name === 'TimeoutError'
        ? '提交结果尚未确认，请先在任务中心核对，避免重复提交。' : error.message;
      if (dialog.open && ticket.session === session) notice(message);
      else toast(`此前的 PDF 提交：${message}`);
    } finally { if (submitting === ticket) submitting = null; controls(); }
  });
  return {open};
}