import { marked } from "../vendor/marked/marked.esm.js";
import DOMPurify from "../vendor/dompurify/purify.es.mjs";
import { headingTexts, pptJump } from "./ppt-jump.js?v=1";

const escape = (value) => String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function canonicalMathExpression(value) {
  const subscriptDigits = { "₀":"0", "₁":"1", "₂":"2", "₃":"3", "₄":"4", "₅":"5", "₆":"6", "₇":"7", "₈":"8", "₉":"9" };
  let text = String(value || "").trim()
    .replace(/([A-Za-zΑ-Ωα-ω])([₀₁₂₃₄₅₆₇₈₉]+)/g, (_all, symbol, digits) => `${symbol}_${[...digits].map((digit) => subscriptDigits[digit]).join("")}`)
    // TeX commands consume following letters: πa must become \pi a, not \pia.
    .replace(/([θλπΔ])(?=[A-Za-z])/g, "$1 ")
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
function isObviousMath(value) {
  // Keep identifiers, assignments and ordinary code intact. Ambiguous ASCII
  // expressions need explicit $ delimiters; an underscore alone is not math.
  if (!/[≈≠≤≥±×÷√∑∫°]|\\(?:frac|sqrt|sin|cos|tan|arcsin)\b/.test(value)) return false;
  const operands = value.replace(/\\(?:frac|sqrt|sin|cos|tan|arcsin|theta|lambda|pi|Delta|approx|le|ge|times|div|circ)\b/g, "")
    .replace(/\b(?:arcsin|sin|cos|tan|sqrt)\b/g, "");
  return /[\dA-Za-zΑ-Ωα-ω]/.test(value)
    && !/[A-Za-z]{3,}|[A-Za-z0-9]_[A-Za-z]{2,}/.test(operands)
    && !/[^\s\dA-Za-zΑ-Ωα-ω₀-₉²³≈≠≤≥±×÷√∑∫°度_{}()\[\].,+\-*/=^<>]/.test(operands);
}
function mathWithReferences(value) {
  // Source citations are Markdown links, not TeX. AI notes sometimes put
  // them inside \text{}, where the fragment's # becomes a TeX parameter.
  const reference = /\[([^\]\r\n]+)\]\((#source-\d+)\)/g;
  let text = String(value).replace(/\\(?:text|textrm|textnormal)\{([^{}]*)\}/g, (raw, body) => {
    const remainder = body.replace(reference, "").replace(/[\s,，、;；]/g, "");
    return !remainder && body !== body.replace(reference, "") ? body.match(reference).join(" ") : raw;
  });
  // KaTeX supports the array alignment but not TeX's intercolumn @ spacing.
  // Only remove spacing directives; @{=} and other inserted values are kept.
  text = text.replace(/\\begin\{array\}\{((?:[lcr|\s]|@\{\\(?:quad|qquad|enspace)\})+)\}/g,
    (_raw, columns) => `\\begin{array}{${columns.replace(/@\{\\(?:quad|qquad|enspace)\}/g, "")}}`);
  const links = [], seen = new Set();
  text = text.replace(reference, (_raw, label, href) => {
    if (!seen.has(href)) {
      links.push(`<a href="${href}">${escape(label)}</a>`);
      seen.add(href);
    }
    return "";
  }).trim().replace(/(?:\\(?:quad|qquad|enspace)\s*)+$/, "").trim();
  return { text, citations: links.length ? ` <span class="math-citations">${links.join("、")}</span>` : "" };
}
function mathHtml(text, displayMode) {
  const math = mathWithReferences(text);
  try { return window.katex.renderToString(math.text, { displayMode, throwOnError: false, trust: false, strict: "ignore", maxExpand: 500 }) + math.citations; }
  catch { return `<code>${escape(math.text)}</code>` + math.citations; }
}
marked.use({ gfm: true, breaks: false, extensions: [
  // Render legacy AI formula formatting without changing token.raw. Fenced
  // and indented code never enter these inline renderers.
  { name: "codespan", renderer(token) {
    return /^`[^`]/.test(token.raw) && !token.raw.includes("\n") && isObviousMath(token.text)
      ? mathHtml(canonicalMathExpression(token.text), false) : false;
  } },
  { name: "strong", renderer(token) {
    return isObviousMath(token.text) ? mathHtml(canonicalMathExpression(token.text), false) : false;
  } },
  { name: "blockMath", level: "block", start: (src) => src.search(/\$\$|\\\[/),
    tokenizer(src) {
      // End at the first closing delimiter, including when a citation or
      // punctuation follows it. Never consume later formulas to find a newline.
      const match = /^(?: {0,3}\$\$([\s\S]+?)\$\$| {0,3}\\\[([\s\S]+?)\\\])/.exec(src);
      if (match) return { type: "blockMath", raw: match[0], text: (match[1] || match[2]).trim() };
    }, renderer: (token) => mathHtml(token.text, true) },
  { name: "inlineMath", level: "inline", start(src) { return src.search(/\$|\\\(/); },
    tokenizer(src) {
      const match = /^(?:\$\$([^\n]+?)\$\$|\$((?:\\.|[^$\n])+?)\$|\\\(([^\n]+?)\\\))/.exec(src);
      if (match) return { type: "inlineMath", raw: match[0], text: match[1] || match[2] || match[3], display: Boolean(match[1]) };
    }, renderer: (token) => mathHtml(token.text, token.display) },
] });

// CommonMark does not close **…** whose content starts/ends with punctuation
// when CJK text touches it (e.g. **俄歇复合（Auger）**的); render those as <strong>.
export function cjkSafeStrong(markdown) {
  return String(markdown || "").split(/(^```[\s\S]*?^```|\$\$[\s\S]*?\$\$)/m).map((part, index) => index % 2 ? part
    : part.replace(/\*\*([^*\n]{1,120}?)\*\*/g, (raw, inner) => !isObviousMath(inner.replace(/\$[^$]*\$/g, '')) &&
      (/^[\s（(“"《【\[]/.test(inner) || /[）)”"》】\]：:，。；、！？]$/.test(inner) ) ? `<strong>${inner}</strong>` : raw)).join("");
}

export function renderMarkdown(markdown, assetBase = '') {
  const safe = DOMPurify.sanitize(marked.parse(cjkSafeStrong(markdown)), {
    FORBID_TAGS: ["style", "iframe", "form", "input"], FORBID_ATTR: ["autofocus"],
  });
  return assetBase ? safe.replace(/src="assets\/(fig-[a-f0-9]{16}\.png)"/g, (_raw, name) => `src="${assetBase}${name}"`) : safe;
}

export function createMarkdownReader({ container, source, toc, onChange }) {
  let active = null;
  let tokens = [];
  // Section → courseware page links (feature-flagged, stored outside the note).
  let pageLinks = null;
  const viewerHref = (recording, target) => {
    const query = new URLSearchParams({ rec: recording, src: target.source_id,
      page: String(target.viewer_page_start || target.page_start), label: String(target.page_start),
      labelEnd: String(target.page_end || target.page_start), title: target.source_name || "课件" });
    if (target.viewer_page_end) query.set("end", String(target.viewer_page_end));
    if (/^[a-f0-9]{64}$/.test(target.viewer_sha256 || "")) query.set("sha", target.viewer_sha256);
    if (target.viewer_pages) query.set("n", String(target.viewer_pages));
    return `/static/pdf-viewer.html?${query}`;
  };
  // Visible buttons prefetch the shell and at most two tiny previews. The full
  // slide waits until the pointer has stayed for a moment: an immediate click
  // also fires pointerenter, and prefetching the large image then joins the
  // new tab's download and starves the preview. Only one full page is in flight.
  const warmed = new Set();
  const thumbLimit = 2;
  const hoverDwellMs = 700;
  let fullWarm = "";
  const viewerFiles = ["/static/pdf-viewer.html", "/static/pdf-viewer.css?v=12", "/static/js/pdf-viewer-page.js?v=12", "/static/js/pdf-page-viewer.js?v=12"];
  const warm = (url) => {
    const key = "prefetch:" + url;
    if (warmed.has(key)) return false;
    warmed.add(key);
    const link = document.createElement("link");
    link.rel = "prefetch";
    if (/\.webp$/.test(url)) link.as = "image";
    link.href = url;
    document.head.append(link);
    return true;
  };
  const preconnect = () => {
    if (warmed.has("preconnect")) return;
    warmed.add("preconnect");
    const link = document.createElement("link");
    link.rel = "preconnect";
    link.href = location.origin;
    document.head.append(link);
  };
  const warmTarget = (target, eager) => {
    const recording = pageLinks?.recording || "";
    if (!recording || !target?.source_id) return;
    preconnect();
    for (const file of viewerFiles) warm(file);
    if (!/^[a-f0-9]{64}$/.test(target.viewer_sha256 || "")) return;
    const page = target.viewer_page_start || target.page_start;
    const base = `/api/section-page-links/${encodeURIComponent(recording)}/pages/${encodeURIComponent(target.source_id)}/`;
    const thumb = `${base}t${page}.webp`;
    warm(`${base}manifest.json`);
    if (!eager) {
      if (warmed.has(`prefetch:${thumb}`)) return;
      const thumbs = [...warmed].filter((key) => /^prefetch:\/api\/section-page-links\/[^/]+\/pages\/[^/]+\/t\d+\.webp$/.test(key)).length;
      if (thumbs >= thumbLimit) return;
      warm(thumb);
      return;
    }
    warm(thumb);
    const full = `${base}${page}.webp`;
    if (fullWarm && fullWarm !== full) return;
    fullWarm = full;
    warm(full);
  };
  const watched = new Set();
  const jumpObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
    for (const entry of entries) if (entry.isIntersecting) {
      jumpObserver.unobserve(entry.target);
      watched.delete(entry.target);
      warmTarget(entry.target._pptTarget, false);
    }
  }, { rootMargin: "200px 0px" }) : null;
  const watchJump = (jump, target) => {
    jump._pptTarget = target;
    watched.add(jump);
    jumpObserver?.observe(jump);
    let timer = 0;
    const start = () => {
      if (timer) return;
      timer = setTimeout(() => { timer = 0; warmTarget(target, true); }, hoverDwellMs);
    };
    const stop = () => { clearTimeout(timer); timer = 0; };
    jump.addEventListener("pointerenter", start);
    jump.addEventListener("pointerleave", stop);
    jump.addEventListener("focusin", start);
    jump.addEventListener("focusout", stop);
    jump.addEventListener("click", stop, true);
  };
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
    for (const jump of watched) jumpObserver?.unobserve(jump);
    watched.clear();
    const markdown = source.value.replace(/\r\n?/g, "\n");
    tokens = marked.lexer(markdown);
    // Lexer raws preserve source, including structural whitespace and links.
    const fragment = document.createDocumentFragment();
    if (toc) toc.replaceChildren();
    if (!markdown.trim()) {
      const empty = document.createElement("p");
      empty.className = "note-empty";
      empty.textContent = "生成课后复习笔记后，在这里阅读完整课堂笔记。点击段落即可编辑。";
      fragment.append(empty);
    }
    const linkByIndex = new Map((pageLinks?.sections || []).map((entry) => [entry.index, entry]));
    const headings = [];
    let headingIndex = -1;
    for (const [index, token] of tokens.entries()) {
      if (token.type === "space" || token.type === "def") continue;
      if (token.type === "heading") headingIndex += 1;
      const block = document.createElement("div");
      block.className = "markdown-block markdown-body";
      block.dataset.block = index;
      block.tabIndex = 0;
      block.setAttribute("aria-label", "笔记段落，按回车编辑");
      const subset = [token]; subset.links = tokens.links;
      block.innerHTML = DOMPurify.sanitize(marked.parser(subset), { FORBID_TAGS: ["style", "iframe", "form", "input"] });
      if (source.dataset?.assetBase) for (const image of block.querySelectorAll('img')) {
        const value = image.getAttribute('src');
        if (/^assets\/fig-[a-f0-9]{16}\.png$/.test(value || '')) image.src = source.dataset.assetBase + value.slice(7);
      }
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
      if (token.type === "heading") headings.push({ headingIndex, block, token });
      fragment.append(block);
    }
    // Buttons are only attached when the rendered headings and the lexer agree,
    // so a late page-links payload cannot land on the wrong title.
    const aligned = !pageLinks || headingTexts(tokens).length === fragment.querySelectorAll("h1,h2,h3,h4,h5,h6").length;
    if (pageLinks && aligned) {
      for (const item of headings) {
        const entry = linkByIndex.get(item.headingIndex);
        if (!entry || entry.heading !== String(item.token.text || "").trim()) continue;
        const jump = pptJump(document, entry, (target) => viewerHref(pageLinks.recording, target));
        const target = (entry.targets || []).find((candidate) => candidate.viewer_page_start || candidate.page_start);
        if (jump && target) {
          item.block.querySelector("h1,h2,h3,h4,h5,h6")?.append(jump);
          watchJump(jump, target);
        }
      }
    }
    if (!pageLinks) {
      if (container.dataset.pptLinks !== "pending") container.dataset.pptLinks = "off";
    } else container.dataset.pptLinks = aligned ? "ready" : "mismatch";
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
  function setPageLinks(value) {
    pageLinks = value && value.enabled && value.sections?.length ? value : null;
    if (!pageLinks) container.dataset.pptLinks = "off";
  }
  return { render, finish, hasActive: () => Boolean(active), setPageLinks };
}
