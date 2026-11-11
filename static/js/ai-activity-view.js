const phases = {
  waiting: '等待 AI 调用', connecting: '正在连接模型', processing: '等待模型返回',
  receiving: '正在接收 AI 输出', checking: '正在校验并保存结果',
  reading:'模型正在读取资料',thinking:'已收到模型思考活动',
  provider_busy:'上游模型暂无容量，AGY 正在重试',upstream_retry:'AGY 正在恢复上游请求',
};

function duration(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return value < 60 ? `${value} 秒` : `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
}

export function aiActivityView(task) {
  if (task.family !== 'ai' || !['running', 'queued'].includes(task.status)) return null;
  const activity = task.ai_activity;
  const requests=activity?.request_activity || [];
  const requestStatus=request=>{
    const pages=request.pages || [],prefix=pages.length?`第 ${pages[0]}${pages.length>1?`–${pages.at(-1)}`:''} 页：`:'';
    const opened=(request.opened_pages || []).filter(n=>!pages.length || pages.includes(n));
    const parts=[`${prefix}${phases[request.phase] || '等待模型返回'}`];
    if(request.upstream_errors) parts.push(`上游已出现 ${request.upstream_errors} 次失败`);
    if(request.read_files) parts.push(`已读取 ${request.read_files} 个资料文件`);
    if(opened.length) parts.push(`已打开 ${opened.length}/${pages.length || opened.length} 页（待核对结果）`);
    if(request.image_reads) parts.push(`已查看 ${request.image_reads} 张图`);
    if(request.model_turns) parts.push(`已收到 ${request.model_turns} 次模型活动`);
    if(request.thinking_chars) parts.push(`思考活动累计 ${Number(request.thinking_chars).toLocaleString('zh-CN')} 字符`);
    const output=Math.max(Number(request.output_chars)||0,Number(request.session_output_chars)||0);
    if(output) parts.push(`结果输出 ${output.toLocaleString('zh-CN')} 字符`);
    parts.push(`最近活动 ${duration(request.idle_seconds)}前`);
    return parts.join(' · ');
  };
  const batches=Array.isArray(task.review_batches)?task.review_batches:[];
  if(batches.length) {
    const current=batches.map(batch=>{
      const pages=batch.pages || [],first=pages[0],last=pages.at(-1);
      const age=Math.max(0,(Date.now()-Date.parse(batch.started_at))/1000);
      const request=requests.find(request=>(request.pages || []).join(',')===pages.join(','));
      return request?requestStatus(request):`第 ${first}${last!==first?`–${last}`:''} 页 · 正在准备请求 · 已用时 ${duration(age)}${batch.attempt>1?` · 第 ${batch.attempt} 次请求`:''}`;
    });
    const deadline=batches.every(batch=>batch.deadline_policy==='until_completion')
      ? '持续等待模型完成，不按固定时长中断；可手动暂停或取消。'
      : `单次请求最多等待 ${duration(batches[0].timeout_seconds || 300)}，明确失败后有限恢复。`;
    return {label:`正在审查 ${current.join('；')}`,detail:`本轮已保存 ${task.completed || 0}/${task.total || 0} 页。${deadline}已审批次可继续复用。`};
  }
  if (!activity) return {
    label: task.status === 'queued' ? '等待 AI 调用' : '等待模型返回',
    detail: '整段输出完成并校验后才会计入完成进度。',
  };
  const chars = Math.max(0, Number(activity.output_chars) || 0);
  const idle = Math.max(0, Number(activity.idle_seconds) || 0);
  const label = activity.phase === 'receiving' && idle >= 15
    ? '等待后续输出' : phases[activity.phase] || '等待模型返回';
  const parts = [`已用时 ${duration(activity.elapsed_seconds)}`];
  if (activity.current_output_chars != null) {
    parts.push(`当前请求已用时 ${duration(activity.request_elapsed_seconds)}`);
    if (activity.current_output_chars) parts.push(`当前请求已接收 ${Number(activity.current_output_chars).toLocaleString('zh-CN')} 字符`);
    else parts.push('当前请求尚未收到结果正文');
    if(chars) parts.push(`本轮累计接收 ${chars.toLocaleString('zh-CN')} 字符（含清单、读图和中间结果）`);
    if(activity.requests) parts.push(`本轮已发起 ${activity.requests} 次请求`);
  }
  else if (chars) parts.push(`已接收 ${chars.toLocaleString('zh-CN')} 字符`);
  else if (activity.phase !== 'waiting') parts.push('尚未收到正文');
  if (activity.phase === 'waiting') parts.push('正在等待可用的处理名额');
  else parts.push(`最近活动 ${duration(idle)}前`);
  if (activity.active_requests > 1) parts.push(`${activity.active_requests} 个请求处理中`);
  if(requests.length) parts.push(...requests.map(requestStatus));
  if(!activity.active_requests && /独立审查/.test(task.phase || task.stage || ''))
    return {label:'等待其他笔记释放审查名额',detail:'本笔记已排队，当前没有向模型发送审查请求。'};
  return {label, detail: parts.join(' · ')};
}
