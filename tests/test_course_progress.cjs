const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/js/course-progress.js'), 'utf8');
const modulePromise = import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const oldQueue = {id: 'course_queue', type: 'course_queue', status: 'running', progress: 10.7,
  course_items: [{id: 'a', title: '课堂 A', course: '测试课程', start: '08:00', stop: '09:30', state: 'running', progress: 12,
    phases: [{key: 'asr', percent: 12.7, state: 'running'}]}]};
const payload = () => ({tasks: [structuredClone(oldQueue), {id: 'other', status: 'completed'}],
  counts: {all: 2, active: 1, completed: 1}, history: {can_restore: true}});
const live = () => ({queue: {paused: false, items: [{id: 'a', title: '课堂 A', state: 'running'}]},
  imports: [{id: 'a', progress: 62, message: '已获取完整音频', phases: {
    download: {label: '获取回放音频', percent: 100, state: 'completed'},
    asr: {label: '本地语音转写', percent: 48.8, state: 'running', detail: '45.9/94.0 分钟'},
  }}]});
test('live progress replaces a stale disk snapshot without changing course metadata', async () => {
  const {mergeLiveCourseProgress} = await modulePromise;
  const original = payload();
  const result = mergeLiveCourseProgress(original, live());
  assert.equal(result.tasks[0].progress, 62);
  assert.equal(result.tasks[0].course_items[0].phases[1].percent, 48.8);
  assert.equal(result.tasks[0].course_items[0].course, '测试课程');
  assert.equal(result.tasks[0].course_items[0].start, '08:00');
  assert.equal(result.tasks[1], original.tasks[1]);
  assert.equal(original.tasks[0].progress, 10.7);
});
test('live updates change even when the task API payload remains unchanged', async () => {
  const {mergeLiveCourseProgress} = await modulePromise;
  const status = live(), first = mergeLiveCourseProgress(payload(), status);
  status.imports[0].progress = 70;
  assert.notEqual(JSON.stringify(first), JSON.stringify(mergeLiveCourseProgress(payload(), status)));
});
test('finished courses disappear and newly queued courses appear before disk catches up', async () => {
  const {mergeLiveCourseProgress} = await modulePromise;
  const status = live();
  status.queue.items = [{id: 'a', state: 'completed'}, {id: 'b', title: '课堂 B', state: 'queued'}];
  const task = mergeLiveCourseProgress(payload(), status).tasks[0];
  assert.equal(task.status, 'queued');
  assert.equal(task.total, 2);
  assert.equal(task.completed, 1);
  assert.equal(task.progress, 0);
  assert.deepEqual(task.course_items.map(item => item.id), ['b']);
  assert.deepEqual(task.actions, ['pause']);
});
test('pausing changes both available actions and task counts', async () => {
  const {mergeLiveCourseProgress} = await modulePromise;
  const status = live(); status.queue.paused = true; status.queue.items[0].state = 'paused';
  const result = mergeLiveCourseProgress(payload(), status);
  assert.equal(result.tasks[0].status, 'paused');
  assert.deepEqual(result.tasks[0].actions, ['continue']);
  assert.equal(result.tasks[0].course_items[0].phases[1].state, 'paused');
  assert.equal(result.counts.active, 0); assert.equal(result.counts.paused, 1);
  assert.equal(result.counts.attention, 1);
});
test('an entirely completed queue is removed with matching counts', async () => {
  const {mergeLiveCourseProgress} = await modulePromise;
  const status = live(); status.queue.items[0].state = 'completed';
  const result = mergeLiveCourseProgress(payload(), status);
  assert.equal(result.tasks.length, 1); assert.equal(result.tasks[0].id, 'other');
  assert.equal(result.counts.all, 1); assert.equal(result.counts.active, 0);
  assert.equal(result.counts.completed, 1); assert.deepEqual(result.history, {can_restore: true});
});
test('an unavailable or malformed live response cannot silently replace saved progress', async () => {
  const {mergeLiveCourseProgress} = await modulePromise;
  for (const status of [null, {}, {queue: {items: []}}, {queue: {items: {}}, imports: []}]) {
    assert.throws(() => mergeLiveCourseProgress(payload(), status), /响应不完整/);
  }
});
