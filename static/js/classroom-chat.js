import { renderMarkdown } from "./markdown-reader.js?v=20261108-ppt-3";
import { createClassroomIntelligence } from "./classroom-intelligence.js?v=20261031-browser-fixes-1";
import {createVisiblePoll, classroomPanelVisible} from './visible-poll.js';

const $ = (selector) => document.querySelector(selector);
const EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"];
const messageFor = (payload) => typeof payload.detail === "string" ? payload.detail : payload.detail?.message || "请求未完成，请重试";
const stamp = (seconds) => {
  const n = Math.max(0, Math.floor(Number(seconds) || 0));
  return [Math.floor(n / 3600), Math.floor(n / 60) % 60, n % 60].map(v => String(v).padStart(2, "0")).join(":");
};
async function api(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...(options.method ? {} : {signal: AbortSignal.timeout(15000)}), ...options });
  const payload = await response.json();
  if (!response.ok) throw new Error(messageFor(payload));
  return payload;
}
const jsonOptions = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export function createClassroomChat({ openSettings, toast }) {
  const intelligence = window.__classroomIntelligence || createClassroomIntelligence({ openSettings, toast });
  const e = Object.fromEntries(["Messages", "Context", "Question", "Send", "Form", "Error", "Clear", "ModelSelect", "Settings", "SaveSettings", "SettingsStatus", "RefreshModels", "Scope"].map(key => [key, $("#chat" + key)]));
  let sessionId = null, settings = null, defaultPrompt = "", models = [], viewVersion = 0;
  let settingsDirty = false, settingsSaving = false, sessionActive = false;
  let settingsEditVersion = 0;
  let loadedId = null, displayed = "";
  let modelsLoading = null, modelsUpdatedAt = 0;
  const documents = new Map(), drafts = new Map(), submitting = new Set(), refreshRequests = new Map();
  const docFor = id => documents.get(id) || { conversation_id: "", turns: [] };
  const baseFor = id => `/api/recordings/${encodeURIComponent(id)}/chat`;

  function error(message = "") {
    e.Error.textContent = message;
    e.Error.classList.toggle("hidden", !message);
  }
  function controls() {
    const doc = docFor(sessionId), busy = submitting.has(sessionId) || doc.running;
    const ready = Boolean(sessionId && loadedId === sessionId && settings);
    e.Question.disabled = !ready;
    e.Send.disabled = !ready || busy || !e.Question.value.trim();
    e.Send.textContent = busy ? "回答中…" : "发送";
    e.Clear.disabled = !ready || busy || !doc.turns.length;
    e.ModelSelect.disabled = !settings || settingsSaving;
    e.SaveSettings.disabled = !settings || settingsSaving;
    if (e.Scope) e.Scope.disabled = !ready || busy;
  }
  function contextText(info) {
    if (!info) return "开始录音后，自动读取本节课上下文";
    if (!info.has_text) return "等待第一段转写 · 问答会读取本节课完整内容";
    const parts = [`全文 ${info.segment_count} 段`, `已转写至 ${stamp(info.transcribed_until)}`];
    if (info.preview_count && settings?.context.live_preview) parts.push("含未定稿预览");
    if (info.pending_segments || info.captured_until - info.available_until > 3) parts.push("仍有语音转写中");
    if (info.scope && info.scope !== "lesson") {
      const scopeMap = { recent: "范围：最近3分钟", course: "范围：课程资料库", full: "范围：本堂课全文" };
      parts.push(scopeMap[info.scope] || info.scope);
    }
    return parts.join(" · ");
  }
  function render(force = false) {
    const doc = docFor(sessionId);
    const contextLabel = contextText(doc.context);
    if (e.Context.textContent !== contextLabel) e.Context.textContent = contextLabel;
    const fingerprint = JSON.stringify([sessionId, doc.turns, submitting.has(sessionId), Boolean(doc.running)]);
    if (force || fingerprint !== displayed) {
      const nearBottom = e.Messages.scrollHeight - e.Messages.scrollTop - e.Messages.clientHeight < 80;
      const fragment = document.createDocumentFragment();
      if (!doc.turns.length) {
        const empty = document.createElement("p"); empty.className = "chat-empty";
        empty.textContent = sessionId ? "哪里没听懂，就在这里问。" : "开始录音后，可以随时提问。";
        fragment.append(empty);
      }
      for (const turn of doc.turns) {
        const article = document.createElement("article"); article.className = "chat-turn";
        const question = document.createElement("p"); question.className = "chat-question";
        question.textContent = turn.question; article.append(question);
        const answer = document.createElement("div");
        if (turn.status === "completed") {
          answer.className = "chat-answer markdown-body";
          answer.innerHTML = renderMarkdown(turn.answer);
          answer.querySelectorAll("a").forEach(link => { link.target = "_blank"; link.rel = "noopener noreferrer"; });

          // Citations and freshness warnings
          const citations = turn.evidence || turn.citations || [];
          const isStale = Boolean(turn.stale || turn.freshness_warning || citations.some(c => c.stale || c.valid === false));
          if (isStale) {
            const warn = document.createElement("div");
            warn.className = "chat-freshness-warning";
            warn.setAttribute("role", "note");
            warn.textContent = typeof turn.freshness_warning === "string" ? turn.freshness_warning : "⚠️ 引用资料版本已变动，请注意核对最新内容";
            article.append(warn);
          }
          if (citations.length > 0) {
            const citBox = document.createElement("div");
            citBox.className = "chat-citations";
            const citHeading = document.createElement("span");
            citHeading.className = "chat-citations-label";
            citHeading.textContent = "参考依据：";
            citBox.append(citHeading);

            const citList = document.createElement("div");
            citList.className = "chat-citations-list";
            for (const cit of citations) {
              const item = document.createElement("a");
              item.className = "chat-citation-link";
              if (cit.stale || cit.valid === false) item.classList.add("stale");
              item.target = "_blank";
              item.rel = "noopener noreferrer";
              const targetUrl = cit.open_url || (cit.source_id ? `/api/knowledge/source/${encodeURIComponent(cit.source_id)}?revision=${encodeURIComponent(cit.revision || "")}${cit.page ? "&page=" + encodeURIComponent(cit.page) : ""}${cit.start !== null && cit.start !== undefined ? "&start=" + encodeURIComponent(cit.start) : ""}` : "#");
              item.href = targetUrl;

              const icon = document.createElement("span");
              icon.className = "chat-citation-icon";
              icon.setAttribute("aria-hidden", "true");
              icon.textContent = (cit.kind === "audio" || (cit.start !== undefined && cit.start !== null)) ? "🎙️" : "📄";
              item.append(icon);

              const title = document.createElement("span");
              title.className = "chat-citation-title";
              const parts = [cit.title || "资料"];
              if (cit.heading) parts.push(cit.heading);
              if (cit.page) parts.push(`P.${cit.page}`);
              if (cit.start !== null && cit.start !== undefined) parts.push(stamp(cit.start));
              title.textContent = parts.join(" ");
              item.append(title);

              if (cit.origin) {
                const originBadge = document.createElement("span");
                originBadge.className = "chat-citation-origin";
                originBadge.textContent = cit.origin === "original" ? "原件" : (cit.origin === "human" ? "速记" : "AI");
                item.append(originBadge);
              }

              if (cit.stale || cit.valid === false) {
                const staleBadge = document.createElement("span");
                staleBadge.className = "chat-citation-stale-badge";
                staleBadge.textContent = "已更新";
                item.append(staleBadge);
              }

              citList.append(item);
            }
            citBox.append(citList);
            article.append(citBox);
          }
        } else {
          answer.className = turn.status === "failed" ? "chat-error" : "chat-pending";
          answer.textContent = turn.status === "failed" ? turn.error : "正在结合本节课全文回答…";
        }
        article.append(answer);
        const meta = document.createElement("div"); meta.className = "chat-turn-meta";
        const label = document.createElement("span");
        const scopeLabel = turn.scope && turn.scope !== "lesson" ? (turn.scope === "recent" ? "近3分钟" : (turn.scope === "course" ? "课程资料" : "本堂课全文")) : "";
        label.textContent = [turn.model, scopeLabel, turn.context ? `上下文至 ${stamp(turn.context.available_until)}` : ""].filter(Boolean).join(" · ");
        meta.append(label);
        if (turn.status !== "pending") {
          const action = document.createElement("button"); action.type = "button"; action.className = "ghost-button";
          action.textContent = turn.status === "completed" ? "复制" : "重试";
          action.disabled = turn.status === "failed" && Boolean(doc.running || submitting.has(sessionId));
          const boundId = sessionId;
          action.addEventListener("click", async () => {
            if (turn.status === "failed") { if (sessionId === boundId) await submit(turn); return; }
            try { await navigator.clipboard.writeText(turn.answer); toast("回答已复制"); }
            catch { toast("复制失败，请选中文本后复制"); }
          });
          meta.append(action);
        }
        article.append(meta); fragment.append(article);
      }
      e.Messages.replaceChildren(fragment); displayed = fingerprint;
      if (force || nearBottom) e.Messages.scrollTop = e.Messages.scrollHeight;
    }
    controls();
  }
  async function refreshConversation() {
    const id = sessionId, version = viewVersion;
    if (!id) return;
    if (refreshRequests.has(id)) return refreshRequests.get(id);
    const request = (async () => {
      try {
        const doc = await api(baseFor(id));
        if (sessionId === id && viewVersion !== version) return;
        const previous = docFor(id);
        if (submitting.has(id) || previous.conversation_id === doc.conversation_id) {
          for (const turn of previous.turns) {
            if (!doc.turns.some(item => item.id === turn.id) && (turn.status === "failed" || submitting.has(id))) doc.turns.push(turn);
          }
        }
        documents.set(id, doc);
        if (sessionId === id && viewVersion === version) { loadedId = id; render(); }
      } catch (exc) { if (sessionId === id && loadedId !== id) error(exc.message); }
      finally { controls(); }
    })().finally(() => { refreshRequests.delete(id); conversationPoll.schedule(); });
    refreshRequests.set(id, request);
    return request;
  }
  function showSession(session) {
    const next = session?.id || null;
    if (window.__classroomIntelligence?.showSession) {
      try { window.__classroomIntelligence.showSession(session); } catch {}
    }
    const wasActive = sessionActive;
    sessionActive = session?.status === 'active';
    if (next === sessionId) {
      if (wasActive !== sessionActive) conversationPoll.wake();
      return;
    }
    if (sessionId) drafts.set(sessionId, e.Question.value);
    sessionId = next; viewVersion++; loadedId = null;
    e.Question.value = drafts.get(next) || "";
    error(); render(true); conversationPoll.wake();
  }
  async function submit(retry = null) {
    const id = sessionId;
    if (!id || loadedId !== id || submitting.has(id) || docFor(id).running) return;
    const question = retry?.question || e.Question.value.trim();
    if (!question) return;
    const requestId = retry?.id || crypto.randomUUID();
    const doc = docFor(id), conversationId = doc.conversation_id;
    const turn = retry || { id: requestId, question };
    const scope = retry?.scope || e.Scope?.value || "lesson";
    turn.scope = scope;
    if (!retry) doc.turns.push(turn);
    turn.status = "pending"; turn.error = "";
    documents.set(id, doc); submitting.add(id);
    if (!retry) { e.Question.value = ""; drafts.delete(id); }
    error(); render(true); conversationPoll.schedule();
    try {
      const result = await api(baseFor(id), jsonOptions("POST", {question, request_id: requestId, conversation_id: conversationId, scope}));
      const latest = docFor(id);
      const index = latest.turns.findIndex(item => item.id === requestId);
      if (index >= 0) latest.turns[index] = result.turn; else latest.turns.push(result.turn);
      latest.conversation_id = result.conversation_id; latest.running = false;
      documents.set(id, latest);
    } catch (exc) {
      // Reconcile a lost HTTP response with the durable server result before
      // offering a retry. The same request id prevents a duplicate AI call.
      let current = null;
      try { current = await api(baseFor(id)); documents.set(id, current); } catch { /* Keep local question for retry. */ }
      const savedTurn = current?.turns.find(item => item.id === requestId);
      if (!savedTurn) {
        turn.status = "failed"; turn.error = exc.message;
        const latest = docFor(id);
        if (!latest.turns.some(item => item.id === requestId)) latest.turns.push(turn);
        documents.set(id, latest);
      }
      if (sessionId === id && savedTurn?.status !== "completed") error(exc.message);
    } finally {
      submitting.delete(id);
      if (sessionId === id) { render(); e.Question.focus({ preventScroll: true }); }
      conversationPoll.schedule();
    }
  }
  function fillSettings() {
    const p = settings.provider;
    const map = {Transport: "transport", Model: "model", Timeout: "cli_timeout_seconds", ContextWindow: "context_window", MaxOutput: "max_output_tokens", BaseUrl: "base_url", ApiMode: "api_mode", Prompt: "system_prompt"};
    for (const [suffix, key] of Object.entries(map)) $("#chat" + suffix).value = p[key] ?? (suffix === "ApiMode" ? "auto" : "");
    const effortIndex = EFFORT_LEVELS.indexOf(p.cli_effort || "high");
    $("#chatEffort").value = effortIndex < 0 ? 2 : effortIndex;
    renderEffort();
    $("#chatApiParams").value = typeof p.extra_params === "string" ? p.extra_params : JSON.stringify(p.extra_params || {}, null, 2);
    $("#chatApiKey").value = "";
    $("#chatKeyHint").textContent = settings.saved_key ? "· 已保存在本机" : "";
    document.querySelectorAll("[data-chat-context]").forEach(input => input.checked = settings.context[input.dataset.chatContext]);
    $("#chatProviderSummary").textContent = p.transport === "agy_cli" ? "AGY CLI" : p.base_url;
    $("#chatModelSummary").textContent = p.model;
    updateTransport(); renderModels(); controls();
  }
  function renderEffort() {
    const level = EFFORT_LEVELS[Number($("#chatEffort").value)] || "high";
    $("#chatEffortValue").textContent = level;
    $("#chatEffort").setAttribute("aria-valuetext", level);
    document.querySelectorAll(".chat-effort-ticks span").forEach(tick => tick.toggleAttribute("data-selected", tick.textContent === level));
  }
  function updateTransport() {
    $("[data-ai-section=chat]").dataset.transport = $("#chatTransport").value;
    document.querySelectorAll(".chat-api-field").forEach(label => label.classList.toggle("hidden", $("#chatTransport").value !== "api"));
  }
  function renderModels() {
    if (!settings) return;
    const current = settings.provider.model;
    const entries = settings.provider.transport === "agy_cli" ? [...models] : [];
    if (!entries.some(item => item.id === current)) entries.unshift({id: current,
      name: settings.provider.transport === "agy_cli" && models.length ? `${current}（当前配置，AGY 暂未列出）` : current,
      unavailable: settings.provider.transport === "agy_cli" && models.length > 0});
    e.ModelSelect.replaceChildren(...entries.map(item => { const option = new Option(item.name, item.id); option.disabled = Boolean(item.unavailable); return option; }));
    e.ModelSelect.value = current;
    $("#chatModelOptions").replaceChildren(...models.map(item => new Option(item.name, item.id)));
  }
  async function loadModels(refresh = false) {
    if (modelsLoading) return modelsLoading;
    modelsLoading = (async () => {
      e.RefreshModels.disabled = true;
      try {
        const result = await api(`/api/ai/classroom-chat/models${refresh ? "?refresh=true" : ""}`);
        models = result.models; modelsUpdatedAt = Date.now(); renderModels();
        if (refresh && !settingsDirty) e.SettingsStatus.textContent = `已读取 ${models.length} 个 AGY 模型`;
      } catch (exc) { e.SettingsStatus.textContent = `${exc.message}；仍可手动填写模型名称。`; }
      finally { e.RefreshModels.disabled = false; modelsLoading = null; }
    })();
    return modelsLoading;
  }
  async function loadSettings(resetDraft = false) {
    if (resetDraft) settingsDirty = false;
    try {
      settings = await api("/api/ai/classroom-chat/settings");
      defaultPrompt = settings.default_prompt || defaultPrompt;
      if (!settingsDirty) fillSettings();
      error(); controls(); void loadModels();
    } catch (exc) { error(exc.message); }
  }
  async function persist(body, refill = true) {
    const editVersion = settingsEditVersion, hadDraft = settingsDirty;
    settingsSaving = true; controls();
    try {
      settings = await api("/api/ai/classroom-chat/settings", jsonOptions("PUT", body));
      settingsDirty = settingsEditVersion !== editVersion || (!refill && hadDraft);
      if (refill && !settingsDirty) fillSettings(); else renderModels();
      e.SettingsStatus.textContent = settingsDirty ? "模型已保存；设置页还有未保存的修改" : "已保存到本机，从下一次提问生效";
      error(); render(); return true;
    } catch (exc) { e.SettingsStatus.textContent = `保存失败：${exc.message}`; error(exc.message); renderModels(); return false; }
    finally { settingsSaving = false; controls(); }
  }
  e.Form.addEventListener("submit", event => { event.preventDefault(); void submit(); });
  e.Question.addEventListener("input", controls);
  e.Question.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!e.Send.disabled) void submit(); }
  });
  e.Clear.addEventListener("click", async () => {
    const id = sessionId;
    if (!id || e.Clear.disabled) return;
    try {
      const result = await api(baseFor(id), {method: "DELETE"});
      if (sessionId === id) viewVersion++;
      documents.set(id, {...result, context: docFor(id).context});
      if (sessionId === id) { error(); render(true); }
    } catch (exc) { if (sessionId === id) error(exc.message); }
  });
  e.Settings.addEventListener("click", () => openSettings($("#chatModel")));
  e.RefreshModels.addEventListener("click", () => void loadModels(true));
  $("#chatTransport").addEventListener("change", updateTransport);
  $("#chatEffort").addEventListener("input", renderEffort);
  $("#chatResetPrompt").addEventListener("click", () => {
    $("#chatPrompt").value = defaultPrompt; settingsDirty = true;
    settingsEditVersion++;
    e.SettingsStatus.textContent = "默认提示词已填入，保存后生效";
  });
  $("[data-ai-section=chat]").addEventListener("input", () => { settingsDirty = true; settingsEditVersion++; e.SettingsStatus.textContent = "有尚未保存的课堂问答设置"; });
  e.SaveSettings.addEventListener("click", () => {
    if (!settings || settingsSaving) return;
    const transport = $("#chatTransport").value, model = $("#chatModel").value.trim();
    const provider = {transport, model, cli_model: model, cli_effort: EFFORT_LEVELS[Number($("#chatEffort").value)],
      cli_timeout_seconds: $("#chatTimeout").value, context_window: $("#chatContextWindow").value,
      max_output_tokens: $("#chatMaxOutput").value, system_prompt: $("#chatPrompt").value,
      base_url: $("#chatBaseUrl").value, api_mode: $("#chatApiMode").value, extra_params: $("#chatApiParams").value};
    if ($("#chatApiKey").value.trim()) provider.api_key = $("#chatApiKey").value.trim();
    const context = Object.fromEntries([...document.querySelectorAll("[data-chat-context]")].map(input => [input.dataset.chatContext, input.checked]));
    void persist({provider, context});
  });
  e.ModelSelect.addEventListener("change", async () => {
    if (!settings || settingsSaving) return;
    const model = e.ModelSelect.value;
    if (await persist({provider: {model, cli_model: model}}, !settingsDirty)) toast("课堂问答模型已切换");
  });
  const conversationPoll = createVisiblePoll({run: refreshConversation,
    enabled: () => sessionId && classroomPanelVisible($('#chatPanel')),
    delay: () => loadedId !== sessionId || sessionActive || submitting.has(sessionId) || docFor(sessionId).running ? 4000 : 60000,
    events: ['qwenasr:viewchange']});
  const refreshModelCatalog = () => {
    if (!document.hidden && settings?.provider.transport === "agy_cli" && Date.now() - modelsUpdatedAt >= 300000 &&
        (classroomPanelVisible($('#chatPanel')) || $('#settingsDialog')?.open)) void loadModels();
  };
  setInterval(refreshModelCatalog, 60000);
  document.addEventListener("visibilitychange", refreshModelCatalog);
  controls(); void loadSettings();
  return { showSession, loadSettings, refreshModels: () => loadModels(true) };
}
