/**
 * Classroom Intelligence: Side Rail Tabs, Scribbles (速记), and Related Materials.
 * Follows the QwenASR knowledge development contract.
 */
import { renderMarkdown } from "./markdown-reader.js?v=20261015-reader-math-scope-8";
import {createVisiblePoll, classroomPanelVisible} from './visible-poll.js';

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);

function escapeHtml(str) {
  if (typeof str !== "string") return "";
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

function stamp(seconds) {
  const n = Math.max(0, Math.floor(Number(seconds) || 0));
  return [Math.floor(n / 3600), Math.floor(n / 60) % 60, n % 60].map(v => String(v).padStart(2, "0")).join(":");
}

function parseStamp(text) {
  if (!text || typeof text !== "string") return null;
  const parts = text.trim().split(":").map(Number);
  if (parts.some(isNaN)) return null;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 1) return parts[0];
  return null;
}

function getSourceUrl(src, defaultSessionId) {
  if (src.open_url) return src.open_url;
  if (src.start !== null && src.start !== undefined) {
    const sess = src.lesson_id || src.session_id || defaultSessionId;
    return `/?recording=${encodeURIComponent(sess)}&session=${encodeURIComponent(sess)}#t=${src.start}`;
  }
  if (src.source_id) {
    const rev = src.revision ? `?revision=${encodeURIComponent(src.revision)}` : "";
    const hash = src.page ? `#page=${encodeURIComponent(src.page)}` : "";
    return `/api/knowledge/source/${encodeURIComponent(src.source_id)}${rev}${hash}`;
  }
  return "#";
}

function handleAudioJump(event, src, currentSessionId) {
  if (src.start !== null && src.start !== undefined) {
    const player = document.getElementById("noteAudio");
    const targetId = src.lesson_id || src.session_id || currentSessionId;
    const targetSource = `/api/recordings/${encodeURIComponent(targetId)}/playback`;
    // The shared player belongs to the note reader, which can show a different
    // lesson from the side rail. Otherwise follow the recording's normal link.
    if (player && document.getElementById("noteDialog")?.open &&
        player.getAttribute("src") === targetSource && !event.ctrlKey && !event.metaKey && !event.shiftKey) {
      event.preventDefault();
      player.currentTime = Number(src.start);
      try { player.play()?.catch(() => {}); } catch {}
    }
  }
}

async function api(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...(options.method ? {} : {signal: AbortSignal.timeout(15000)}), ...options });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    const msg = typeof payload.detail === "string" ? payload.detail : payload.detail?.message || payload.message || "请求失败，请重试";
    const err = new Error(msg);
    err.status = response.status;
    err.payload = payload;
    throw err;
  }
  return payload;
}

const jsonPost = (body) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const jsonPatch = (body) => ({ method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export function createClassroomIntelligence({ toast = () => {}, openSettings = () => {} } = {}) {
  if (window.__classroomIntelligence) {
    return window.__classroomIntelligence;
  }
  let currentSession = null;
  let currentSessionId = null;
  let activeTab = "scribbles"; // "scribbles" | "related" | "chat"
  let notes = [];
  let renderedNotesSignature = "";
  let relatedResults = [];
  let scribblesPoll = null, scribblesVersion = 0, relatedVersion = 0, sourcesLoadedFor = '', sourcesLoadedAt = 0;
  let relatedSignature = '';
  const scribblesRequests = new Map();
  let sourceHint = {};
  let isSaving = false;
  let selectedAnchor = "current"; // "current" | "previous" | "manual"
  const sessionDrafts = new Map(); // sessionId -> string
  const draftVersions = new Map();
  const composerRequestIds = new Map();
  const activeInlineEditors = new Set(); // noteId currently being edited

  // DOM elements cache
  const tabs = {
    scribbles: $("#sideTabScribbles"),
    related: $("#sideTabRelated"),
    chat: $("#sideTabChat"),
  };

  const panels = {
    scribbles: $("#scribblesPanel"),
    related: $("#relatedPanel"),
    chat: $("#chatPanel"),
  };

  const scribblesEls = {
    input: $("#scribbleInput"),
    saveBtn: $("#saveScribbleBtn"),
    autoEnhance: $("#scribbleAutoEnhance"),
    anchorCurrent: $("#anchorCurrent"),
    anchorPrevious: $("#anchorPrevious"),
    anchorManual: $("#anchorManual"),
    manualInputs: $("#manualTimeInputs"),
    manualStart: $("#manualStartTime"),
    manualEnd: $("#manualEndTime"),
    list: $("#scribblesList"),
    empty: $("#scribblesEmpty"),
    status: $("#scribblesStatus"),
    exportBtn: $("#exportScribblesBtn"),
    badge: $("#scribblesCountBadge"),
  };

  const relatedEls = {
    list: $("#relatedList"),
    empty: $("#relatedEmpty"),
    sourceSelect: $("#relatedSourceSelect"),
    headingInput: $("#relatedHeadingInput"),
    pageInput: $("#relatedPageInput"),
    refreshBtn: $("#relatedRefreshBtn"),
    badge: $("#relatedCountBadge"),
  };

  // ==================== Tab Switching ====================

  function switchTab(tabName) {
    activeTab = tabName;
    for (const [key, btn] of Object.entries(tabs)) {
      if (!btn) continue;
      const isActive = key === tabName;
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-selected", String(isActive));
    }
    for (const [key, panel] of Object.entries(panels)) {
      if (!panel) continue;
      panel.classList.toggle("hidden", key !== tabName);
    }
    if (tabName === "scribbles" && scribblesEls.input) {
      scribblesEls.input.focus({ preventScroll: true });
    } else if (tabName === "related") {
      void loadRelatedSources(currentSession).then(() => fetchRelatedMaterials());
    }
    document.dispatchEvent(new Event('qwenasr:viewchange'));
  }

  // ==================== Anchor Selection ====================

  function setAnchor(anchor) {
    selectedAnchor = anchor;
    if (scribblesEls.anchorCurrent) scribblesEls.anchorCurrent.classList.toggle("active", anchor === "current");
    if (scribblesEls.anchorPrevious) scribblesEls.anchorPrevious.classList.toggle("active", anchor === "previous");
    if (scribblesEls.anchorManual) scribblesEls.anchorManual.classList.toggle("active", anchor === "manual");
    if (scribblesEls.manualInputs) {
      scribblesEls.manualInputs.classList.toggle("hidden", anchor !== "manual");
    }
  }

  // ==================== Draft Management ====================

  function saveCurrentDraft() {
    if (currentSessionId && scribblesEls.input) {
      sessionDrafts.set(currentSessionId, scribblesEls.input.value);
    }
  }

  function restoreDraft(sessionId) {
    if (!scribblesEls.input) return;
    const draft = sessionDrafts.get(sessionId) || "";
    scribblesEls.input.value = draft;
    updateComposerControls();
  }

  function updateComposerControls() {
    const ready = Boolean(currentSessionId);
    if (scribblesEls.input) scribblesEls.input.disabled = !ready;
    const hasText = Boolean(scribblesEls.input?.value.trim());
    if (scribblesEls.saveBtn) {
      scribblesEls.saveBtn.disabled = !ready || !hasText || isSaving;
    }
  }

  // ==================== API Calls: Scribbles ====================

  async function loadScribbles(quiet = false) {
    const id = currentSessionId;
    if (!id) return;
    if (scribblesRequests.has(id)) return scribblesRequests.get(id);
    const version = scribblesVersion;
    const request = (async () => {
      try {
        const data = await api(`/api/recordings/${encodeURIComponent(id)}/scribbles`);
        if (currentSessionId !== id || version !== scribblesVersion) return;
        notes = data.notes || [];
        sourceHint=data.context?.source_hint || {};
        if (relatedEls.sourceSelect && document.activeElement!==relatedEls.sourceSelect) relatedEls.sourceSelect.value=sourceHint.source_id || '';
        if (relatedEls.headingInput && document.activeElement!==relatedEls.headingInput) relatedEls.headingInput.value=sourceHint.heading || '';
        if (relatedEls.pageInput && document.activeElement!==relatedEls.pageInput) relatedEls.pageInput.value=sourceHint.page || '';
        renderScribbles();
        if (!quiet && scribblesEls.status) {
          scribblesEls.status.textContent = `共 ${notes.length} 条速记`;
        }
      } catch (err) {
        if (currentSessionId === id && !quiet && scribblesEls.status) {
          scribblesEls.status.textContent = "读取速记失败";
        }
      }
    })().finally(() => {
      scribblesRequests.delete(id);
      if (currentSessionId === id && version !== scribblesVersion) void loadScribbles(quiet);
      else checkPolling();
    });
    scribblesRequests.set(id, request);
    return request;
  }

  async function saveScribble() {
    const id = currentSessionId;
    if (!id || isSaving) return;
    const submittedDraft = scribblesEls.input?.value || "";
    const text = submittedDraft.trim();
    if (!text) return;

    isSaving = true;
    updateComposerControls();
    if (scribblesEls.status) scribblesEls.status.textContent = "正在保存速记…";

    const draftVersion = draftVersions.get(id) || 0;
    const autoEnhance = scribblesEls.autoEnhance?.checked ?? true;
    if (!composerRequestIds.has(id)) composerRequestIds.set(id, crypto.randomUUID());
    const requestId = composerRequestIds.get(id);
    const payload = {
      text,
      anchor: selectedAnchor,
      request_id: requestId,
    };

    if (selectedAnchor === "manual") {
      const startSec = parseStamp(scribblesEls.manualStart?.value);
      const endSec = parseStamp(scribblesEls.manualEnd?.value);
      if (startSec !== null) payload.start = startSec;
      if (endSec !== null) payload.end = endSec;
    }

    try {
      // 1. Instant raw save
      const res = await api(`/api/recordings/${encodeURIComponent(id)}/scribbles`, jsonPost(payload));
      const newNote = res.note;

      // Only acknowledge the submitted version. Typing and changing lessons
      // remain possible while the network request is pending.
      if (composerRequestIds.get(id) === requestId) composerRequestIds.delete(id);
      if ((draftVersions.get(id) || 0) === draftVersion) {
        sessionDrafts.delete(id);
        if (currentSessionId === id && scribblesEls.input?.value === submittedDraft) scribblesEls.input.value = "";
      }
      updateComposerControls();

      if (currentSessionId === id && newNote) {
        scribblesVersion++;
        notes = [newNote, ...notes.filter(note => note.id !== newNote.id)];
        renderScribbles();
      }
      toast("速记已保存");

      // 2. Trigger async enhance if checked
      if (autoEnhance && newNote?.id) {
        void triggerEnhance(newNote.id, true, id);
      }
    } catch (err) {
      // Failed POST: an unchanged draft retries with the same request id.
      toast(`保存失败: ${err.message}`);
      if (currentSessionId === id && scribblesEls.status) scribblesEls.status.textContent = `保存失败: ${err.message}（可点击重试）`;
    } finally {
      isSaving = false;
      updateComposerControls();
    }
  }

  async function triggerEnhance(noteId, quiet = false, id = currentSessionId) {
    if (!id) return;
    try {
      // Update note status locally to pending
      const note = currentSessionId === id && notes.find(n => n.id === noteId);
      if (note) {
        note.status = "pending";
        renderScribbles();
      }
      // Scoped or direct route
      const res = await api(`/api/recordings/${encodeURIComponent(id)}/scribbles/${encodeURIComponent(noteId)}/enhance`, jsonPost({}));
      if (currentSessionId === id && res.note) {
        scribblesVersion++;
        const idx = notes.findIndex(n => n.id === noteId);
        if (idx >= 0) notes[idx] = res.note;
        renderScribbles();
      }
      if (!quiet) toast("已提交 AI 增强");
    } catch (err) {
      if (!quiet) toast(`增强请求失败: ${err.message}`);
      if (currentSessionId === id) void loadScribbles(true);
    }
  }

  async function applyNoteAction(noteId, action, expectedRevision, suggestionId = null) {
    const id = currentSessionId;
    if (!id) return;
    try {
      const payload = { action, expected_revision: expectedRevision };
      if (suggestionId) payload.suggestion_id = suggestionId;
      const res = await api(`/api/recordings/${encodeURIComponent(id)}/scribbles/${encodeURIComponent(noteId)}/action`, jsonPost(payload));
      if (currentSessionId === id && res.note) {
        scribblesVersion++;
        const idx = notes.findIndex(n => n.id === noteId);
        if (idx >= 0) notes[idx] = res.note;
        renderScribbles();
      }
      const labelMap = { accept: "已采纳建议", reject: "已驳回建议", undo: "已撤销修改", cancel: "已取消任务" };
      toast(labelMap[action] || "操作成功");
    } catch (err) {
      if (err.status === 409) {
        toast("内容已被修改，已刷新最新版本");
        void loadScribbles(false);
      } else {
        toast(`操作未完成: ${err.message}`);
      }
    }
  }

  async function patchNote(noteId, patchData, id = currentSessionId) {
    if (!id) return;
    try {
      const res = await api(`/api/recordings/${encodeURIComponent(id)}/scribbles/${encodeURIComponent(noteId)}`, jsonPatch(patchData));
      if (currentSessionId === id && res.note) {
        scribblesVersion++;
        const idx = notes.findIndex(n => n.id === noteId);
        if (idx >= 0) notes[idx] = res.note;
        renderScribbles();
      }
      toast("笔记已更新");
      return res.note || true;
    } catch (err) {
      if (err.status === 409) {
        toast("正文版本已变化，当前编辑草稿保留；取消后可查看最新正文");
        if (currentSessionId === id) void loadScribbles(false);
      } else {
        toast(`保存修改失败: ${err.message}`);
      }
      return false;
    }
  }

  // ==================== Render Scribbles Cards ====================

  function renderScribbles() {
    checkPolling();
    const signature=JSON.stringify([currentSessionId,notes,[...activeInlineEditors]]);
    if (renderedNotesSignature===signature) return;
    renderedNotesSignature=signature;
    if (!scribblesEls.list) return;

    if (scribblesEls.badge) {
      scribblesEls.badge.textContent = String(notes.length);
      scribblesEls.badge.classList.toggle("hidden", notes.length === 0);
    }

    if (!notes.length) {
      if (scribblesEls.empty) scribblesEls.empty.classList.remove("hidden");
      // Clean list except empty state
      const cards = scribblesEls.list.querySelectorAll(".scribble-card");
      cards.forEach(c => c.remove());
      return;
    }

    if (scribblesEls.empty) scribblesEls.empty.classList.add("hidden");

    // Don't overwrite the entire list if the user is typing in an inline editor
    // We update cards individually or reconstruct without disturbing active editors
    const fragment = document.createDocumentFragment();

    for (const note of notes) {
      // Check if user is currently editing this card inline
      const isEditing = activeInlineEditors.has(note.id);
      const existingCard = scribblesEls.list.querySelector(`.scribble-card[data-note-id="${note.id}"]`);
      if (existingCard && isEditing && existingCard.querySelector(".inline-edit-textarea")) {
        // Active inline edit protection: preserve typing and focus
        fragment.append(existingCard);
        continue;
      }

      const card = document.createElement("article");
      card.className = "scribble-card";
      card.dataset.noteId = note.id;
      if (note.locked) card.classList.add("locked");
      if (note.status === "pending") card.classList.add("pending");

      // Card Header
      const header = document.createElement("div");
      header.className = "scribble-card-header";

      const badges = document.createElement("div");
      badges.className = "scribble-badges";

      // Time badge
      const timeBadge = document.createElement("span");
      timeBadge.className = "scribble-time-badge";
      const s = note.start !== null && note.start !== undefined ? stamp(note.start) : "";
      const e = note.end !== null && note.end !== undefined ? stamp(note.end) : "";
      timeBadge.textContent = s && e ? `${s}–${e}` : s || "随堂速记";
      badges.append(timeBadge);

      // Status badge
      if (note.status === "pending") {
        const statusBadge = document.createElement("span");
        statusBadge.className = "scribble-status-badge pending";
        statusBadge.textContent = "⚡ 对齐增强中…";
        badges.append(statusBadge);
      } else if (note.status === "enhanced") {
        const statusBadge = document.createElement("span");
        statusBadge.className = "scribble-status-badge enhanced";
        statusBadge.textContent = note.suggestion ? "已生成建议" : "笔记已保存";
        badges.append(statusBadge);
      } else if (note.status === "failed") {
        const statusBadge = document.createElement("span");
        statusBadge.className = "scribble-status-badge failed";
        statusBadge.textContent = "⚠️ 增强待重试";
        badges.append(statusBadge);
      }

      if (note.locked) {
        const lockedBadge = document.createElement("span");
        lockedBadge.className = "scribble-status-badge locked";
        lockedBadge.textContent = "🔒 已锁定";
        badges.append(lockedBadge);
      }

      header.append(badges);

      // Card header tools (Lock / Pin, Edit)
      const tools = document.createElement("div");
      tools.className = "scribble-card-tools";

      const lockBtn = document.createElement("button");
      lockBtn.type = "button";
      lockBtn.className = "card-tool-btn";
      lockBtn.title = note.locked ? "点击解锁速记" : "锁定速记（防止自动修改）";
      lockBtn.textContent = note.locked ? "解锁" : "锁定";
      lockBtn.addEventListener("click", () => {
        void patchNote(note.id, { locked: !note.locked, expected_revision: note.revision });
      });
      tools.append(lockBtn);

      if (!isEditing && !note.locked) {
        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "card-tool-btn";
        editBtn.textContent = "编辑";
        editBtn.addEventListener("click", () => {
          activeInlineEditors.add(note.id);
          renderScribbles();
        });
        tools.append(editBtn);
      }

      header.append(tools);
      card.append(header);

      // Inline editor vs normal view
      if (isEditing) {
        const editingSessionId = currentSessionId;
        let editingRevision = note.revision;
        const editorBox = document.createElement("div");
        editorBox.className = "scribble-inline-editor";

        const textarea = document.createElement("textarea");
        textarea.className = "inline-edit-textarea";
        textarea.setAttribute("aria-label", "编辑速记正文");
        textarea.value = note.content || note.raw || "";

        const actions = document.createElement("div");
        actions.className = "inline-edit-actions";

        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "scribble-btn";
        cancelBtn.textContent = "取消";
        cancelBtn.addEventListener("click", () => {
          activeInlineEditors.delete(note.id);
          renderScribbles();
        });

        const saveBtn = document.createElement("button");
        saveBtn.type = "button";
        saveBtn.className = "scribble-btn primary";
        saveBtn.textContent = "保存修改";
        saveBtn.addEventListener("click", async () => {
          const val = textarea.value.trim();
          if (!val || saveBtn.disabled) return;
          saveBtn.disabled = cancelBtn.disabled = true;
          // Keep this exact editor protected from refreshes until the save is
          // acknowledged; an error must leave its draft and focus intact.
          const saved = await patchNote(note.id, { content: val, expected_revision: editingRevision }, editingSessionId);
          if (saved && currentSessionId === editingSessionId) {
            editingRevision = saved.revision ?? editingRevision;
            if (textarea.value.trim() === val) activeInlineEditors.delete(note.id);
            renderScribbles();
          }
          saveBtn.disabled = cancelBtn.disabled = false;
        });

        actions.append(cancelBtn, saveBtn);
        editorBox.append(textarea, actions);
        card.append(editorBox);
      } else {
        // Raw section
        const rawBlock = document.createElement("div");
        rawBlock.className = "scribble-raw-block";
        const rawLabel = document.createElement("span");
        rawLabel.className = "section-label-tag raw";
        rawLabel.textContent = "速记原话";
        const rawP = document.createElement("p");
        rawP.className = "scribble-raw-text";
        rawP.textContent = note.raw || note.text || "";
        rawBlock.append(rawLabel, rawP);
        card.append(rawBlock);

        // Content section (if different from raw or enhanced)
        if (note.content && note.content !== note.raw) {
          const contentBlock = document.createElement("div");
          contentBlock.className = "scribble-content-block";
          const contentLabel = document.createElement("span");
          contentLabel.className = "section-label-tag content";
          contentLabel.textContent = "笔记正文";
          const contentDiv = document.createElement("div");
          contentDiv.className = "scribble-content-text markdown-body";
          contentDiv.innerHTML = renderMarkdown(note.content);
          contentDiv.querySelectorAll("a").forEach(l => { l.target = "_blank"; l.rel = "noopener noreferrer"; });
          contentBlock.append(contentLabel, contentDiv);
          card.append(contentBlock);
        }

        // Suggestion block (if suggestion exists)
        if (note.suggestion) {
          const sugg = note.suggestion;
          const suggBlock = document.createElement("div");
          suggBlock.className = "scribble-suggestion-block";

          const suggHeader = document.createElement("div");
          suggHeader.className = "suggestion-header";
          const suggLabel = document.createElement("span");
          suggLabel.className = "section-label-tag suggestion";
          suggLabel.textContent = "💡 AI 建议补充与核实";
          suggHeader.append(suggLabel);

          if (sugg.stale) {
            const staleNote = document.createElement("span");
            staleNote.className = "source-freshness-warn";
            staleNote.textContent = "⚠️ 速记内容已有手动修改，此建议供参考";
            suggHeader.append(staleNote);
          }
          suggBlock.append(suggHeader);

          const readable=document.createElement('div');
          readable.className='suggestion-content markdown-body';
          readable.innerHTML=renderMarkdown(sugg.content || '');
          readable.querySelectorAll('a').forEach(link=>{link.target='_blank';link.rel='noopener noreferrer';});
          suggBlock.append(readable);
          // Proposed diff & comparison: native details summary comparison without innerHTML untrusted texts
          const diffDetails = document.createElement("details");
          diffDetails.className = "scribble-diff-details";
          diffDetails.open = false;

          const summary = document.createElement("summary");
          summary.textContent = "建议修改与原速记对比";
          diffDetails.append(summary);

          const diffContent = document.createElement("div");
          diffContent.className = "scribble-diff-content";

          const rawPane = document.createElement("div");
          rawPane.className = "scribble-diff-pane";
          const rawLabel = document.createElement("span");
          rawLabel.className = "scribble-diff-label";
          rawLabel.textContent = "当前已保存正文";
          const rawP = document.createElement("p");
          rawP.className = "scribble-diff-text";
          rawP.textContent = note.content || note.raw || note.text || "";
          rawPane.append(rawLabel, rawP);

          const suggPane = document.createElement("div");
          suggPane.className = "scribble-diff-pane";
          const suggPaneLabel = document.createElement("span");
          suggPaneLabel.className = "scribble-diff-label";
          suggPaneLabel.textContent = "AI 建议正文";
          const suggP = document.createElement("div");
          suggP.className = "scribble-diff-text markdown-body";
          suggP.innerHTML = renderMarkdown(sugg.content || note.content || "");
          suggP.querySelectorAll("a").forEach(l => { l.target = "_blank"; l.rel = "noopener noreferrer"; });
          suggPane.append(suggPaneLabel, suggP);

          diffContent.append(rawPane, suggPane);
          diffDetails.append(diffContent);
          suggBlock.append(diffDetails);

          // Section labels from claims
          if (sugg.claims && typeof sugg.claims === "object") {
            const claimsBox = document.createElement("details");
            claimsBox.className = "suggestion-claims";
            const claimsSummary=document.createElement("summary");claimsSummary.textContent="查看逐条依据与核对说明";claimsBox.append(claimsSummary);

            const claimLabels = {
              classroom: { name: "课堂依据", class: "classroom" },
              material: { name: "教材关联", class: "material" },
              ai_explanation: { name: "AI 解析（待核对）", class: "ai_explanation" },
              check: { name: "存疑待核实", class: "check" },
            };

            for (const [key, meta] of Object.entries(claimLabels)) {
              const claimItems = sugg.claims[key];
              if (!claimItems) continue;

              const list = Array.isArray(claimItems) ? claimItems : (typeof claimItems === "object" ? Object.values(claimItems) : []);
              if (!list.length) continue;

              for (const item of list) {
                const row = document.createElement("div");
                row.className = "claim-row";

                const tag = document.createElement("span");
                tag.className = `claim-tag ${meta.class}`;
                tag.textContent = meta.name;
                row.append(tag);

                const claimText = document.createElement("span");
                claimText.textContent = typeof item === "string" ? item : (item.statement || item.claim || item.text || item.content || "");
                row.append(claimText);

                if (typeof item === "object" && item.quote) {
                  const quoteSpan = document.createElement("span");
                  quoteSpan.className = "claim-quote";
                  quoteSpan.textContent = `“${item.quote}”`;
                  row.append(quoteSpan);
                }

                claimsBox.append(row);
              }
            }
            if (claimsBox.children.length > 0) {
              suggBlock.append(claimsBox);
            }
          }

          // Readable sources list
          const used=new Set(['classroom','material','interpretation_pending'].flatMap(kind=>(sugg.claims?.[kind] || []).map(claim=>claim.citation_id)).filter(Boolean));
          const sources = (sugg.evidence || note.evidence || []).filter(src=>!used.size || used.has(src.citation_id));
          if (sources.length > 0) {
            const sourcesList = document.createElement("div");
            sourcesList.className = "scribble-sources-list";

            for (const src of sources) {
              const link = document.createElement("a");
              link.className = "scribble-source-link";
              if (src.stale || src.valid === false) link.classList.add("stale");
              link.target = "_blank";
              link.rel = "noopener noreferrer";

              const targetUrl = getSourceUrl(src, currentSessionId);
              link.href = targetUrl;
              link.addEventListener("click", (e) => handleAudioJump(e, src, currentSessionId));

              const titleSpan = document.createElement("span");
              const parts = [src.title || "资料"];
              if (src.heading) parts.push(src.heading);
              if (src.page) parts.push(`P.${src.page}`);
              if (src.start !== null && src.start !== undefined) parts.push(stamp(src.start));
              titleSpan.textContent = parts.join(" ");
              link.append(titleSpan);

              if (src.origin) {
                const org = document.createElement("span");
                org.className = "source-origin-badge";
                org.textContent = src.origin === "original" ? "原件" : (src.origin === "human" ? "速记" : "AI");
                link.append(org);
              }

              if (src.stale || src.valid === false) {
                const staleSpan = document.createElement("span");
                staleSpan.className = "source-freshness-warn";
                staleSpan.textContent = "⚠️已变动";
                link.append(staleSpan);
              }

              sourcesList.append(link);
            }
            suggBlock.append(sourcesList);
          }

          card.append(suggBlock);
        }

        // Card Action Buttons
        const actionsBar = document.createElement("div");
        actionsBar.className = "scribble-card-actions";

        // Accept / Reject if suggestion exists and not locked
        if (note.suggestion && !note.locked) {
          const acceptBtn = document.createElement("button");
          acceptBtn.type = "button";
          acceptBtn.className = "scribble-btn primary";
          const outdated=Boolean(note.suggestion.stale) || Number(note.suggestion.input_revision)!==Number(note.revision);
          acceptBtn.textContent = outdated ? "旧建议，需重新增强" : note.manual_edit ? "保留手记并合并补充" : "采纳建议";
          acceptBtn.disabled = outdated;
          acceptBtn.addEventListener("click", () => {
            void applyNoteAction(note.id, "accept", note.revision, note.suggestion.id);
          });

          const rejectBtn = document.createElement("button");
          rejectBtn.type = "button";
          rejectBtn.className = "scribble-btn";
          rejectBtn.textContent = "驳回";
          rejectBtn.addEventListener("click", () => {
            void applyNoteAction(note.id, "reject", note.revision, note.suggestion.id);
          });

          actionsBar.append(rejectBtn, acceptBtn);
        }

        // Re-enhance button
        if (!note.locked && note.status !== "pending") {
          const enhanceBtn = document.createElement("button");
          enhanceBtn.type = "button";
          enhanceBtn.className = "scribble-btn";
          enhanceBtn.textContent = "重新增强";
          enhanceBtn.addEventListener("click", () => {
            void triggerEnhance(note.id);
          });
          actionsBar.append(enhanceBtn);
        }

        // Undo button (if history exists)
        if (note.history && note.history.length > 0 && !note.locked) {
          const undoBtn = document.createElement("button");
          undoBtn.type = "button";
          undoBtn.className = "scribble-btn";
          undoBtn.textContent = "撤销";
          undoBtn.title = "撤销到上一次保存的内容";
          undoBtn.addEventListener("click", () => {
            void applyNoteAction(note.id, "undo", note.revision);
          });
          actionsBar.append(undoBtn);
        }

        if (actionsBar.children.length > 0) {
          card.append(actionsBar);
        }
      }

      fragment.append(card);
    }

    scribblesEls.list.replaceChildren(fragment);
  }

  // ==================== Related Materials ====================

  async function loadRelatedSources(session, force = false) {
    const id=session?.id;
    if(!id || !relatedEls.sourceSelect || (!force && sourcesLoadedFor === id && Date.now() - sourcesLoadedAt < 60000)) return;
    await loadScribbles(false);
    try {
      const data=await api('/api/knowledge/status?'+new URLSearchParams({category:session.category || '未分类'}));
      if(currentSessionId!==id) return;
      sourcesLoadedFor = id;
      sourcesLoadedAt = Date.now();
      const select=relatedEls.sourceSelect;
      select.replaceChildren(new Option('自动关联当前讲解',''));
      for(const item of data.sources || []) {
        if(!String(item.source_id || '').startsWith('cf_') || String(item.source_id).endsWith('_mineru')) continue;
        if(item.state != null && item.state !== 'active') continue;
        if(!['已就绪','仅词法可用'].includes(item.status)) continue;
        select.append(new Option(item.title || item.name,item.source_id));
      }
      select.value=sourceHint.source_id || '';
    } catch { /* Source choices appear after the first course index is ready. */ }
  }

  async function fetchRelatedMaterials(saveHint = false) {
    const id = currentSessionId;
    if (!id) return;
    const version = ++relatedVersion;
    try {
      const sourceId = relatedEls.sourceSelect?.value || "";
      const heading = relatedEls.headingInput?.value?.trim() || "";
      const page = relatedEls.pageInput?.value ? Number(relatedEls.pageInput.value) : "";
      if (saveHint) {
        const saved=await api(`/api/recordings/${encodeURIComponent(id)}/intelligence/context`,jsonPost({source_id:sourceId,heading,page:page || null}));
        if (currentSessionId !== id || version !== relatedVersion) return;
        sourceHint=saved.source_hint || {};
      }

      const params = new URLSearchParams({ session_id: id });
      if (sourceId) params.set("source_id", sourceId);
      if (heading) params.set("heading", heading);
      if (page) params.set("page", String(page));

      const res = await api(`/api/intelligence/related?${params.toString()}`);
      if (currentSessionId !== id || version !== relatedVersion) return;
      relatedResults = (res.results || []).slice(0, 4); // Few 4 results per contract
      renderRelatedMaterials();
    } catch (error) {
      if (currentSessionId !== id || version !== relatedVersion) return;
      if(saveHint) toast(error.message);
      // Quiet fallback
      renderRelatedMaterials();
    }
  }

  function renderRelatedMaterials() {
    if (!relatedEls.list) return;
    const signature = JSON.stringify([currentSessionId, relatedResults]);
    if (signature === relatedSignature) return;
    relatedSignature = signature;

    if (relatedEls.badge) {
      relatedEls.badge.textContent = String(relatedResults.length);
      relatedEls.badge.classList.toggle("hidden", relatedResults.length === 0);
    }

    if (!relatedResults.length) {
      if (relatedEls.empty) relatedEls.empty.classList.remove("hidden");
      const cards = relatedEls.list.querySelectorAll(".related-card");
      cards.forEach(c => c.remove());
      return;
    }

    if (relatedEls.empty) relatedEls.empty.classList.add("hidden");

    const fragment = document.createDocumentFragment();

    for (const item of relatedResults) {
      const card = document.createElement("article");
      card.className = "related-card";

      const header = document.createElement("div");
      header.className = "related-card-header";

      const titleGroup = document.createElement("div");
      titleGroup.className = "related-card-title-group";

      const title = document.createElement("h3");
      title.className = "related-card-title";
      title.textContent = item.title || "参考资料";
      titleGroup.append(title);

      if (item.heading) {
        const heading = document.createElement("span");
        heading.className = "related-card-heading";
        heading.textContent = item.heading;
        titleGroup.append(heading);
      }
      header.append(titleGroup);

      const badges = document.createElement("div");
      badges.className = "related-card-badges";

      if (item.origin) {
        const orgBadge = document.createElement("span");
        orgBadge.className = `related-origin-badge ${item.origin}`;
        orgBadge.textContent = item.origin === "original" ? "原件" : (item.origin === "human" ? "速记" : "AI衍生");
        badges.append(orgBadge);
      }
      header.append(badges);
      card.append(header);

      // Snippet
      if (item.text) {
        const snippet = document.createElement("p");
        snippet.className = "related-snippet";
        snippet.textContent = item.text;
        card.append(snippet);
      }

      // Footer with direct links to PDF page or audio timestamp
      const footer = document.createElement("div");
      footer.className = "related-card-footer";

      const openLink = document.createElement("a");
      openLink.className = "related-open-link";
      openLink.target = "_blank";
      openLink.rel = "noopener noreferrer";

      const targetUrl = getSourceUrl(item, currentSessionId);
      openLink.href = targetUrl;
      openLink.addEventListener("click", (e) => handleAudioJump(e, item, currentSessionId));

      const linkLabel = item.page ? `📄 跳转到第 ${item.page} 页` : (item.start !== null && item.start !== undefined ? `🎙️ 定位到 ${stamp(item.start)}` : "📄 打开资料原文");
      openLink.textContent = linkLabel;
      footer.append(openLink);

      if (item.stale) {
        const staleWarn = document.createElement("span");
        staleWarn.className = "related-stale-warning";
        staleWarn.textContent = "⚠️ 资料已变动";
        footer.append(staleWarn);
      }

      card.append(footer);
      fragment.append(card);
    }

    relatedEls.list.replaceChildren(fragment);
  }

  // ==================== Auto Polling ====================

  function checkPolling() {
    scribblesPoll?.schedule();
  }

  // ==================== Session Shown Callback ====================

  function showSession(session) {
    const nextId = session?.id || null;
    if (nextId === currentSessionId) {
      const changed = currentSession?.status !== session?.status;
      currentSession = session;
      if (changed) scribblesPoll.wake();
      return;
    }
    saveCurrentDraft();

    currentSession = session;
    currentSessionId = nextId;
    scribblesVersion++; relatedVersion++; sourcesLoadedFor = '';
    notes = []; relatedResults = []; sourceHint = {};

    restoreDraft(nextId);
    activeInlineEditors.clear();

    renderScribbles(); renderRelatedMaterials();
    if (nextId && activeTab === "related") void loadRelatedSources(session).then(() => fetchRelatedMaterials());
    scribblesPoll.wake();
  }

  // ==================== Event Listeners ====================

  // Tabs
  if (tabs.scribbles) tabs.scribbles.addEventListener("click", () => switchTab("scribbles"));
  if (tabs.related) tabs.related.addEventListener("click", () => switchTab("related"));
  if (tabs.chat) tabs.chat.addEventListener("click", () => switchTab("chat"));

  // Anchors
  if (scribblesEls.anchorCurrent) scribblesEls.anchorCurrent.addEventListener("click", () => setAnchor("current"));
  if (scribblesEls.anchorPrevious) scribblesEls.anchorPrevious.addEventListener("click", () => setAnchor("previous"));
  if (scribblesEls.anchorManual) scribblesEls.anchorManual.addEventListener("click", () => setAnchor("manual"));

  // Composer
  if (scribblesEls.input) {
    scribblesEls.input.addEventListener("input", () => {
      if (currentSessionId) {
        composerRequestIds.delete(currentSessionId);
        draftVersions.set(currentSessionId, (draftVersions.get(currentSessionId) || 0) + 1);
      }
      saveCurrentDraft();
      updateComposerControls();
    });
    scribblesEls.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey || !e.shiftKey) && !e.isComposing) {
        e.preventDefault();
        if (!scribblesEls.saveBtn?.disabled) {
          void saveScribble();
        }
      }
    });
  }

  if (scribblesEls.saveBtn) {
    scribblesEls.saveBtn.addEventListener("click", () => void saveScribble());
  }

  // Export
  if (scribblesEls.exportBtn) {
    scribblesEls.exportBtn.addEventListener("click", () => {
      if (!currentSessionId) return;
      window.open(`/api/recordings/${encodeURIComponent(currentSessionId)}/scribbles/export`, "_blank");
    });
  }

  // Related query
  if (relatedEls.refreshBtn) {
    relatedEls.refreshBtn.addEventListener("click", () => {
      void loadRelatedSources(currentSession, true).then(() => fetchRelatedMaterials(true));
    });
  }
  for (const input of [relatedEls.sourceSelect,relatedEls.headingInput,relatedEls.pageInput]) {
    input?.addEventListener('change',()=>void fetchRelatedMaterials(true));
  }

  // Visibility changes
  scribblesPoll = createVisiblePoll({run: () => loadScribbles(true),
    enabled: () => currentSessionId && (classroomPanelVisible(panels.scribbles) || classroomPanelVisible(panels.related)),
    delay: () => currentSession?.status === 'active' || notes.some(note => note.status === 'pending' ||
      ['queued', 'running', 'pending', 'waiting_retry', 'stopping'].includes(note.job?.status)) ? 3500 : 60000,
    events: ['qwenasr:viewchange']});

  // ==================== Intelligence & Knowledge Settings ====================

  const EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"];

  async function loadIntelligenceSettings() {
    const elModel = $("#intelModel");
    if (!elModel) return;
    try {
      const data = await api("/api/intelligence/settings");
      const p = data.provider || {};
      if ($("#intelTransport")) $("#intelTransport").value = p.transport || "agy_cli";
      if (elModel) elModel.value = p.model || "gemini-3.8-flash-high";
      const effortIdx = EFFORT_LEVELS.indexOf(p.cli_effort || "high");
      if ($("#intelEffort")) $("#intelEffort").value = effortIdx >= 0 ? effortIdx : 2;
      if ($("#intelEffortValue")) $("#intelEffortValue").textContent = p.cli_effort || "high";
      if ($("#intelTimeout")) $("#intelTimeout").value = p.cli_timeout_seconds || 180;
      if ($("#intelContextWindow")) $("#intelContextWindow").value = p.context_window || 1048576;
      if ($("#intelMaxOutput")) $("#intelMaxOutput").value = p.max_output_tokens || 4096;
      if ($("#intelBaseUrl")) $("#intelBaseUrl").value = p.base_url || "";
      if ($("#intelApiMode")) $("#intelApiMode").value = p.api_mode || "auto";
      if ($("#intelApiParams")) $("#intelApiParams").value = typeof p.extra_params === "string" ? p.extra_params : JSON.stringify(p.extra_params || {}, null, 2);
      if ($("#intelPrompt")) $("#intelPrompt").value = p.system_prompt || "";
      if ($("#intelProviderSummary")) $("#intelProviderSummary").textContent = p.transport === "agy_cli" ? "AGY CLI" : (p.base_url || "API");
      if ($("#intelModelSummary")) $("#intelModelSummary").textContent = p.model || "gemini-3.8-flash-high";
    } catch {
      // Quiet fallback
    }
  }

  async function saveIntelligenceSettings() {
    const statusEl = $("#intelSettingsStatus");
    try {
      if (statusEl) statusEl.textContent = "正在保存…";
      const effort = EFFORT_LEVELS[Number($("#intelEffort")?.value)] || "high";
      const provider = {
        transport: $("#intelTransport")?.value || "agy_cli",
        model: $("#intelModel")?.value?.trim() || "gemini-3.8-flash-high",
        cli_model: $("#intelModel")?.value?.trim() || "gemini-3.8-flash-high",
        cli_effort: effort,
        cli_timeout_seconds: Number($("#intelTimeout")?.value) || 180,
        context_window: Number($("#intelContextWindow")?.value) || 1048576,
        max_output_tokens: Number($("#intelMaxOutput")?.value) || 4096,
        system_prompt: $("#intelPrompt")?.value || "",
        base_url: $("#intelBaseUrl")?.value?.trim() || "",
        api_mode: $("#intelApiMode")?.value || "auto",
        extra_params: $("#intelApiParams")?.value || "{}",
      };
      if ($("#intelApiKey")?.value?.trim()) {
        provider.api_key = $("#intelApiKey").value.trim();
      }

      await api("/api/intelligence/settings", jsonPost({ provider }));
      if (statusEl) statusEl.textContent = "设置已保存到本机";
      toast("随堂速记增强设置已保存");
      void loadIntelligenceSettings();
    } catch (err) {
      if (statusEl) statusEl.textContent = `保存失败：${err.message}`;
      toast(`保存失败: ${err.message}`);
    }
  }

  async function loadKnowledgeSettings() {
    if (!$("#knowledgeModelSelect")) return;
    try {
      const data = await api("/api/knowledge/settings");
      const s = data.settings || {};
      if ($("#knowledgeModelSelect")) $("#knowledgeModelSelect").value = s.model_id || "Qwen/Qwen3-Embedding-4B";
      if ($("#knowledgeDeviceSelect")) $("#knowledgeDeviceSelect").value = s.device || "mps";
      if ($("#knowledgeRerankerSelect")) $("#knowledgeRerankerSelect").value = s.reranker || "none";
      if ($("#knowledgeBatchSize")) $("#knowledgeBatchSize").value = s.batch_size || 2;
      if ($("#knowledgeMaxLength")) $("#knowledgeMaxLength").value = s.max_length || 2048;
      if ($("#knowledgeIdleSeconds")) $("#knowledgeIdleSeconds").value = s.idle_seconds || 30;
      if ($("#knowledgeTimeoutSeconds")) $("#knowledgeTimeoutSeconds").value = s.timeout_seconds || 180;

      if ($("#knowledgeModelSummary")) $("#knowledgeModelSummary").textContent = (s.model_id || "").split("/").pop() || "Qwen3";
      if ($("#knowledgeDeviceSummary")) $("#knowledgeDeviceSummary").textContent = s.device === "mps" ? "Apple GPU (MPS)" : "CPU";

      const statusEl = $("#knowledgeSettingsStatus");
      if (statusEl) {
        statusEl.textContent = data.prepared ? "模型已准备就绪" : "模型尚未准备，请点击下载/核验";
      }
    } catch {
      // Quiet
    }
  }

  async function saveKnowledgeSettings() {
    const statusEl = $("#knowledgeSettingsStatus");
    try {
      if (statusEl) statusEl.textContent = "正在保存知识设置…";
      const settings = {
        model_id: $("#knowledgeModelSelect")?.value,
        device: $("#knowledgeDeviceSelect")?.value,
        reranker: $("#knowledgeRerankerSelect")?.value || "none",
        batch_size: Number($("#knowledgeBatchSize")?.value) || 2,
        max_length: Number($("#knowledgeMaxLength")?.value) || 2048,
        idle_seconds: Number($("#knowledgeIdleSeconds")?.value) || 30,
        timeout_seconds: Number($("#knowledgeTimeoutSeconds")?.value) || 180,
      };

      await api("/api/knowledge/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ settings }) });
      if (statusEl) statusEl.textContent = "知识检索设置已保存";
      toast("知识检索设置已保存");
      void loadKnowledgeSettings();
    } catch (err) {
      if (statusEl) statusEl.textContent = `保存失败：${err.message}`;
      toast(`保存失败: ${err.message}`);
    }
  }

  async function prepareKnowledgeModel() {
    const statusEl = $("#knowledgeSettingsStatus");
    const btn = $("#knowledgePrepareModelBtn");
    try {
      if (btn) btn.disabled = true;
      if (statusEl) statusEl.textContent = "正在下载与核验本地模型，请稍候…";
      const model_id = $("#knowledgeModelSelect")?.value;
      await api("/api/knowledge/models/prepare", jsonPost({ model_id }));
      if (statusEl) statusEl.textContent = "模型下载与准备完成！";
      toast("模型已准备完成");
      void loadKnowledgeSettings();
    } catch (err) {
      if (statusEl) statusEl.textContent = `准备失败：${err.message}`;
      toast(`模型准备失败: ${err.message}`);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // Hook settings UI events
  $("#intelSaveSettings")?.addEventListener("click", () => void saveIntelligenceSettings());
  $("#intelEffort")?.addEventListener("input", (e) => {
    const val = EFFORT_LEVELS[Number(e.target.value)] || "high";
    if ($("#intelEffortValue")) $("#intelEffortValue").textContent = val;
  });
  $("#intelResetPrompt")?.addEventListener("click", () => {
    if ($("#intelPrompt")) {
      $("#intelPrompt").value = "你是课堂速记整理与智能扩充助手。根据学生在课上记下的速记、当前课堂真实转写与课程参考资料，将速记整理为条理清晰的课堂笔记。严格区分课堂事实、教材内容与 AI 推理补充，每一条课堂或教材事实必须严格引用提供的依据。";
    }
  });

  $("#knowledgeSaveSettingsBtn")?.addEventListener("click", () => void saveKnowledgeSettings());
  $("#knowledgePrepareModelBtn")?.addEventListener("click", () => void prepareKnowledgeModel());

  // Initialize settings
  void loadIntelligenceSettings();
  void loadKnowledgeSettings();

  // Export instance methods
  const instance = {
    showSession,
    loadScribbles,
    fetchRelatedMaterials,
    switchTab,
    loadIntelligenceSettings,
    loadKnowledgeSettings,
  };

  // Attach globally for interoperability
  window.__classroomIntelligence = instance;

  return instance;
}
