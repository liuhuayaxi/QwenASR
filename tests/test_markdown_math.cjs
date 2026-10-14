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

test('screenshot LL/LLL/LLLL formulas render with clickable source citations', () => {
  const examples = [
    [String.raw`\text{LL} = 19 \quad \text{[原文 S30](#source-30)}`, ['30']],
    [String.raw`\text{LLL} = 5 \quad \text{[原文 S30](#source-30), [原文 S31](#source-31)}`, ['30','31']],
    [String.raw`\text{LLLL} = 19 + 5 = 24 \quad \text{[原文 S31](#source-31), [原文 S32](#source-32)}`, ['31','32']],
  ];
  for (const [expression, ids] of examples) {
    for (const markdown of [`$${expression}$`, `$$${expression}$$`, `$$\n${expression}\n$$`, String.raw`\(${expression}\)`]) {
      const rendered = context.renderMarkdown(markdown);
      assert.match(rendered,/class="katex/);
      assert.doesNotMatch(rendered,/katex-error|#cc0000/);
      for (const id of ids) assert.match(rendered,new RegExp(`href="#source-${id}"`));
      assert.doesNotThrow(() => katex.renderToString(context.mathWithReferences(expression).text,{throwOnError:true}));
    }
    assert.doesNotMatch(context.mathWithReferences(expression).text,/\\quad|,|source-/);
  }
});

test('array column spacing renders without removing inserted mathematical values', () => {
  const array=String.raw`\begin{array}{r@{\quad}l} &1011\\ -&0010\\\hline &1001\end{array}`;
  const rendered=context.renderMarkdown(`$$\n${array}\n$$`);
  assert.match(rendered,/class="katex/);
  assert.doesNotMatch(rendered,/katex-error/);
  assert.match(rendered,/1011|>1</);
  assert.equal(context.mathWithReferences(String.raw`\begin{array}{r@{=}l}x&y\end{array}`).text,String.raw`\begin{array}{r@{=}l}x&y\end{array}`);
});

test('display formulas followed by references stop before following prose and formulas', () => {
  const text=String.raw`$$k_1 r \sin\theta_1$$ [原文 S71](#source-71)。
反射波矢 $k_1'$ 与法线夹角为 $\theta_1'$。
$$k_1' r \sin\theta_1'$$ [原文 S72](#source-72)。
代入得：
$$k_1 = k_1'$$
`;
  const tokens=marked.lexer(text);tokens.links||={};
  const html=marked.parser(tokens);
  assert.doesNotMatch(html,/katex-error/);
  assert.match(html,/反射波矢/);
  assert.match(html,/href="#source-71"/);
  assert.match(html,/href="#source-72"/);
  assert.equal(tokens.map(token=>token.raw).join(''),text);
  const expressions=[];
  marked.walkTokens(tokens,token=>{if(token.type==='blockMath')expressions.push(token.text)});
  assert.equal(expressions.length,3);
  assert.ok(expressions.every(expression=>!expression.includes('$')&&!expression.includes('反射')));
  assert.doesNotMatch(context.renderMarkdown(String.raw`\[x=1\] 原文紧随公式。`),/katex-error/);
});

test('citation extraction retains math text, order and safe escaped link labels', () => {
  const expression=String.raw`x = 1 [原文 S1](#source-1) + \text{单位 [原文 S2](#source-2)} \text{[原文 S1](#source-1)}`;
  const rendered=context.renderMarkdown(`$${expression}$`);
  assert.doesNotMatch(rendered,/katex-error/);
  assert.match(rendered,/单位/);
  assert.ok(rendered.indexOf('href="#source-1"')<rendered.indexOf('href="#source-2"'));
  assert.equal((rendered.match(/href="#source-1"/g)||[]).length,1);
  const malicious=context.renderMarkdown(String.raw`$x=1\text{[<img src=x onerror=alert(1)>](#source-3)}$`);
  assert.doesNotMatch(malicious,/<img\b|<script\b/);
  assert.match(malicious,/href="#source-3"/);
  assert.doesNotMatch(context.mathWithReferences('[bad](javascript:alert(1))').citations,/javascript:/);
});

test('quoted/list math citations render while code and stored Markdown stay intact', () => {
  const formula=String.raw`\text{LL} = 19 \quad \text{[原文 S30](#source-30)}`;
  const text=`> 1. 求解 LL\n>    - 占用 19 个字节，故：$$${formula}$$\n`;
  const tokens=marked.lexer(text);tokens.links||={};
  const rendered=marked.parser(tokens);
  assert.match(rendered,/href="#source-30"/);
  assert.doesNotMatch(rendered,/katex-error/);
  assert.equal(tokens.map(token=>token.raw).join(''),text);
  for (const code of [`\`${formula}\``,`\`\`\`latex\n$$${formula}$$\n\`\`\``]) {
    const html=context.renderMarkdown(code);
    assert.match(html,/<code/);
    assert.doesNotMatch(html,/class="katex/);
  }
});
