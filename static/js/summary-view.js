import { renderMarkdown } from "./markdown-reader.js?v=20261015-reader-math-scope-8";

export function createSummaryView({ els, escapeHtml, formatDuration }) {
  const cards = new Map();

  function refreshEmpty() {
    els.summaryEmpty.classList.toggle("hidden", cards.size > 0);
  }

  function clear() {
    cards.clear();
    els.summaryList.innerHTML = "";
    els.summaryStatus.classList.add("hidden");
    refreshEmpty();
  }

  function renderItem(item, pending = false) {
    const key = String(item.id || item.end || cards.size + 1);
    let card = cards.get(key);
    if (!card) {
      card = document.createElement("article");
      card.className = "summary-card";
      cards.set(key, card);
      els.summaryList.appendChild(card);
    }
    const end = formatDuration(item.end || 0);
    if (pending) {
      card.innerHTML = `<div class="summary-meta"><b>实时累计总结</b><span>截至 ${end}</span></div>
        <p class="ai-pending">正在结合前面内容更新总结${item.parts > 1 ? ` · ${item.parts} 个上下文安全分块` : ""}</p>`;
      refreshEmpty();
      return;
    }
    const points = Array.isArray(item.key_points) ? item.key_points : [];
    card.innerHTML = `<div class="summary-meta"><b>实时累计总结</b><span>截至 ${end}</span></div>
      ${item.summary ? `<div class="summary-text markdown-body">${renderMarkdown(item.summary)}</div>` : ""}
      ${points.length ? `<ul>${points.map((point) => `<li>${escapeHtml(point)}</li>`).join("")}</ul>` : ""}
      ${item.ai_error ? `<p class="ai-error">课堂实时提纲未完成：${escapeHtml(item.ai_error)}</p>` : ""}`;
    refreshEmpty();
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function renderSession(session) {
    clear();
    for (const item of session?.ai_summaries || []) renderItem(item);
  }

  function renderPending(message) {
    els.summaryStatus.textContent = `正在生成课堂实时提纲 ${formatDuration(message.start)}–${formatDuration(message.end)}`;
    els.summaryStatus.classList.remove("hidden");
    renderItem({
      id: `${message.start}-${message.end}`,
      start: message.start,
      end: message.end,
      parts: message.parts,
    }, true);
  }

  function renderResult(item) {
    els.summaryStatus.classList.add("hidden");
    renderItem(item);
  }

  function renderError(message) {
    els.summaryStatus.textContent = `课堂实时提纲未完成：${message}`;
    els.summaryStatus.classList.remove("hidden");
  }

  return { clear, renderError, renderPending, renderResult, renderSession };
}
