import { renderMarkdown } from "./markdown-reader.js?v=20261015-reader-math-scope-8";

export function createTranscriptView({
  els, state, toast, updateButtons, switchView, onSessionShown = () => {},
  onRetrySegment = () => {},
}) {
  let followLatest = true;
  let pendingScrollFrame = 0;
  const reviewWindows = new Map();

  function isNearLatest() {
    const container = els.transcript;
    return container.scrollHeight - container.scrollTop - container.clientHeight <= 72;
  }

  function scrollToLatest({ force = false, behavior = "smooth" } = {}) {
    if (!force && !followLatest) return;
    if (pendingScrollFrame) cancelAnimationFrame(pendingScrollFrame);
    pendingScrollFrame = requestAnimationFrame(() => {
      pendingScrollFrame = 0;
      if (!force && !followLatest) return;
      const container = els.transcript;
      container.scrollTo({ top: container.scrollHeight, behavior });
    });
  }

  function pauseFollowing() {
    followLatest = false;
    if (pendingScrollFrame) {
      cancelAnimationFrame(pendingScrollFrame);
      pendingScrollFrame = 0;
    }
    // Cancel a possibly unfinished smooth auto-scroll before the user's own
    // wheel/touch/scrollbar movement takes over.
    els.transcript.scrollTo({ top: els.transcript.scrollTop, behavior: "auto" });
  }

  els.transcript.addEventListener("wheel", (event) => {
    if (event.deltaY < 0) pauseFollowing();
  }, { passive: true });
  els.transcript.addEventListener("touchstart", () => {
    pauseFollowing();
  }, { passive: true });
  els.transcript.addEventListener("pointerdown", () => {
    pauseFollowing();
  }, { passive: true });
  els.transcript.addEventListener("scroll", () => {
    // Programmatic smooth scrolling can emit intermediate events far from the
    // bottom, so only re-enable following here. Explicit user gestures above
    // are responsible for pausing it.
    if (isNearLatest()) followLatest = true;
  }, { passive: true });

  function formatDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    const hours = String(Math.floor(total / 3600)).padStart(2, "0");
    const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
    const secs = String(total % 60).padStart(2, "0");
    return `${hours}:${minutes}:${secs}`;
  }

  function showSession(session, renderSegments = false) {
    if (!session) return;
    followLatest = true;
    switchView("transcript");
    state.shownSession = session;
    els.sessionBar.classList.remove("hidden");
    const active = session.status === "active";
    const statusText = active
      ? state.capturing ? "会话进行中 · 正在收音" : "会话进行中 · 已暂停"
      : session.status === "interrupted" || session.status === "failed"
        ? "批量转写已暂停 · 可在课堂记录继续"
        : session.kind === "upload" ? "上传录音 · 已完成" : "会话已结束 · AAC 已生成";
    els.sessionState.textContent = `${session.title || "未命名录音"} · ${statusText}`;
    const when = session.started_at ? new Date(session.started_at).toLocaleString() : "";
    const source = session.kind === "upload" && session.source_filename
      ? ` · ${session.source_filename}` : "";
    els.sessionInfo.textContent = `${session.category || "未分类"} · ${when} · ${formatDuration(session.duration)}${source}`;
    const canDownload = session.status === "completed" && session.downloads;
    if (canDownload) {
      els.bundleDownload.href = session.downloads.bundle;
      els.audioDownload.href = session.downloads.audio;
      els.textDownload.href = session.downloads.raw_transcript || session.downloads.transcript;
      els.correctedTextDownload.href = session.downloads.corrected_transcript || session.downloads.transcript;
      els.downloadLinks.classList.remove("hidden");
    } else {
      els.downloadLinks.classList.add("hidden");
      [els.bundleDownload, els.audioDownload, els.textDownload, els.correctedTextDownload]
        .forEach((link) => link.removeAttribute("href"));
    }
    if (renderSegments) {
      renderSavedSegments(session.segments || []);
      renderSavedReviews(session.ai_reviews || []);
    }
    onSessionShown(session);
    updateButtons();
  }

  function renderSavedSegments(segments) {
    clearAll();
    for (const segment of segments) {
      renderFinal({ ...segment, ai_pending: false });
      if (segment.corrected || segment.translation) renderAiResult(segment);
      if (segment.ai_error) {
        renderAiError({ id: segment.id, message: segment.ai_error, historical: true });
      }
    }
  }

  function renderSavedReviews(reviews) {
    for (const review of [...reviews].sort((left, right) =>
      Number(left.window_end || left.end || 0) - Number(right.window_end || right.end || 0)
      || Number(left.start || 0) - Number(right.start || 0))) {
      renderPeriodicReview(review);
    }
  }

  function aggregateReviewWindow(review, pending) {
    if (review.manual_refinement || !Number.isFinite(Number(review.window_end))) return review;
    const windowEnd = Number(review.window_end);
    const windowStart = Number.isFinite(Number(review.window_start))
      ? Number(review.window_start) : Math.max(0, windowEnd - 300);
    const key = `${windowStart}-${windowEnd}`;
    let window = reviewWindows.get(key);
    if (!window) {
      window = { key, start: windowStart, end: windowEnd, blocks: new Map(), segmentIds: new Set(), pending: false, error: null };
      reviewWindows.set(key, window);
    }
    for (const id of review.segment_ids || []) window.segmentIds.add(Number(id));
    if (pending) {
      window.pending = true;
    } else {
      window.pending = false;
      for (const block of review.blocks || [review]) {
        const blockKey = String(block.id || `${block.start}-${block.end}`);
        window.blocks.set(blockKey, block);
        for (const id of block.segment_ids || []) window.segmentIds.add(Number(id));
      }
      window.error = review.ai_error ? review : null;
    }
    const blocks = [...window.blocks.values()].sort((left, right) =>
      Number(left.start || 0) - Number(right.start || 0));
    return {
      id: key,
      start: windowStart,
      end: windowEnd,
      window_start: windowStart,
      window_end: windowEnd,
      segment_ids: [...window.segmentIds],
      corrected: blocks.map((block) => block.corrected || "").filter(Boolean).join("\n\n"),
      translation: blocks.map((block) => block.translation || "").filter(Boolean).join("\n\n"),
      raw: blocks.map((block) => block.raw || "").filter(Boolean).join("\n\n"),
      ai_error: window.error?.ai_error || "",
      blocks,
      _periodic_window: true,
      _pending: window.pending,
    };
  }

  function renderPeriodicReview(review, pending = false) {
    review = aggregateReviewWindow(review, pending);
    pending = pending || Boolean(review._pending);
    const key = String(review.id || `${review.start}-${review.end}`);
    let card = state.reviewCards.get(key);
    if (!card) {
      card = document.createElement("article");
      card.className = "segment periodic-review";
      card.dataset.reviewId = key;
      const firstSource = (review.segment_ids || [])
        .map((id) => state.segments.get(String(id)))
        .find(Boolean);
      if (firstSource) els.transcript.insertBefore(card, firstSource);
      else els.transcript.appendChild(card);
      state.reviewCards.set(key, card);
    }
    const timing = `${formatDuration(review.start)}–${formatDuration(review.end)}`;
    const periodic = Boolean(review._periodic_window);
    const badge = periodic ? "5m" : "AI";
    const label = review.manual_refinement ? "AI 手动精修" : "AI 整体整理";
    if (pending) {
      card.innerHTML = `<span class="segment-index review-index">${badge}</span><div class="segment-body">
        <div class="segment-meta"><span class="language-pill">${label}</span><span class="review-time-range">${timing}</span></div>
        <span class="ai-pending">正在合并段落并结合上下文修正</span></div>`;
      refreshEmptyState();
      scrollToLatest();
      return;
    }

    const corrected = review.corrected || "";
    const failed = Boolean(review.ai_error) || !corrected;
    if (!failed) {
      for (const segmentId of review.segment_ids || []) {
        const sourceCard = state.segments.get(String(segmentId));
        if (sourceCard) {
          sourceCard.dataset.reviewedBy = key;
          sourceCard.classList.remove("hidden");
          sourceCard.classList.add("reviewed-source");
        }
      }
    }
    card.dataset.finalText = corrected;
    card.dataset.translation = review.translation || "";
    const refinedBlocks = (review.blocks || []).filter((block) => block.corrected);
    const refinedContent = refinedBlocks.length
      ? `<div class="review-window-blocks">${refinedBlocks.map((block) => `<section class="review-window-block"><span>${formatDuration(block.start)}–${formatDuration(block.end)}</span><div class="corrected-text review-text transcript-markdown markdown-body">${renderMarkdown(block.corrected)}</div>${block.translation ? `<div class="translation-block"><small>翻译</small><div class="translation-text transcript-markdown markdown-body">${renderMarkdown(block.translation)}</div></div>` : ""}</section>`).join("")}</div>`
      : corrected ? `<div class="corrected-text review-text transcript-markdown markdown-body">${renderMarkdown(corrected)}</div>` : "";
    card.innerHTML = `<span class="segment-index review-index">${badge}</span><div class="segment-body">
      <div class="segment-meta"><span class="language-pill">${label}</span><span class="review-time-range">${timing}</span></div>
      ${refinedContent}
      ${!refinedBlocks.length && review.translation ? `<div class="translation-block"><small>翻译</small><div class="translation-text transcript-markdown markdown-body">${renderMarkdown(review.translation)}</div></div>` : ""}
      ${review.ai_error ? `<p class="ai-error">五分钟整体整理未完成：${escapeHtml(review.ai_error)}</p>` : ""}
      ${review.raw ? `<details class="review-source"><summary>查看合并前的小段文本</summary><p>${escapeHtml(review.raw)}</p></details>` : ""}
      ${!failed && (review.segment_ids || []).length ? `<p class="review-timestamp-note">本窗口覆盖 ${(review.segment_ids || []).length} 个原分段；下方仍保留其完整时间戳</p>` : ""}
    </div>`;
    refreshEmptyState();
    scrollToLatest();
  }

  function ensureSegment(id, pending = false) {
    const key = String(id);
    if (state.segments.has(key)) return state.segments.get(key);
    els.emptyState.classList.add("hidden");
    const card = document.createElement("article");
    card.className = "segment";
    card.dataset.id = key;
    card.innerHTML = `<span class="segment-index">${String(state.segments.size + 1).padStart(2, "0")}</span>
      <div class="segment-body"><div class="segment-meta"><span>${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span></div>
      ${pending ? '<span class="ai-pending asr-pending">Qwen 正在识别</span>' : ""}</div>`;
    els.transcript.appendChild(card);
    state.segments.set(key, card);
    refreshEmptyState();
    scrollToLatest();
    return card;
  }

  function renderFinal(message) {
    const card = ensureSegment(message.id);
    const body = card.querySelector(".segment-body");
    const timing = Number.isFinite(Number(message.start))
      ? `${formatDuration(message.start)}–${formatDuration(message.end)}` : "";
    body.innerHTML = `<div class="segment-meta"><span class="language-pill">${escapeHtml(languageName(message.language))}</span>
        ${timing ? `<span>${timing}</span>` : ""}<span>${Number(message.duration || 0).toFixed(1)}s 音频</span>
        <span>${Number(message.latency || 0).toFixed(1)}s 识别</span></div>
      <p class="raw-text">${escapeHtml(message.raw)}</p>
      ${message.ai_pending ? '<span class="ai-pending">AI 正在修正 / 翻译</span>' : ""}`;
    card.dataset.raw = message.raw;
    card.dataset.finalText = message.raw;
    refreshEmptyState();
    scrollToLatest();
  }

  function renderAiResult(message) {
    const card = state.segments.get(String(message.id));
    if (!card) return;
    const body = card.querySelector(".segment-body");
    body.querySelector(".ai-pending")?.remove();
    const raw = card.dataset.raw || "";
    if (message.corrected) {
      const rawNode = body.querySelector(".raw-text");
      body.querySelectorAll(".corrected-text").forEach((node) => node.remove());
      rawNode.classList.add("dimmed");
      rawNode.innerHTML = `<span class="raw-label">原始识别</span>${escapeHtml(raw)}`;
      const corrected = document.createElement("div");
      corrected.className = "corrected-text transcript-markdown markdown-body";
      corrected.innerHTML = renderMarkdown(message.corrected);
      rawNode.before(corrected);
      card.dataset.finalText = message.corrected;
    }
    if (message.translation) {
      body.querySelector(".translation-block")?.remove();
      const translation = document.createElement("div");
      translation.className = "translation-block";
      translation.innerHTML = `<small>${message.translation_target === "English" ? "ENGLISH" : "中文翻译"}</small><div class="translation-text transcript-markdown markdown-body">${renderMarkdown(message.translation)}</div>`;
      body.appendChild(translation);
      card.dataset.translation = message.translation;
    }
    card.querySelectorAll(".ai-error").forEach((node) => node.remove());
    scrollToLatest();
  }

  function renderAiError(message) {
    const card = state.segments.get(String(message.id));
    if (!card) return;
    card.querySelector(".ai-pending")?.remove();
    card.querySelectorAll(".ai-error").forEach((node) => node.remove());
    const error = document.createElement("p");
    error.className = "ai-error";
    const text = message.historical
      ? `历史 AI 处理未完成（本次连接前）：${message.message}`
      : `AI 处理未完成：${message.message}`;
    error.append(document.createTextNode(text));
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "segment-retry-button";
    retry.textContent = "重试";
    retry.setAttribute("aria-label", `重试第 ${message.id} 段 AI 处理`);
    retry.addEventListener("click", () => onRetrySegment(String(message.id), retry));
    error.append(" ", retry);
    card.querySelector(".segment-body").appendChild(error);
    scrollToLatest();
  }

  function renderError(message) {
    if (!message.id) return;
    const card = ensureSegment(message.id);
    card.querySelector(".asr-pending")?.remove();
    const error = document.createElement("p");
    error.className = "ai-error";
    error.textContent = message.message;
    card.querySelector(".segment-body").appendChild(error);
    scrollToLatest();
  }

  function refreshEmptyState() {
    const hasSegments = state.segments.size > 0 || state.reviewCards.size > 0;
    els.emptyState.classList.toggle("hidden", hasSegments);
    els.copyButton.disabled = !hasSegments;
    els.clearButton.disabled = !hasSegments;
  }

  function languageName(value) {
    return ({ Chinese: "中文", English: "English", Cantonese: "粤语" })[value]
      || value || "自动识别";
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text ?? "";
    return div.innerHTML;
  }

  function copyAll() {
    const lines = [];
    els.transcript.querySelectorAll(".segment:not(.hidden)").forEach((card) => {
      if (card.dataset.finalText) lines.push(card.dataset.finalText);
      if (card.dataset.translation) lines.push(`译文：${card.dataset.translation}`);
    });
    navigator.clipboard.writeText(lines.join("\n\n")).then(() => toast("已复制全文"));
  }

  function clearAll() {
    followLatest = true;
    reviewWindows.clear();
    state.segments.clear();
    state.reviewCards.clear();
    els.transcript.innerHTML = "";
    els.partialCard.classList.add("hidden");
    refreshEmptyState();
  }

  return {
    clearAll,
    copyAll,
    ensureSegment,
    escapeHtml,
    formatDuration,
    refreshEmptyState,
    renderAiError,
    renderAiResult,
    renderError,
    renderFinal,
    renderPeriodicReview,
    showSession,
  };
}
