const fields = {
  tier: 'mineruTier', ocr_mode: 'mineruOcrMode', image_analysis: 'mineruImageAnalysis',
  small_backend: 'mineruSmallBackend', vlm_engine: 'mineruVlmEngine', model_source: 'mineruModelSource',
  processing_window_size: 'mineruWindowSize', vlm_concurrency: 'mineruConcurrency',
  virtual_vram_gb: 'mineruVram',
};
const tierHints = {
  flash: '极速：适合预览和原生文字 PDF。扫描件、复杂公式与表格建议选择更高档位。',
  basic: '基础：使用小模型识别 OCR、公式和表格，适合较低资源的本地处理。',
  standard: '标准：结合视觉模型处理复杂排版，适合日常讲义、论文和公式课件。',
  advanced: '高级：在标准模型上增加推理计算，适合难识别的排版与最高质量需求，耗时更长。',
};

export function createMineruSettings({ root = document, fetcher = fetch } = {}) {
  const get = id => root.querySelector(`#${id}`);
  const status = get('mineruSettingsStatus');
  const save = get('saveMineruSettings');
  const reset = get('resetMineruSettings');
  let ready = false, loading = false, defaults;
  const hint = () => {
    get('mineruTierHint').textContent = tierHints[get('mineruTier').value];
    const vlm = ['standard', 'advanced'].includes(get('mineruTier').value);
    for (const id of ['mineruVlmEngine', 'mineruConcurrency']) get(id).disabled = !vlm;
  };
  const populate = settings => {
    for (const [key, id] of Object.entries(fields)) {
      const field = get(id);
      if (field.type === 'checkbox') field.checked = settings[key];
      else field.value = settings[key];
    }
    hint();
  };
  const runtime = info => {
    get('mineruRuntimeStatus').textContent = info.compatible
      ? `本机 MinerU ${info.version} · 本地识别`
      : info.available ? `当前 MinerU ${info.version || '版本未知'}，需要安装 4.x` : '未找到 MinerU 4 运行环境';
  };
  const dirty = () => { hint(); status.textContent = '有未保存的修改'; };
  for (const id of Object.values(fields)) get(id).addEventListener('change', dirty);
  reset.addEventListener('click', () => { if (ready) { populate(defaults); status.textContent = '已恢复推荐值，点击保存后生效'; } });
  save.addEventListener('click', async () => {
    if (!ready) return;
    const settings = {};
    for (const [key, id] of Object.entries(fields)) {
      const field = get(id);
      if (!field.checkValidity()) { field.reportValidity(); return; }
      settings[key] = field.type === 'checkbox' ? field.checked : field.type === 'number' ? Number(field.value) : field.value;
    }
    save.disabled = reset.disabled = true;
    status.textContent = '正在保存…';
    try {
      const response = await fetcher('/api/mineru/settings', {
        method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({settings}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '保存失败');
      populate(data.settings); runtime(data.runtime);
      status.textContent = '已保存到本机；新建、继续或重试识别任务时生效';
    } catch (error) { status.textContent = `保存失败：${error.message}`; }
    finally { save.disabled = reset.disabled = false; }
  });
  const load = async () => {
    if (loading) return;
    loading = true;
    save.disabled = reset.disabled = true;
    try {
      const response = await fetcher('/api/mineru/settings');
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '读取失败');
      defaults = data.defaults; populate(data.settings); runtime(data.runtime); ready = true;
      save.disabled = reset.disabled = false;
      status.textContent = '配置已读取；适用于本地 PDF 与课程资料识别';
    } catch (error) { status.textContent = `读取失败：${error.message}。重新打开设置可重试。`; }
    finally { loading = false; }
  };
  return { load, isReady: () => ready };
}
