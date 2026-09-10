const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));

function context() {
  const timers = new Map();
  const sent = [];
  let id = 0;
  const socket = { readyState: 1, send: (text) => sent.push(JSON.parse(text)), close() { this.closed = true; } };
  const ctx = {
    state: { capturing: true, pausing: false, ending: false, activeSession: { id: 'session-test' }, ws: socket },
    els: { listeningLabel: { textContent: '' } },
    WebSocket: { OPEN: 1 },
    setTimeout(fn) { timers.set(++id, fn); return id; },
    clearTimeout(key) { timers.delete(key); },
    stopAudioNodes() {}, updateButtons() {}, showSession() {}, toast() {},
  };
  vm.createContext(ctx);
  vm.runInContext(section('function pauseCapture()', 'async function endSession()'), ctx);
  return { ctx, timers, socket, sent };
}

async function main() {
  {
    const { ctx, timers, socket, sent } = context();
    ctx.pauseCapture();
    assert.equal(ctx.state.capturing, false);
    assert.equal(sent[0].type, 'pause');
    assert.equal(timers.size, 1);
    [...timers.values()][0](); // No pause acknowledgement ever arrived.
    assert.equal(ctx.state.pausing, false);
    assert.equal(ctx.state.ws, null);
    assert.equal(socket.closed, true);
    assert.equal(ctx.state.activeSession.id, 'session-test');
  }
  {
    const { ctx, timers } = context();
    ctx.pauseCapture();
    ctx.finishPause();
    assert.equal(ctx.state.pausing, false);
    assert.equal(timers.size, 0);
  }
  {
    const { ctx } = context();
    ctx.state.capturing = false;
    ctx.state.pausing = true;
    ctx.sessionConfig = () => ({ type: 'config' });
    ctx.wsUrl = () => 'ws://test';
    ctx.handleMessage = () => {};
    let created;
    ctx.WebSocket = class {
      static OPEN = 1;
      constructor() { created = this; this.readyState = 1; }
      send() {}
      close() { this.onclose?.(); }
    };
    vm.runInContext(section('function connectFor(', 'async function startCapture()'), ctx);
    const connecting = ctx.connectFor('start');
    created.onopen();
    created.onmessage({ data: JSON.stringify({ type: 'session_started' }) });
    await connecting;
    created.onclose(); // Socket failure during pause must release controls.
    assert.equal(ctx.state.pausing, false);
    assert.equal(ctx.state.ws, null);
  }
  {
    const { ctx, sent } = context();
    const saved = new Map();
    ctx.localStorage = { setItem: (k, v) => saved.set(k, v) };
    ctx.PENDING_END_KEY = 'pending-end';
    ctx.automaticSummaryPayload = () => ({ config: { ai: { summary_provider: { model: 'selected-summary-model' } } } });
    ctx.watchEndSession = (id) => { ctx.watched = id; };
    vm.runInContext(section('async function endSession()', 'async function watchEndSession('), ctx);
    await ctx.endSession();
    assert.equal(sent[0].type, 'end');
    assert.equal(sent[0].summary_payload.config.ai.summary_provider.model, 'selected-summary-model');
    assert.equal(saved.get('pending-end'), 'session-test');
    assert.equal(ctx.watched, 'session-test');
  }
  {
    const { ctx } = context();
    ctx.state.noteSession = null;
    const responses = [{ running: true }, { running: false, status: 'completed' },
      { session: { id: 'session-test', ai_note_summary: { summary: '完整笔记' } } }];
    ctx.fetch = async () => ({ ok: true, json: async () => responses.shift() });
    ctx.setTimeout = (fn) => fn();
    vm.runInContext(section('async function waitForSummaryJob(', 'async function watchSummaryJob('), ctx);
    const result = await ctx.waitForSummaryJob('session-test');
    assert.equal(result.summary.summary, '完整笔记');
    ctx.fetch = async () => ({ ok: true, json: async () => ({ running: false, status: 'interrupted', error: '服务重启' }) });
    await assert.rejects(ctx.waitForSummaryJob('session-test'), /服务重启/);
  }
  console.log('5 session control scenarios passed');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
