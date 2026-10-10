// Uploaded files are submitted before observing their background jobs.
export function createBatchView({els, state, formatDuration, escapeHtml, summaryView, showSession, updateButtons, toast, sessionConfig, selectedRecordCategory, loadLibrary}) {
const $ = selector => document.querySelector(selector);
function setBatchProgress(fileIndex, fileCount, progress, stage, filename) {
  const overall = Math.min(100, ((fileIndex + Number(progress || 0) / 100) / fileCount) * 100);
  els.batchProgressBar.style.width = `${Math.max(parseFloat(els.batchProgressBar.style.width) || 0, overall)}%`;
  els.batchFileStatus.textContent = `${fileIndex + 1}/${fileCount} · ${filename}`;
  els.batchStage.textContent = `${stage || "处理中"} · 总进度 ${Math.round(overall)}%`;
}

function addBatchBlock(container, segment, text, label) {
  const block = document.createElement("p");
  block.dataset.id = String(segment?.id || "");
  const timing = segment && Number.isFinite(Number(segment.start))
    ? `${formatDuration(segment.start)}–${formatDuration(segment.end)}` : "";
  block.innerHTML = `<small>${escapeHtml(label)}${timing ? ` · ${timing}` : ""}</small>${escapeHtml(text || "")}`;
  container.appendChild(block);
  container.scrollTop = container.scrollHeight;
}

async function submitUploadedFile(file, config) {
  const form = new FormData();
  form.append("file", file, file.name); form.append("config_json", JSON.stringify(config));
  const response = await fetch("/api/upload-jobs?background=true", {method:"POST", body:form});
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || "上传失败");
  return result;
}

async function transcribeUploadedFile(file, config, fileIndex, fileCount, resumeRecord = null, acceptedJob = null) {
  const displayName = resumeRecord?.title || file?.name || "录音";
  let response;
  if (acceptedJob) {
    response = await fetch(`/api/upload-jobs/${acceptedJob.id}/events`);
  } else if (resumeRecord) {
    setBatchProgress(fileIndex, fileCount, resumeRecord.batch_progress?.percent || 1, "正在恢复任务", displayName);
    response = await fetch(`/api/recordings/${encodeURIComponent(resumeRecord.id)}/resume`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config),
    });
  } else {
    const job = await submitUploadedFile(file, config);
    response = await fetch(`/api/upload-jobs/${job.id}/events`);
  }
  if (!response.ok || !response.body) {
    let message = "上传转写失败";
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completedSession = null;
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    if (done && buffer.trim()) { lines.push(buffer); buffer = ""; }
    for (const line of lines) {
      if (!line.trim()) continue;
      const event = JSON.parse(line);
      setBatchProgress(fileIndex, fileCount, event.progress, event.stage, displayName);
      if (event.type === "raw") {
        addBatchBlock(els.batchRaw, event.segment, event.segment.raw, `原始段 ${event.segment.id}`);
      } else if (event.type === "retry") {
        addBatchBlock(
          els.batchRaw,
          { start: event.start, end: event.end },
          event.stage,
          "自动修复"
        );
      } else if (event.type === "ai_group") {
        els.batchAi.classList.remove("muted-output");
        if (els.batchAi.dataset.placeholder === "true") els.batchAi.innerHTML = "";
        els.batchAi.dataset.placeholder = "false";
        const group = event.group || {};
        const text = [group.corrected || group.raw, group.translation && `译文：${group.translation}`].filter(Boolean).join("\n\n");
        addBatchBlock(els.batchAi, group, text, `AI 长段 ${group.id || ""}`);
      } else if (event.type === "ai_segment") {
        els.batchAi.classList.remove("muted-output");
        if (els.batchAi.dataset.placeholder === "true") els.batchAi.innerHTML = "";
        els.batchAi.dataset.placeholder = "false";
        const segment = event.segment || {};
        const text = [segment.corrected || segment.raw, segment.translation && `译文：${segment.translation}`]
          .filter(Boolean).join("\n\n");
        addBatchBlock(els.batchAi, segment, text, `逐段修正 ${segment.id || ""}`);
      } else if (event.type === "ai_global_part") {
        const part = event.part || {};
        const text = [part.corrected || part.raw, part.translation && `译文：${part.translation}`]
          .filter(Boolean).join("\n\n");
        addBatchBlock(els.batchAi, part, text, `全局改写 ${event.index}/${event.count}`);
      } else if (event.type === "ai_global_complete") {
        const result = event.global_rewrite || {};
        addBatchBlock(
          els.batchAi,
          {},
          result.context_split
            ? "内容超过 Context Window，已按安全分块完成全局整理，未丢弃正文。"
            : "全部内容已完成最终全局统一改写。",
          "全局改写完成"
        );
      } else if (event.type === "summary") {
        summaryView.renderResult(event.summary || {});
        addBatchBlock(els.batchAi, {}, event.summary?.summary || "", "课堂总结");
      } else if (event.type === "summary_error") {
        summaryView.renderError(event.message || "未知错误");
        addBatchBlock(els.batchAi, {}, `课后复习笔记未完成：${event.message}`, "课堂总结");
      } else if (event.type === "ai_error") {
        const label = event.scope === "segment" ? `逐段修正 ${event.id}` : `AI 处理 ${event.id}`;
        addBatchBlock(els.batchAi, { id: event.id }, `AI 处理未完成：${event.message}`, label);
      } else if (event.type === "complete") {
        completedSession = event.session;
        const inference = completedSession.inference || {};
        const origin = inference.provider === 'remote' ? 'Windows 识别' : inference.provider === 'local' ? 'Mac 识别' : '来源未验证';
        $("#batchTiming").textContent = `本次处理耗时 ${formatDuration(completedSession.processing_seconds)} · ${origin}累计 ${formatDuration(completedSession.asr_seconds)}${inference.fallback ? ' · 已回退本机' : ''}${completedSession.ai_warnings ? ` · ${completedSession.ai_warnings} 项 AI 处理未完成` : ""}`;
      } else if (event.type === "error") {
        throw new Error(event.message || "录音转写失败");
      }
    }
    if (done) break;
  }
  if (!completedSession) throw new Error("服务没有返回完整的录音结果");
  return completedSession;
}

async function uploadRecordings(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length || state.uploading || state.activeSession) return;
  state.uploading = true;
  els.batchRaw.innerHTML = "";
  els.batchAi.textContent = els.aiCorrection.checked || els.translation.value !== "off"
    ? "等待转写与 转写校对"
    : "等待转写";
  els.batchAi.dataset.placeholder = "true";
  els.batchAi.classList.add("muted-output");
  els.batchQueue.textContent = `队列：${files.map((file) => file.name).join(" · ")}`;
  els.batchProgressBar.style.width = "0%";
  if (!els.batchDialog.open) els.batchDialog.showModal();
  els.listeningLabel.textContent = "正在批量处理上传录音";
  updateButtons();
  let lastSession = null;
  try {
    const accepted = [];
    for (let index = 0; index < files.length; index++) {
      const file = files[index];
      const config = sessionConfig(false);
      delete config.type;
      config.title = files.length === 1 && els.recordName.value.trim()
        ? els.recordName.value.trim() : file.name.replace(/\.[^.]+$/, "") || `录音 ${index + 1}`;
      config.category = selectedRecordCategory(true);
      config.summary_enabled = false;
      setBatchProgress(index, files.length, 0, "正在上传；上传成功的任务会在后台继续", file.name);
      accepted.push({file, config, job: await submitUploadedFile(file, config)});
    }
    els.batchQueue.textContent = `${accepted.length} 个文件已上传并进入后台队列。关闭页面后继续处理，可在任务中心查看。`;
    for (let index = 0; index < accepted.length; index++) {
      const {file, config, job} = accepted[index];
      lastSession = await transcribeUploadedFile(file, config, index, files.length, null, job);
    }
    if (lastSession) showSession(lastSession, true);
    els.listeningLabel.textContent = `${files.length} 个上传录音已转写并保存`;
    els.batchStage.textContent = `全部完成 · 共 ${files.length} 个文件`;
    els.recordName.value = "";
    await loadLibrary();
    toast("批量转写完成；AAC 与转写已保存，ZIP 可按需下载", 4800);
  } catch (error) {
    els.listeningLabel.textContent = "请到任务中心查看上传任务";
    els.batchStage.textContent = `进度连接未完成：${error.message}。已接收的文件仍由后台处理，可在任务中心查看。`;
    toast(error.message, 6200);
  } finally {
    state.uploading = false;
    await loadLibrary();
    els.fileInput.value = "";
    updateButtons();
  }
}


return {uploadRecordings, transcribeUploadedFile};
}
