const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture() {
  const elements = {};
  for (const id of ['exportLibraryBackup', 'downloadLibraryBackup', 'libraryBackupProgress', 'libraryBackupStatus', 'tab']) {
    elements[id] = {
      textContent: '', disabled: false, listeners: {}, classes: new Set(),
      classList: { toggle(name, value) { value ? elements[id].classes.add(name) : elements[id].classes.delete(name); } },
      addEventListener(name, action) { this.listeners[name] = action; },
      removeAttribute(name) { delete this[name]; },
    };
  }
  const responses = [{backup: null}], requests = [], timers = new Map();
  let timerId = 0;
  const context = vm.createContext({
    document: {getElementById: id => elements[id], querySelector: () => elements.tab},
    fetch: async (path, options) => {
      requests.push({path, options});
      const response = responses.shift();
      if (response instanceof Error) throw response;
      if (!response) throw new Error('Unexpected fetch');
      return {ok: !response.detail, json: async () => response};
    },
    setTimeout: action => { timers.set(++timerId, action); return timerId; },
    clearTimeout: id => timers.delete(id),
  });
  vm.runInContext(fs.readFileSync('static/js/library-backup.js', 'utf8').replace('export function', 'function'), context);
  const panel = context.createLibraryBackup();
  const flush = () => new Promise(resolve => setImmediate(resolve));
  return {elements, responses, requests, timers, panel, flush};
}

test('backup shows real progress and only enables download after completion', async () => {
  const f = fixture(); await f.flush();
  f.responses.push({backup: {id:'one', status:'scanning', percent:null}});
  await f.elements.exportLibraryBackup.listeners.click();
  assert.equal(f.elements.exportLibraryBackup.disabled, true);
  assert.equal(f.requests.at(-1).options.headers['X-QwenASR-Client'], 'library-backup');
  assert.equal('value' in f.elements.libraryBackupProgress, false);
  f.responses.push({backup: {id:'one', status:'packing', percent:40, completed_files:2, file_count:5, processed_bytes:40, total_bytes:100}});
  await f.panel.refresh();
  assert.equal(f.elements.libraryBackupProgress.value, 40);
  assert.match(f.elements.libraryBackupStatus.textContent, /2 \/ 5/);
  assert.equal(f.elements.downloadLibraryBackup.classes.has('hidden'), true);
  f.responses.push({backup: {id:'one', status:'completed', percent:100, file_count:5, archive_bytes:80, download_url:'/safe-download', filename:'backup.zip'}});
  await f.panel.refresh();
  assert.equal(f.elements.exportLibraryBackup.disabled, false);
  assert.equal(f.elements.downloadLibraryBackup.classes.has('hidden'), false);
  assert.equal(f.elements.downloadLibraryBackup.href, '/safe-download');
  assert.equal(f.elements.downloadLibraryBackup.download, 'backup.zip');
});

test('a connection error resumes the active backup without creating another job', async () => {
  const f = fixture(); await f.flush();
  f.responses.push({backup:{id:'one', status:'packing', percent:10}});
  await f.panel.refresh();
  f.responses.push(new Error('disconnected'));
  await f.panel.refresh();
  assert.equal(f.elements.exportLibraryBackup.disabled, false);
  f.responses.push({backup:{id:'one', status:'verifying', percent:99}});
  await f.elements.exportLibraryBackup.listeners.click();
  assert.equal(f.requests.filter(item => item.options.method === 'POST').length, 0);
  assert.match(f.elements.libraryBackupStatus.textContent, /核对文件清单/);
});

test('server failure shows its reason and permits an explicit retry', async () => {
  const f = fixture(); await f.flush();
  f.responses.push({backup:{id:'one', status:'failed', error:'磁盘空间不足'}});
  await f.panel.refresh();
  assert.equal(f.elements.libraryBackupStatus.textContent, '磁盘空间不足');
  assert.equal(f.elements.exportLibraryBackup.disabled, false);
  assert.equal(f.elements.downloadLibraryBackup.classes.has('hidden'), true);
  assert.equal(f.elements.libraryBackupProgress.classes.has('hidden'), true);
});

test('a released temporary ZIP hides its expired download and permits a new export', async () => {
  const f = fixture(); await f.flush();
  f.responses.push({backup:{id:'one',status:'released',percent:100}});
  await f.panel.refresh();
  assert.equal(f.elements.exportLibraryBackup.disabled,false);
  assert.equal(f.elements.downloadLibraryBackup.classes.has('hidden'),true);
  assert.equal(f.elements.libraryBackupProgress.classes.has('hidden'),true);
  assert.match(f.elements.libraryBackupStatus.textContent,/临时 ZIP 已清理/);
});
