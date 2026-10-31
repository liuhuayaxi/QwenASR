// Production workspace + a minimal embedded library host. HTTP is synthetic;
// live-library evidence is recorded separately in output/playwright/live-audit.
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const out = path.resolve(process.env.QWEN_ASR_BROWSER_OUTPUT || 'output/playwright/workspace-course-context');
const records = [
  {id: 'alpha-1', title: 'Alpha lesson', category: 'Alpha', has_note: true},
  {id: 'beta-1', title: 'Beta lesson 1', category: 'Beta', has_note: true},
  {id: 'beta-2', title: 'Beta lesson 2', category: 'Beta', has_note: true},
];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const json = value => res.writeHead(200, {'Content-Type': 'application/json'}).end(JSON.stringify(value));
  if (url.pathname === '/api/library') {
    const selected = records.filter(record => !url.searchParams.get('category') || record.category === url.searchParams.get('category'));
    json({records: selected, total: selected.length, note_count: selected.length, next_offset: null, categories: ['Alpha', 'Beta']}); return;
  }
  if (url.pathname === '/api/tasks') { json({tasks: []}); return; }
  if (url.pathname === '/api/library/events') { res.writeHead(200, {'Content-Type': 'text/event-stream'}).end(': fixture\n\n'); return; }
  if (url.pathname === '/library') {
    const selected = url.searchParams.get('category') || '';
    res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'}).end(`<!doctype html><html><body>
      <label>分类<select id="libraryCategory">${['', 'Alpha', 'Beta'].map(value => `<option value="${value}" ${value === selected ? 'selected' : ''}>${value || '全部分类'}</option>`).join('')}</select></label>
      <input id="librarySearch" aria-label="全文搜索录音"><p id="draft">library context</p>
      <script>window.libraryIdentity = Math.random();</script></body></html>`); return;
  }
  const file = path.resolve(root, url.pathname === '/workspace' ? 'static/workspace.html' : '.' + url.pathname);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) { res.writeHead(404).end(); return; }
  res.writeHead(200, {'Content-Type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'}).end(fs.readFileSync(file));
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
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin + '/workspace?category=Alpha&tab=records');
    const frame = page.frameLocator('#workspaceFrame');
    await frame.locator('#libraryCategory').waitFor();
    await frame.locator('#librarySearch').fill('keep my search');
    const identity = await frame.locator('body').evaluate(() => window.libraryIdentity);
    await frame.locator('#libraryCategory').selectOption('Beta');
    await page.waitForFunction(() => document.querySelector('#workspaceTitle').textContent === 'Beta' && document.querySelector('#workspaceMeta').textContent.includes('2 堂课堂'));
    assert.equal(await page.locator('#workspaceCourse').inputValue(), 'Beta');
    assert.equal(new URL(page.url()).searchParams.get('category'), 'Beta');
    assert.equal(await frame.locator('#librarySearch').inputValue(), 'keep my search');
    assert.equal(await frame.locator('body').evaluate(() => window.libraryIdentity), identity, 'inner selection must not reload its frame');
    await page.locator('[data-tab="notes"]').click();
    assert.equal(await frame.locator('#libraryCategory').inputValue(), 'Beta');
    assert.equal(await frame.locator('body').evaluate(() => window.libraryIdentity), identity, 'record/notes tabs share the selected course frame');
    await page.reload();
    await frame.locator('#libraryCategory').waitFor();
    assert.equal(await frame.locator('#libraryCategory').inputValue(), 'Beta', 'reload restores selected course from outer URL');
    await frame.locator('#libraryCategory').selectOption('');
    await page.waitForFunction(() => document.querySelector('#workspaceMeta').textContent.includes('3 堂课堂'));
    assert.equal(await page.locator('#workspaceTitle').innerText(), '全部课程');
    assert.equal(new URL(page.url()).searchParams.has('category'), false);
    await page.locator('#workspaceCourse').selectOption('Alpha');
    await page.waitForFunction(() => document.querySelector('#workspaceFrame').contentDocument?.querySelector('#libraryCategory')?.value === 'Alpha');
    assert.deepEqual(errors, []);
    await page.screenshot({path: path.join(out, 'context.png')});
    fs.writeFileSync(path.join(out, 'results.json'), JSON.stringify({passed: true, checks: ['inner selection updates heading/counts/URL', 'inner selection preserves search and frame', 'notes tab and refresh retain course', 'all courses clears URL scope', 'outer selection still navigates frame']}, null, 2));
    console.log('PASS workspace course-context integration (5 checks)');
    await context.close();
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
