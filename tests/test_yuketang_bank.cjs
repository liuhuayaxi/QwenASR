const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const renderPath = path.join(__dirname, '../static/js/yuketang-render.js');
const modulePromise = import(pathToFileURL(renderPath).href);
const knowledgeSource = fs.readFileSync(path.join(__dirname, '../static/js/course-knowledge.js'), 'utf8')
  .replace(/from "\.\/yuketang-render\.js[^"]*"/, `from "${pathToFileURL(renderPath).href}"`);
const knowledgePromise = import(`data:text/javascript;base64,${Buffer.from(knowledgeSource).toString('base64')}`);

test('counts summary names every source and the vision backlog', async () => {
  const {countsText} = await modulePromise;
  assert.equal(countsText({}), '还没有题目');
  assert.equal(countsText({total: 9, exam: 5, lesson: 4, courseware: 0, wrong: 2, needs_vision: 3, vision_done: 1}),
    '共 9 题：课前/课后测试与考试 5，课上习题 4（答错 2，图片转写 1/3）');
});

test('question card escapes content and links stored images only', async () => {
  const {questionHtml} = await modulePromise;
  const html = questionHtml({classroom_id: '26260946', source_label: '课上习题', activity_title: '<b>绪论</b>', date: '2026-10-21', page: 3,
    type_text: '单选题', body: 'a < b?', options: [{key: 'A', value: '对'}], answer: ['A'], student_answer: ['B'], correct: false,
    score: 0, max_score: 2, images: [{kind: 'slide', path: 'images/1/p 3.jpg', source: 'https://x/y.jpg'}, {kind: 'slide', source: 'https://x/z.jpg'}]});
  assert.match(html, /&lt;b&gt;绪论&lt;\/b&gt;/);
  assert.match(html, /a &lt; b\?/);
  assert.match(html, /答错/);
  assert.match(html, /我的答案：<\/b>B（0\/2 分）/);
  assert.equal((html.match(/<img /g) || []).length, 1);
  assert.match(html, /\/api\/yuketang\/image\/26260946\/images\/1\/p%203\.jpg/);
});

test('current course comes from the selected course button', async () => {
  const {currentCategory} = await modulePromise;
  const doc = {querySelector: () => ({dataset: {category: '通信原理 C'}})};
  assert.equal(currentCategory(doc), '通信原理 C');
});

test('legacy vision inference is not displayed as a published correct answer', async () => {
  const {questionHtml} = await modulePromise;
  const q = {body: '题目', answer: [], images: [], vision: {status: 'done', answer_text: 'B'}};
  assert.match(questionHtml(q), /正确答案：<\/b>未公布/);
  q.vision.version = 'teacher-evidence-v2';
  assert.match(questionHtml(q), /正确答案：<\/b>B/);
});

test('LaTeX renders through the vendored KaTeX while plain text stays escaped', async () => {
  const {mathText} = await modulePromise;
  const katex = require(path.join(__dirname, '../static/vendor/katex/katex.min.js'));
  const html = mathText('波长 $\\lambda_0 = 1.31\\,\\mu\\mathrm{m}$，角度 \\(\\theta_{\\perp}\\) <script>x</script> $$\\frac{a}{b}$$ \\[E=mc^2\\]', katex);
  assert.equal((html.match(/class="katex"/g) || []).length, 4);
  assert.equal((html.match(/katex-display/g) || []).length, 2);
  assert.match(html, /&lt;script&gt;x&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  // Formulas cannot inject markup either: KaTeX runs with trust off.
  assert.doesNotMatch(mathText('$\\href{javascript:alert(1)}{x}$', katex), /href="javascript/);
  // Without KaTeX the source is shown escaped, never as raw HTML.
  assert.equal(mathText('$a<b$', undefined), '$a&lt;b$');
  assert.equal(mathText('价格 5 元', katex), '价格 5 元');
});

test('question card renders formulas in body, options, answers and my answer', async () => {
  const {questionHtml} = await modulePromise;
  const calls = [];
  globalThis.katex = {renderToString: tex => { calls.push(tex); return `<span class="katex">${tex.length}</span>`; }};
  try {
    questionHtml({classroom_id: '1', source_label: '课上习题', type_text: '单选题', body: '求 $x^2$', options: [{key: 'A', value: '\\(y\\)'}],
      answer: ['$z$'], remark: '$$w$$', student_answer: ['$v$'], images: []});
  } finally { delete globalThis.katex; }
  assert.deepEqual(calls.sort(), ['v', 'w', 'x^2', 'y', 'z']);
});

test('panel modules import the shared renderer', () => {
  const bank = fs.readFileSync(path.join(__dirname, '../static/js/yuketang-bank.js'), 'utf8');
  assert.match(bank, /from '\.\/yuketang-render\.js/);
  assert.doesNotMatch(bank, /ykt-classroom|ykt-university/, 'manual classroom/university inputs are gone');
});

test('binding area: hidden when bound, dropdown on 改绑 or unbound, login without a course list', async () => {
  const {bindView} = await modulePromise;
  const courses = {suggested: '26549233', courses: [
    {classroom_id: '26260946', university_id: '3278', course_name: '通信原理C', classroom_name: '班级甲', teacher: '教师甲'},
    {classroom_id: '26549233', university_id: '0', course_name: '光电子学', classroom_name: '', teacher: '教师乙'}]};
  const bound = {classrooms: [{classroom_id: '26260946'}]};
  assert.deepEqual(bindView(bound, courses), {mode: 'hidden'});
  const rebind = bindView(bound, courses, true);
  assert.equal(rebind.mode, 'select');
  assert.equal(rebind.selected, '26260946', 'current binding is preselected while re-binding');
  assert.deepEqual(rebind.options[0], {value: '26260946', label: '通信原理C · 班级甲 · 教师甲', university: '3278'});
  assert.equal(rebind.options[1].label, '光电子学 · 教师乙');
  assert.equal(bindView({classrooms: []}, courses).selected, '26549233', 'suggested course is preselected');
  assert.deepEqual(bindView({classrooms: []}, {courses: []}), {mode: 'login'});
});

test('header line shows bound course, counts and last sync', async () => {
  const {headerLine} = await modulePromise;
  assert.equal(headerLine({classrooms: []}), '未绑定雨课堂课程');
  const line = headerLine({classrooms: [{classroom_id: '1', course_name: '光电子学', last_sync: '2026-10-22T15:01:00+08:00'}], counts: {total: 19, wrong: 2}});
  assert.match(line, /^光电子学 · 19 题（答错 2） · 上次同步 10\/22 \d\d:01$/);
  assert.match(headerLine({classrooms: [{classroom_id: '1', name: 'A'}, {classroom_id: '2'}], counts: {}}), /^A 等 2 个班级 · 尚无题目 · 从未同步$/);
});

test('course 检索 also returns ranked 雨课堂 hits, labeled and rendered with KaTeX', async () => {
  const {searchYuketang, yuketangHitsHtml} = await knowledgePromise;
  const urls = [];
  const fetchJson = async url => {
    urls.push(url);
    if (url.includes('%E5%85%89')) return {questions: [{classroom_id: '1', source_label: '课上习题', type_text: '单选题', body: '发散角 $\\theta$', _score: 2, images: []}]};
    throw new Error('not bound');
  };
  const hits = await searchYuketang(['光电子学', '未绑定课'], '发散角', fetchJson);
  assert.equal(hits.length, 1);
  assert.equal(hits[0]._category, '光电子学');
  assert.ok(urls.every(url => url.includes('mode=rank')));
  const katex = require(path.join(__dirname, '../static/vendor/katex/katex.min.js'));
  globalThis.katex = katex;
  try {
    const html = yuketangHitsHtml(hits, true);
    assert.match(html, /雨课堂习题 · 光电子学/);
    assert.match(html, /ykt-hit/);
    assert.match(html, /class="katex"/);
  } finally { delete globalThis.katex; }
});

test('course page: 雨课堂 section sits right below 检索 and review tools are gone', () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/course-files.html'), 'utf8');
  const sections = [...html.matchAll(/^    <(?:section|details) id="([^"]+)"/gm)].map(match => match[1]);
  assert.equal(sections[sections.indexOf('knowledgeSection') + 1], 'yuketangSection');
  assert.doesNotMatch(html, /review-tools|导入复习资料|保存位置/);
  assert.doesNotMatch(fs.readFileSync(path.join(__dirname, '../static/js/course-files.js'), 'utf8'), /review-materials\.js/);
});

test('sync time is truthful: last success, or an honest failure with the last success', async () => {
  const {syncText} = await modulePromise;
  assert.equal(syncText([]), '从未同步');
  assert.match(syncText([{last_sync: '2026-10-22T15:28:56+08:00', last_attempt_at: '2026-10-22T15:28:56+08:00', last_attempt_status: 'ok'}]), /^上次同步 10\/22 \d\d:28$/);
  const failed = syncText([{last_sync: '2026-10-22T15:01:00+08:00', last_attempt_at: '2026-10-22T16:05:00+08:00', last_attempt_status: 'login_required'}]);
  assert.match(failed, /^上次同步失败（需要登录） 10\/22 \d\d:05（上次成功 10\/22 \d\d:01）$/);
  assert.match(syncText([{last_sync: '', last_attempt_at: '2026-10-22T16:05:00+08:00', last_attempt_status: 'failed'}]), /^上次同步失败 .*（上次成功 无）$/);
  // An old failure followed by a success is not shown.
  assert.match(syncText([{last_sync: '2026-10-22T17:00:00+08:00', last_attempt_at: '2026-10-22T17:00:00+08:00', last_attempt_status: 'ok'}]), /^上次同步 /);
});
