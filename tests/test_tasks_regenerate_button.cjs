const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('failed summary groupCard surfaces regenerate button prominently on material deadlocks', () => {
  const source = fs.readFileSync(path.join(__dirname, '../static/js/tasks.js'), 'utf8');
  const snippet = source.slice(
    source.indexOf('function reviewIssueDetails('),
    source.indexOf('function groupSection('),
  );
  const context = {
    labels: { running: '进行中', failed: '失败', paused: '已暂停' },
    actionLabels: { continue: '继续', retry: '重试', regenerate: '重新生成', pause: '暂停', cancel: '取消' },
    state: { selected: new Set(), busy: false },
    active: new Set(['running', 'queued', 'waiting_retry', 'stopping']),
    esc: (text) => String(text ?? ''),
    dateLabel: () => '',
    friendlyError: (task) => String(task?.error || ''),
    operationLabel: () => '',
    taskProgressView: () => ({ index: 0, steps: ['核对资料', '梳理知识', '编写图文', '逐页审查', '保存'], label: '准备配图', percent: null, count: '步骤 1/5' }),
    aiActivityView: () => null,
    legacyMineruProgress: () => null,
    mineruEstimateLabel: () => '',
    resourceStatus: () => null,
    leadingTask: (group) => group.current_tasks[0],
  };
  vm.createContext(context);
  vm.runInContext(snippet, context);

  const deadlockTask = {
    id: 'summary:lesson1',
    type: 'summary',
    label: '课后复习笔记',
    status: 'failed',
    error: '本节需要教学配图，但尚未取得合适原图；成果保留待补选',
    actions: ['continue', 'retry', 'regenerate', 'cancel'],
  };
  const deadlockHtml = context.groupCard({
    id: 'recording:lesson1',
    category: '微机原理',
    title: '8086中断系统与8259A可编程中断控制器',
    status: 'failed',
    recording_id: 'lesson1',
    result_url: '/library?recording=lesson1',
    current_tasks: [deadlockTask],
    history: [],
    errors: [{ id: deadlockTask.id, error: deadlockTask.error }],
  });
  const focusBlock = deadlockHtml.slice(deadlockHtml.indexOf('class="focus-actions"'), deadlockHtml.indexOf('<details class="group-details"'));
  assert.match(focusBlock, /<button data-action="regenerate" class="ui-button primary"[^>]*>重新生成<\/button>/);
  assert.match(focusBlock, /<button data-action="continue" class="ui-button\s*"[^>]*>继续<\/button>/);

  const networkTask = {
    id: 'summary:lesson2',
    type: 'summary',
    label: '课后复习笔记',
    status: 'failed',
    error: '模型服务临时限流。',
    actions: ['continue', 'retry', 'regenerate', 'cancel'],
  };
  const networkHtml = context.groupCard({
    id: 'recording:lesson2',
    category: '微机原理',
    title: '普通失败',
    status: 'failed',
    recording_id: 'lesson2',
    result_url: '/library?recording=lesson2',
    current_tasks: [networkTask],
    history: [],
    errors: [{ id: networkTask.id, error: networkTask.error }],
  });
  const networkFocus = networkHtml.slice(networkHtml.indexOf('class="focus-actions"'), networkHtml.indexOf('<details class="group-details"'));
  assert.match(networkFocus, /<button data-action="continue" class="ui-button primary"[^>]*>继续<\/button>/);
  assert.match(networkFocus, /<button data-action="regenerate" class="ui-button"[^>]*>重新生成<\/button>/);
});
