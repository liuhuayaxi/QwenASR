// The course service owns live progress; the task API also has a disk snapshot.
export function mergeLiveCourseProgress(payload, live) {
  if (!Array.isArray(live?.queue?.items) || !Array.isArray(live.imports)) {
    throw new Error('课程进度响应不完整');
  }
  const entries = new Map(live.imports.map(entry => [entry.id, entry]));
  const remaining = live.queue.items.filter(item => item.state !== 'completed');
  const tasks = (payload.tasks || []).flatMap(task => {
    if (task.type !== 'course_queue') return [task];
    if (!remaining.length) return [];
    const previous = new Map((task.course_items || []).map(item => [item.id, item]));
    const items = remaining.map(item => {
      const saved = previous.get(item.id) || {};
      const entry = entries.get(item.id);
      const phases = entry?.phases ? Object.entries(entry.phases).map(([key, phase]) => ({key, ...phase}))
        : item.state === 'queued' ? [] : saved.phases || [];
      return {...saved, ...item, title: item.title || entry?.title || saved.title,
        progress: Number(entry?.progress ?? (item.state === 'queued' ? 0 : saved.progress)) || 0,
        message: entry?.message || (item.state === 'queued' ? '已加入入库队列' : saved.message) || '',
        phases: phases.map(phase => ({...phase, state: item.state === 'paused' && phase.state === 'running' ? 'paused' : phase.state})),
      };
    });
    const states = new Set(items.map(item => item.state));
    const status = live.queue.paused ? 'paused' : states.has('running') ? 'running'
      : states.has('queued') ? 'queued' : 'interrupted';
    const progress = Math.round(items.reduce((sum, item) => sum + item.progress, 0) / items.length * 10) / 10;
    return [{...task, title: `课程回放 · ${items.length} 堂课`, total: live.queue.items.length, completed: live.queue.items.length-items.length,
      progress, status, actions: ['running', 'queued'].includes(status) ? ['pause'] : ['continue'],
      course_items: items, warnings: items.map(item => `${item.title}：${item.message || item.state}`)}];
  });
  const active = new Set(['running', 'queued', 'waiting_retry', 'stopping']);
  const attention = new Set(['failed', 'error', 'interrupted', 'pending', 'stale', 'completed_with_errors', 'paused']);
  return {...payload, tasks, counts: {...payload.counts, all: tasks.length,
    active: tasks.filter(task => active.has(task.status)).length,
    attention: tasks.filter(task => attention.has(task.status)).length,
    ...Object.fromEntries(['paused', 'completed', 'cancelled'].map(status => [status, tasks.filter(task => task.status === status).length])),
  }};
}
