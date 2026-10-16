const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/ai-activity-view.js'), 'utf8');
const modulePromise = import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const task = activity => ({family: 'ai', status: 'running', ai_activity: activity});

test('waiting for a model has elapsed time without pretending text is being streamed', async () => {
  const {aiActivityView} = await modulePromise;
  const view = aiActivityView(task({phase: 'processing', elapsed_seconds: 74, idle_seconds: 70, output_chars: 0}));
  assert.equal(view.label, '等待模型返回');
  assert.match(view.detail, /1 分 14 秒/);
  assert.match(view.detail, /尚未收到正文/);
  assert.match(view.detail, /最近活动 1 分 10 秒前/);
});

test('actual output shows its count, and a quiet stream reports waiting for more', async () => {
  const {aiActivityView} = await modulePromise;
  const state = {phase: 'receiving', elapsed_seconds: 100, idle_seconds: 1, output_chars: 3456, active_requests: 3};
  const view = aiActivityView(task(state));
  assert.equal(view.label, '正在接收 AI 输出');
  assert.match(view.detail, /3,456 字符/);
  assert.match(view.detail, /3 个请求处理中/);
  assert.equal(aiActivityView(task({...state, idle_seconds: 40})).label, '等待后续输出');
});

test('queue, validation and final states retain their distinct meanings', async () => {
  const {aiActivityView} = await modulePromise;
  assert.match(aiActivityView(task({phase: 'waiting'})).detail, /等待可用的处理名额/);
  assert.equal(aiActivityView(task({phase: 'checking', output_chars: 200})).label, '正在校验并保存结果');
  for (const status of ['completed', 'failed', 'waiting_retry', 'paused']) {
    assert.equal(aiActivityView({...task({phase: 'receiving'}), status}), null);
  }
});

test('an active AI card shows an activity bar and keeps the previous error in details', async () => {
  const {aiActivityView} = await modulePromise;
  const source = fs.readFileSync(path.join(__dirname, '../static/js/tasks.js'), 'utf8');
  const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
  const context = {aiActivityView, legacyMineruProgress: () => null, resourceStatus: () => null,
    mineruEstimateLabel: () => '', state: {busy: false, selected: new Set()},
    esc: text => String(text ?? ''), active: new Set(['running']),
    labels: {running: '进行中', completed: '已完成'}, actionLabels: {}, dateLabel: () => ''};
  vm.createContext(context);
  for (const [start, end] of [['function friendlyError(', 'function operationLabel('],
    ['function operationLabel(', 'function courseQueueDetails('], ['function taskStatus(', 'function selectionControls(']]) {
    vm.runInContext(section(start, end), context);
  }
  const item = {...task({phase: 'processing', elapsed_seconds: 30, idle_seconds: 25}),
    id: 'test', title: '课堂记录', label: '转写校对', completed: 0, total: 1,
    error: '', error_kind: 'provider_auth', previous_error: 'AI 没有返回精修文本'};
  const html = context.card(item);
  assert.match(html, /task-progress-indeterminate/);
  assert.match(html, /等待模型返回/);
  assert.match(html, /已完成 0\/1 批/);
  assert.doesNotMatch(html, /0%|task-error/);
  assert.match(html, /上次处理失败：AI 没有返回精修文本/);
  const completed = context.card({...item, status: 'completed', completed: 1});
  assert.match(completed, /value="100"/);
  assert.doesNotMatch(completed, /task-ai-activity|task-progress-indeterminate/);
});
