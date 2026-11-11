// Exercise the actual offline export, including the Markdown reader's imports.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');

const repo = path.resolve(__dirname, '../..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'qwen-note-export-'));
try {
  const output = path.join(repo, 'output/playwright/note-export.pdf');
  fs.mkdirSync(path.dirname(output), {recursive: true});
  fs.rmSync(output, {force: true});
  const request = path.join(temporary, 'request.json');
  fs.writeFileSync(request, JSON.stringify({
    repo_root: repo, output,
    module_roots: [path.join(__dirname, 'node_modules')],
    markdown: '# 离线笔记导出\n\n正文与公式必须在离线渲染器中正常显示。\n\n$$N=4000$$\n',
    chapters: ['合成测试章节'],
  }));
  const result = spawnSync(process.execPath,
    [path.join(repo, 'scripts/render_review_note.cjs'), request],
    {encoding: 'utf8', timeout: 60000});
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const pdf = fs.readFileSync(output);
  assert.equal(pdf.subarray(0, 5).toString(), '%PDF-');
  assert.ok(pdf.length > 1000, 'offline export returned an empty PDF');
  console.log('Offline note export: Markdown, KaTeX and local module imports passed');
} finally {
  fs.rmSync(temporary, {recursive: true, force: true});
}
