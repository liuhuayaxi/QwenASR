const $ = id => document.getElementById(id);
const channel = 'qwenasr-userscript-v1';
const query = new URLSearchParams(location.search);
let target = null, bridgeReady = false, running = false, finished = false;
const timeText = seconds => {
  const total = Math.floor(Number(seconds) || 0);
  return `${String(Math.floor(total / 3600)).padStart(2,'0')}:${String(Math.floor(total / 60) % 60).padStart(2,'0')}:${String(total % 60).padStart(2,'0')}`;
};
function status(text, state = '') { $('launchStatus').textContent = text; $('launchStatus').dataset.state = state; }
function launch() {
  if (!target || !bridgeReady || running || finished) return;
  running = true;
  $('bridgeInstall').hidden = true;
  $('retryReplay').hidden = true;
  status('正在检查浏览器中的南邮 Smart Class 登录…');
  window.postMessage({channel, type:'open', target}, location.origin);
}
window.addEventListener('message', event => {
  if (event.source !== window || event.origin !== location.origin || event.data?.channel !== channel) return;
  const message = event.data;
  if (message.type === 'ready') { bridgeReady = true; launch(); }
  if (message.type === 'progress' && running) status(message.text);
  if (message.type === 'result' && running) {
    running = false;
    if (message.ok) {
      finished = true;
      status(`已确认浏览器登录，并定位到回放 ${timeText(message.seconds)}。可以关闭本页。`);
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
$('retryReplay').addEventListener('click', () => { finished = false; launch(); });

async function initialize() {
  const recording = query.get('recording');
  const seconds = Number(query.get('seconds') || 0);
  if (!recording || !Number.isFinite(seconds) || seconds < 0 || seconds > 7200) {
    status('录音或时间无效，请从录音库重新跳转。', 'error'); return;
  }
  try {
    const response = await fetch(`/api/recordings/${encodeURIComponent(recording)}/replay-target?seconds=${seconds}`, {cache:'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '没有找到对应回放');
    target = data;
    $('replayTitle').textContent = target.source_title || target.recording_title;
    $('replayTime').textContent = `录音 ${timeText(seconds)} → 此节回放 ${timeText(target.seconds)}`;
    $('officialReplay').href = target.url;
    $('officialReplay').hidden = false;
    $('loginReplay').href = `/course/?returnTo=${encodeURIComponent(location.href)}`;
    launch();
    for (let attempt = 0; attempt < 5 && !bridgeReady; attempt += 1) {
      window.postMessage({channel, type:'probe'}, location.origin);
      await new Promise(resolve => setTimeout(resolve, 300));
    }
    if (!bridgeReady) {
      status('首次使用请安装油猴脚本，安装后刷新本页即可继续这次跳转。');
      $('bridgeInstall').hidden = false;
    }
  } catch (error) { status(error.message, 'error'); }
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
