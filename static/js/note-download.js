// Keep failed/temporarily busy PDF downloads inside the reader. A stored review
// flag is not a guarantee that the current artifact can be served right now.
const pending = new Map();

function downloadName(header, fallback) {
  const encoded = String(header || '').match(/filename\*=UTF-8''([^;]+)/i);
  if (encoded) {
    try { return decodeURIComponent(encoded[1]).split(/[\\/]/).pop(); } catch { /* use the plain name */ }
  }
  const plain = String(header || '').match(/filename="([^"]+)"|filename=([^;]+)/i);
  return (plain?.[1] || plain?.[2] || fallback).trim().split(/[\\/]/).pop();
}

export function downloadNotePdf(url, {title = '课后复习笔记', onStatus = () => {}} = {}) {
  if (pending.has(url)) return pending.get(url);
  const request = (async () => {
    onStatus('正在核验并下载当前 PDF…');
    const response = await fetch(url, {cache:'no-store'});
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const detail = typeof data.detail === 'string' ? data.detail : `PDF 下载失败（${response.status}）`;
      throw new Error(response.status === 409 && !/后台|任务中心/.test(detail)
        ? `${detail}；笔记已保留，请稍后重试下载` : detail);
    }
    if (!response.headers.get('content-type')?.toLowerCase().includes('application/pdf')) {
      throw new Error('下载响应不是 PDF，笔记已保留，请稍后重试');
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = downloadName(response.headers.get('content-disposition'), `${title}.pdf`);
    link.hidden = true;
    document.body.append(link);
    link.click(); link.remove();
    // Keep the blob alive long enough for browser download handoff.
    setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
    onStatus('当前 PDF 已开始下载');
  })().finally(() => pending.delete(url));
  pending.set(url, request);
  return request;
}
