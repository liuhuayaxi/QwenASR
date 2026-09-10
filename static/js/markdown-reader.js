import { marked } from "../vendor/marked/marked.esm.js";
import DOMPurify from "../vendor/dompurify/purify.es.mjs";

const escape = (value) => String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function canonicalMathExpression(value) {
  const subscriptDigits = { "₀":"0", "₁":"1", "₂":"2", "₃":"3", "₄":"4", "₅":"5", "₆":"6", "₇":"7", "₈":"8", "₉":"9" };
  let text = String(value || "").trim()
    .replace(/([A-Za-zΑ-Ωα-ω])([₀₁₂₃₄₅₆₇₈₉]+)/g, (_all, symbol, digits) => `${symbol}_${[...digits].map((digit) => subscriptDigits[digit]).join("")}`)
    .replaceAll("θ", "\\theta").replaceAll("λ", "\\lambda")
    .replaceAll("π", "\\pi").replaceAll("Δ", "\\Delta")
    .replace(/\bn([012])\b/g, "n_$1")
    .replaceAll("²", "^2").replaceAll("³", "^3")
    .replaceAll("≈", "\\approx ").replaceAll("≤", "\\le ").replaceAll("≥", "\\ge ")
    .replaceAll("×", "\\times ").replaceAll("÷", "\\div ")
    .replace(/(^|[^\\\w])(arcsin|sin|cos|tan)\b/g, "$1\\$2")
    .replace(/(^|[^\\])sqrt\(([^()]+)\)/gi, "$1\\sqrt{$2}")
    .replace(/√\(([^()]+)\)/g, "\\sqrt{$1}")
    .replace(/(\d+(?:\.\d+)?)\s*(?:度|°)/g, "$1^{\\circ}");
  return text.replace(/\s+/g, " ").trim();
}
export function normalizeMarkdownMath(markdown) {
  const normalized = String(markdown || "").replace(/(^|[^`])`([^`\n]+)`(?!`)/gm, (whole, prefix, raw) => {
    const value = raw.trim();
    const hasOperator = /[=≈≠≤≥±×÷√∑∫]|(?:^|\s)[+\-*/](?:\s|$)/.test(value);
    const hasOperand = /\d|[A-Za-zα-ωΑ-Ωθλμσπω]|\\(?:frac|sqrt|sin|cos|tan)/.test(value);
    const hasFormulaShape = /(?:\d+(?:\.\d+)?°|[_^][A-Za-z0-9{}]+|\b(?:sin|cos|tan|arcsin|sqrt)\s*\()/i.test(value);
    return hasOperand && (hasOperator || hasFormulaShape)
      ? `${prefix}$${canonicalMathExpression(value)}$` : whole;
  });
  return normalized.replace(/\*\*([^*\n]+?)\*\*/g, (whole, raw) => {
    const value = raw.trim();
    if (value.includes("$")) return whole;
    const hasOperator = /[=≈≠≤≥<>±×÷√∑∫]/.test(value);
    const hasOperand = /\d|[A-Za-zα-ωΑ-Ω]/.test(value);
    const proseChars = (value.match(/[\u4e00-\u9fff]/g) || []).length;
    return hasOperator && hasOperand && proseChars <= 3
      ? `$${canonicalMathExpression(value)}$` : whole;
  });
}
function mathHtml(text, displayMode) {
  try { return window.katex.renderToString(text, { displayMode, throwOnError: false, trust: false, strict: "ignore", maxExpand: 500 }); }
  catch { return `<code>${escape(text)}</code>`; }
}
marked.use({ gfm: true, breaks: false, extensions: [
  { name: "blockMath", level: "block", start: (src) => src.indexOf("$$"),
    tokenizer(src) {
      const match = /^(?: {0,3}\$\$([\s\S]+?)\$\$| {0,3}\\\[([\s\S]+?)\\\])(?:[ \t]*\n|$)/.exec(src);
      if (match) return { type: "blockMath", raw: match[0], text: (match[1] || match[2]).trim() };
    }, renderer: (token) => mathHtml(token.text, true) },
  { name: "inlineMath", level: "inline", start(src) { return src.search(/\$|\\\(/); },
    tokenizer(src) {
      const match = /^(?:\$\$([^\n]+?)\$\$|\$((?:\\.|[^$\n])+?)\$|\\\(([^\n]+?)\\\))/.exec(src);
      if (match) return { type: "inlineMath", raw: match[0], text: match[1] || match[2] || match[3], display: Boolean(match[1]) };
    }, renderer: (token) => mathHtml(token.text, token.display) },
] });

export function renderMarkdown(markdown) {
  return DOMPurify.sanitize(marked.parse(normalizeMarkdownMath(markdown)), {
    FORBID_TAGS: ["style", "iframe", "form", "input"], FORBID_ATTR: ["autofocus"],
  });
}

export function createMarkdownReader({ container, source, toc, onChange }) {
  let active = null;
  let tokens = [];
  const editedRaw = (input, original) => {
    const ending = original.match(/\n+$/)?.[0] || "";
    return input.value.replace(/\n*$/, "") + ending;
  };

  function updateSource() {
    source.value = tokens.map((token) => token.raw).join("");
    onChange?.();
  }
  function finish(cancel = false) {
    if (!active) return;
    const { input, token, original } = active;
    token.raw = cancel ? original : editedRaw(input, original);
    active = null;
    updateSource();
    const scrollTop = container.scrollTop;
    render();
    container.scrollTop = scrollTop;
  }
  function begin(token, element) {
    if (active?.token === token) return;
    // Commit before rebuilding, then locate by the original token index.
    const index = tokens.indexOf(token);
    if (active) {
      finish();
      token = tokens[index];
      element = container.querySelector(`[data-block="${index}"]`);
      if (!token || !element) return;
    }
    const input = document.createElement("textarea");
    input.className = "markdown-block-input";
    input.setAttribute("aria-label", "编辑当前 Markdown 段落");
    input.spellcheck = false;
    input.value = token.raw;
    const original = token.raw;
    element.classList.add("editing");
    element.replaceChildren(input);
    const resize = () => { input.style.height = "auto"; input.style.height = `${Math.max(70, input.scrollHeight + 2)}px`; };
    input.addEventListener("input", () => { token.raw = editedRaw(input, original); updateSource(); resize(); });
    input.addEventListener("keydown", (event) => {
      if (event.isComposing) return;
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); finish(true); }
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") { event.preventDefault(); finish(); }
    });
    input.addEventListener("blur", () => { if (active?.input === input) finish(); });
    active = { input, token, original };
    resize();
    input.focus({ preventScroll: true });
  }
  function render() {
    active = null;
    const markdown = normalizeMarkdownMath(source.value.replace(/\r\n?/g, "\n"));
    tokens = marked.lexer(markdown);
    // Lexer raws preserve source, including structural whitespace and links.
    const fragment = document.createDocumentFragment();
    if (toc) toc.replaceChildren();
    if (!markdown.trim()) {
      const empty = document.createElement("p");
      empty.className = "note-empty";
      empty.textContent = "生成 AI 总结后，在这里阅读完整课堂笔记。点击段落即可编辑。";
      fragment.append(empty);
    }
    for (const [index, token] of tokens.entries()) {
      if (token.type === "space" || token.type === "def") continue;
      const block = document.createElement("div");
      block.className = "markdown-block markdown-body";
      block.dataset.block = index;
      block.tabIndex = 0;
      block.setAttribute("aria-label", "笔记段落，按回车编辑");
      const subset = [token]; subset.links = tokens.links;
      block.innerHTML = DOMPurify.sanitize(marked.parser(subset), { FORBID_TAGS: ["style", "iframe", "form", "input"] });
      block.addEventListener("click", (event) => {
        if (event.target.closest("a,button,textarea") || window.getSelection()?.toString()) return;
        begin(token, block);
      });
      block.addEventListener("keydown", (event) => {
        if (event.target === block && event.key === "Enter") { event.preventDefault(); begin(token, block); }
      });
      if (token.type === "heading" && token.depth <= 3 && toc) {
        const link = document.createElement("button");
        link.type = "button"; link.className = `toc-level-${token.depth}`;
        link.textContent = block.textContent;
        link.addEventListener("click", () => { finish(); container.querySelector(`[data-block="${index}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" }); });
        toc.append(link);
      }
      fragment.append(block);
    }
    container.replaceChildren(fragment);
  }
  container.addEventListener("pointerdown", (event) => {
    if (!active || event.target.closest("a,button,textarea") || event.button !== 0) return;
    const block = event.target.closest("[data-block]");
    if (!block) return;
    const index = Number(block.dataset.block);
    if (tokens[index] === active.token) return;
    event.preventDefault();
    const offset = tokens.slice(0, index).reduce((sum, token) => sum + token.raw.length, 0);
    finish();
    let cursor = 0;
    const newIndex = tokens.findIndex((token) => { const at = cursor; cursor += token.raw.length; return at === offset; });
    const next = container.querySelector(`[data-block="${newIndex}"]`);
    if (next) begin(tokens[newIndex], next);
  });
  return { render, finish, hasActive: () => Boolean(active) };
}
