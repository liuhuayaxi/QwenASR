export function durationLabel(seconds) {
  const value = Math.max(0, Math.round(Number(seconds) || 0));
  if (value < 60) return `${value} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
  return `${Math.floor(value / 3600)} 小时 ${Math.floor(value % 3600 / 60)} 分`;
}

export function mineruEstimateLabel(estimate, status) {
  if (!estimate?.estimated || status === 'completed') return '';
  if (!['running', 'queued'].includes(status)) return '已停止更新预估';
  if (estimate.phase === 'queued' || status === 'queued') return '排队等待 · 开始处理后预估耗时';
  const basis = estimate.sample_count > 0 ? `参考同配置 ${estimate.sample_count} 次已完成任务`
    : estimate.observed_windows > 0 ? '根据当前已完成页段估算' : '初步估算 · 正在积累耗时样本';
  const remaining = estimate.overrun ? '当前部分超出预估耗时，仍在处理'
    : estimate.remaining_seconds == null ? '正在估算剩余时间'
      : `${estimate.scope === 'current_file' ? '当前文件' : ''}预计剩余约 ${durationLabel(estimate.remaining_seconds)}`;
  return [`已用 ${durationLabel(estimate.elapsed_seconds)}`, remaining, basis,
    estimate.sample_count > 0 && estimate.observed_windows > 0 ? '已按当前页段速度修正' : ''].filter(Boolean).join(' · ');
}
const legacyValues = new Map();
export function legacyMineruProgress(task) {
  const key = task.id ? `${task.id}:${task.remote_job_id || task.remote_operation_key || ''}` : '';
  if (task.family !== 'mineru' || task.progress != null || task.status !== 'running') {
    if (key) legacyValues.delete(key);
    return null;
  }
  const previous = legacyValues.get(key);
  const match = String(task.stage || '').match(/第\s*(\d+)[–-](\d+)\s*页\/(\d+)\s*·\s*窗口\s*(\d+)\/(\d+)/);
  if (!match || Number(match[3]) <= 0) return previous?.percent ?? null;
  // The current window is still in progress; only preceding pages are complete.
  let percent = Math.min(99, Math.round((Number(match[1]) - 1) / Number(match[3]) * 1000) / 10);
  if (previous && Number(match[4]) >= previous.window) percent = Math.max(percent, previous.percent);
  if (key) {
    if (legacyValues.size > 200) legacyValues.delete(legacyValues.keys().next().value);
    legacyValues.set(key, {percent,window:Number(match[4])});
  }
  return percent;
}
