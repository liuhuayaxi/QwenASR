import { startViewer } from './pdf-page-viewer.js?v=6';
const params = new URLSearchParams(location.search);
const recording = params.get('rec') || '', source = params.get('src') || '';
const ok = /^[A-Za-z0-9_-]{6,80}$/.test(recording) && /^[a-f0-9]{32}$/.test(source);
const url = ok ? `/api/section-page-links/${encodeURIComponent(recording)}/pdf/${encodeURIComponent(source)}` : '';
const page = Math.max(1, parseInt(params.get('page'), 10) || 1);
const end = parseInt(params.get('end'), 10) || 0;
const label = parseInt(params.get('label'), 10) || 0;
const root = document.getElementById('viewer');
if (label && label !== page) {
  const note = root.querySelector('[data-pv="note"]');
  note.textContent = `课件原第 ${label} 页（AI 友好版含说明页，本文件第 ${page} 页）`; note.hidden = false;
}
const labelEnd = parseInt(params.get("labelEnd"), 10) || 0;
const rangeText = label && labelEnd > label ? `本节对应课件第 ${label}–${labelEnd} 页` : "";
startViewer({ root, url, page, end, title: params.get("title") || "课件", fallback: ok ? "" : "链接参数无效。", rangeText });
