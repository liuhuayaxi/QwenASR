const fields = {
  tier:'mineruTier', ocr_mode:'mineruOcrMode', image_analysis:'mineruImageAnalysis',
  small_backend:'mineruSmallBackend', vlm_engine:'mineruVlmEngine', model_source:'mineruModelSource',
  processing_window_size:'mineruWindowSize', vlm_concurrency:'mineruConcurrency', virtual_vram_gb:'mineruVram',
};
export function createMineruSettings({root = document, fetcher = fetch} = {}) {
  const get = id => root.querySelector(`#${id}`);
  const status = get('mineruSettingsStatus'), save = get('saveMineruSettings'), reset = get('resetMineruSettings');
  let ready = false, loading = null, saving = false, defaults, autoTierSupported = false, revision = 0;
  function controls() {
    const tier = get('mineruTier').value;
    const autoOption = get('mineruTier').querySelector('option[value="auto"]');
    if (autoOption) autoOption.disabled = !autoTierSupported;
    const vlm = ['standard','advanced','auto'].includes(tier);
    for (const id of Object.values(fields)) get(id).disabled = !ready || !!loading || (!vlm && ['mineruVlmEngine','mineruConcurrency'].includes(id));
    save.disabled = reset.disabled = !ready || !!loading || saving;
    get('mineruTierHint').textContent = tier === 'auto'
      ? '原生文字层完整的 PPT 走 Standard；扫描件或视觉元素密集的页面走 Advanced。'
      : '';
  }
  const populate = settings => {
    for (const [key, id] of Object.entries(fields)) {
      const field = get(id);
      if (field.type === 'checkbox') field.checked = settings[key];
      else field.value = settings[key];
    }
  };
  const runtime = info => {
    get('mineruRuntimeStatus').textContent = info.compatible
      ? `本机 MinerU ${info.version} · 本地识别`
      : info.available ? `当前 MinerU ${info.version || '版本未知'}，需要安装 4.x` : '未找到 MinerU 4 运行环境';
  };
  for (const id of Object.values(fields)) for (const event of ['input','change']) get(id).addEventListener(event, () => {
    revision += 1; controls(); status.textContent = '有未保存的修改';
  });
  reset.addEventListener('click', () => {
    if (!ready || loading || saving) return;
    revision += 1; populate(defaults); controls(); status.textContent = '已恢复推荐值，点击保存后生效';
  });
  save.addEventListener('click', async () => {
    if (!ready || loading || saving) return;
    if (get('mineruTier').value === 'auto' && !autoTierSupported) {
      status.textContent = '当前后端尚未支持自动档，请先重启并更新工作台后再使用';
      get('mineruTier').focus();
      return;
    }
    const settings = {};
    for (const [key, id] of Object.entries(fields)) {
      const field = get(id);
      if (!field.checkValidity() || (field.type === 'number' && !field.disabled && field.value === '')) {
        for (let node = field.parentElement; node; node = node.parentElement) if (node.tagName === 'DETAILS') node.open = true;
        status.textContent = '请填写有效的数值后再保存'; field.focus(); field.reportValidity(); return;
      }
      settings[key] = field.type === 'checkbox' ? field.checked : field.type === 'number' ? Number(field.value) : field.value;
    }
    const started = revision; saving = true; controls(); status.textContent = '正在保存…';
    try {
      const response = await fetcher('/api/mineru/settings', {method:'PUT', signal:AbortSignal.timeout(30000),
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({settings})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '保存失败');
      if (started === revision) populate(data.settings);
      runtime(data.runtime);
      status.textContent = started === revision ? '已保存' : '此前配置已保存；当前还有未保存的修改';
    } catch (error) { status.textContent = `保存失败：${error.message}。当前输入仍保留。`; }
    finally { saving = false; controls(); }
  });
  const load = () => {
    if (loading) return loading;
    if (saving) return Promise.resolve();
    ready = false; controls();
    loading = (async () => {
      try {
        const response = await fetcher('/api/mineru/settings', {cache:'no-store', signal:AbortSignal.timeout(30000)});
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '读取失败');
        defaults = data.defaults;
        autoTierSupported = Array.isArray(data.tier_choices) && data.tier_choices.includes('auto');
        const settings = {...data.settings};
        if (!autoTierSupported && settings.tier === 'auto') {
          settings.tier = defaults?.tier === 'advanced' ? 'advanced' : 'standard';
          status.textContent = '当前后端尚未支持自动档，已暂时使用兼容档位；更新后端并重启后可启用自动档';
        }
        populate(settings); runtime(data.runtime); ready = true;
        if (!status.textContent) status.textContent = '配置已读取';
      } catch (error) { ready = false; status.textContent = `读取失败：${error.message}。重新打开设置可重试。`; }
      finally { loading = null; controls(); }
    })();
    return loading;
  };
  controls();
  return {load, isReady:() => ready};
}
