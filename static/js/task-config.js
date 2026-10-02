// Read the same saved providers and course settings as the recording workbench.
export function aiTaskPayload(task, action, defaults, library) {
  const payload = {id: task.id, action};
  if (task.family !== 'ai' || !['continue', 'retry'].includes(action)) return payload;
  let saved;
  try { saved = JSON.parse(localStorage.getItem('qwen-asr-api-settings-v1') || '{}'); }
  catch { throw new Error('保存的 AI 配置无法读取，请在录音工作台重新保存设置'); }
  const selectedMode = {summary: 'summary', refinement: 'correction', live_summary: 'review'}[task.type];
  function provider(mode) {
    const prefix = ['review', 'summary'].includes(mode) ? `${mode}_` : '';
    const fallbackPrefix = prefix || '';
    const raw = saved.function_providers?.[mode] || {
      base_url: saved[`${prefix}base_url`] || saved.base_url || defaults[`ai_${fallbackPrefix}base_url`],
      model: saved[`${prefix}model`] || saved.model || defaults[`ai_${fallbackPrefix}model`],
      api_key: saved[`${prefix}api_key`] ?? saved.api_key ?? '',
      api_mode: saved[`${prefix}api_mode`] || saved.api_mode || 'auto',
      context_window: saved[`${prefix}context_window`] || saved.context_window || defaults.default_context_window,
      extra_params: saved[`${prefix}extra_params`] || saved.extra_params || '{}',
    };
    let params = raw.extra_params || {};
    if (typeof params === 'string') {
      try { params = JSON.parse(params); }
      catch { if (mode === selectedMode) throw new Error('AI 参数不是有效 JSON，请在录音工作台修正后重试'); params = {}; }
    }
    if (!params || Array.isArray(params) || typeof params !== 'object') {
      if (mode === selectedMode) throw new Error('AI 参数需要是 JSON 对象');
      params = {};
    }
    if (mode === selectedMode && (!raw.base_url?.trim() || !raw.model?.trim())) {
      throw new Error('请先在录音工作台配置对应的 AI 模型，再继续任务');
    }
    return {...raw, extra_params: params, context_window: Number(raw.context_window) || defaults.default_context_window,
      max_output_tokens: Number(raw.max_output_tokens) || (mode === 'summary' ? 16384 : 8000),
      concurrency: Number(raw.concurrency) || (mode === 'summary' ? 3 : 2)};
  }
  const record = (library.recordings || library.records || []).find(item => item.id === task.recording_id) || {};
  const profile = library.category_profiles?.[task.category] || record.course_settings || {};
  const config = {terminology: profile.terminology || [], ai: {
    ...provider('sentence'), review_provider: provider('review'), summary_provider: provider('summary'),
    global_provider: provider('global'), correction_provider: provider('correction'),
    system_prompt: profile.ai_system_prompt || saved.function_providers?.sentence?.system_prompt || defaults.default_ai_system_prompt,
    history_chars: Number(saved.history_chars) || 0,
  }};
  if (task.type === 'refinement') Object.assign(config, {ai_correction: true, translation: 'off'});
  if (task.type === 'live_summary') Object.assign(config, {summary_enabled: true, summary_interval_minutes: Math.max(1, Number(saved.summary_interval) || 10)});
  payload.config = config;
  if (task.type === 'summary') payload.summary_payload = {config,
    summary_system_prompt: saved.function_providers?.summary?.system_prompt || saved.detailed_summary_prompt || defaults.default_note_summary_prompt,
    abstract_system_prompt: saved.abstract_prompt || defaults.default_note_abstract_prompt};
  return payload;
}
