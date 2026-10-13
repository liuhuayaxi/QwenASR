export function resourceStatus(task) {
  // A course queue aggregates several jobs; it owns no GPU-release observation.
  if (task.type === 'course_queue' || !['asr','mineru'].includes(task.family)) return null;
  const inference = task.inference || {};
  const metrics = inference.metrics || {};
  const state = metrics.gpu_release_state;
  if (state === 'failed' || metrics.resource_warning === 'GPU_RELEASE_FAILED') {
    return {text:'资源释放异常，需检查', tone:'error', detail:'节点检查发现显存或工作进程未正常释放，请检查 Windows 推理节点。'};
  }
  if (state === 'unknown' || metrics.resource_warning === 'GPU_RELEASE_UNKNOWN') {
    return {text:'暂无法确认资源释放', tone:'unknown', detail:'节点已尝试检查，但没有取得完整的显存或工作进程数据。'};
  }
  if (state === 'verified' || (!state && metrics.gpu_release_verified === true)) {
    return {text:'显存释放已确认', tone:'ok', detail:'节点已确认任务结束后的显存占用恢复到允许范围。'};
  }
  const remote = inference.provider === 'remote' || task.actual_provider === 'remote' || !!task.remote_job_id;
  if (!remote) return null;
  if (['running','queued','stopping','waiting_retry'].includes(task.status)) {
    return {text:'任务结束后检查资源释放', tone:'pending', detail:'当前任务尚未结束，暂时没有结束后的资源检查结果。'};
  }
  return {text:'未记录资源检查数据', tone:'unknown', detail:'这条任务记录没有保存资源检查结果；仅凭此项不能判断是否存在资源占用。'};
}
