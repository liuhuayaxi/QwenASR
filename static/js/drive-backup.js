export function createDriveBackup() {
  const root = document.getElementById('driveBackupSettings');
  if (!root) return;
  const byId = id => document.getElementById(id);
  const status = byId('driveBackupStatus');
  const account = byId('driveBackupAccount');
  const details = byId('driveBackupDetails');
  const progress = byId('driveBackupProgress');
  const folder = byId('driveBackupFolder');
  const authLink = byId('driveBackupAuthLink');
  const credential = byId('driveBackupCredentials');
  const options = ['pdf', 'transcripts', 'notes', 'automatic'];
  let current = null, pending = false, dirty = false, timer;
  const size = bytes => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  const labels = {scanning:'正在扫描资料', uploading:'正在上传', verifying:'正在校验云端文件', stopping:'正在暂停',
    completed:'备份完成', partial:'部分文件未完成，可重试', failed:'备份失败，可重试', stopped:'已暂停', interrupted:'等待继续备份'};
  async function api(action, payload) {
    const response = await fetch('/api/library/drive' + (action ? '/' + action : ''), {
      method: action ? 'POST' : 'GET', cache:'no-store', signal:AbortSignal.timeout(action === 'preview' ? 120000 : 30000),
      headers: {'Content-Type':'application/json', 'X-QwenASR-Client':'drive-backup'},
      ...(payload ? {body:JSON.stringify(payload)} : {}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '云盘操作未完成');
    return data;
  }
  function controls() {
    const busy = pending || current?.busy || current?.authorizing;
    root.querySelectorAll('[data-drive-action]').forEach(button => {
      const action = button.dataset.driveAction;
      button.disabled = pending || (busy && !['stop','cancel-auth','refresh'].includes(action))
        || (['start','test','disconnect'].includes(action) && !current?.connected)
        || (['start','test','preview','connect'].includes(action) && dirty)
        || (action === 'connect' && !current?.configured)
        || (action === 'stop' && !current?.busy)
        || (action === 'cancel-auth' && !current?.authorizing);
    });
    root.querySelectorAll('input,select').forEach(input => { input.disabled = !!busy; });
    byId('driveBackupSave').textContent = dirty ? '保存备份选项 *' : '保存备份选项';
  }
  function render(data) {
    const previouslyConfigured = current?.configured;
    current = data;
    if (!dirty) {
      options.forEach(name => { byId('driveOption-' + name).checked = data.options[name]; });
      byId('driveBackupInterval').value = String(data.options.interval_minutes);
    }
    account.textContent = data.authorizing ? '等待 Google 授权，请在打开的页面完成登录'
      : data.connected ? `已连接 · ${data.account.email || data.account.name || 'Google 账号'}`
      : data.configured ? '已导入客户端配置 · 等待连接 Google 账号' : '尚未配置 · 展开下方首次连接说明';
    if (previouslyConfigured !== data.configured) byId('driveBackupSetup').open = !data.configured;
    byId('driveBackupCancelAuth').classList.toggle('hidden', !data.authorizing);
    if (!data.authorizing) { authLink.classList.add('hidden'); authLink.removeAttribute('href'); }
    folder.classList.toggle('hidden', !data.folder_url);
    if (data.folder_url) folder.href = data.folder_url;
    else folder.removeAttribute('href');
    byId('driveBackupLastSuccess').textContent = data.last_success ? `最近完整备份：${data.last_success}` : '尚未完成全量备份';
    const job = data.job;
    progress.classList.toggle('hidden', !job);
    if (job) {
      if (job.status === 'scanning') progress.removeAttribute('value');
      else progress.value = job.total ? Math.min(100, 100 * (job.completed + (data.busy && job.file_size ? job.file_bytes / job.file_size : 0)) / job.total) : job.status === 'completed' ? 100 : 0;
      status.textContent = `${job.sample ? '小批量测试 · ' : ''}${labels[job.status] || job.status} · 已核对 ${job.completed}/${job.total} · 新传/更新 ${job.uploaded} · 未变化 ${job.skipped} · 失败 ${job.failed}`
        + (job.current ? `\n${job.current} · ${size(job.file_bytes)} / ${size(job.file_size)}` : '')
        + (job.message ? `\n${job.message}` : '');
    } else status.textContent = '连接后可先上传最多 3 个较小文件，校验成功后再备份全部资料。';
    if (data.error) status.textContent += '\n' + data.error;
    details.replaceChildren();
    for (const error of job?.errors || []) {
      const line = document.createElement('li'); line.textContent = `${error.file}：${error.message}`; details.append(line);
    }
    const preview = data.preview;
    byId('driveBackupPreviewSummary').textContent = preview ? `当前范围：${preview.count} 个文件 · ${size(preview.bytes)}${preview.count > 40 ? '（展示前 40 项）' : ''}` : '可先预览待备份文件，预览不会上传。';
    const list = byId('driveBackupPreviewList'); list.replaceChildren();
    for (const file of preview?.files || []) { const li = document.createElement('li'); li.textContent = file; list.append(li); }
    controls();
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(() => run('refresh'), current?.busy || current?.authorizing ? 1500 : 15000);
  }
  async function run(action) {
    if (pending) return;
    pending = true; clearTimeout(timer); controls();
    // Open synchronously on the user's gesture, avoiding popup blockers after fetch.
    const popup = action === 'connect' ? window.open('about:blank', '_blank') : null;
    if (popup) popup.opener = null;
    try {
      let payload;
      if (action === 'configure') {
        payload = {options:Object.fromEntries(options.map(name => [name, byId('driveOption-' + name).checked]))};
        payload.options.interval_minutes = Number(byId('driveBackupInterval').value);
        if (credential.files[0]) {
          if (credential.files[0].size > 32768) throw new Error('请选择 Google 下载的桌面应用 OAuth 客户端 JSON');
          payload.credentials = JSON.parse(await credential.files[0].text());
        }
      }
      const data = await api(action === 'refresh' ? '' : action, payload);
      if (action === 'configure') { dirty = false; credential.value = ''; }
      render(data);
      if (data.authorization_url) {
        const url = new URL(data.authorization_url);
        if (url.origin !== 'https://accounts.google.com') throw new Error('授权地址无效');
        if (popup) popup.location.replace(url.href);
        authLink.href = url.href; authLink.classList.remove('hidden');
        account.textContent = '等待 Google 授权，请在打开的页面完成登录';
      }
    } catch (error) {
      if (popup) popup.close();
      status.textContent = `操作未完成：${error.message}。可点击“刷新状态”核对结果。`;
    } finally { pending = false; controls(); schedule(); }
  }
  root.querySelectorAll('[data-drive-action]').forEach(button => button.addEventListener('click', () => run(button.dataset.driveAction)));
  root.querySelectorAll('input,select').forEach(input => input.addEventListener('change', () => { dirty = true; controls(); }));
  document.querySelector('[data-settings-tab="library"]')?.addEventListener('click', () => run('refresh'));
  run('refresh');
  return {refresh:() => run('refresh')};
}
