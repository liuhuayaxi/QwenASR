const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/js/visible-poll.js'), 'utf8');
const implementation = import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function environment(t) {
  const saved = {document: global.document, window: global.window, setTimeout: global.setTimeout, clearTimeout: global.clearTimeout};
  const document = new EventTarget(), window = new EventTarget(), timers = new Map();
  const disposables = [];
  let serial = 0;
  document.hidden = false;
  Object.assign(global, {document, window, setTimeout(fn, delay) { timers.set(++serial, {fn, delay}); return serial; }, clearTimeout(id) { timers.delete(id); }});
  t.after(() => { for (const poll of disposables) poll.destroy(); Object.assign(global, saved); });
  return {document, window, timers, track(poll) { disposables.push(poll); }, visibility(hidden) { document.hidden = hidden; document.dispatchEvent(new Event('visibilitychange')); }};
}
const tick = async () => { for (let n = 0; n < 8; n++) await Promise.resolve(); };

test('overlapping refresh and recovery signals share one request', async t => {
  const {createVisiblePoll} = await implementation;
  const env = environment(t);
  let calls = 0, finish;
  const poll = createVisiblePoll({run: () => { calls++; return new Promise(resolve => finish = resolve); }, delay: 5000});
  env.track(poll);
  const first = poll.refresh();
  assert.equal(poll.refresh(), first);
  env.window.dispatchEvent(new Event('online'));
  env.document.dispatchEvent(new Event('visibilitychange'));
  await tick(); assert.equal(calls, 1);
  finish(); await first;
  assert.equal(env.timers.size, 1);
});

test('hidden tabs stop timers without cancelling in-flight work and recover immediately', async t => {
  const {createVisiblePoll} = await implementation;
  const env = environment(t);
  let calls = 0, finish;
  const poll = createVisiblePoll({run: () => { calls++; return new Promise(resolve => finish = resolve); }, delay: 3500});
  env.track(poll);
  const request = poll.refresh(); await tick();
  env.visibility(true); finish(); await request;
  assert.equal(env.timers.size, 0);
  assert.equal(calls, 1);
  env.visibility(false); await tick(); assert.equal(calls, 2);
  finish(); await tick(); assert.equal(env.timers.size, 1);
});

test('state selects active cadence and invisible panels stay quiet until opened', async t => {
  const {createVisiblePoll} = await implementation;
  const env = environment(t);
  let active = false, panelOpen = true, calls = 0;
  const poll = createVisiblePoll({run: () => { calls++; }, delay: () => active ? 2000 : 60000,
    enabled: () => panelOpen, events: ['viewchange']});
  env.track(poll);
  await poll.refresh(); assert.equal([...env.timers.values()][0].delay, 60000);
  active = true; poll.schedule(); assert.equal([...env.timers.values()][0].delay, 2000);
  panelOpen = false; env.document.dispatchEvent(new Event('viewchange')); await tick();
  assert.equal(env.timers.size, 0); assert.equal(calls, 1);
  panelOpen = true; env.document.dispatchEvent(new Event('viewchange')); await tick();
  assert.equal(calls, 2); assert.equal([...env.timers.values()][0].delay, 2000);
});

test('failed requests release their slot and disposal removes recovery listeners', async t => {
  const {createVisiblePoll} = await implementation;
  const env = environment(t);
  let calls = 0;
  const poll = createVisiblePoll({run: () => { if (++calls === 1) throw new Error('offline'); }, delay: 5000});
  await assert.rejects(poll.refresh(), /offline/);
  assert.equal(env.timers.size, 1);
  await poll.refresh(); assert.equal(calls, 2);
  poll.destroy(); assert.equal(env.timers.size, 0);
  env.window.dispatchEvent(new Event('online')); env.visibility(false); await tick(); assert.equal(calls, 2);
});
