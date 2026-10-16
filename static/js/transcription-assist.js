/** Source-bound human decisions, with stale-reader and duplicate-submit guards. */
const renders=new WeakMap();
// Polling may rebuild transcript nodes while a reader is checking one passage.
// Keep disclosure state and unsaved edits outside those replaceable nodes.
const reviewStates=new Map();
export async function renderCorrectionReview(session,{onResolved=()=>{}}={}) {
  const parent=document.querySelector('#noteTranscript');
  if (!parent || !session?.id) return;
  const token={};renders.set(parent,token);
  const current=()=>renders.get(parent)===token && document.querySelector('#noteSummaryEditor')?.dataset.noteId===session.id;
  const response=await fetch(`/api/recordings/${encodeURIComponent(session.id)}/correction-review`,{cache:'no-store',signal:AbortSignal.timeout(30000)});
  if (!response.ok) return;
  const payload=await response.json();
  if (!current())return;
  parent.querySelectorAll('[data-correction-review]').forEach(node=>node.remove());
  if (!payload.candidates?.length) return;
  const notice=document.createElement('p');notice.dataset.correctionReview='';notice.className='transcript-review-notice';
  const updateCount=remaining=>{notice.textContent=`${remaining} 处改动等待 AI 自动复核，无需逐条确认；可展开查看。`;if(!remaining)notice.remove();};
  updateCount(payload.candidates.length);parent.prepend(notice);
  const time=value=>{const seconds=Math.max(0,Math.round(Number(value)||0));return [Math.floor(seconds/3600),Math.floor(seconds/60)%60,seconds%60].map(n=>String(n).padStart(2,'0')).join(':');};
  for (const candidate of payload.candidates) {
    const stateKey=session.id+':'+candidate.candidate_id;
    let state=reviewStates.get(stateKey);
    if(!state){state={open:false,editing:false,text:candidate.after || candidate.before || '',saving:false,error:''};reviewStates.set(stateKey,state);}
    const details=document.createElement('details');details.dataset.correctionReview='';details.className='transcript-review-disclosure';
    details.open=state.open;details.addEventListener('toggle',()=>{if(current())state.open=details.open;});
    const summary=document.createElement('summary');summary.textContent=`AI 待复核 · ${time(candidate.start)}–${time(candidate.end)}`;details.append(summary);
    const card=document.createElement('article');card.className='correction-candidate';
    const title=document.createElement('b');title.textContent=[candidate.level,...(candidate.risks || [])].join(' · ');card.append(title);
    for (const [label,text] of [['原内容',candidate.before],['待核对建议',candidate.after]]) {
      const p=document.createElement('p');p.textContent=label+'：'+text;card.append(p);
    }
    const editor=document.createElement('textarea');editor.className='ui-field correction-edit';editor.value=state.text;editor.hidden=!state.editing;editor.setAttribute('aria-label','人工核对后的文本');editor.addEventListener('input',()=>{state.text=editor.value;});card.append(editor);
    const error=document.createElement('p');error.className='correction-error';error.setAttribute('role','status');error.textContent=state.error;error.hidden=!state.error;
    const actions=document.createElement('div');actions.className='correction-actions';
    state.view={current,details,actions,error,updateCount,onResolved};
    const save=async decision=>{
      if(state.saving || !current())return;
      state.saving=true;state.error='';actions.querySelectorAll('button').forEach(b=>b.disabled=true);error.hidden=true;
      try {
        const reply=await fetch(`/api/recordings/${encodeURIComponent(session.id)}/correction-review`,{
          method:'POST',headers:{'Content-Type':'application/json','X-QwenASR-Client':'recording-review'},
          body:JSON.stringify({candidate_id:candidate.candidate_id,decision,...(decision==='edit'?{text:editor.value}:{})}),signal:AbortSignal.timeout(30000)});
        const result=await reply.json();
        if(!reply.ok)throw new Error(result.detail || '核对未保存，请重试');
        reviewStates.delete(stateKey);
        const view=state.view;
        if(!view.current())return;
        // Hide this candidate immediately only after a durable acknowledgement.
        view.details.remove();
        view.updateCount(parent.querySelectorAll('.correction-candidate').length);
        view.onResolved(result.session);
      } catch(problem) {
        state.error=`${problem.message}。原文与建议均保留。`;
        const view=state.view;
        if(view.current()) {view.error.textContent=state.error;view.error.hidden=false;}
      } finally {state.saving=false;state.view.actions.querySelectorAll('button').forEach(b=>b.disabled=false);}
    };
    for(const [label,decision] of [['采用建议','accept'],['保留原文','keep'],['手动修改','edit']]) {
      const button=document.createElement('button');button.type='button';button.className='ui-button';button.textContent=decision==='edit' && state.editing?'保存修改':label;button.disabled=state.saving;
      button.addEventListener('click',()=>{
        if(decision==='edit' && editor.hidden) {state.editing=true;editor.hidden=false;editor.focus();button.textContent='保存修改';return;}
        void save(decision);
      });actions.append(button);
    }
    if (candidate.start != null) {const button=document.createElement('button');button.type='button';button.className='ghost-button';button.dataset.seek=String(candidate.start);button.textContent='回听原录音';actions.append(button);}
    card.append(actions,error);
    for(const evidence of candidate.evidence || []) {
      if(!String(evidence.open_url || '').startsWith('/') || String(evidence.open_url).startsWith('//'))continue;
      const link=document.createElement('a');link.href=evidence.open_url;link.target='_blank';link.rel='noopener';
      link.textContent='查看候选依据：'+(evidence.title || '课程资料')+(evidence.page ? ` · 第 ${evidence.page} 页` : '');
      const row=document.createElement('p');row.append(link);card.append(row);
    }
    details.append(card);
    const ids=(candidate.segment_ids || []).join(',');
    const segment=[...parent.querySelectorAll('.note-transcript-segment')].find(node=>node.dataset.segmentIds===ids);
    if(segment)segment.append(details);
    else {details.open=true;parent.append(details);}
  }
}
