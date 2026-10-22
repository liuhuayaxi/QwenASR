const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/js/yuketang-bank.js'), 'utf8')
  .replace(/^import .*visible-poll\.js';$/m, 'const createVisiblePoll = () => ({refresh: async () => {}});');
const modulePromise = import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

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
