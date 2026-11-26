// Let the browser skip layout/paint only for blocks verified below the fold.
// The DOM stays searchable and keyboard accessible, including without support.
export function deferOffscreen(container) {
  if (!CSS.supports('content-visibility', 'auto') || container.children.length < 24) return;
  const bounds = container.getBoundingClientRect();
  const bottom = Math.min(window.innerHeight, bounds.bottom);
  const candidates = [...container.children].map(element => ({element, rect: element.getBoundingClientRect()}));
  // Read all geometry before writing styles; never alternate layout reads/writes.
  for (const {element, rect} of candidates) {
    if (rect.top < bottom + window.innerHeight || rect.height === 0) continue;
    element.style.containIntrinsicBlockSize = `auto ${Math.ceil(rect.height)}px`;
    element.classList.add('deferred-content');
  }
}
