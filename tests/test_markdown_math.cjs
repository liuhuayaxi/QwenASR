const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const root = path.join(__dirname, '..');
const katex = require(path.join(root, 'static/vendor/katex/katex.min.js'));
const { marked } = require(path.join(root, 'static/vendor/marked/marked.esm.js'));
const source = fs.readFileSync(path.join(root, 'static/js/markdown-reader.js'), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
const context = vm.createContext({
  marked, window: { katex },
  // This test targets the real Markdown + KaTeX rendering before sanitization.
  DOMPurify: { sanitize: value => value },
});
vm.runInContext(source, context);

test('screenshot formulas render pi followed by a separate variable', () => {
  for (const formula of ['V = (2πa / λ) √(n₁² - n₂²)', 'V = (2πa / λ) × NA', 'Δn ≈ θx + λf']) {
    for (const delimiter of ['**', '`']) {
      const rendered = context.renderMarkdown(`${delimiter}${formula}${delimiter}`);
      assert.match(rendered, /class="katex"/);
      assert.doesNotMatch(rendered, /katex-error|#cc0000|\\pia|\\Deltan|\\thetax|\\lambdaf/);
      assert.doesNotThrow(() => katex.renderToString(context.canonicalMathExpression(formula), { throwOnError: true }));
    }
  }
});

test('valid inline and display LaTeX keep rendering', () => {
  for (const text of [String.raw`$V = \frac{2\pi a}{\lambda}\sqrt{n_1^2-n_2^2}$`, '$$\nV = 2\\pi a\n$$']) {
    const rendered = context.renderMarkdown(text);
    assert.match(rendered, /class="katex/);
    assert.doesNotMatch(rendered, /katex-error|#cc0000/);
  }
});

test('real code and fenced formula examples stay code', () => {
  for (const text of ['`git status`', '`max_tokens`', '```text\n**V = (2πa / λ) × NA**\n```']) {
    assert.match(context.renderMarkdown(text), /<code/);
    assert.doesNotMatch(context.renderMarkdown(text), /class="katex/);
  }
});

test('rendering does not rewrite Markdown used by the editor', () => {
  const text = '**V = (2πa / λ) × NA**\n\n> - **Δn ≈ θx**\n';
  const tokens = marked.lexer(text);
  tokens.links ||= {};
  marked.parser(tokens);
  assert.equal(tokens.map(token => token.raw).join(''), text);
});
