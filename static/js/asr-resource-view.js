// Device provenance and utilization belong to the same task/host observation.
export function asrResourceView(phase, live, now = Date.now()) {
  const saved = phase?.execution || {};
  const execution = saved.provider && (!live?.provider || Number(saved.updated_at || 0) >= Number(live.updated_at || 0))
    ? saved : live || saved;
  const name = {local: 'Mac', remote: 'Windows'}[execution.provider];
  const label = name ? `${name} 转写${execution.fallback ? '（已回退）' : ''}` : '转写设备待确认';
  const usage = live?.usage;
  const age = typeof usage?.sampled_at === 'number' ? now / 1000 - usage.sampled_at : Infinity;
  const validHost = name && usage?.device === name && usage.scope === 'host' && age >= -5 && age <= 15;
  const percentage = value => validHost && typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100
    ? `${Math.round(value)}%` : '暂不可用';
  const active = phase?.state === 'running';
  const text = active && name ? `${label} · CPU ${percentage(usage?.cpu_percent)} · GPU ${percentage(usage?.gpu_percent)}` : label;
  const basis = execution.basis === 'configured' ? '按本任务保存的设备配置，尚无转写结果。'
    : execution.basis === 'requested' ? '本段已请求此设备；连接或排队期间也显示这台机器的占用率。'
    : execution.basis === 'last_segment' ? '设备依据最近已保存片段的实际执行记录。' : '';
  return {text, title: [basis, active && name ? `${name} 整机 CPU / GPU 占用率（0–100%），不是单个任务占比；约每 5 秒采样。` : '',
    active && name && !validHost ? '当前未获得新鲜监测数据。' : ''].filter(Boolean).join(' ')};
}
