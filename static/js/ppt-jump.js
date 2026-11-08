// "↗ PPT 第N页" links beside note headings. The heading's own text, id and
// search entries are untouched: the link lives in a separate, non-selectable
// span that is hidden when printing.
export function pageLabel(target) {
  const start = target.page_start;
  const end = target.page_end && target.page_end !== start ? target.page_end : 0;
  return end ? `第${start}–${end}页` : `第${start}页`;
}

export function headingTexts(tokens, out = []) {
  // Headings in rendered order, including nested ones (lists, quotes).
  for (const token of tokens || []) {
    if (token.type === 'heading') out.push(String(token.text || '').trim());
    if (token.tokens && token.type !== 'heading') headingTexts(token.tokens, out);
    if (token.items) for (const item of token.items) headingTexts(item.tokens, out);
  }
  return out;
}

export function pptJump(document, entry, hrefFor) {
  const targets = (entry.targets || []).filter((t) => t.viewer_page_start || t.page_start);
  if (!targets.length) return null;
  const wrap = document.createElement('span');
  wrap.className = 'ppt-jump';
  wrap.contentEditable = 'false';
  wrap.setAttribute('aria-hidden', 'false');
  const link = (target, text) => {
    const a = document.createElement('a');
    a.href = hrefFor(target);
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.className = 'ppt-jump-link';
    a.textContent = text;
    a.title = `在新页面打开课件 ${target.source_name || ''} ${pageLabel(target)}`.trim();
    a.addEventListener('click', (event) => event.stopPropagation());
    return a;
  };
  if (targets.length === 1) {
    wrap.append(link(targets[0], `↗ PPT ${pageLabel(targets[0])}`));
    return wrap;
  }
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'ppt-jump-link';
  button.textContent = `↗ PPT · ${targets.length} 份`;
  button.setAttribute('aria-expanded', 'false');
  const menu = document.createElement('span');
  menu.className = 'ppt-jump-menu';
  menu.hidden = true;
  for (const target of targets) menu.append(link(target, `${target.source_name || '课件'} · ${pageLabel(target)}`));
  button.addEventListener('click', (event) => {
    event.stopPropagation(); event.preventDefault();
    menu.hidden = !menu.hidden; button.setAttribute('aria-expanded', String(!menu.hidden));
  });
  document.addEventListener('click', () => { menu.hidden = true; button.setAttribute('aria-expanded', 'false'); });
  wrap.append(button, menu);
  return wrap;
}
