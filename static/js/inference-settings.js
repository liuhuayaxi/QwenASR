export function createInferenceSettings({root = document, fetcher = fetch} = {}) {
  const get = id => root.querySelector(`#${id}`);
  const fields = ['inferenceMineruProvider','inferenceAsrProvider','inferenceKnowledgeProvider','inferencePowerSaving','inferenceRemoteUrl'];
  let settings, ready = false, loading = null, operation = null, revision = 0;
  const save = get('saveInferenceSettings'), test = get('testInferenceConnection'), status = get('inferenceSettingsStatus');
  const current = () => ({...settings, mineru_provider:get('inferenceMineruProvider').value,
    asr_provider:get('inferenceAsrProvider').value, knowledge_provider:get('inferenceKnowledgeProvider').value || 'local',
    classroom_power_saving:get('inferencePowerSaving').value === 'on', base_url:get('inferenceRemoteUrl').value.trim()});
  function controls() {
    save.disabled = test.disabled = !ready || !!loading || !!operation;
    for (const id of fields) get(id).disabled = !ready || !!loading;
  }
  // Callers see the confirmed server snapshot, never the editable form draft.
  function populate(value, replaceDraft = true) {
    settings = value;
    if (replaceDraft) {
      get('inferenceMineruProvider').value = value.mineru_provider;
      get('inferenceAsrProvider').value = value.asr_provider;
      get('inferenceKnowledgeProvider').value = value.knowledge_provider || 'local';
      get('inferencePowerSaving').value = value.classroom_power_saving ? 'on' : 'off';
      get('inferenceRemoteUrl').value = value.base_url;
    }
    const upload = get('uploadUseMac');
    if (upload) { upload.disabled = !!value.classroom_power_saving; if (value.classroom_power_saving) upload.checked = false; }
  }
  const load = () => {
    if (loading) return loading;
    if (operation) return Promise.resolve();
    ready = false; controls();
    loading = (async () => {
      try {
        const response = await fetcher('/api/inference/settings', {cache:'no-store', signal:AbortSignal.timeout(30000)});
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '读取失败');
        populate(data.settings); ready = true; status.textContent = '';
      } catch (error) { ready = false; status.textContent = error.message; throw error; }
      finally { loading = null; controls(); }
    })();
    return loading;
  };
  save.addEventListener('click', async () => {
    if (!ready || loading || operation) return;
    if (!get('inferenceRemoteUrl').reportValidity()) return;
    const started = revision, value = current(); operation = 'save'; controls(); status.textContent = '正在保存…';
    try {
      const response = await fetcher('/api/inference/settings', {method:'PUT', signal:AbortSignal.timeout(30000),
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({settings:value})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '保存失败');
      populate(data.settings, started === revision);
      status.textContent = started === revision ? '已保存' : '此前配置已保存；当前还有未保存的修改';
    } catch (error) { status.textContent = `保存失败：${error.message}。当前输入仍保留。`; }
    finally { operation = null; controls(); }
  });
  test.addEventListener('click', async () => {
    if (!ready || loading || operation) return;
    if (!get('inferenceRemoteUrl').reportValidity()) return;
    const started = revision, value = current(); operation = 'test'; controls(); status.textContent = '正在检查远程网关…';
    try {
      const response = await fetcher('/api/inference/test', {method:'POST', signal:AbortSignal.timeout(30000),
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({settings:value})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '连接失败');
      status.textContent = started === revision
        ? `远程网关连接正常 · ${data.capabilities?.version || '旧节点，尚无课堂向量能力'} · 检查未加载模型`
        : '此前配置的连接检查已完成；当前有未保存的修改，请重新测试。';
    } catch (error) {
      status.textContent = started === revision ? `连接失败：${error.message}` : '此前配置的连接检查失败；当前有未保存的修改，请重新测试。';
    } finally { operation = null; controls(); }
  });
  for (const id of fields) for (const event of ['input','change']) get(id).addEventListener(event, () => {
    revision += 1; if (ready) status.textContent = '有未保存的修改';
  });
  controls();
  return {load, isReady:() => ready, current:() => ready ? {...settings} : null};
}