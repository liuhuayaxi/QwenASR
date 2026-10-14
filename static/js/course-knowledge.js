/**
 * Course Knowledge UI: Unified search across course materials, index status, rebuild & jobs.
 * Follows the QwenASR knowledge development contract.
 */

const $ = (selector) => document.querySelector(selector);

function escapeHtml(str) {
  if (typeof str !== "string") return "";
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

function stamp(seconds) {
  const n = Math.max(0, Math.floor(Number(seconds) || 0));
  return [Math.floor(n / 3600), Math.floor(n / 60) % 60, n % 60].map(v => String(v).padStart(2, "0")).join(":");
}

async function api(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
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

function formatProgress(progress) {
  if (progress === null || progress === undefined) return "0%";
  const p = Number(progress);
  if (isNaN(p)) return "0%";
  const pct = p <= 1.0 ? Math.round(p * 100) : Math.round(p);
  return `${pct}%`;
}

function getSourceUrl(src) {
  if (src.open_url) return src.open_url;
  if (src.start !== null && src.start !== undefined) {
    const sess = src.lesson_id || src.session_id || "";
    return `/?recording=${encodeURIComponent(sess)}&session=${encodeURIComponent(sess)}#t=${src.start}`;
  }
  if (src.source_id) {
    const rev = src.revision ? `?revision=${encodeURIComponent(src.revision)}` : "";
    const hash = src.page ? `#page=${encodeURIComponent(src.page)}` : "";
    return `/api/knowledge/source/${encodeURIComponent(src.source_id)}${rev}${hash}`;
  }
  return "#";
}

export function initCourseKnowledge() {
  const els = {
    section: $("#knowledgeSection"),
    searchInput: $("#knowledgeSearchInput"),
    searchBtn: $("#knowledgeSearchBtn"),
    crossCourse: $("#knowledgeCrossCourse"),
    searchStatus: $("#knowledgeSearchStatus"),
    resultsContainer: $("#knowledgeResultsContainer"),
    resultsList: $("#knowledgeResultsList"),
    emptyState: $("#knowledgeEmptyState"),

    // Index status & management
    statusBadge: $("#knowledgeStatusBadge"),
    docCount: $("#knowledgeDocCount"),
    chunkCount: $("#knowledgeChunkCount"),
    indexPolicy: $("#knowledgeIndexPolicy"),
    rebuildBtn: $("#knowledgeRebuildBtn"),
    jobsBox: $("#knowledgeJobsBox"),
    jobsText: $("#knowledgeJobsText"),
    cancelJobBtn: $("#knowledgeCancelJobBtn"),
    sourcesList: $("#knowledgeSourcesList"),
    sourcesDisclosure: $("#knowledgeSourcesDisclosure"),
  };

  if (!els.searchInput) return;

  let currentCategory = "";
  let isSearching = false;
  let activeJobId = null;
  let jobPollInterval = null;

  function getActiveCategory() {
    const fromUrl = new URLSearchParams(location.search).get("category");
    if (fromUrl) return fromUrl.trim();
    const activeNav = document.querySelector("#courses button.selected, #courses [aria-current='page'], #courses button.active, #courses a.active");
    if (activeNav?.dataset?.category) return activeNav.dataset.category.trim();
    if (activeNav?.textContent) return activeNav.textContent.trim();
    const recordSelect = document.querySelector("#recordCategory");
    if (recordSelect?.value && recordSelect.value !== "__new__") return recordSelect.value.trim();
    const title = $("#courseTitle")?.textContent?.trim();
    if (title) {
      const stripped = title.replace(/\s*·\s*PDF\s*资料库\s*$/, "").trim();
      if (stripped && stripped !== "PDF 资料库" && stripped !== "所有课程") return stripped;
    }
    return "未分类";
  }

  function updateCategory(cat) {
    const nextCat = cat || getActiveCategory();
    if (!nextCat) return;
    currentCategory = nextCat;
    if (els.searchStatus && !isSearching) {
      els.searchStatus.textContent = currentCategory ? `当前范围：${currentCategory}` : "未选择课程，可开启跨课程检索";
    }
    void refreshIndexStatus();
  }

  // ==================== Knowledge Search ====================

  async function performSearch() {
    const query = els.searchInput?.value.trim();
    if (!query) {
      if (els.searchStatus) els.searchStatus.textContent = "请输入搜索关键词";
      return;
    }

    const isCross = Boolean(els.crossCourse?.checked);
    const category = isCross ? "" : (currentCategory || getActiveCategory());

    if (!isCross && !category) {
      if (els.searchStatus) els.searchStatus.textContent = "请先选择课程，或勾选“跨课程全局检索”";
      return;
    }

    isSearching = true;
    if (els.searchBtn) els.searchBtn.disabled = true;
    if (els.searchStatus) els.searchStatus.textContent = "正在检索课程知识库…";

    try {
      const params = new URLSearchParams({
        q: query,
        limit: "12",
        include_derived: "true",
      });
      const categories=isCross ? [...new Set([...document.querySelectorAll('#courses [data-category]')].map(x=>x.dataset.category).filter(Boolean))] : [category];
      const results=[];
      for (const course of categories) {
        params.set('category',course);
        const data=await api(`/api/knowledge/search?${params.toString()}`);
        results.push(...(data.results || []));
      }
      results.sort((a,b)=>(b.score || 0)-(a.score || 0));
      renderSearchResults(results, query, category);
      if (els.searchStatus) {
        els.searchStatus.textContent = `找到 ${results.length} 条相关知识段落${category ? `（${category}）` : "（全课程）"}`;
      }
    } catch (err) {
      if (els.searchStatus) els.searchStatus.textContent = `检索失败：${err.message}`;
      renderSearchResults([], query, category);
    } finally {
      isSearching = false;
      if (els.searchBtn) els.searchBtn.disabled = false;
    }
  }

  function renderSearchResults(results, query, category) {
    if (!els.resultsList) return;

    if (!results.length) {
      if (els.emptyState) {
        els.emptyState.classList.remove("hidden");
        els.emptyState.textContent = `未检索到与 “${query}” 相关的知识点${category ? `（课程：${category}）` : ""}`;
      }
      els.resultsList.replaceChildren();
      return;
    }

    if (els.emptyState) els.emptyState.classList.add("hidden");

    const fragment = document.createDocumentFragment();

    for (const item of results) {
      const card = document.createElement("article");
      card.className = "knowledge-result-card";

      const header = document.createElement("div");
      header.className = "result-card-header";

      const titleGroup = document.createElement("div");
      titleGroup.className = "result-title-group";

      const titleRow = document.createElement("div");
      titleRow.className = "result-title-row";

      if (item.citation_id) {
        const citBadge = document.createElement("span");
        citBadge.className = "result-citation-id";
        citBadge.textContent = item.citation_id;
        titleRow.append(citBadge);
      }

      const title = document.createElement("h3");
      title.className = "result-title";
      title.textContent = item.title || "资料";
      titleRow.append(title);
      titleGroup.append(titleRow);

      if (item.heading) {
        const heading = document.createElement("span");
        heading.className = "result-heading";
        heading.textContent = item.heading;
        titleGroup.append(heading);
      }
      header.append(titleGroup);

      const badges = document.createElement("div");
      badges.className = "result-badges";

      if (item.origin) {
        const orgBadge = document.createElement("span");
        orgBadge.className = `origin-badge ${item.origin}`;
        orgBadge.textContent = item.origin === "original" ? "原件" : (item.origin === "human" ? "速记" : "AI衍生");
        badges.append(orgBadge);
      }

      if (item.kind) {
        const kindBadge = document.createElement("span");
        kindBadge.className = "kind-badge";
        kindBadge.textContent = item.kind;
        badges.append(kindBadge);
      }
      header.append(badges);
      card.append(header);

      // Snippet
      if (item.text) {
        const snippet = document.createElement("p");
        snippet.className = "result-snippet";
        snippet.textContent = item.text;
        card.append(snippet);
      }

      // Footer
      const footer = document.createElement("div");
      footer.className = "result-footer";

      const locationSpan = document.createElement("span");
      locationSpan.className = "result-location";
      const locText = item.page ? `📄 第 ${item.page} 页` : (item.start !== null && item.start !== undefined ? `🎙️ 录音 ${stamp(item.start)}` : "📄 课程文件");
      locationSpan.textContent = locText;
      footer.append(locationSpan);

      const openLink = document.createElement("a");
      openLink.className = "result-open-link";
      openLink.target = "_blank";
      openLink.rel = "noopener noreferrer";

      const targetUrl = getSourceUrl(item);
      openLink.href = targetUrl;
      openLink.textContent = "打开来源原文 ↗";
      footer.append(openLink);

      if (item.stale) {
        const staleWarn = document.createElement("span");
        staleWarn.className = "result-stale-warning";
        staleWarn.textContent = "⚠️ 文件版本已更新";
        footer.append(staleWarn);
      }

      card.append(footer);
      fragment.append(card);
    }

    els.resultsList.replaceChildren(fragment);
  }

  // ==================== Index Status & Management ====================

  async function refreshIndexStatus() {
    const category = currentCategory || getActiveCategory();
    if (!category) {
      if (els.statusBadge) {
        els.statusBadge.textContent = "未选课程";
        els.statusBadge.className = "index-status-badge";
      }
      if (els.docCount) els.docCount.textContent = "资料：—";
      if (els.chunkCount) els.chunkCount.textContent = "索引段落：—";
      if (els.rebuildBtn) els.rebuildBtn.disabled = true;
      return;
    }

    if (els.statusBadge) {
      els.statusBadge.textContent = "读取中…";
      els.statusBadge.className = "index-status-badge";
    }
    if (els.docCount) els.docCount.textContent = "资料：读取中…";
    if (els.chunkCount) els.chunkCount.textContent = "索引段落：读取中…";
    if (els.rebuildBtn) els.rebuildBtn.disabled = false;

    try {
      const status = await api(`/api/knowledge/status?category=${encodeURIComponent(category)}`);
      renderIndexStatus(status);
    } catch {
      if (els.statusBadge) {
        els.statusBadge.textContent = "尚未索引";
        els.statusBadge.className = "index-status-badge";
      }
      if (els.docCount) els.docCount.textContent = "资料：—";
      if (els.chunkCount) els.chunkCount.textContent = "索引段落：—";
    }

    void checkJobs();
  }

  function renderIndexStatus(status) {
    if (!status) return;

    const sourceCount = status.active_sources_count ?? status.source_count ?? status.documents;
    const chunkCount = status.active_chunks_count ?? status.chunk_count;
    if (els.indexPolicy) {
      const policy = status.index_policy_label || '';
      const waiting = Number(status.waiting_conversion_count) || 0;
      els.indexPolicy.textContent = [policy, waiting ? `${waiting} 份资料待完成 MinerU 转换，完成后自动增量收录` : ''].filter(Boolean).join(' · ');
      els.indexPolicy.hidden = !els.indexPolicy.textContent;
    }

    if (els.statusBadge) {
      const isReady = status.ready ?? ((chunkCount || 0) > 0);
      els.statusBadge.textContent = isReady ? (status.lexical_only ? "已建词法索引" : "已建索引") : "待更新";
      els.statusBadge.className = `index-status-badge ${isReady ? "ready" : "indexing"}`;
    }

    if (els.docCount) {
      els.docCount.textContent = sourceCount !== undefined ? `资料：${sourceCount} 份` : "资料：—";
    }

    if (els.chunkCount) {
      els.chunkCount.textContent = chunkCount !== undefined ? `索引段落：${chunkCount} 条` : "索引段落：—";
    }

    // Per-source status list
    if (els.sourcesList) {
      if (Array.isArray(status.sources) && status.sources.length > 0) {
        const fragment = document.createDocumentFragment();
        for (const src of status.sources) {
          const item = document.createElement("div");
          item.className = "source-status-item";

          const title = document.createElement("span");
          title.className = "source-item-title";
          title.textContent = src.title || src.name || "文件";
          item.append(title);

          const meta = document.createElement("div");
          meta.className = "source-item-meta";

          if (src.origin) {
            const org = document.createElement("span");
            org.className = `origin-badge ${src.origin}`;
            org.textContent = src.origin === "original" ? "原件" : (src.origin === "human" ? "速记" : "AI衍生");
            meta.append(org);
          }

          const chunks = document.createElement("span");
          chunks.textContent = src.status || (src.chunk_count == null ? "已收录" : `${src.chunk_count} 段`);
          meta.append(chunks);

          if (src.error) {
            const rev = document.createElement("span");
            rev.className = "source-rev-badge";
            rev.textContent = src.error;
            meta.append(rev);
          }

          item.append(meta);
          fragment.append(item);
        }
        els.sourcesList.replaceChildren(fragment);
      } else {
        const emptyMsg = document.createElement("p");
        emptyMsg.className = "sources-empty-note";
        emptyMsg.style.cssText = "font-size:12px;color:var(--muted);padding:8px 0;margin:0;";
        if (sourceCount && sourceCount > 0) {
          emptyMsg.textContent = `当前课程已建立 ${sourceCount} 份资料与 ${chunkCount ?? 0} 条索引段落`;
        } else {
          emptyMsg.textContent = "当前课程尚未建立资料索引，请点击“重建当前课程索引”开始";
        }
        els.sourcesList.replaceChildren(emptyMsg);
      }
    }
  }

  async function triggerRebuild() {
    const category = currentCategory || getActiveCategory();
    if (!category || (els.rebuildBtn && els.rebuildBtn.disabled)) return;

    if (els.rebuildBtn) els.rebuildBtn.disabled = true;
    try {
      const res = await api("/api/knowledge/index", jsonPost({ category }));
      activeJobId = res.job_id;
      void checkJobs();
    } catch (err) {
      alert(`启动索引重建失败: ${err.message}`);
    } finally {
      if (els.rebuildBtn) els.rebuildBtn.disabled = false;
    }
  }

  async function cancelJob() {
    if (!activeJobId) return;
    try {
      await api(`/api/knowledge/jobs/${encodeURIComponent(activeJobId)}/cancel`, jsonPost({}));
      activeJobId = null;
      void checkJobs();
    } catch (err) {
      alert(`取消任务失败: ${err.message}`);
    }
  }

  async function checkJobs() {
    try {
      const data = await api("/api/knowledge/jobs");
      const jobs = data.jobs || [];
      const runningJob = jobs.find(j => ['running', 'queued', 'pending'].includes(j.status) && (!j.category || j.category === currentCategory));

      if (runningJob) {
        activeJobId = runningJob.job_id || runningJob.id;
        if (els.jobsBox) {
          els.jobsBox.classList.remove("hidden");
          if (els.jobsText) {
            els.jobsText.textContent = `${runningJob.status === 'queued' ? '索引已排队' : '正在建立索引'}：${runningJob.category || "课程"} · 进度 ${formatProgress(runningJob.progress)}`;
          }
        }
        if (!jobPollInterval) {
          jobPollInterval = setInterval(checkJobs, 2500);
        }
      } else {
        activeJobId = null;
        if (els.jobsBox) els.jobsBox.classList.add("hidden");
        if (jobPollInterval) {
          clearInterval(jobPollInterval);
          jobPollInterval = null;
          void refreshIndexStatus();
        }
      }
    } catch {
      // Quiet
    }
  }

  // ==================== Event Listeners ====================

  if (els.searchBtn) {
    els.searchBtn.addEventListener("click", () => void performSearch());
  }

  if (els.searchInput) {
    els.searchInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) {
        e.preventDefault();
        void performSearch();
      }
    });
  }

  if (els.crossCourse) {
    els.crossCourse.addEventListener("change", () => {
      if (els.searchStatus) {
        els.searchStatus.textContent = els.crossCourse.checked ? "范围：全部课程知识库" : `当前范围：${currentCategory || getActiveCategory()}`;
      }
    });
  }

  if (els.rebuildBtn) {
    els.rebuildBtn.addEventListener("click", () => void triggerRebuild());
  }

  $('#knowledgeRerankBtn')?.addEventListener('click',async event=>{
    const query=els.searchInput.value.trim();
    if (!query || els.crossCourse.checked) {
      els.searchStatus.textContent='精排针对当前课程的候选结果，请先输入查询并关闭跨课程选项。';
      return;
    }
    event.currentTarget.disabled=true;
    els.searchStatus.textContent='正在精排少量候选，录音和手记可继续使用…';
    try {
      const result=await api('/api/knowledge/rerank',jsonPost({category:currentCategory || getActiveCategory(),q:query}));
      renderSearchResults(result.results || [],query,currentCategory);
      els.searchStatus.textContent=result.warning || '已完成精排，来源仍需对照原文核对。';
    }catch(error){els.searchStatus.textContent=error.message;}
    finally{event.currentTarget.disabled=false;}
  });

  if (els.cancelJobBtn) {
    els.cancelJobBtn.addEventListener("click", () => void cancelJob());
  }

  // Monitor category change from URL, clicks, or dropdown
  const handlePotentialCategoryChange = () => {
    const cat = getActiveCategory();
    if (cat && cat !== currentCategory) {
      updateCategory(cat);
    }
  };

  const observer = new MutationObserver(handlePotentialCategoryChange);

  const coursesNav = $("#courses");
  if (coursesNav) {
    observer.observe(coursesNav, { attributes: true, subtree: true, childList: true });
    coursesNav.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-category]");
      if (btn?.dataset?.category) {
        updateCategory(btn.dataset.category);
      } else {
        setTimeout(handlePotentialCategoryChange, 50);
      }
    });
  }

  const courseTitle = $("#courseTitle");
  if (courseTitle) {
    observer.observe(courseTitle, { childList: true, characterData: true, subtree: true });
  }

  const recordCategorySelect = $("#recordCategory");
  if (recordCategorySelect) {
    recordCategorySelect.addEventListener("change", (e) => {
      if (e.target.value && e.target.value !== "__new__") {
        updateCategory(e.target.value);
      }
    });
  }

  window.addEventListener("popstate", handlePotentialCategoryChange);

  // Initial load
  updateCategory(getActiveCategory());
  void checkJobs();

  return {
    performSearch,
    refreshIndexStatus,
    updateCategory,
  };
}

// Auto-initialize when loaded on course-files page
if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      if (document.querySelector(".course-files-page") || document.querySelector("#knowledgeSearchInput")) {
        initCourseKnowledge();
      }
    });
  } else {
    if (document.querySelector(".course-files-page") || document.querySelector("#knowledgeSearchInput")) {
      initCourseKnowledge();
    }
  }
}
