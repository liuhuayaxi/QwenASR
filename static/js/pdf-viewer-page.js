import { startViewer } from './pdf-page-viewer.js?v=20261119-paper';
// Local viewer for one linked courseware. The inline script in pdf-viewer.html
// starts the target page's preview and the manifest while this module is still
// downloading. The sharp image starts after that preview finishes, so the two
// do not share one transfer. PDF.js is imported only when that image set is
// absent; it then reads the AI-friendly PDF from this app's own route.
const params = new URLSearchParams(location.search);
const recording = params.get('rec') || '', source = params.get('src') || '';
const ok = /^[A-Za-z0-9_-]{6,80}$/.test(recording) && /^[a-f0-9]{32}$/.test(source);
const pdfUrl = ok ? `/api/section-page-links/${encodeURIComponent(recording)}/pdf/${encodeURIComponent(source)}` : '';
const page = Math.max(1, parseInt(params.get('page'), 10) || 1);
const end = parseInt(params.get('end'), 10) || 0;
const label = parseInt(params.get('label'), 10) || 0;
const labelEnd = parseInt(params.get('labelEnd'), 10) || 0;
const hinted = /^[a-f0-9]{64}$/.test(params.get('sha') || '') ? params.get('sha') : '';
const root = document.getElementById('viewer');
const early = window.__pvEarly && window.__pvEarly.sha === hinted && window.__pvEarly.rec === recording && window.__pvEarly.src === source ? window.__pvEarly : null;
const folder = ok ? `/api/section-page-links/${encodeURIComponent(recording)}/pages/${encodeURIComponent(source)}/` : '';
const manifestFor = () => fetch(`${folder}manifest.json`).then((response) => response.ok ? response.json() : null).catch(() => null);

function imagesFrom(manifest, sha) {
  if (!ok || !manifest || manifest.pdf_sha256 !== sha || !(manifest.pages > 0) || manifest.sizes?.length !== manifest.pages) return null;
  const sources = manifest.source_pages || [];
  const ext = manifest.format === 'webp' ? 'webp' : 'png';
  return {
    count: manifest.pages, sizes: manifest.sizes,
    src: (n) => `${folder}${n}.${ext}`,
    thumb: manifest.thumb_width ? (n) => `${folder}t${n}.${ext}` : null,
    text: (n) => sources[n - 1] ? `[[AI_TRANSCRIPT_PAGE:${sources[n - 1]}]]` : '',
  };
}

if (label && label !== page) {
  const note = root.querySelector('[data-pv="note"]');
  note.textContent = `课件原第 ${label} 页（AI 友好版含说明页，本文件第 ${page} 页）`;
  note.hidden = false;
}
const system = root.querySelector('[data-pv="system"]');
system.textContent = '下载 PDF ↗';
if (!pdfUrl) system.hidden = true;
let images = null;
if (hinted && ok) images = imagesFrom(await (early?.manifest || manifestFor()), hinted);
if (!images) early?.box?.remove();
else if (early?.failed) { early.full?.remove(); early.thumb?.remove(); }
const shown = early?.box?.classList.contains('pv-rendered') || early?.box?.classList.contains('pv-preview');
const viewer = startViewer({
  root, images, page, end, early: images ? early : null,
  url: images ? undefined : pdfUrl,
  title: params.get('title') || '课件',
  systemUrl: pdfUrl,
  rangeText: label && labelEnd > label ? `本节对应课件第 ${label}–${labelEnd} 页` : '',
  fallback: ok ? '' : '链接参数无效。',
});
if (images && shown) root.querySelector('[data-pv="status"]').hidden = true;
await viewer;
