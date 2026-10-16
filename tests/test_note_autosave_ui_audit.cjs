const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const sourceFile = process.env.QWEN_ASR_AUDIT_SOURCE || path.join(__dirname, '../static/js/note-autosave.js');
const source = fs.readFileSync(sourceFile, 'utf8');

function harness({storageFails = false} = {}) {
  const saved = new Map(), timers = new Map(), calls = [], statuses = [];
  let nextTimer = 0;
  const context = {
    Blob,
    localStorage: {
      getItem: key => saved.get(key) ?? null,
      setItem(key, value) { if (storageFails) throw new Error('Quota exceeded'); saved.set(key, value); },
      removeItem(key) { if (storageFails) throw new Error('Quota exceeded'); saved.delete(key); },
    },
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, {callback, delay}); return id; },
    clearTimeout: id => timers.delete(id),
    fetch(url, options) {
      return new Promise((resolve, reject) => calls.push({url, body: JSON.parse(options.body), resolve, reject}));
    },
  };
  vm.createContext(context);
  // The production module has a single named export and no imports. No
  // production logic is replaced; globals are isolated deterministic IO.
  vm.runInContext(source.replace('export function createNoteAutosave', 'function createNoteAutosave'), context);
  const autosave = context.createNoteAutosave({onStatus(id, phase, message) { statuses.push({id, phase, message}); }, onSaved() {}});
  const reply = (index, payload, status = 200) => calls[index].resolve({ok: status >= 200 && status < 300, status,
    headers: {get() { return null; }}, json: async () => payload});
  return {autosave, calls, reply, saved, timers, statuses, phase: () => statuses.at(-1).phase};
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const ok = summary => ({summary: {summary}});


test('clean note is synchronously clean and needs no request', async () => {
  const h = harness();
  h.autosave.open('note', 'original');
  assert.equal(h.autosave.isDirty('note'), false);
  assert.equal(await h.autosave.flush('note'), true);
  assert.equal(h.calls.length, 0);
});

test('reverting an in-flight PUT is still dirty until compensated and acknowledged', async () => {
  const h = harness();
  h.autosave.open('note', 'original');
  h.autosave.change('note', 'edit');
  const done = h.autosave.flush('note');
  h.autosave.change('note', 'original');
  assert.notEqual(h.phase(), 'saved');
  assert.equal(h.autosave.isDirty('note'), true);
  h.reply(0, ok('edit'));
  await tick();
  assert.equal(h.calls.length, 2);
  assert.deepEqual(h.calls[1].body, {summary: 'original', expected_summary: 'edit'});
  h.reply(1, ok('original'));
  assert.equal(await done, true);
  assert.equal(h.phase(), 'saved');
  assert.equal(h.autosave.isDirty('note'), false);
});

test('uncertain write followed by revert must retain a retry and never claim saved', async () => {
  const h = harness();
  h.autosave.open('note', 'original');
  h.autosave.change('note', 'edit');
  const failed = h.autosave.flush('note');
  h.calls[0].reject(new TypeError('Network response lost'));
  assert.equal(await failed, false);
  h.autosave.change('note', 'original');
  assert.notEqual(h.phase(), 'saved');
  assert.equal(h.autosave.isDirty('note'), true);
  assert.ok([...h.timers.values()].some(timer => timer.delay === 900));
  const retry = h.autosave.flush('note');
  h.reply(1, ok('original'));
  assert.equal(await retry, true);
  assert.equal(h.autosave.isDirty('note'), false);
});

test('rapid edits remain ordered and old responses cannot replace newer text', async () => {
  const h = harness();
  h.autosave.open('note', 'original');
  h.autosave.change('note', 'first');
  const done = h.autosave.flush('note');
  h.autosave.change('note', 'latest');
  const joined = h.autosave.flush('note');
  assert.equal(h.calls.length, 1);
  h.reply(0, ok('first'));
  await tick();
  assert.equal(h.autosave.get('note').text, 'latest');
  assert.deepEqual(h.calls[1].body, {summary: 'latest', expected_summary: 'first'});
  h.reply(1, ok('latest'));
  assert.equal(await done, true);
  assert.equal(await joined, true);
  assert.equal(h.calls.length, 2);
});

test('CAS conflict preserves draft and requires explicit replacement intent', async () => {
  const h = harness();
  h.autosave.open('note', 'original');
  h.autosave.change('note', 'my draft');
  const first = h.autosave.flush('note');
  h.reply(0, {detail: 'Conflict', current_summary: 'other edit'}, 409);
  assert.equal(await first, false);
  assert.equal(h.phase(), 'error');
  assert.equal(h.autosave.get('note').text, 'my draft');
  assert.equal(JSON.parse(h.saved.get('qwen-asr-note-draft-v1:note')).text, 'my draft');
  const confirmed = h.autosave.flush('note', {replaceConflict: true});
  assert.deepEqual(h.calls[1].body, {summary: 'my draft', expected_summary: 'other edit'});
  h.reply(1, ok('my draft'));
  assert.equal(await confirmed, true);
});

test('failed local draft storage is not represented as safely persisted', async () => {
  const h = harness({storageFails: true});
  h.autosave.open('note', 'original');
  h.autosave.change('note', 'draft');
  const done = h.autosave.flush('note');
  h.calls[0].reject(new TypeError('Offline'));
  await done;
  assert.equal(h.autosave.get('note').localSafe, false);
  assert.equal(h.phase(), 'error');
  assert.equal(h.autosave.isDirty('note'), true);
});

test('a persisted draft is restored without replacing it with old server text', () => {
  const h = harness();
  h.saved.set('qwen-asr-note-draft-v1:note', JSON.stringify({text: 'recovered', base: 'original'}));
  const doc = h.autosave.open('note', 'original');
  assert.equal(doc.text, 'recovered');
  assert.equal(h.autosave.isDirty('note'), true);
  assert.equal(h.phase(), 'pending');
});

test('empty input does not replace the existing server note', async () => {
  const h = harness();
  h.autosave.open('note', 'original');
  h.autosave.change('note', '  ');
  assert.equal(await h.autosave.flush('note'), false);
  assert.equal(h.calls.length, 0);
  assert.equal(h.phase(), 'error');
});
