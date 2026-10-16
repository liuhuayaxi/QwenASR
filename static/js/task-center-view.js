// Task center presents one study item at a time. Durable task identities remain
// the action targets; display grouping never changes their receipts or status.
export const activeStatuses = new Set(['running', 'queued', 'waiting_retry', 'stopping', 'active']);
export const errorStatuses = new Set(['failed', 'error', 'interrupted', 'stale', 'completed_with_errors', 'attention']);
const archived = task => task.recovered || task.display_status === 'recovered' || ['completed', 'cancelled'].includes(task.status);
const recordingId = task => {
  if (task.recording_id) return task.recording_id;
  try { return new URL(task.result_url || '', 'http://localhost').searchParams.get('recording'); } catch { return null; }
};
export function taskGroups(payload = {}) {
  const source = Array.isArray(payload.groups) ? payload.groups : (() => {
    const groups = new Map();
    for (const task of payload.tasks || []) {
      const recording = recordingId(task);
      // A course may contain many unrelated PDFs and lessons. A missing record
      // identity must never silently combine those operations by course name.
      const id = recording ? `recording:${recording}` : `task:${task.id}`;
      if (!groups.has(id)) groups.set(id, {id, recording_id: recording, title: task.title, category: task.category, tasks: []});
      groups.get(id).tasks.push(task);
    }
    return [...groups.values()];
  })();
  return source.map(group => {
    const tasks = group.tasks || [];
    const current = (group.current_tasks || tasks.filter(task => !archived(task))).filter(task => !archived(task));
    const history = group.history || tasks.filter(archived);
    const errors = group.errors || current.filter(task => errorStatuses.has(task.status) || task.error).map(task => ({...task, label: task.label || task.title}));
    const status = group.status || (current.some(task => activeStatuses.has(task.status)) ? 'running' : errors.length ? 'failed' : current.some(task => task.status === 'paused') ? 'paused' : current.length ? 'pending' : tasks.length && tasks.every(task => task.status === 'cancelled') ? 'cancelled' : 'completed');
    const leading = current.find(task => activeStatuses.has(task.status)) || current[0];
    return {...group, tasks, current_tasks: current, history, errors, status,
      title: group.title || leading?.title || '未命名任务', category: group.category || '',
      current_stage: group.current_stage || leading?.stage || (status === 'completed' ? '处理已完成' : ''),
      result_url: group.result_url || tasks.find(task => task.result_url)?.result_url || '',
      review_count: group.review_count ?? current.filter(task => task.status === 'pending' && (task.attention_kind === 'review' || Number(task.review_required_blocks) > 0)).length,
      recovered_count: group.recovered_count ?? history.filter(task => task.recovered || task.display_status === 'recovered').length};
  });
}
export function groupMatchesStatus(group, filter) {
  if (filter === 'all') return true;
  if (filter === 'daily') return activeStatuses.has(group.status) || errorStatuses.has(group.status) || ['paused', 'pending'].includes(group.status) || !!group.errors.length;
  // A lesson can be processing one step while another needs attention. Both
  // filters must include it, even when the error takes headline priority.
  if (filter === 'active') return activeStatuses.has(group.status) || group.current_tasks.some(task => activeStatuses.has(task.status));
  if (filter === 'attention') return !!group.errors.length || errorStatuses.has(group.status);
  if (filter === 'review') return group.review_count > 0;
  if (filter === 'recovered') return group.recovered_count > 0;
  return group.status === filter;
}
export function filterTaskGroups(groups, {filter = 'daily', type = '', category = '', query = ''} = {}) {
  const search = query.trim().toLocaleLowerCase();
  return groups.filter(group => groupMatchesStatus(group, filter)
    && (!type || group.tasks.some(task => task.type === type || task.family === type))
    && (!category || (category === '__local__' ? !group.category : group.category === category))
    && (!search || [group.title, group.category, ...group.tasks.flatMap(task => [task.title, task.label, task.stage, ...(task.files || []).map(file => file.source_name)])].join(' ').toLocaleLowerCase().includes(search)));
}
export function groupCounts(groups) {
  return Object.fromEntries(['all', 'active', 'attention', 'review', 'pending', 'paused', 'completed', 'cancelled', 'recovered'].map(key => [key, groups.filter(group => groupMatchesStatus(group, key)).length]));
}
export function actionableTasks(groups) {
  return [...new Map(groups.flatMap(group => [...group.current_tasks, ...(group.context_tasks || [])]).filter(task => !archived(task) && (task.actions || []).length).map(task => [task.id, task])).values()];
}
