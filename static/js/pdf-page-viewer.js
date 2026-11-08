// Continuous-scroll courseware viewer for "↗ PPT" links. Two sources:
//  * a PDF: PDF.js is imported on demand and reads it with HTTP Range, so
//    opening page 70 of a 60 MB deck does not download the whole file first;
//  * pre-rendered page images (``images``: {count, sizes: [[w, h], ...], src(n)}):
//    each page is one small image file, no PDF.js at all.
// All pages are laid out as fixed-size placeholders; only pages near the
// viewport get a canvas (IntersectionObserver), far ones are released again.
// Loading is page-at-a-time: streaming is off (it would pull the whole file),
// the target page renders first and neighbours wait in a one-at-a-time queue.
const MAX_PIXELS = 16e6;   // per-canvas backing-store cap
const MAX_WIDTH = 960;     // CSS px of a page at "fit width" on large screens
const GAP = 14;            // CSS px between pages
const KEEP = '250% 0px';   // render/keep pages within ~2.5 screens of the viewport
const ZOOMS = [0.5, 0.67, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2];

export async function startViewer({ root, url, images = null, page = 1, end = 0, title = "", systemUrl = url, fallback = "", rangeText = "" }) {
  const $ = (name) => root.querySelector(`[data-pv="${name}"]`);
  const status = (text) => { $('status').textContent = text; $('status').hidden = !text; };
  document.title = (title ? title + ' · ' : '') + `第 ${page} 页`;
  $('title').textContent = title || '课件';
  if (rangeText || (end && end > page)) { $("range").textContent = rangeText || `本节对应 第 ${page}–${end} 页`; $("range").hidden = false; }
  let current = page, doc = null;
  const system = () => { $('system').href = `${systemUrl}#page=${current}`; };
  system();
  if (!url && !images) { status(fallback || '这份课件暂时无法在页内打开。'); return; }
  status('正在加载课件…');
  let pdfjs;
  if (!images) try {
    pdfjs = await import(new URL('../vendor/pdfjs/pdf.min.mjs', import.meta.url).href);
    pdfjs.GlobalWorkerOptions.workerSrc = new URL('../vendor/pdfjs/pdf.worker.min.mjs', import.meta.url).href;
    doc = await pdfjs.getDocument({ url, isEvalSupported: false, disableAutoFetch: true, disableStream: true, rangeChunkSize: 65536, enableXfa: false }).promise;
  } catch (error) {
    status((fallback ? fallback + ' ' : '') + `课件读取未完成（${error?.message || error}）。可用“系统查看器打开”。`);
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
  const bar = () => root.querySelector('.pv-bar')?.offsetHeight || 0;
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
  for (let n = 1; n <= total; n++) {
    const el = document.createElement('div');
    el.className = 'pv-page' + (n >= target && n <= (rangeEnd || target) && (rangeEnd || rangeText) ? ' pv-in-range' : '');
    el.dataset.page = String(n);
    el.setAttribute('aria-label', `第 ${n} 页`);
    const tag = document.createElement('span'); tag.className = 'pv-page-no'; tag.textContent = String(n);
    el.append(tag);
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
  function release(n) {
    const s = state.get(n); if (!s) return;
    s.task?.cancel?.(); if (s.canvas instanceof HTMLCanvasElement) s.canvas.width = s.canvas.height = 0; else s.canvas.removeAttribute('src'); s.canvas.remove(); state.delete(n);
  }
  // an image scales with its box; a canvas is redrawn when the width changes
  const fresh = (n) => state.has(n) && (images || state.get(n).cssWidth === size(n).w);
  async function render(n) {
    const want = size(n).w;
    if (fresh(n)) return;
    if (images) return renderImage(n);
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
  async function renderImage(n) {
    const img = new Image();
    img.alt = ''; img.decoding = 'async'; img.draggable = false;
    const entry = { canvas: img, task: null, cssWidth: 0 };
    state.set(n, entry);
    img.src = images.src(n);
    try {
      await img.decode();
      if (state.get(n) === entry) { els[n].append(img); els[n].classList.add('pv-rendered'); }
    } catch (error) {
      if (state.get(n) === entry) els[n].dataset.error = `第 ${n} 页图片加载未完成`;
    }
  }
  // one page at a time, the page nearest the current one first, so a
  // neighbour never competes with the page being read for bandwidth
  const near = new Set();
  let busy = false;
  async function pump() {
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
      else { near.delete(n); release(n); els[n].classList.remove('pv-rendered'); }
    }
    pump();
  }, { rootMargin: KEEP });

  // ---- current page tracking
  let urlTimer = 0;
  function setCurrent(n) {
    if (n === current && $('input').value) return;
    current = n;
    if (document.activeElement !== $('input')) $('input').value = String(n);
    $('prev').disabled = n <= 1; $('next').disabled = n >= total;
    document.title = (title ? title + ' · ' : '') + `第 ${n} 页`;
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
    const instant = behavior !== 'smooth' || Math.abs(top - window.scrollY) < 2;
    scrollTarget = instant ? 0 : n;
    window.scrollTo({ top, behavior: instant ? 'auto' : 'smooth' });
    setCurrent(n);
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
  $('zoom-out').onclick = () => setZoom(ZOOMS.slice().reverse().find((z) => z < zoom - 1e-6) ?? zoom);
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
  await render(target);                       // the target page alone first
  for (let n = 1; n <= total; n++) observer.observe(els[n]);
  const api = { show, get page() { return current; }, get pages() { return total; }, get target() { return target; },
    get zoom() { return zoom; }, get rendered() { return [...state.keys()].sort((a, b) => a - b); },
    async text(number = current) {
      if (images) return images.text ? images.text(number) : '';
      const c = await (await doc.getPage(number)).getTextContent(); return c.items.map((i) => i.str).join(" ");
    } };
  root.pdfViewer = Object.freeze(api);
  return api;
}
