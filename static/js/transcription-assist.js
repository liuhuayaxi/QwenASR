/** A quiet review disclosure in the existing recording reader. */
export async function renderCorrectionReview(session) {
  const parent=document.querySelector('#noteTranscript');
  if (!parent || !session?.id) return;
  const response=await fetch(`/api/recordings/${encodeURIComponent(session.id)}/correction-review`,{cache:'no-store'});
  if (!response.ok) return;
  const payload=await response.json();
  if (!payload.candidates?.length || document.querySelector('#noteSummaryEditor')?.dataset.noteId!==session.id) return;
  parent.querySelector('[data-correction-review]')?.remove();
  const details=document.createElement('details'); details.dataset.correctionReview='';details.className='transcript-review-disclosure';
  const summary=document.createElement('summary');summary.textContent=`${payload.candidates.length} 处精修改动待核对（原内容已保留）`;details.append(summary);
  const notice=document.createElement('p');notice.textContent=payload.notice;details.append(notice);
  for (const candidate of payload.candidates) {
    const card=document.createElement('article');card.style.cssText='padding:12px;border-bottom:1px solid var(--border-color,#ddd)';
    const title=document.createElement('b');title.textContent=[candidate.level,...candidate.risks].join(' · ');card.append(title);
    for (const [label,text] of [['原内容',candidate.before],['待核对建议',candidate.after]]) {
      const p=document.createElement('p');p.textContent=label+'：'+text;card.append(p);
    }
    if (candidate.start != null) {const button=document.createElement('button');button.type='button';button.className='ghost-button';button.dataset.seek=String(candidate.start);button.textContent='回听原录音';card.append(button);}
    for(const evidence of candidate.evidence || []) {
      if(!String(evidence.open_url || '').startsWith('/'))continue;
      const link=document.createElement('a');link.href=evidence.open_url;link.target='_blank';link.rel='noopener';
      link.textContent='查看候选依据：'+(evidence.title || '课程资料')+(evidence.page ? ` · 第 ${evidence.page} 页` : '');
      const row=document.createElement('p');row.append(link);card.append(row);
    }
    details.append(card);
  }
  parent.prepend(details);
}
