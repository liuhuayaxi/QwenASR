const SETTINGS_URL = '/?settings=ai&embedded=1';

let bridgeDialog = null;
let opener = null;

function closeSettingsBridge() {
  if (!bridgeDialog) return;
  const dialog = bridgeDialog;
  bridgeDialog = null;
  const returnTarget = opener;
  opener = null;
  dialog.close();
  dialog.remove();
  returnTarget?.focus?.({preventScroll: true});
}

function openSettingsBridge(event) {
  event.preventDefault();
  if (bridgeDialog) return;

  opener = event.currentTarget;
  const dialog = document.createElement('dialog');
  dialog.className = 'settings-bridge-dialog';
  dialog.setAttribute('aria-label', '全部设置');
  const loading = document.createElement('div');
  loading.className = 'settings-bridge-loading';
  loading.setAttribute('role', 'status');
  loading.textContent = '正在打开设置…';
  const frame = document.createElement('iframe');
  frame.title = '全部设置';
  frame.src = SETTINGS_URL;
  frame.style.visibility = 'hidden';
  dialog.append(loading, frame);
  document.body.append(dialog);
  bridgeDialog = dialog;

  dialog.addEventListener('cancel', closeSettingsBridge, {once: true});
  dialog.addEventListener('close', () => {
    if (bridgeDialog === dialog) closeSettingsBridge();
  }, {once: true});
  dialog.addEventListener('click', event => {
    if (event.target === dialog) closeSettingsBridge();
  });
  dialog.showModal();
}

window.addEventListener('message', event => {
  if (event.origin !== location.origin || !bridgeDialog) return;
  const frame = bridgeDialog.querySelector('iframe');
  if (event.source !== frame?.contentWindow) return;
  if (event.data?.type === 'qwenasr:settings-ready') {
    frame.style.visibility = 'visible';
    const loading = bridgeDialog.querySelector('.settings-bridge-loading');
    if (loading) loading.hidden = true;
    return;
  }
  if (event.data?.type === 'qwenasr:settings-close') closeSettingsBridge();
});

document.querySelectorAll('[data-open-settings]').forEach(link => {
  link.addEventListener('click', openSettingsBridge);
});
