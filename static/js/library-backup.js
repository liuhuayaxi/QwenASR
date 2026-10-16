export function createLibraryBackup() {
  const button = document.getElementById("exportLibraryBackup");
  const download = document.getElementById("downloadLibraryBackup");
  const progress = document.getElementById("libraryBackupProgress");
  const status = document.getElementById("libraryBackupStatus");
  if (!button || !download || !progress || !status) return;
  let timer, busy = false, request = null, uncertain = true;
  const size = value => Number(value || 0) >= 1024 ** 3
    ? `${(Number(value) / 1024 ** 3).toFixed(2)} GB`
    : `${(Number(value || 0) / 1024 ** 2).toFixed(1)} MB`;
  async function api(path, options = {}) {
    const response = await fetch(path, {cache:"no-store", signal:AbortSignal.timeout(30000), ...options});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "无法读取备份进度");
    if (!Object.hasOwn(data, 'backup') || (data.backup && !['scanning','packing','verifying','failed','completed','released'].includes(data.backup.status))) {
      throw new Error('备份状态响应不完整，请重新读取');
    }
    return data.backup;
  }
  function controls() {
    button.disabled = !!request || (busy && !uncertain);
    button.textContent = uncertain && !request ? "重新读取备份进度"
      : busy || request === 'create' ? "正在打包日常备份…"
      : request ? "正在读取备份进度…" : "导出日常备份 ZIP";
  }
  function render(job) {
    uncertain = false;
    busy = !!job && ["scanning", "packing", "verifying"].includes(job.status);
    download.classList.toggle("hidden", job?.status !== "completed");
    progress.classList.toggle("hidden", !job || ["failed", "released"].includes(job.status));
    if (job?.status !== 'completed') { download.removeAttribute('href'); download.removeAttribute('download'); }
    if (!job) { status.textContent = '暂无正在处理的备份，可以导出日常备份。'; return; }
    if (job.percent == null) progress.removeAttribute("value");
    else progress.value = job.percent;
    if (job.status === "scanning") status.textContent = "正在清点课程资料、录音和配置…";
    else if (job.status === "packing") status.textContent = `正在打包 ${job.completed_files} / ${job.file_count} 个文件 · ${size(job.processed_bytes)} / ${size(job.total_bytes)} · ${job.percent ?? 0}%`;
    else if (job.status === "verifying") status.textContent = "正在核对文件清单，确认备份完整…";
    else if (job.status === "failed") status.textContent = job.error || "日常备份失败，请重试";
    else if (job.status === "completed") {
      status.textContent = `备份已生成 · ${job.file_count} 个文件 · ZIP ${size(job.archive_bytes)}。点击“下载备份 ZIP”保存；下载结束后会清理临时 ZIP。`;
      download.href = job.download_url;
      download.download = job.filename;
    }
    else if (job.status === "released") status.textContent = "临时 ZIP 已清理。请到浏览器的下载列表查看结果；如需重新下载，请再次导出。";
  }
  // Serialize reads and creation. A tab-refresh cannot unlock a pending POST,
  // and an unknown POST outcome must be reconciled by GET before another POST.
  async function run(kind) {
    if (request) return;
    clearTimeout(timer); request = kind; controls();
    try {
      render(await api('/api/library/backup', kind === 'create'
        ? {method:'POST', headers:{'X-QwenASR-Client':'library-backup'}} : {}));
    } catch (error) {
      uncertain = true;
      status.textContent = `备份结果尚未确认：${error.message}。请重新读取进度，暂不重复创建。`;
    } finally {
      request = null; controls();
      if (busy) timer = setTimeout(refresh, uncertain ? 1500 : 1000);
    }
  }
  const refresh = () => run('read');
  button.addEventListener('click', () => run(uncertain || busy ? 'read' : 'create'));
  document.querySelector('[data-settings-tab="library"]')?.addEventListener('click', refresh);
  refresh();
  return {refresh};
}