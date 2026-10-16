/** Source-bound human decisions, with stale-reader and duplicate-submit guards. */
const renders=new WeakMap();
export async function renderCorrectionReview(session,{onResolved=()=>{}}={}) {
  const parent=document.querySelector('#noteTranscript');
  if (!parent || !session?.id) return;
  const token={};renders.set(parent,token);
  const current=()=>renders.get(parent)===token && document.querySelector('#noteSummaryEditor')?.dataset.noteId===session.id;
  const response=await fetch(`/api/recordings/${encodeURIComponent(session.id)}/correction-review`,{cache:'no-store',signal:AbortSignal.timeout(30000)});
  if (!response.ok) return;
  const payload=await response.json();
  if (!current())return;
  parent.querySelector('[data-correction-review]')?.remove();
  if (!payload.candidates?.length) return;
  const details=document.createElement('details');details.open=true;
  details.dataset.correctionReview='';details.className='transcript-review-disclosure';
  const summary=document.createElement('summary');summary.textContent=`${payload.candidates.length} 处精修改动待核对`;details.append(summary);
  const notice=document.createElement('p');notice.textContent=payload.notice;details.append(notice);
  for (const candidate of payload.candidates) {
    const card=document.createElement('article');card.className='correction-candidate';
    const title=document.createElement('b');title.textContent=[candidate.level,...(candidate.risks || [])].join(' · ');card.append(title);
    for (const [label,text] of [['原内容',candidate.before],['待核对建议',candidate.after]]) {
      const p=document.createElement('p');p.textContent=label+'：'+text;card.append(p);
    }
    const editor=document.createElement('textarea');editor.className='ui-field correction-edit';editor.value=candidate.after || candidate.before || '';editor.hidden=true;editor.setAttribute('aria-label','人工核对后的文本');card.append(editor);
    const error=document.createElement('p');error.className='correction-error';error.setAttribute('role','status');error.hidden=true;
    const actions=document.createElement('div');actions.className='correction-actions';
    let saving=false;
    const save=async decision=>{
      if(saving || !current())return;
      saving=true;actions.querySelectorAll('button').forEach(b=>b.disabled=true);error.hidden=true;
      try {
        const reply=await fetch(`/api/recordings/${encodeURIComponent(session.id)}/correction-review`,{
          method:'POST',headers:{'Content-Type':'application/json','X-QwenASR-Client':'recording-review'},
          body:JSON.stringify({candidate_id:candidate.candidate_id,decision,...(decision==='edit'?{text:editor.value}:{})}),signal:AbortSignal.timeout(30000)});
        const result=await reply.json();
        if(!reply.ok)throw new Error(result.detail || '核对未保存，请重试');
        if(!current())return;
        // Hide this candidate immediately only after a durable acknowledgement.
        card.remove();
        const remaining=details.querySelectorAll('.correction-candidate').length;
        summary.textContent=`${remaining} 处精修改动待核对`;
        if(!remaining)details.remove();
        onResolved(result.session);
      } catch(problem) {
        if(current()) {error.textContent=`${problem.message}。原文与建议均保留。`;error.hidden=false;}
      } finally {saving=false;actions.querySelectorAll('button').forEach(b=>b.disabled=false);}
    };
    for(const [label,decision] of [['采用建议','accept'],['保留原文','keep'],['手动修改','edit']]) {
      const button=document.createElement('button');button.type='button';button.className='ui-button';button.textContent=label;
      button.addEventListener('click',()=>{
        if(decision==='edit' && editor.hidden) {editor.hidden=false;editor.focus();button.textContent='保存修改';return;}
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
  }
  parent.prepend(details);
}
