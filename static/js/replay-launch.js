const $ = id => document.getElementById(id);
const channel = 'qwenasr-userscript-v1';
const query = new URLSearchParams(location.search);
let target = null, bridgeReady = false, running = false, finished = false;
const timeText = seconds => {
  const total = Math.floor(Number(seconds) || 0);
  return `${String(Math.floor(total / 3600)).padStart(2,'0')}:${String(Math.floor(total / 60) % 60).padStart(2,'0')}:${String(total % 60).padStart(2,'0')}`;
};
function status(text, state = '') {
  $('launchStatus').textContent = text; $('launchStatus').dataset.state = state;
  if (state === 'error') $('replayNotice').hidden = false;
}
function launch() {
  if (!target || !bridgeReady || running || finished) return;
  running = true;
  $('replayNotice').hidden = true;
  $('bridgeInstall').hidden = true;
  $('retryReplay').hidden = true;
  status('正在检查浏览器中的南邮 Smart Class 登录…');
  window.postMessage({channel, type:'open', target, sameTab:true}, location.origin);
}
window.addEventListener('message', event => {
  if (event.source !== window || event.origin !== location.origin || event.data?.channel !== channel) return;
  const message = event.data;
  if (message.type === 'ready') {
    // A legacy userscript would open a second tab and leave this one behind.
    if (!message.sameTab) {
      status('请将油猴跳转脚本更新到 1.1.0，更新后即可直接打开回放。', 'error');
      $('bridgeInstall').hidden = false;
      return;
    }
    bridgeReady = true;
    if (message.resuming) running = true;
    else launch();
  }
  if (message.type === 'progress' && running) status(message.text);
  if (message.type === 'result' && running) {
    running = false;
    if (message.ok) {
      finished = true;
    } else {
      const errorText = String(message.error || '回放未能打开，请重试');
      status(errorText.startsWith('Smart Class 加载超时')
        ? '学校播放器未完成加载。请点击“重新检查并跳转”；若仍失败，再检查校园网络。'
        : errorText, 'error');
      $('retryReplay').hidden = false;
      $('loginReplay').hidden = !message.needs_credentials;
    }
  }
});
$('retryReplay').addEventListener('click', () => { finished = false; if (target) launch(); else initialize(); });

async function initialize() {
  const recording = query.get('recording');
  const video = query.get('video');
  const seconds = Number(query.get('seconds') || 0);
  if ((!recording && !video) || (video && !/^[a-f0-9]{32}$/i.test(video)) || !Number.isFinite(seconds) || seconds < 0 || seconds > 7200) {
    status('录音或时间无效，请从录音库重新跳转。', 'error'); return;
  }
  $('loginReplay').href = `/course/?returnTo=${encodeURIComponent(location.href)}`;
  if (video) { $('replayBack').href = '/course/'; $('replayBack').textContent = '‹ 返回课程回放'; }
  try {
    const path = video ? `/course/api/replay-video-target?video=${encodeURIComponent(video)}`
      : `/api/recordings/${encodeURIComponent(recording)}/replay-target?seconds=${seconds}`;
    const response = await fetch(path, {cache:'no-store'});
    const data = await response.json();
    if (!response.ok) { $('loginReplay').hidden = response.status !== 401; $('retryReplay').hidden = false; }
    if (!response.ok) throw new Error(data.detail || '没有找到对应回放');
    target = data;
    $('replayTitle').textContent = target.source_title || target.recording_title;
    $('replayTime').textContent = video ? '从此节课的开头播放' : `录音 ${timeText(seconds)} → 此节回放 ${timeText(target.seconds)}`;
    $('officialReplay').href = target.url;
    $('officialReplay').hidden = false;
    launch();
    for (let attempt = 0; attempt < 5 && !bridgeReady; attempt += 1) {
      window.postMessage({channel, type:'probe'}, location.origin);
      await new Promise(resolve => setTimeout(resolve, 300));
    }
    if (!bridgeReady) {
      status('请安装或更新油猴跳转脚本，完成后刷新本页即可直接打开回放。', 'error');
      $('bridgeInstall').hidden = false;
    }
  } catch (error) { $('retryReplay').hidden = false; status(error.message, 'error'); }
}
initialize();

$('copyReplayScript').addEventListener('click', async event => {
  const button = event.currentTarget;
  try {
    const response = await fetch('/course/replay-script.user.js', {cache:'no-store'});
    if (!response.ok) throw new Error('脚本暂时无法读取');
    await navigator.clipboard.writeText(await response.text());
    button.textContent = '已复制脚本';
  } catch (_) { status('请点击“安装油猴脚本”，或打开脚本源文件后手动复制。', 'error'); }
});
