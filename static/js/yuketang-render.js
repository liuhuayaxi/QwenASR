// Pure rendering helpers for 雨课堂 questions, shared by the course panel and
// the course 检索 results. No side effects on import.
export const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
export const SOURCES = {exam: '课前/课后测试与考试', lesson: '课上习题', courseware: '课件习题'};

// Math delimiters used by Yuketang text and by vision transcripts. Text
// outside formulas stays escaped; formulas go through the vendored KaTeX
// (trust off), or stay escaped source when KaTeX is unavailable.
const MATH = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$((?:\\.|[^$\n\\]|\\)+?)\$/g;
export function mathText(value, katex = globalThis.katex) {
  const text = String(value ?? '');
  let html = '', last = 0;
  for (const match of text.matchAll(MATH)) {
    const [raw, block, bracket, paren, inline] = match;
    const tex = (block ?? bracket ?? paren ?? inline).trim();
    html += esc(text.slice(last, match.index));
    last = match.index + raw.length;
    if (!tex || !katex?.renderToString) { html += esc(raw); continue; }
    try {
      html += katex.renderToString(tex, {displayMode: block !== undefined || bracket !== undefined, throwOnError: false, trust: false, strict: 'ignore', maxExpand: 500});
    } catch { html += esc(raw); }
  }
  return html + esc(text.slice(last));
}

export function countsText(counts = {}) {
  if (!counts.total) return '还没有题目';
  const parts = Object.entries(SOURCES).filter(([key]) => counts[key]).map(([key, label]) => `${label} ${counts[key]}`);
  const extra = [];
  if (counts.wrong) extra.push(`答错 ${counts.wrong}`);
  if (counts.needs_vision) extra.push(`图片转写 ${counts.vision_done || 0}/${counts.needs_vision}`);
  return `共 ${counts.total} 题：${parts.join('，')}${extra.length ? `（${extra.join('，')}）` : ''}`;
}

export function imageUrl(q, image) {
  return `/api/yuketang/image/${encodeURIComponent(q.classroom_id)}/${image.path.split('/').map(encodeURIComponent).join('/')}`;
}

export function questionHtml(q, url = image => imageUrl(q, image), {label = ''} = {}) {
  const mark = q.correct === true ? '<span class="ykt-ok">答对</span>' : q.correct === false ? '<span class="ykt-wrong">答错</span>' : q.graded === false && (q.student_answer || []).length ? '<span class="muted">未批改</span>' : '';
  const body = q.body || q.vision?.text || (q.images?.length ? '（图片题，见下方图片）' : '');
  const options = (q.options || []).map(o => `<li><b>${esc(o.key)}</b> ${mathText(o.value)}</li>`).join('');
  const answer = (q.answer || []).join('、') || (q.vision?.version === 'teacher-evidence-v2' ? q.vision.answer_text : '') || '';
  const images = (q.images || []).filter(image => image.path && image.kind !== 'student').map(image => `<img loading="lazy" alt="${esc(image.kind === 'remark' ? '参考答案图片' : '题目图片')}" data-yuketang-src="${esc(url(image))}">`).join('');
  return `<article class="ykt-question">
    <header>${label ? `<span class="ykt-hit-label">${esc(label)}</span> ` : ''}<span class="count-badge">${esc(q.source_label)}</span> <b>${esc(q.activity_title)}</b> <span class="muted">${esc(q.date || '')}${q.page ? ` · 第 ${esc(q.page)} 页` : ''} · ${esc(q.type_text)}</span> ${mark}</header>
    <p class="ykt-body">${mathText(body)}</p>${options ? `<ul class="ykt-options">${options}</ul>` : ''}
    <p><b>正确答案：</b>${answer ? mathText(answer) : '未公布'}${q.remark ? `<br><b>解析：</b>${mathText(q.remark)}` : ''}</p>
    ${(q.student_answer || []).length ? `<p class="ykt-mine"><b>我的答案：</b>${mathText(q.student_answer.join('、'))}${q.score != null ? `（${esc(q.score)}/${esc(q.max_score ?? '?')} 分）` : ''}</p>` : ''}
    ${images ? `<div class="ykt-images">${images}</div>` : ''}</article>`;
}

export function courseLabel(course) {
  return [course.course_name, course.classroom_name, course.teacher].filter(Boolean).join(' · ');
}

// Header line: bound course · counts · last sync.
export function headerLine(status = {}) {
  const rooms = status.classrooms || [];
  if (!rooms.length) return '未绑定雨课堂课程';
  const first = rooms[0];
  const name = first.course_name || first.name || `班级 ${first.classroom_id}`;
  const counts = status.counts || {};
  const parts = [rooms.length > 1 ? `${name} 等 ${rooms.length} 个班级` : name, counts.total ? `${counts.total} 题${counts.wrong ? `（答错 ${counts.wrong}）` : ''}` : '尚无题目'];
  parts.push(syncText(rooms));
  return parts.join(' · ');
}

export function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || '');
  const pad = n => String(n).padStart(2, '0');
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function currentCategory(doc = document) {
  return doc.querySelector('#courses button.selected')?.dataset.category || new URLSearchParams(globalThis.location?.search || '').get('category') || '';
}

// What the binding area shows: nothing (bound), the dropdown, or a login action.
export function bindView(status = {}, courses = {}, rebinding = false) {
  const bound = (status.classrooms || []).length > 0;
  if (bound && !rebinding) return {mode: 'hidden'};
  const list = courses.courses || [];
  if (!list.length) return {mode: 'login'};
  const current = bound ? String(status.classrooms[0].classroom_id) : '';
  return {mode: 'select', selected: current || courses.suggested || '', options: list.map(course => ({value: String(course.classroom_id), label: courseLabel(course), university: String(course.university_id ?? '')}))};
}

// Truthful sync time: the end of the last successful sync (even when nothing
// changed); when the latest attempt failed, say so and keep the last success.
const FAILED = {partial: '上次同步部分完成', failed: '上次同步失败', login_required: '上次同步失败（需要登录）', stopped: '上次同步已停止'};
export function syncText(rooms = []) {
  const success = rooms.map(room => room.last_sync).filter(Boolean).sort().pop() || '';
  const attempts = rooms.filter(room => room.last_attempt_at).sort((a, b) => String(a.last_attempt_at).localeCompare(String(b.last_attempt_at)));
  const latest = attempts[attempts.length - 1];
  const failed = rooms.filter(room => FAILED[room.last_attempt_status] && String(room.last_attempt_at || '') > String(room.last_sync || ''));
  if (failed.length) {
    const room = failed.sort((a, b) => String(a.last_attempt_at).localeCompare(String(b.last_attempt_at))).pop();
    return `${FAILED[room.last_attempt_status]} ${formatTime(room.last_attempt_at)}（上次成功 ${success ? formatTime(success) : '无'}）`;
  }
  if (!success) return latest ? '从未成功同步' : '从未同步';
  return `上次同步 ${formatTime(success)}`;
}


const imageJobs = new WeakMap();

export function releaseImages(root) {
  imageJobs.get(root)?.controller.abort();
  imageJobs.delete(root);
  for (const image of root.querySelectorAll('img[data-yuketang-object-url]')) {
    URL.revokeObjectURL(image.getAttribute('data-yuketang-object-url'));
    image.removeAttribute('data-yuketang-object-url');
  }
}

export async function hydrateImages(root = document) {
  if (imageJobs.has(root)) return imageJobs.get(root).promise;
  const images = [...root.querySelectorAll('img[data-yuketang-src]')];
  const controller = new AbortController();
  const job = {controller, promise: null};
  imageJobs.set(root, job);
  let next = 0;
  async function worker() {
    while (!controller.signal.aborted && next < images.length) {
      const image = images[next++];
      const source = image.getAttribute('data-yuketang-src') || '';
      try {
        if (!source.startsWith('/api/yuketang/image/') || source.includes('\\')) throw new Error('invalid image route');
        const response = await fetch(source, {headers: {'X-QwenASR-Client': 'course-files'},
          cache: 'no-store', credentials: 'same-origin', signal: controller.signal});
        if (!response.ok || !response.headers.get('content-type')?.startsWith('image/')) throw new Error('invalid image response');
        const blob = await response.blob();
        if (blob.size > 25 * 1024 * 1024) throw new Error('image too large');
        if (controller.signal.aborted || image.isConnected === false || root.isConnected === false) continue;
        const url = URL.createObjectURL(blob);
        image.setAttribute('data-yuketang-object-url', url);
        image.src = url;
        image.removeAttribute('data-yuketang-src');
      } catch (error) {
        if (!controller.signal.aborted && image.isConnected !== false) {
          image.setAttribute('data-yuketang-error', 'true');
          image.alt = '题目图片加载失败，请刷新后重试';
        }
      }
    }
  }
  // A large bank must not create hundreds of simultaneous image requests.
  job.promise = Promise.all(Array.from({length: Math.min(4, images.length)}, worker)).finally(() => {
    if (imageJobs.get(root) === job) imageJobs.delete(root);
  });
  return job.promise;
}
