const phases = {
  waiting: '等待 AI 调用', connecting: '正在连接模型', processing: '等待模型返回',
  receiving: '正在接收 AI 输出', checking: '正在校验并保存结果',
};

function duration(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return value < 60 ? `${value} 秒` : `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
}

export function aiActivityView(task) {
  if (task.family !== 'ai' || !['running', 'queued'].includes(task.status)) return null;
  const activity = task.ai_activity;
  if (!activity) return {
    label: task.status === 'queued' ? '等待 AI 调用' : '等待模型返回',
    detail: '整段输出完成并校验后才会计入完成进度。',
  };
  const chars = Math.max(0, Number(activity.output_chars) || 0);
  const idle = Math.max(0, Number(activity.idle_seconds) || 0);
  const label = activity.phase === 'receiving' && idle >= 15
    ? '等待后续输出' : phases[activity.phase] || '等待模型返回';
  const parts = [`已用时 ${duration(activity.elapsed_seconds)}`];
  if (chars) parts.push(`已接收 ${chars.toLocaleString('zh-CN')} 字符`);
  else if (activity.phase !== 'waiting') parts.push('尚未收到正文');
  if (activity.phase === 'waiting') parts.push('正在等待可用的处理名额');
  else parts.push(`最近活动 ${duration(idle)}前`);
  if (activity.active_requests > 1) parts.push(`${activity.active_requests} 个请求处理中`);
  return {label, detail: parts.join(' · ')};
}
