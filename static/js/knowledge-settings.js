/** Durable model choices are edited separately from the classroom content. */
const esc = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url, method='GET', data) {
  const response = await fetch(url,{method,cache:'no-store',...(data ? {headers:{'Content-Type':'application/json'},body:JSON.stringify(data)} : {})});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail==='string' ? result.detail : '设置未保存');
  return result;
}
function input(name,label,value,type='text') {
  return `<label style="display:grid;gap:6px"><span>${esc(label)}</span><input name="${name}" type="${type}" value="${esc(value)}" style="width:100%"></label>`;
}
export async function openKnowledgeSettings(mode='knowledge') {
  let dialog = document.querySelector('#knowledgeModelDialog');
  if (!dialog) {
    dialog = document.createElement('dialog');
    dialog.id = 'knowledgeModelDialog';
    dialog.className = 'ui-dialog settings-dialog';
    dialog.style.cssText = 'max-width:720px;width:calc(100vw - 32px);padding:24px';
    document.body.append(dialog);
  }
  dialog.innerHTML='<p role="status">正在读取本机设置…</p>';
  dialog.showModal();
  try {
    const response = await api(mode==='knowledge' ? '/api/knowledge/settings' : '/api/intelligence/settings');
    const cfg=mode==='knowledge' ? response.settings : response.provider;
    const remote=mode==='knowledge' && response.compute_location==='Windows';
    const fields = mode==='knowledge' ? `
      <label style="display:grid;gap:6px">向量模型<select name="model_id">${response.models.map(m=>`<option value="${esc(m.id)}" ${m.id===cfg.model_id?'selected':''}>${esc(m.id)}</option>`).join('')}</select></label>
      <label style="display:grid;gap:6px">本机运行位置<select name="device"><option value="mps" ${cfg.device==='mps'?'selected':''}>Apple GPU</option><option value="cpu" ${cfg.device==='cpu'?'selected':''}>CPU</option></select></label>
      ${input('model_root','模型目录',cfg.model_root)}
      ${input('batch_size','索引批大小',cfg.batch_size,'number')}
      ${input('max_length','每块输入预算',cfg.max_length,'number')}
      ${input('idle_seconds','空闲释放模型（秒）',cfg.idle_seconds,'number')}
      <label style="display:grid;gap:6px">重排策略<select name="reranker"><option value="none" ${cfg.reranker==='none'?'selected':''}>排名融合（实时路径）</option><option value="qwen3-4b" ${cfg.reranker==='qwen3-4b'?'selected':''}>Qwen3 Reranker 4B（按需，须先准备）</option></select></label>` : `
      <label style="display:grid;gap:6px">生成通道<select name="transport"><option value="agy_cli" ${cfg.transport==='agy_cli'?'selected':''}>AGY CLI</option><option value="api" ${cfg.transport==='api'?'selected':''}>已配置的 API</option></select></label>
      ${input('model','速记增强模型',cfg.model)}
      <label style="display:grid;gap:6px">推理档位<select name="cli_effort">${['low','medium','high','xhigh','max'].map(x=>`<option ${x===cfg.cli_effort?'selected':''}>${x}</option>`).join('')}</select></label>
      ${input('cli_timeout_seconds','请求超时（秒）',cfg.cli_timeout_seconds,'number')}
      ${input('context_window','上下文窗口',cfg.context_window,'number')}
      ${input('max_output_tokens','最大输出',cfg.max_output_tokens,'number')}
      ${input('base_url','API 地址（API 通道）',cfg.base_url)}
      ${input('api_key','API 密钥（留空保留）','','password')}`;
    dialog.innerHTML=`<form><div style="display:flex;justify-content:space-between;align-items:center"><h2>${mode==='knowledge'?'课程知识检索设置':'速记增强设置'}</h2><button type="button" data-close class="ghost-button">关闭</button></div>
      <p>${mode==='knowledge' ? `当前计算位置：${remote?'Windows（文档、查询向量与相似度检索）':'Mac'}。文档与查询使用同一版本、维度和提示契约。${remote?'下方 Mac 位置与目录仅在改回本机后使用；Windows 模型须提前部署。':'更换模型后需要更新课程索引。'}` : '课堂问答和课后精修沿用各自设置。这里仅调整随堂速记增强。'}</p>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">${fields}</div>
      <p role="status" data-status>${mode==='knowledge' ? (remote?(response.remote_warning || (response.prepared?'Windows 向量模型已准备；不可用时保留中文词法检索':'Windows 向量模型尚未准备；请提前部署，当前可使用中文词法检索')):response.prepared?'当前模型已准备':'模型尚未准备，词法检索仍可使用') : '配置保存在 Mac 上'}</p>
      <div style="display:flex;gap:12px;justify-content:flex-end">${mode==='knowledge'&&!remote?'<button type="button" data-prepare class="ghost-button">准备所选模型</button>':''}<button type="submit" class="ui-button primary">保存设置</button></div></form>`;
    dialog.querySelector('[data-close]').onclick=()=>dialog.close();
    const form=dialog.querySelector('form'),status=dialog.querySelector('[data-status]');
    form.onsubmit=async event=>{
      event.preventDefault();const payload=Object.fromEntries(new FormData(form));
      for (const key of ['batch_size','max_length','idle_seconds','cli_timeout_seconds','context_window','max_output_tokens']) if (key in payload) payload[key]=Number(payload[key]);
      if (mode!=='knowledge') {payload.cli_model=payload.model;if (!payload.api_key) delete payload.api_key;}
      const button=form.querySelector('[type=submit]');button.disabled=true;
      try {const result=await api(mode==='knowledge'?'/api/knowledge/settings':'/api/intelligence/settings','PUT',mode==='knowledge'?{settings:payload}:{provider:payload});status.textContent=result.message || '已保存，下一次任务使用新设置';}
      catch(error){status.textContent=error.message;}finally{button.disabled=false;}
    };
    dialog.querySelector('[data-prepare]')?.addEventListener('click',async event=>{
      event.currentTarget.disabled=true;status.textContent='正在准备模型，可继续录音和保存手记…';
      try {await api('/api/knowledge/models/prepare','POST',{model_id:form.elements.model_id.value});status.textContent='模型已准备，更新课程索引后启用';}
      catch(error){status.textContent=error.message;}finally{dialog.querySelector('[data-prepare]').disabled=false;}
    });
  }catch(error){dialog.innerHTML=`<p>${esc(error.message)}</p><button type="button">关闭</button>`;dialog.querySelector('button').onclick=()=>dialog.close();}
}
document.addEventListener('click',event=>{const button=event.target.closest('[data-knowledge-settings]');if(button) void openKnowledgeSettings(button.dataset.knowledgeSettings || 'knowledge');});
