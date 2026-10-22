// UI refreshes stop while hidden. Backend jobs and media capture have their own
// lifetimes and must not be cancelled by this scheduler.
export function createVisiblePoll({run, delay, enabled = () => true, events = []}) {
  let timer = null, request = null, disposed = false;
  const allowed = () => !disposed && !document.hidden && enabled();
  function schedule() {
    clearTimeout(timer); timer = null;
    if (allowed()) timer = setTimeout(wake, typeof delay === 'function' ? delay() : delay);
  }
  function refresh() {
    clearTimeout(timer); timer = null;
    if (request) return request;
    if (!allowed()) return Promise.resolve();
    request = Promise.resolve().then(run).finally(() => { request = null; schedule(); });
    return request;
  }
  function wake() { void refresh().catch(() => {}); }
  document.addEventListener('visibilitychange', wake);
  window.addEventListener('online', wake);
  window.addEventListener('pageshow', wake);
  for (const event of events) document.addEventListener(event, wake);
  return {refresh, wake, schedule, destroy() {
    disposed = true; clearTimeout(timer);
    document.removeEventListener('visibilitychange', wake);
    window.removeEventListener('online', wake);
    window.removeEventListener('pageshow', wake);
    for (const event of events) document.removeEventListener(event, wake);
  }};
}

export function classroomPanelVisible(panel) {
  return Boolean(panel && !document.hidden && !document.body.classList.contains('library-page') &&
    !document.querySelector('dialog[open]') && panel.getClientRects().length);
}
