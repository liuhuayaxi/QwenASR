const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/smartclass.js'), 'utf8')
  .replace(/\ncheckSession\(\);\nscheduledRefresh\(\);\s*$/, `
globalThis.selection = {loadLessons, renderLessons, renderCourseState, checkSession, refresh,
  groups(value) { currentGroups = value; renderLessons(); },
  ids() { return [...selectedLessonIds]; }};`);

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.dataset = {}; this.style = {};
    this.value = ''; this.checked = false; this.disabled = false; this.className = '';
  }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text || ''; }
  replaceChildren(...children) { this.text = ''; this.children = children; }
  append(...children) { this.children.push(...children); }
  add(child) { this.append(child); }
  addEventListener(event, callback) { this[event] = callback; }
  querySelectorAll(selector) {
    const matches = node => selector.startsWith('.')
      ? node.className.split(' ').includes(selector.slice(1)) : node.tag === selector;
    return this.children.flatMap(child => [
      ...(matches(child) ? [child] : []), ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

const group = (id, single = false) => ({id, course_name: '测试课程', date: '2026-10-14',
  start: '2026-10-14 08:00', stop: '2026-10-14 09:35', duration: 5640,
  periods: single ? [1] : [1, 2], videos: (single ? [1] : [1, 2]).map(n => ({NewID: `${id}-${n}`}))});
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return {promise, resolve, reject};
};

function environment(groups = [group('a'), group('b')]) {
  const elements = new Map(), requests = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const status = () => ({state: 'running', courses: [
    {key: 'course-a', CourseName: '课程 A', TeacherName: '教师', VideoTotal: 4},
    {key: 'course-b', CourseName: '课程 B', TeacherName: '教师', VideoTotal: 2},
  ], course_revision: null, options: {}, imports: [], lesson_course: element('courses').value});
  const env = {element, requests, lessonResponse: async () => ({groups}),
    importResponse: async () => ({added: ['a']}), status};
  const context = {URL, URLSearchParams, Date, clearTimeout() {},
    Option: function(text, value) { const node = new Element('option'); node.textContent = text; node.value = value; return node; },
    location: {origin: 'http://127.0.0.1:8765', search: ''},
    document: {getElementById: element, createElement: tag => new Element(tag), addEventListener() {}},
    window: {addEventListener() {}, setTimeout() { return 1; }, clearTimeout() {}},
    async fetch(url, init) {
      requests.push({url, body: init?.body ? JSON.parse(init.body) : null});
      const data = url.startsWith('/api/lessons?') ? await env.lessonResponse(url)
        : url === '/api/import' ? await env.importResponse()
        : url.startsWith('/api/status') ? env.status() : {saved: true};
      return {ok: true, json: async () => data};
    },
  };
  vm.createContext(context); vm.runInContext(source, context);
  env.api = context.selection;
  element('courses').value = 'course-a';
  env.api.groups(groups);
  env.input = id => element('lessons').querySelectorAll('input').find(input => input.value === id);
  env.choose = (id, checked = true) => { const input = env.input(id); input.checked = checked; input.onchange(); };
  env.ids = () => Array.from(env.api.ids());
  return env;
}

test('checking a lesson updates the count and polling keeps the same checkbox and choice', () => {
  const env = environment();
  assert.equal(env.element('import').disabled, true);
  env.choose('a');
  const input = env.input('a');
  assert.equal(env.element('import').disabled, false);
  assert.equal(env.element('selectionStatus').textContent, '已选择 1 个课次');
  assert.match(env.element('import').textContent, /1 个/);
  env.api.renderCourseState(env.status());
  env.api.renderCourseState(env.status());
  assert.equal(env.input('a'), input);
  assert.equal(input.checked, true);
  env.choose('a', false);
  assert.deepEqual(env.ids(), []);
  assert.equal(env.element('import').disabled, true);
});

test('login refresh exposes cancel and keeps the cached course selector available', async () => {
  const env = environment();
  env.api.renderCourseState({...env.status(), state:'syncing', session_check:'checking'});
  assert.equal(env.element('courses').disabled, false);
  assert.equal(env.element('cancelConnection').hidden, false);
  await env.element('cancelConnection').onclick();
  assert.equal(env.requests[0].url, '/api/cancel-connection');
  await env.api.checkSession();
  assert.equal(env.requests.some(r => r.url === '/api/ensure-session'), false);
  assert.equal(env.element('cancelConnection').disabled, false);
});

test('lost service releases disabled login controls and explains shutdown', async () => {
  const env = environment();
  env.element('connect').disabled = env.element('reconnect').disabled = true;
  env.api.renderCourseState({...env.status(), state:'syncing'});
  env.status = () => { throw Error('offline'); };
  await env.api.refresh();
  assert.equal(env.element('connect').disabled, false);
  assert.equal(env.element('reconnect').disabled, false);
  assert.equal(env.element('courses').disabled, false);
  assert.match(env.element('state').textContent, /已停止或连接中断/);
});

test('refresh leaves the list visible and retains choices made while its request is pending', async () => {
  const env = environment(), response = deferred();
  env.choose('a'); env.lessonResponse = () => response.promise;
  const loading = env.api.loadLessons();
  assert.equal(env.input('a').checked, true);
  assert.equal(env.element('import').disabled, true);
  env.choose('b');
  response.resolve({groups: [group('a'), group('b')]}); await loading;
  assert.equal(env.input('a').checked, true);
  assert.equal(env.input('b').checked, true);
  assert.deepEqual(env.ids(), ['a', 'b']);
  assert.equal(env.element('import').disabled, false);
  assert.equal(env.element('lessonsStatus').hidden, true);
});

test('failed refresh and a later retry preserve selection, while removed lessons are pruned', async () => {
  const env = environment(); env.choose('a'); env.choose('b');
  env.lessonResponse = async () => { throw Error('网络中断'); };
  await env.api.loadLessons();
  assert.equal(env.input('a').checked, true);
  assert.equal(env.element('import').disabled, false);
  assert.match(env.element('lessonsStatus').textContent, /选择已保留.*网络中断/);
  env.lessonResponse = async () => ({groups: [group('a')]});
  await env.api.loadLessons();
  assert.deepEqual(env.ids(), ['a']);
  assert.equal(env.input('a').checked, true);
});

test('a response from the previous course cannot overwrite the new course or its selection', async () => {
  const env = environment(), old = deferred(), next = deferred();
  env.choose('a'); env.lessonResponse = () => old.promise;
  const first = env.api.loadLessons();
  env.element('courses').value = 'course-b'; env.lessonResponse = () => next.promise;
  env.element('courses').onchange();
  assert.deepEqual(env.ids(), []);
  next.resolve({groups: [group('new')]});
  await new Promise(resolve => setImmediate(resolve));
  env.choose('new'); old.resolve({groups: [group('a')]}); await first;
  assert.deepEqual(env.ids(), ['new']);
  assert.equal(env.input('a'), undefined);
  assert.equal(env.input('new').checked, true);
});

test('turning off single-period import during a refresh removes the invalid choice', async () => {
  const env = environment([group('a'), group('single', true)]), response = deferred();
  assert.equal(env.input('single').disabled, true);
  env.element('allowSingle').checked = true; env.element('allowSingle').onchange(); env.choose('single');
  env.lessonResponse = () => response.promise; const loading = env.api.loadLessons();
  env.element('allowSingle').checked = false; env.element('allowSingle').onchange();
  assert.equal(env.input('single').disabled, true);
  assert.deepEqual(env.ids(), []);
  response.resolve({groups: [group('a'), group('single', true)]}); await loading;
  assert.equal(env.input('single').checked, false);
  assert.equal(env.element('import').disabled, true);
});

test('failed import keeps the choice; success clears only submitted lessons', async () => {
  const env = environment(); env.choose('a');
  env.importResponse = async () => { throw Error('提交失败'); };
  await env.element('import').onclick();
  assert.deepEqual(env.ids(), ['a']);
  assert.equal(env.input('a').checked, true);
  assert.equal(env.element('import').disabled, false);
  const response = deferred(); env.importResponse = () => response.promise;
  const submitting = env.element('import').onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(env.requests.at(-1).body.ids, ['a']);
  env.choose('b'); response.resolve({added: ['a']}); await submitting;
  assert.deepEqual(env.ids(), ['b']);
  assert.equal(env.input('a').checked, false);
  assert.equal(env.input('b').checked, true);
  assert.equal(env.element('selectionStatus').textContent, '已选择 1 个课次');
});

test('import completion from a previous course does not clear a new selection', async () => {
  const env = environment(), response = deferred(); env.choose('a');
  env.importResponse = () => response.promise;
  const submitting = env.element('import').onclick();
  await new Promise(resolve => setImmediate(resolve));
  env.element('courses').value = 'course-b';
  env.lessonResponse = async () => ({groups: [group('a')]}); env.element('courses').onchange();
  await new Promise(resolve => setImmediate(resolve)); env.choose('a');
  response.resolve({added: ['a']}); await submitting;
  assert.deepEqual(env.ids(), ['a']);
  assert.equal(env.input('a').checked, true);
});

test('course timing UI preserves unknown durations and labels measured loading separately', () => {
  const env = environment();
  const status = env.status();
  const render = metrics => {
    status.imports = [{state:'queued', title:'课堂任务', metrics}];
    env.api.renderCourseState(status);
    return env.element('result').querySelectorAll('p').at(-1).textContent;
  };
  const unknown = render({first_text_seconds:50.9, asr_queue_seconds:null,
    asr_inference_seconds:null, asr_load_seconds:null});
  assert.match(unknown, /首段文字 50\.9 秒/);
  assert.match(unknown, /ASR 排队 未记录.*纯推理 未记录.*模型加载 未记录/);
  assert.doesNotMatch(unknown, /0\.0 秒/);
  // The old service returned fabricated zero totals before nested receipts
  // were counted. Cached pages must not turn those into a timing claim.
  assert.match(render({first_text_seconds:1,asr_queue_seconds:0,asr_inference_seconds:0}),
    /ASR 排队 未记录.*纯推理 未记录/);
  const measured = render({first_text_seconds:0,asr_queue_seconds:0,
    asr_inference_seconds:91.01,asr_load_seconds:69.81,
    asr_timing_observations:{asr_queue_seconds:4,asr_inference_seconds:13,asr_load_seconds:13}});
  assert.match(measured, /首段文字 0\.0 秒.*ASR 排队 0\.0 秒.*纯推理 91\.0 秒.*模型加载 69\.8 秒/);
  assert.match(measured, /已完成片段累计/);
});
