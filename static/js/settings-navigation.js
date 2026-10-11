const LEVELS = ["low", "medium", "high", "xhigh", "max"];
const $ = selector => document.querySelector(selector);

export function createSettingsNavigation() {
  const dialog = $("#settingsDialog"), content = dialog.querySelector(".settings-page-body");
  const pages = {
    ai: ["AI 模型与提示词", "按功能分别选择模型与回答方式。"],
    recording: ["实时录音", "调整收音灵敏度、分段和课堂实时提纲。"],
    course: ["课程背景与术语", "为当前课程保存背景、提示词与专业术语。"],
    inference: ["推理位置", "选择语音和文档识别使用的设备。"],
    documents: ["文件识别", "调整 PDF 识别质量与处理方式。"],
    library: ["资料库与配置", "管理本机资料的位置与保存的配置。"],
    connections: ["账号同步", "管理 IdeaShell 连接与同步账号。"],
  };
  let activePage = "ai";
  const sliders = new Map();
  function syncActions() {
    $("#settingsActionBar").classList.toggle("hidden", activePage !== "ai" || $("[data-ai-section=chat]").classList.contains("hidden"));
  }

  function select(page) {
    if (!pages[page]) return;
    const changed = activePage !== page;
    activePage = page;
    dialog.querySelectorAll("[data-settings-page]").forEach(group => group.classList.toggle("hidden", group.dataset.settingsPage !== page));
    dialog.querySelectorAll("[data-settings-tab]").forEach(button => {
      const current = button.dataset.settingsTab === page;
      button.classList.toggle("active", current);
      if (current) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    $("#settingsPageTitle").textContent = pages[page][0];
    if (changed) content.scrollTop = 0;
    syncActions();
  }

  function reveal(target) {
    if (!target) return;
    const group = target.closest("[data-settings-page]");
    if (group) select(group.dataset.settingsPage);
    for (let ancestor = target.parentElement; ancestor && ancestor !== dialog; ancestor = ancestor.parentElement) {
      if (ancestor.tagName === "DETAILS") ancestor.open = true;
    }
  }

  function readEffort(input) {
    return input?.type === "range" ? LEVELS[Number(input.value)] || "high" : input?.value || "high";
  }
  function setEffort(input, value) {
    if (!input) return;
    if (input.type !== "range") { input.value = value; return; }
    const index = LEVELS.indexOf(value || "high");
    input.value = index < 0 ? 2 : index;
    sliders.get(input.id)?.();
  }
  function convertEffort(selectInput) {
    const original = selectInput.closest("label");
    const container = document.createElement("div"); container.className = "chat-effort-setting";
    const heading = document.createElement("div"); heading.className = "chat-effort-heading";
    const label = document.createElement("label"); label.htmlFor = selectInput.id; label.textContent = "AGY 思考强度";
    const output = document.createElement("output"); output.setAttribute("for", selectInput.id);
    const input = document.createElement("input"); input.type = "range"; input.id = selectInput.id;
    input.min = 0; input.max = 4; input.step = 1;
    const ticks = document.createElement("div"); ticks.className = "chat-effort-ticks"; ticks.setAttribute("aria-hidden", "true");
    for (const level of LEVELS) { const tick = document.createElement("span"); tick.textContent = level; ticks.append(tick); }
    const update = () => {
      const level = readEffort(input); output.textContent = level; input.setAttribute("aria-valuetext", level);
      ticks.querySelectorAll("span").forEach(tick => tick.toggleAttribute("data-selected", tick.textContent === level));
    };
    sliders.set(input.id, update);
    input.addEventListener("input", update);
    heading.append(label, output); container.append(heading, input, ticks);
    setEffort(input, selectInput.value);
    original.replaceWith(container);
  }
  dialog.querySelectorAll('select[id$="Effort"]').forEach(convertEffort);

  const fieldContainer = id => $("#" + id)?.closest("label, .chat-effort-setting");
  const advanced = (title, nodes) => {
    const details = document.createElement("details"); details.className = "settings-advanced wide";
    const summary = document.createElement("summary"); summary.textContent = title;
    const grid = document.createElement("div"); grid.className = "settings-group-grid";
    grid.append(...nodes.filter(Boolean)); details.append(summary, grid); return details;
  };
  function syncProvider(mode) {
    const panel = dialog.querySelector(`[data-ai-section="${mode}"]`);
    if (panel) {
      panel.dataset.transport = $("#" + mode + "Transport")?.value || "api";
      const modelInput = $(mode === "sentence" ? "#aiModel" : "#" + mode + "Model");
      if (modelInput && mode !== "chat") {
        if (panel.dataset.transport === "agy_cli") modelInput.setAttribute("list", "chatModelOptions");
        else modelInput.removeAttribute("list");
      }
    }
  }
  for (const panel of dialog.querySelectorAll("[data-ai-section]")) {
    const mode = panel.dataset.aiSection, sentence = mode === "sentence";
    const body = panel.querySelector(".provider-settings-body");
    const grid = body.classList.contains("settings-group-grid") ? body : body.querySelector(".settings-group-grid");
    if (!grid) continue;
    // The old sentence page placed concurrency outside its field grid.
    const concurrency = fieldContainer(mode + "Concurrency");
    if (concurrency && !grid.contains(concurrency)) grid.append(concurrency);
    const transport = fieldContainer(mode + "Transport");
    const model = fieldContainer(sentence ? "aiModel" : mode + "Model");
    if (transport && model) grid.prepend(transport, model);
    const modelCaption = model?.querySelector("span"); if (modelCaption) modelCaption.textContent = "模型";
    const effort = fieldContainer(mode + "Effort"); if (effort) { effort.dataset.agyField = ""; effort.classList.add("wide"); }
    const timeout = fieldContainer(mode + "Timeout"); if (timeout) timeout.dataset.agyField = "";
    const apiFields = [sentence ? "aiBaseUrl" : mode + "BaseUrl", sentence ? "aiApiKey" : mode + "ApiKey",
                       sentence ? "aiApiMode" : mode + "ApiMode", sentence ? "apiParams" : mode + "ApiParams"];
    apiFields.forEach(id => { const node = fieldContainer(id); if (node) node.dataset.apiField = ""; });
    const advancedFields = [sentence ? "contextWindow" : mode + "ContextWindow", mode + "MaxOutput",
                            mode + "Concurrency", sentence ? "aiApiMode" : mode + "ApiMode",
                            sentence ? "apiParams" : mode + "ApiParams", mode + "Timeout"];
    const nodes = advancedFields.map(fieldContainer).filter(Boolean);
    if (concurrency?.querySelector("span")) concurrency.querySelector("span").textContent = "最大并发请求";
    if (nodes.length) grid.append(advanced("高级参数", nodes));
    if (mode === "summary") grid.append(advanced("录音标题与短摘要提示词", [fieldContainer("abstractPrompt"), fieldContainer("titlePrompt")]));
    if (mode === "chat") {
      const context = grid.querySelector(".chat-context-settings");
      const prompt = fieldContainer("chatPrompt");
      const details = context ? advanced("携带的课堂内容", [context]) : null;
      if (details && prompt) prompt.after(details);
      const actions = grid.querySelector(".chat-settings-actions"), status = $("#chatSettingsStatus");
      if (status) $("#settingsActionBar").append(status);
      if (actions) $("#settingsActionBar").append(actions);
    }
    const test = body.querySelector(".test-provider");
    const output = $("#test-" + mode);
    if (test) {
      const actions = document.createElement("div"); actions.className = "settings-provider-actions wide";
      actions.append(test); if (output) actions.append(output); grid.append(actions);
    }
    panel.querySelectorAll("textarea").forEach(input => { input.rows = Math.min(input.rows, 7); });
    syncProvider(mode);
    $("#" + mode + "Transport")?.addEventListener("change", () => syncProvider(mode));
  }
  const asr = fieldContainer("asrParams");
  if (asr) {
    const placeholder = document.createElement("div"); asr.before(placeholder);
    placeholder.replaceWith(advanced("ASR 推理参数", [asr]));
    $("#asrParams").rows = 8;
  }
  dialog.querySelectorAll("[data-settings-tab]").forEach(button => button.addEventListener("click", () => select(button.dataset.settingsTab)));
  select(activePage);
  return { select, reveal, readEffort, setEffort, syncProvider, syncActions, resetScroll: () => { content.scrollTop = 0; } };
}
