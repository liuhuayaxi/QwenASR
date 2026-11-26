// Continuous-scroll courseware viewer for "↗ PPT" links. Two sources:
//  * a PDF: PDF.js is imported on demand and reads it with HTTP Range, so
//    opening page 70 of a 60 MB deck does not download the whole file first;
//  * pre-rendered page images (``images``: {count, sizes: [[w, h], ...], src(n)}):
//    each page is one small image file (+ optional tiny preview), no PDF.js.
// All pages are laid out as fixed-size placeholders; only pages near the
// viewport get a canvas (IntersectionObserver), far ones are released again.
// Loading is page-at-a-time for a PDF (streaming is off: it would pull the
// whole file). Page images are different: the target page is requested from
// the URL before this module loads, a viewport page starts immediately, and
// only a short run of neighbours is fetched ahead so a later jump is not stuck
// behind a deck-wide download.
const MAX_PIXELS = 16e6;   // per-canvas backing-store cap
const MAX_WIDTH = 960;     // CSS px of a page at "fit width" on large screens
const GAP = 14;            // CSS px between pages
const KEEP = '250% 0px';   // PDF: render/keep pages within ~2.5 screens of the viewport
const KEEP_IMAGES = '120% 0px';   // images: one small preview just beyond the viewport
const ZOOMS = [0.5, 0.67, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2];

export async function startViewer({ root, url, images = null, page = 1, end = 0, title = "", systemUrl = url, fallback = "", rangeText = "", early = null }) {
  const $ = (name) => root.querySelector(`[data-pv="${name}"]`);
  const titleText = () => typeof title === 'function' ? (title() || '') : (title || '');
  const systemHref = () => typeof systemUrl === 'function' ? (systemUrl() || '') : (systemUrl || '');
  const status = (text, loading = false) => {
    const message = $('status');
    message.textContent = text;
    message.hidden = !text;
    message.toggleAttribute('data-loading', loading);
  };
  document.title = (titleText() ? titleText() + ' · ' : '') + `第 ${page} 页`;
  $('title').textContent = titleText() || '课件';
  if (rangeText || (end && end > page)) { $("range").textContent = rangeText || `本节对应 第 ${page}–${end} 页`; $("range").hidden = false; }
  let current = page, doc = null;
  const system = () => {
    const href = systemHref();
    $('system').hidden = !href;
    if (href) $('system').href = `${href}#page=${current}`;
  };
  system();
  if (!url && !images) { status(fallback || '这份课件暂时无法在页内打开。'); return; }
  if (!(early?.box?.classList.contains('pv-rendered') || early?.box?.classList.contains('pv-preview'))) status('正在加载课件…', true);
  let pdfjs;
  if (!images) try {
    pdfjs = await import(new URL('../vendor/pdfjs/pdf.min.mjs', import.meta.url).href);
    pdfjs.GlobalWorkerOptions.workerSrc = new URL('../vendor/pdfjs/pdf.worker.min.mjs', import.meta.url).href;
    doc = await pdfjs.getDocument({ url, isEvalSupported: false, disableAutoFetch: true, disableStream: true, rangeChunkSize: 65536, enableXfa: false }).promise;
  } catch (error) {
    status((fallback ? fallback + ' ' : '') + `课件读取未完成（${error?.message || error}）。可用“下载 PDF”。`);
    return;
  }
  const total = images ? images.count : doc.numPages;
  const target = Math.max(1, Math.min(total, Math.round(page)));
  const rangeEnd = end > target ? Math.min(end, total) : 0;
  $('total').textContent = String(total);
  $('input').max = String(total);
  const container = $('pages');
  container.style.setProperty('--pv-gap', `${GAP}px`);

  // ---- layout: one placeholder per page, sized from page 1 until rendered
  const sizes = new Array(total + 1).fill(null);   // per-page base size once known
  if (images) images.sizes.forEach(([w, h], i) => { sizes[i + 1] = { w, h }; });
  else try { const v = (await doc.getPage(target)).getViewport({ scale: 1 }); sizes[target] = { w: v.width, h: v.height }; }
  catch (error) { status(`第 ${target} 页读取未完成（${error?.message || error}）。`); return; }
  const base = { width: sizes[target].w, height: sizes[target].h };
  let zoom = 1;
  const toolbar = root.querySelector('.pv-bar');
  const bar = () => toolbar ? toolbar.offsetHeight + (parseFloat(getComputedStyle(toolbar).top) || 0) : 0;
  function fitWidth() {
    const avail = Math.max(240, document.documentElement.clientWidth - 32);
    const height = Math.max(240, window.innerHeight - bar() - 2 * GAP);
    // landscape slides: cap width so the whole page fits one screen;
    // portrait pages: don't shrink below ~60% of the available width
    const byHeight = height * base.width / base.height;
    return Math.min(MAX_WIDTH, avail, Math.max(byHeight, Math.min(avail, MAX_WIDTH) * .6));
  }
  const cssWidth = () => Math.round(fitWidth() * zoom);
  const els = [null];
  const fragment = document.createDocumentFragment();
  const earlyBox = early?.page === target ? early.box : null;
  if (early && !earlyBox) { early.cancel?.(); early.box?.remove(); }
  for (let n = 1; n <= total; n++) {
    const el = n === target && earlyBox ? earlyBox : document.createElement('div');
    el.classList.remove('pv-early');
    el.classList.add('pv-page');
    el.classList.toggle('pv-in-range', n >= target && n <= (rangeEnd || target) && !!(rangeEnd || rangeText));
    el.dataset.page = String(n);
    el.setAttribute('aria-label', `第 ${n} 页`);
    if (!el.querySelector('.pv-page-no')) {
      const tag = document.createElement('span'); tag.className = 'pv-page-no'; tag.textContent = String(n);
      el.append(tag);
    }
    els.push(el); fragment.append(el);
  }
  container.replaceChildren(fragment);
  function size(n) {
    const s = sizes[n] || { w: base.width, h: base.height };
    const w = cssWidth();
    return { w, h: Math.round(w * s.h / s.w) };
  }
  function layout() {
    for (let n = 1; n <= total; n++) { const { w, h } = size(n); els[n].style.width = `${w}px`; els[n].style.height = `${h}px`; }
    $('zoom').textContent = `${Math.round(zoom * 100)}%`;
  }
  layout();

  // ---- rendering
  const state = new Map();   // n -> { canvas, task, scale }
  const jobs = new Map(), thumbsStarted = new Map();
  let earlyAvailable = Boolean(earlyBox);
  function releaseThumb(n) {
    const entry = thumbsStarted.get(n);
    thumbsStarted.delete(n); entry?.controller.abort(); entry?.img.removeAttribute('src'); entry?.img.remove();
    els[n].querySelector('.pv-thumb')?.remove();
    els[n].classList.remove('pv-preview');
  }
  function release(n) {
    const entry = state.get(n);
    state.delete(n); jobs.delete(n);
    if (entry) {
      entry.controller?.abort(); entry.task?.cancel?.();
      if (entry.early) early.cancel?.();
      if (entry.canvas instanceof HTMLCanvasElement) entry.canvas.width = entry.canvas.height = 0;
      else entry.canvas.removeAttribute('src');
      entry.canvas.remove();
    }
    els[n].classList.remove('pv-rendered');
    delete els[n].dataset.error;
  }
  function trimImages() {
    if (!images) return;
    // Bound decoded full images even after many distant jumps. The image URL
    // stays in HTTP cache; clearing a job lets a released page load on return.
    for (const n of state.keys()) if (Math.abs(n - current) > 2) release(n);
    for (const n of thumbsStarted.keys()) if (Math.abs(n - current) > 2 && !near.has(n)) releaseThumb(n);
  }
  function decoded(img, signal) {
    return new Promise(resolve => {
      let finished = false;
      const finish = ok => {
        if (finished) return; finished = true;
        img.removeEventListener('load', loaded); img.removeEventListener('error', failed);
        signal.removeEventListener('abort', failed); resolve(ok);
      };
      const failed = () => finish(false);
      const loaded = () => img.decode().then(() => finish(Boolean(img.naturalWidth)), failed);
      img.addEventListener('load', loaded); img.addEventListener('error', failed);
      signal.addEventListener('abort', failed, {once:true});
      if (signal.aborted) failed();
      else if (img.getAttribute('src') && img.complete) img.naturalWidth ? loaded() : failed();
    });
  }
  // an image scales with its box; a canvas is redrawn when the width changes
  const fresh = (n) => state.has(n) && (images || state.get(n).cssWidth === size(n).w);
  async function render(n) {
    const want = size(n).w;
    if (images) return renderImage(n);
    if (fresh(n)) return;
    release(n);
    const canvas = document.createElement('canvas');
    canvas.setAttribute('aria-hidden', 'true');
    const entry = { canvas, task: null, cssWidth: want };
    state.set(n, entry);
    try {
      const pdfPage = await doc.getPage(n);
      if (state.get(n) !== entry) return;
      const vp1 = pdfPage.getViewport({ scale: 1 });
      if (!sizes[n] || sizes[n].w !== vp1.width || sizes[n].h !== vp1.height) {
        const before = els[n].getBoundingClientRect();
        sizes[n] = { w: vp1.width, h: vp1.height };
        const { w, h } = size(n); els[n].style.width = `${w}px`; els[n].style.height = `${h}px`;
        if (before.bottom <= bar()) window.scrollBy(0, h - before.height);   // keep the view anchored (browser scroll anchoring is off: .pv-pages)
      }
      const ratio = window.devicePixelRatio || 1;
      let scale = want / vp1.width * ratio;
      if (vp1.width * vp1.height * scale * scale > MAX_PIXELS) scale = Math.sqrt(MAX_PIXELS / (vp1.width * vp1.height));
      const viewport = pdfPage.getViewport({ scale });
      canvas.width = Math.floor(viewport.width); canvas.height = Math.floor(viewport.height);
      entry.task = pdfPage.render({ canvasContext: canvas.getContext('2d'), viewport });
      await entry.task.promise;
      if (state.get(n) === entry) { els[n].append(canvas); els[n].classList.add('pv-rendered'); }
    } catch (error) {
      if (error?.name !== 'RenderingCancelledException' && state.get(n) === entry) {
        els[n].dataset.error = `第 ${n} 页渲染未完成（${error?.message || error}）`;
      }
    }
  }
  function renderImage(n, priority = 'high') {
    if (jobs.has(n)) return jobs.get(n);
    if (state.get(n)?.ready) return Promise.resolve();
    if (Math.abs(n - current) > 1) return Promise.resolve();
    const adopted = earlyAvailable && n === target && early.full && !early.failed ? early.full : null;
    if (n === target) earlyAvailable = false;
    const img = adopted || new Image();
    img.alt = ''; img.decoding = 'async'; img.draggable = false; img.fetchPriority = priority;
    if (adopted) img.classList.remove('pv-full');
    const entry = {canvas: img, controller: new AbortController(), early: Boolean(adopted), ready: false};
    state.set(n, entry);
    delete els[n].dataset.error;
    if (!adopted) img.src = images.src(n);
    const job = decoded(img, entry.controller.signal).then(ok => {
      if (state.get(n) !== entry) return;
      if (!ok) {
        release(n); els[n].dataset.error = `第 ${n} 页图片加载未完成`;
        return;
      }
      entry.ready = true;
      if (!img.isConnected) els[n].append(img);
      els[n].classList.add('pv-rendered');
      releaseThumb(n);
    }).finally(() => { if (jobs.get(n) === job) jobs.delete(n); });
    jobs.set(n, job);
    return job;
  }
  // PDF pages render one at a time, nearest the current page first. A page
  // image shows its small preview as soon as it is near, and the full image
  // only when it is about to enter the viewport, so a jump is not queued
  // behind a download of the whole deck.
  const near = new Set();
  let busy = false;
  function requestThumb(n, priority = 'low') {
    if (!images?.thumb || state.get(n)?.ready || jobs.has(n)) return Promise.resolve();
    if (thumbsStarted.has(n)) return thumbsStarted.get(n).promise;
    if (n === target && early?.thumb?.isConnected) return Promise.resolve();
    const img = new Image();
    img.alt = ''; img.decoding = 'async'; img.draggable = false; img.className = 'pv-thumb'; img.fetchPriority = priority;
    const entry = {img, controller: new AbortController()};
    thumbsStarted.set(n, entry);
    img.src = images.thumb(n);
    entry.promise = decoded(img, entry.controller.signal).then(ok => {
      if (thumbsStarted.get(n) !== entry) return;
      if (!ok) { thumbsStarted.delete(n); return; }
      if (!state.get(n)?.ready && (Math.abs(n - current) <= 2 || near.has(n))) {
        els[n].prepend(img); els[n].classList.add('pv-preview');
      }
    });
    return entry.promise;
  }
  function requestPage(n, priority = 'high') {
    if (state.get(n)?.ready || jobs.has(n)) return jobs.get(n) || Promise.resolve();
    if (earlyAvailable && n === target && early.full && !early.failed) return renderImage(n, priority);
    const preview = requestThumb(n, priority);
    // The bootstrap already starts the opened page's full image after its
    // preview. Other pages keep the same preview-first order.
    if (images?.thumb) return preview.then(() => renderImage(n, priority));
    return renderImage(n, priority);
  }
  async function pump() {
    if (images) return;
    if (busy) return; busy = true;
    try {
      for (;;) {
        const next = [...near].filter((n) => !fresh(n)).sort((a, b) => Math.abs(a - current) - Math.abs(b - current))[0];
        if (!next) break;
        await render(next);
      }
    } finally { busy = false; }
  }
  const observer = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const n = Number(e.target.dataset.page);
      if (e.isIntersecting) near.add(n);
      else { near.delete(n); if (!images) { release(n); els[n].classList.remove('pv-rendered'); } }
    }
    if (images) { trimImages(); [...near].sort((a, b) => Math.abs(a - current) - Math.abs(b - current)).slice(0, 2).forEach((n) => requestThumb(n)); }
    else pump();
  }, { rootMargin: images ? KEEP_IMAGES : KEEP });

  // ---- current page tracking
  let urlTimer = 0;
  function setCurrent(n) {
    if (n === current && $('input').value) return;
    current = n;
    if (images) { trimImages(); requestPage(n, 'high'); }
    if (document.activeElement !== $('input')) $('input').value = String(n);
    $('prev').disabled = n <= 1; $('next').disabled = n >= total;
    document.title = (titleText() ? titleText() + ' · ' : '') + `第 ${n} 页`;
    system();
    clearTimeout(urlTimer);
    urlTimer = setTimeout(() => { const u = new URL(location.href); u.searchParams.set('page', String(current)); history.replaceState(null, '', u); }, 250);
  }
  function visiblePage() {
    // the page under a line one third down the visible area
    const line = bar() + (window.innerHeight - bar()) / 3;
    let lo = 1, hi = total;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (els[mid].getBoundingClientRect().bottom + GAP / 2 < line) lo = mid + 1; else hi = mid;
    }
    return lo;
  }
  let ticking = false, scrollTarget = 0;
  window.addEventListener('scroll', () => {
    if (ticking) return; ticking = true;
    requestAnimationFrame(() => {
      ticking = false;
      // during a programmatic smooth scroll show the destination, not pages passed on the way
      setCurrent(scrollTarget || visiblePage());
    });
  }, { passive: true });
  window.addEventListener('scrollend', () => { scrollTarget = 0; setCurrent(visiblePage()); });

  function show(number, behavior = 'smooth') {
    const n = Math.max(1, Math.min(total, Math.round(Number(number) || 1)));
    const top = els[n].getBoundingClientRect().top + window.scrollY - bar() - GAP / 2;
    const instant = behavior !== 'smooth' || Math.abs(top - window.scrollY) < 2
      || matchMedia('(prefers-reduced-motion: reduce)').matches;
    scrollTarget = instant ? 0 : n;
    window.scrollTo({ top, behavior: instant ? 'instant' : 'smooth' });
    setCurrent(n);
    if (images) requestPage(n, 'high');
    if (!instant) setTimeout(() => { if (scrollTarget === n) { scrollTarget = 0; setCurrent(visiblePage()); } }, 1200);   // browsers without scrollend
  }
  function setZoom(value) {
    const keep = current;
    const offset = (bar() - els[keep].getBoundingClientRect().top) / els[keep].offsetHeight;   // fraction of the page above the bar
    zoom = Math.max(ZOOMS[0], Math.min(ZOOMS[ZOOMS.length - 1], value));
    layout();
    window.scrollTo({ top: els[keep].getBoundingClientRect().top + window.scrollY - bar() + offset * els[keep].offsetHeight });
    pump();
  }
  $('prev').onclick = () => show(current - 1);
  $('next').onclick = () => show(current + 1);
  $('form').onsubmit = (event) => { event.preventDefault(); show($('input').value); $('input').blur(); };
  $('input').addEventListener('blur', () => { $('input').value = String(current); });
  $('zoom-out').onclick = () => setZoom(ZOOMS.findLast((z) => z < zoom - 1e-6) ?? zoom);
  $('zoom-in').onclick = () => setZoom(ZOOMS.find((z) => z > zoom + 1e-6) ?? zoom);
  $('zoom-fit').onclick = () => setZoom(1);
  window.addEventListener('keydown', (event) => {
    if (event.target.matches('input') || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === 'ArrowLeft') { event.preventDefault(); show(current - 1); }
    if (event.key === 'ArrowRight') { event.preventDefault(); show(current + 1); }
  });
  let resize, lastWidth = document.documentElement.clientWidth;
  window.addEventListener('resize', () => {
    clearTimeout(resize);
    resize = setTimeout(() => { if (document.documentElement.clientWidth !== lastWidth) { lastWidth = document.documentElement.clientWidth; setZoom(zoom); } }, 200);
  });

  status('');
  show(target, 'auto');
  if (images) {
    // Observe immediately: a slow opened page must not block a later scroll.
    // Only the current visible page requests its full image.
    const urgent = new IntersectionObserver((entries) => {
      const visible = [];
      for (const e of entries) if (e.isIntersecting) visible.push(Number(e.target.dataset.page));
      visible.sort((a, b) => Math.abs(a - current) - Math.abs(b - current));
      if (visible.length) requestPage(visible[0], 'high');
    }, { rootMargin: '0px' });
    for (let n = 1; n <= total; n++) { urgent.observe(els[n]); observer.observe(els[n]); }
  } else {
    await render(target);
    for (let n = 1; n <= total; n++) observer.observe(els[n]);
    pump();
  }
  const api = { show, get page() { return current; }, get pages() { return total; }, get target() { return target; },
    get zoom() { return zoom; }, get rendered() { return [...state.keys()].sort((a, b) => a - b); },
    async text(number = current) {
      if (images) return images.text ? images.text(number) : '';
      const c = await (await doc.getPage(number)).getTextContent(); return c.items.map((i) => i.str).join(" ");
    } };
  root.pdfViewer = Object.freeze(api);
  return api;
}
