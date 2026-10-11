export function createInferenceSettings({root = document, fetcher = fetch} = {}) {
  const get = id => root.querySelector(`#${id}`);
  let settings, ready = false, loading = null;
  const save = get('saveInferenceSettings');
  const test = get('testInferenceConnection');
  const status = get('inferenceSettingsStatus');
  const current = () => ({...settings, mineru_provider: get('inferenceMineruProvider').value,
    asr_provider: get('inferenceAsrProvider').value, base_url: get('inferenceRemoteUrl').value.trim()});
  const populate = value => {
    settings = value;
    get('inferenceMineruProvider').value = value.mineru_provider;
    get('inferenceAsrProvider').value = value.asr_provider;
    get('inferenceRemoteUrl').value = value.base_url;
  };
  const load = () => {
    if (loading) return loading;
    loading = (async () => {
      try {
        const response = await fetcher('/api/inference/settings');
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '读取失败');
        populate(data.settings); ready = true;
        save.disabled = test.disabled = false;
        get('inferenceSettingsStatus').textContent = '';
      } catch (error) { ready = false; status.textContent = error.message; throw error; }
      finally { loading = null; }
    })();
    return loading;
  };
  get('saveInferenceSettings').addEventListener('click', async () => {
    if (!ready) return;
    const button = get('saveInferenceSettings');
    button.disabled = true;
    try {
      const value = current();
      const response = await fetcher('/api/inference/settings', {method: 'PUT',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({settings: value})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '保存失败');
      populate(data.settings);
      get('inferenceSettingsStatus').textContent = '已保存';
    } catch (error) { get('inferenceSettingsStatus').textContent = error.message; }
    finally { button.disabled = false; }
  });
  test.addEventListener('click', async () => {
    if (!ready) return;
    test.disabled = true;
    status.textContent = '正在检查远程网关…';
    try {
      const response = await fetcher('/api/inference/test', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({settings: current()})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '连接失败');
      status.textContent = '远程网关连接正常 · 检查未加载模型';
    } catch (error) { status.textContent = `连接失败：${error.message}`; }
    finally { test.disabled = false; }
  });
  for (const id of ['inferenceMineruProvider','inferenceAsrProvider','inferenceRemoteUrl']) {
    get(id).addEventListener('change', () => { if (ready) status.textContent = '有未保存的修改'; });
  }
  // Realtime and upload callers use the same server-owned snapshot.  Exposing
  // this read-only view keeps the selected provider from getting lost when a
  // WebSocket session is opened before another settings request is made.
  return {load, isReady: () => ready, current: () => ready ? {...settings} : null};
}
