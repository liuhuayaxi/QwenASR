// Real browser + production Yuketang panel/modules; synthetic read-only HTTP.
// No user settings, cookies, models, or external services are accessed.
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const out = path.resolve(process.env.QWEN_ASR_BROWSER_OUTPUT || 'output/playwright/yuketang-bank');
const pageSource = fs.readFileSync(path.join(root, 'static/course-files.html'), 'utf8');
const section = pageSource.match(/<section id="yuketangSection"[\s\S]*?<\/section>/)[0];
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7L0AAAAASUVORK5CYII=', 'base64');
const requests = [], forbidden = [], results = [];
let activeImages = 0, maximumImages = 0;
const courses = ['Alpha', 'Beta'];
function questions(category) {
  return Array.from({length: 8}, (_, i) => ({id: `q-${category}-${i}`, classroom_id: category === 'Alpha' ? '1' : '2',
    source: 'lesson', source_label: 'Synthetic lesson', activity_title: category, body: `HDB3 ${category} question ${i}`,
    options: [], answer: ['A'], answer_known: true, images: [
      {kind: 'body', path: `images/1/q${i}.png`}, {kind: 'student', path: 'images/1/private.png'}]}));
}
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://localhost');
  if (u.pathname.startsWith('/api/yuketang/')) {
    const record = {path: u.pathname, query: u.search, method: req.method, header: req.headers['x-qwenasr-client']};
    requests.push(record);
    if (record.header !== 'course-files') { forbidden.push(record); res.writeHead(403).end(); return; }
    const category = u.searchParams.get('category') || 'Alpha';
    const cid = category === 'Alpha' ? '1' : '2';
    if (u.pathname.startsWith('/api/yuketang/image/')) {
      if (u.pathname.includes('private')) { res.writeHead(404).end(); return; }
      maximumImages = Math.max(maximumImages, ++activeImages);
      setTimeout(() => { --activeImages; res.writeHead(200, {'Content-Type': 'image/png', 'Cache-Control': 'no-store'}).end(png); }, 30);
      return;
    }
    let value = {};
    if (u.pathname.endsWith('/status')) value = {playwright: true, running: false, job: {status: 'completed'},
      classrooms: [{classroom_id: cid, name: category, course_name: category, pending_activities: []}], counts: {total: 8, lesson: 8}};
    else if (u.pathname.endsWith('/courses')) value = {courses: courses.map((c, i) => ({classroom_id: String(i+1), course_name: c}))};
    else if (u.pathname.endsWith('/questions')) value = {questions: u.searchParams.get('q') === 'no-results' ? [] : questions(category)};
    const send = () => res.writeHead(200, {'Content-Type': 'application/json'}).end(JSON.stringify(value));
    if (u.searchParams.get('q') === 'slow') setTimeout(send, 500); else send();
    return;
  }
  if (u.pathname === '/') {
    res.writeHead(200, {'Content-Type': 'text/html'}).end(`<!doctype html><html><head><meta charset="utf-8"></head><body>
      <nav id="courses"><button class="selected" data-category="Alpha">Alpha</button><button data-category="Beta">Beta</button></nav>
      ${section}<script type="module" src="/static/js/yuketang-bank.js"></script></body></html>`);
    return;
  }
  const file = path.resolve(root, '.' + u.pathname);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) { res.writeHead(404).end(); return; }
  res.writeHead(200, {'Content-Type': file.endsWith('.js') ? 'text/javascript' : 'text/css'}).end(fs.readFileSync(file));
});
(async () => {
  fs.mkdirSync(out, {recursive: true});
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({headless: true, executablePath: process.env.QWEN_ASR_BROWSER_EXECUTABLE || undefined});
    const context = await browser.newContext();
    await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await context.addInitScript(() => {
      window.__blobs = new Set();
      const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
      URL.createObjectURL = blob => { const u = create(blob); __blobs.add(u); return u; };
      URL.revokeObjectURL = u => { __blobs.delete(u); revoke(u); };
    });
    const page = await context.newPage(), errors = [];
    page.setDefaultTimeout(10000);
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(origin);
    await page.locator('#ykt-browseSummary').click();
    await page.waitForFunction(() => document.querySelectorAll('#ykt-questions img').length === 8 &&
      [...document.querySelectorAll('#ykt-questions img')].every(i => i.complete && i.naturalWidth > 0));
    assert.equal(forbidden.length, 0, 'production panel omitted access headers');
    assert(maximumImages <= 4, `too many image requests: ${maximumImages}`);
    assert(!requests.some(r => r.path.includes('private.png')));
    results.push({name: 'protected image reads render with bounded concurrency and exclude student images', passed: true});
    await page.locator('#ykt-search').fill('no-results');
    await page.waitForFunction(() => document.querySelectorAll('#ykt-questions img').length === 0 && __blobs.size === 0);
    results.push({name: 'filter replacement releases all image object URLs', passed: true});
    const started = page.waitForRequest(r => new URL(r.url()).searchParams.get('q') === 'slow');
    await page.locator('#ykt-search').fill('slow');
    await started;
    await page.evaluate(() => {
      document.querySelector('#ykt-search').value = '';
      document.querySelector('#courses .selected').classList.remove('selected');
      document.querySelector('#courses [data-category=Beta]').classList.add('selected');
    });
    await page.waitForFunction(() => document.querySelectorAll('#ykt-questions article').length === 8 &&
      document.querySelector('#ykt-questions').textContent.includes('HDB3 Beta'));
    await page.waitForTimeout(600); // deliberately deliver the older Alpha response
    assert(!await page.locator('#ykt-questions').textContent().then(x => x.includes('HDB3 Alpha')));
    results.push({name: 'late question responses cannot leak into a different course', passed: true});
    const status = await page.evaluate(async () => (await fetch('/api/yuketang/status')).status);
    assert.equal(status, 403);
    assert.deepEqual(errors, []);
    await page.screenshot({path: path.join(out, 'panel.png'), fullPage: true});
    fs.writeFileSync(path.join(out, 'results.json'), JSON.stringify({browser: browser.version(), total: results.length,
      passed: results.length, maximumImages, results}, null, 2));
    console.log(`PASS ${results.length} Yuketang browser scenarios; max concurrent images=${maximumImages}`);
    await context.close();
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
