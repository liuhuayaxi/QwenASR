import {createVisiblePoll} from './visible-poll.js';

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
  const previewPanel = byId('driveBackupPreview');
  const options = ['pdf', 'transcripts', 'notes', 'yuketang', 'automatic'];
  let current = null, pending = false, dirty = false, rendered = '', refreshFailed = false;
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
      button.disabled = pending || (busy && !['stop','cancel-auth'].includes(action))
        || (['start','disconnect'].includes(action) && !current?.connected)
        || (['start','connect'].includes(action) && dirty)
        || (action === 'connect' && !current?.configured)
        || (action === 'stop' && !current?.busy)
        || (action === 'cancel-auth' && !current?.authorizing);
    });
    root.querySelectorAll('input,select').forEach(input => { input.disabled = !!busy; });
    byId('driveBackupSave').classList.toggle('hidden', !dirty);
    byId('driveBackupStop').classList.toggle('hidden', !current?.busy);
  }
  function render(data) {
    const signature = JSON.stringify([data, dirty, previewPanel.open]);
    if (signature === rendered) { current = data; return; }
    rendered = signature;
    const wasConnected = current?.connected;
    const wasAuthorizing = current?.authorizing;
    current = data;
    if (!dirty) {
      options.forEach(name => { byId('driveOption-' + name).checked = data.options[name]; });
      byId('driveBackupInterval').value = String(data.options.interval_minutes);
    }
    account.textContent = data.authorizing ? '等待 Google 授权，请在打开的页面完成登录'
      : data.connected ? `已连接 · ${data.account.email || data.account.name || 'Google 账号'}`
      : data.configured ? '等待连接 Google 账号' : '尚未连接 · 展开“账号与连接”完成配置';
    if (wasConnected !== data.connected || wasAuthorizing !== data.authorizing) {
      byId('driveBackupSetup').open = !data.connected || data.authorizing;
    }
    byId('driveBackupConnect').textContent = data.connected ? '重新连接' : '连接 Google 账号';
    byId('driveBackupCancelAuth').classList.toggle('hidden', !data.authorizing);
    if (!data.authorizing) { authLink.classList.add('hidden'); authLink.removeAttribute('href'); }
    folder.classList.toggle('hidden', !data.folder_url);
    if (data.folder_url) folder.href = data.folder_url;
    else folder.removeAttribute('href');
    const completedAt = data.last_success ? new Date(data.last_success) : null;
    byId('driveBackupLastSuccess').textContent = completedAt && !Number.isNaN(completedAt.getTime())
      ? `最近完整备份：${completedAt.toLocaleString('zh-CN', {hour12:false})}` : '尚未完成全量备份';
    const job = data.job;
    progress.classList.toggle('hidden', !data.busy);
    if (job) {
      if (job.status === 'scanning') progress.removeAttribute('value');
      else progress.value = job.total ? Math.min(100, 100 * (job.completed + (data.busy && job.file_size ? job.file_bytes / job.file_size : 0)) / job.total) : job.status === 'completed' ? 100 : 0;
      status.textContent = `${labels[job.status] || job.status} · 已核对 ${job.completed}/${job.total} · 新传/更新 ${job.uploaded} · 未变化 ${job.skipped} · 失败 ${job.failed}`
        + (job.current ? `\n${job.current} · ${size(job.file_bytes)} / ${size(job.file_size)}` : '')
        + (job.message ? `\n${job.message}` : '');
    } else status.textContent = data.connected ? '已准备好，点击“立即备份”或开启自动备份。' : '连接 Google 账号后即可备份资料。';
    if (data.error) status.textContent += '\n' + data.error;
    details.replaceChildren();
    for (const error of job?.errors || []) {
      const line = document.createElement('li'); line.textContent = `${error.file}：${error.message}`; details.append(line);
    }
    const preview = data.preview;
    byId('driveBackupPreviewSummary').textContent = previewPanel.open && preview
      ? `备份清单 · ${preview.count} 个文件 · ${size(preview.bytes)}${preview.count > 40 ? '（展示前 40 项）' : ''}` : '查看备份清单';
    const list = byId('driveBackupPreviewList'); list.replaceChildren();
    for (const file of preview?.files || []) { const li = document.createElement('li'); li.textContent = file; list.append(li); }
    controls();
  }
  async function run(action) {
    if (pending) return;
    pending = true; controls();
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
      refreshFailed = false;
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
      refreshFailed = true;
      if (popup) popup.close();
      status.textContent = `操作未完成：${error.message}。正在自动核对状态…`;
    } finally { pending = false; controls(); polling.schedule(); }
  }
  root.querySelectorAll('[data-drive-action]').forEach(button => button.addEventListener('click', () => run(button.dataset.driveAction)));
  root.querySelectorAll('input,select').forEach(input => input.addEventListener('change', () => { dirty = true; controls(); }));
  previewPanel.addEventListener('toggle', () => {
    if (previewPanel.open && !dirty && !pending) void run('preview');
    if (!previewPanel.open) byId('driveBackupPreviewSummary').textContent = '查看备份清单';
  });
  const polling = createVisiblePoll({run: () => run('refresh'),
    enabled: () => Boolean(root.getClientRects().length),
    delay: () => current?.busy || current?.authorizing ? 1500 : refreshFailed ? 5000 : 60000,
    events: ['qwenasr:viewchange']});
  polling.wake();
  return {refresh:() => run('refresh')};
}
