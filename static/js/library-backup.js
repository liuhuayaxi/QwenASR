export function createLibraryBackup() {
  const button = document.getElementById("exportLibraryBackup");
  const download = document.getElementById("downloadLibraryBackup");
  const progress = document.getElementById("libraryBackupProgress");
  const status = document.getElementById("libraryBackupStatus");
  if (!button || !download || !progress || !status) return;
  let timer, busy = false, latest = null;
  const size = value => Number(value || 0) >= 1024 ** 3
    ? `${(Number(value) / 1024 ** 3).toFixed(2)} GB`
    : `${(Number(value || 0) / 1024 ** 2).toFixed(1)} MB`;
  async function api(path, options = {}) {
    const response = await fetch(path, { cache: "no-store", ...options });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "无法读取备份进度");
    return data.backup;
  }
  function render(job) {
    latest = job;
    busy = !!job && ["scanning", "packing", "verifying"].includes(job.status);
    button.disabled = busy;
    button.textContent = busy ? "正在打包日常备份…" : "导出日常备份 ZIP";
    download.classList.toggle("hidden", job?.status !== "completed");
    progress.classList.toggle("hidden", !job || ["failed", "released"].includes(job.status));
    if (!job) return;
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
  async function refresh() {
    clearTimeout(timer);
    try {
      render(await api("/api/library/backup"));
      if (busy) timer = setTimeout(refresh, 1000);
    } catch (error) {
      // Keep the last state so a connection error cannot cause a second export.
      button.disabled = false;
      button.textContent = "重新读取备份进度";
      status.textContent = `备份进度暂时无法读取：${error.message}`;
    }
  }
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      if (busy) { await refresh(); return; }
      clearTimeout(timer);
      render(await api("/api/library/backup", {
        method: "POST", headers: { "X-QwenASR-Client": "library-backup" },
      }));
      timer = setTimeout(refresh, 300);
    } catch (error) {
      button.disabled = false;
      status.textContent = error.message;
      // A previous export may still be working on the server.
      if (latest && busy) timer = setTimeout(refresh, 1500);
    }
  });
  document.querySelector('[data-settings-tab="library"]')?.addEventListener("click", refresh);
  refresh();
  return { refresh };
}
