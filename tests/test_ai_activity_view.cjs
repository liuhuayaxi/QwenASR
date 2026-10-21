const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/ai-activity-view.js'), 'utf8');
const modulePromise = import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const task = activity => ({family: 'ai', status: 'running', ai_activity: activity});

test('a new request separates earlier intermediate output from current text', async () => {
  const {aiActivityView} = await modulePromise;
  const view = aiActivityView(task({phase:'processing',elapsed_seconds:1800,idle_seconds:20,
    output_chars:90000,current_output_chars:0,request_elapsed_seconds:35,requests:6}));
  assert.match(view.detail,/当前请求尚未收到结果正文/);
  assert.match(view.detail,/本轮累计接收 90,000 字符（含清单、读图和中间结果）/);
  assert.match(view.detail,/当前请求已用时 35 秒/);
  assert.match(view.detail,/本轮已发起 6 次请求/);
});

test('review waits until completion without claiming a fixed timeout', async () => {
  const {aiActivityView}=await modulePromise;
  const view=aiActivityView({...task(null),completed:8,total:31,
    review_batches:[{pages:[9,10,11,12,13,14,15,16],started_at:new Date().toISOString(),deadline_policy:'until_completion',timeout_seconds:null,attempt:1}]});
  assert.match(view.label,/第 9–16 页/);
  assert.match(view.detail,/持续等待模型完成，不按固定时长中断/);
  assert.doesNotMatch(view.detail,/最多等待|超时会/);
});

test('review shows real per-request reads and thinking metadata in the main label',async()=>{
  const {aiActivityView}=await modulePromise;
  const activity={request_activity:[{pages:[1,2,3,4,5,6,7,8],phase:'reading',read_files:12,
    opened_pages:[1,2,3],image_reads:4,model_turns:14,thinking_chars:600,idle_seconds:2}]};
  const view=aiActivityView({...task(activity),completed:0,total:31,review_batches:[{
    pages:[1,2,3,4,5,6,7,8],started_at:new Date().toISOString(),deadline_policy:'until_completion'}]});
  assert.match(view.label,/模型正在读取资料/);assert.match(view.label,/已打开 3\/8 页/);
  assert.match(view.label,/思考活动累计 600 字符/);assert.match(view.label,/最近活动 2 秒前/);
  assert.doesNotMatch(view.label,/已等待/);
});

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
  const context = {taskProgressView:()=>({percent:null,count:''}),aiActivityView, legacyMineruProgress: () => null, resourceStatus: () => null,
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
  assert.match(html, /<progress[^>]*value="0"/);
  assert.match(html, /等待模型返回/);
  assert.match(html, /已完成 0\/1 批/);
  assert.doesNotMatch(html, /task-error/);
  assert.match(html, /上次处理失败：AI 没有返回精修文本/);
  const completed = context.card({...item, status: 'completed', completed: 1});
  assert.match(completed, /value="100"/);
  assert.doesNotMatch(completed, /task-ai-activity|task-progress-indeterminate/);
});
