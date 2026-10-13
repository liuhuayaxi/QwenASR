const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/js/mineru-progress-view.js'), 'utf8');
const modulePromise = import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
test('queue, paused and completed tasks do not show a running ETA', async () => {
  const {mineruEstimateLabel: label} = await modulePromise;
  const estimate = {estimated:true, phase:'pages', remaining_seconds:15, elapsed_seconds:60, sample_count:3};
  assert.match(label(estimate,'queued'), /排队等待/);
  assert.doesNotMatch(label(estimate,'queued'), /15 秒/);
  assert.match(label(estimate,'paused'), /停止更新/);
  assert.equal(label(estimate,'completed'), '');
});
test('sample basis, current-file scope and overrun are explicit', async () => {
  const {mineruEstimateLabel: label} = await modulePromise;
  const estimate = {estimated:true, phase:'pages', remaining_seconds:75, elapsed_seconds:120, sample_count:3, observed_windows:2};
  assert.match(label(estimate,'running'), /同配置 3 次/);
  assert.match(label(estimate,'running'), /1 分 15 秒/);
  assert.match(label({...estimate,scope:'current_file'},'running'), /当前文件预计/);
  assert.match(label({...estimate,overrun:true},'running'), /超出预估/);
  assert.doesNotMatch(label({...estimate,overrun:true},'running'), /预计剩余/);
  assert.match(label({...estimate,sample_count:0,observed_windows:0},'running'), /初步估算/);
});
test('old backend uses only completed windows, never the current stage percentage', async () => {
  const {legacyMineruProgress: progress} = await modulePromise;
  const task={family:'mineru',status:'running',progress:null,stage:'阶段 100% · 第 17–20 页/90 · 窗口 5/23'};
  assert.equal(progress(task),17.8);
  assert.equal(progress({...task,stage:'阶段 0% · 第 17–20 页/90 · 窗口 5/23'}),17.8);
  assert.equal(progress({...task,stage:'阶段 100% · 第 1–4 页/90 · 窗口 1/23'}),0);
  assert.equal(progress({...task,progress:44}),null);
  assert.equal(progress({...task,status:'queued'}),null);
  assert.equal(progress({...task,id:'stable'}),17.8);
  assert.equal(progress({...task,id:'stable',stage:'Windows · 下载识别结果'}),17.8);
  assert.equal(progress({...task,id:'stable',status:'completed',progress:100}),null);
});
