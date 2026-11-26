"""Durable refinement results and bounded, course-aware recovery requests."""
from __future__ import annotations

import copy
import hashlib
import json

from .context_budget import estimate_tokens, truncate_tail_to_tokens
from .durable import atomic_json
from .note_content import transcript_blocks
from .storage import local_now


def source_hash(segments):
    source=[{k:s.get(k) for k in ('id','raw','start','end')} for s in segments]
    return hashlib.sha256(json.dumps(source,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def pending_ids(record):
    # Saved proposals are finalized locally, never rewritten a second time.
    return {int(i) for block in transcript_blocks(record)
            if not block['refined'] and not block.get('correction_review')
            for i in block.get('segment_ids',[])}


def saved_progress(record):
    pending=pending_ids(record)
    usable=[s for s in record.segments if str(s.get('raw') or s.get('corrected') or '').strip() and not s.get('ai_suppressed')]
    return {'saved_segments':sum(int(s['id']) not in pending for s in usable),
            'total_segments':len(usable),'remaining_segments':len(pending),
            'review_blocks':sum(bool(b.get('correction_review')) for b in transcript_blocks(record))}


def finalize_corrections(record):
    """Install the saved first-pass corrections without another AI request.

    Older checkpoints held numerical/formula changes as proposals. Keep their
    complete previous holders on disk, then use the already generated text.
    Raw acoustic transcripts remain the source of truth and are never changed.
    """
    if record is None:
        return {'adopted': 0}
    from .transcription_assist.routes import active_candidates, _holders, _identity, _refresh_global_text
    candidates = active_candidates(record)
    created = local_now()
    owners = []
    for candidate in candidates:
        holder = next(holder for level, holder in _holders(record)
                      if _identity(level, holder, holder['correction_review']) == candidate['candidate_id'])
        if not str(candidate.get('after') or '').strip():
            raise ValueError('已保存的校对正文为空，原文保留')
        atomic_json(record.directory/'.correction-finalizations'/(candidate['candidate_id']+'.json'), {
            'policy': 'single-pass-correction-v1', 'candidate': candidate,
            'before_holder': copy.deepcopy(holder), 'created_at': created,
        })
        owners.append((holder, candidate))
    for holder, candidate in owners:
        holder.update(corrected=candidate['after'], ai_pending=False,
                      correction_resolution={'policy': 'single-pass-correction-v1',
                                             'candidate_id': candidate['candidate_id'], 'created_at': created})
        holder.pop('correction_review', None)
        holder.pop('ai_error', None)
    previous = record.metadata.pop('correction_verification', None)
    if previous:
        atomic_json(record.directory/'.correction-finalizations'/'previous-verification.json', previous)
    job = (record.metadata.get('ai_jobs') or {}).get('refinement') or {}
    if str(job.get('phase') or '').startswith('AI 自动复核'):
        job['phase'] = ''
    if owners or previous:
        _refresh_global_text(record)
        record.metadata['correction_finalization'] = {
            'policy': 'single-pass-correction-v1', 'adopted': len(owners), 'updated_at': created,
        }
        record.save_all()
    return {'adopted': len(owners)}


def commit_blocks(record, blocks, *, destination='reviews'):
    """Journal complete validated blocks before installing the visible text."""
    by_id={int(s['id']):s for s in record.segments}
    ids=[int(i) for b in blocks for i in b['segment_ids']]
    if not blocks or any(not str(b.get('corrected') or '').strip() for b in blocks) or len(ids)!=len(set(ids)) or any(i not in by_id for i in ids):
        raise ValueError('校对检查点的来源编号无效，原文未改变')
    members=[by_id[i] for i in ids]
    key=source_hash(members)
    epoch=str(record.metadata.get('refinement_epoch') or '')
    payload={'source_sha256':key,'segment_ids':ids,'blocks':blocks,'created_at':local_now(),
             'epoch':epoch,'destination':destination}
    output_hash=hashlib.sha256(json.dumps(blocks,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:16]
    epoch_key=hashlib.sha256(epoch.encode()).hexdigest()[:12]
    atomic_json(record.directory/'.refinement-checkpoints'/f'{epoch_key}-{key}-{output_hash}.json',payload)
    install_blocks(record,blocks,destination=destination)


def install_blocks(record, blocks, *, destination='reviews'):
    by_id={int(s['id']):s for s in record.segments}
    ids={int(i) for b in blocks for i in b['segment_ids']}
    if destination=='segments':
        if any(len(b['segment_ids'])!=1 for b in blocks):raise ValueError('逐段校对不能合并来源编号')
        for block in blocks:
            member=by_id[int(block['segment_ids'][0])]
            member.update(corrected=block['corrected'],ai_pending=False)
            member.pop('ai_error',None)
            for field in ('correction_review','correction_provenance'):
                if block.get(field):member[field]=copy.deepcopy(block[field])
                else:member.pop(field,None)
        record.save_all()
        return
    existing=record.metadata.get('ai_reviews') or []
    # Whole-block replacement avoids deleting an accepted neighbouring range.
    for old in existing:
        old_ids={int(i) for i in old.get('segment_ids',[])}
        if old_ids & ids and not old_ids <= ids:
            raise ValueError('校对检查点与已保存段落部分重叠，未覆盖已有内容')
    reviews=[r for r in existing if not ids.intersection(int(i) for i in r.get('segment_ids',[]))]
    for block in blocks:
        members=[by_id[int(i)] for i in block['segment_ids']]
        ident=hashlib.sha256(json.dumps(block,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:20]
        reviews.append({'id':'checkpoint-'+ident,'start':min(float(s.get('start',0)) for s in members),
            'end':max(float(s.get('end',0)) for s in members),'raw':'\n\n'.join(str(s.get('raw') or '') for s in members),
            **copy.deepcopy(block),'manual_refinement':True,'created_at':local_now()})
        for member in members:
            member.pop('ai_error',None);member['ai_pending']=False
    record.metadata['ai_reviews']=reviews
    record.save_all()


def restore_checkpoints(record):
    by_id={int(s['id']):s for s in record.segments}
    for path in sorted((record.directory/'.refinement-checkpoints').glob('*.json')):
        saved=json.loads(path.read_text())
        if saved.get('epoch','')!=str(record.metadata.get('refinement_epoch') or ''):continue
        ids=saved['segment_ids']
        if any(i not in by_id for i in ids) or source_hash([by_id[i] for i in ids])!=saved['source_sha256']:continue
        pending=pending_ids(record)
        blocks=[b for b in saved['blocks'] if set(b['segment_ids'])<=pending]
        if blocks:install_blocks(record,blocks,destination=saved.get('destination','reviews'))


def context_for(record, group, window):
    """Aim for 40% total input with relevant context, never pad with unrelated text."""
    target=max(0,int(window*.4)-estimate_tokens(json.dumps(group,ensure_ascii=False))-8000)
    ids={int(s['id']) for s in group}
    blocks=transcript_blocks(record)
    left=[];right=[]
    start=float(group[0].get('start',0))
    for block in blocks:
        if ids.intersection(block.get('segment_ids',[])):continue
        text=f"[{block['start']:.0f}–{block['end']:.0f}秒] {block['text']}"
        (left if block['start']<start else right).append(text)
    before='\n'.join(left);after='\n'.join(right)
    # Keep nearby context on each side, then use spare capacity for the other.
    before=truncate_tail_to_tokens(before,max(0,target-estimate_tokens(after))) if estimate_tokens(after)<target//2 else truncate_tail_to_tokens(before,target//2)
    remaining=max(0,target-estimate_tokens(before))
    kept=[];used=0
    for row in right:
        cost=estimate_tokens(row)
        if used+cost>remaining:break
        kept.append(row);used+=cost
    return ('课程：'+str(record.metadata.get('category',''))+'\n课次：'+str(record.metadata.get('title',''))+
            '\n以下仅供术语与语意衔接，不属于本批输出，不得重复改写或补入本批。\n前文：\n'+before+'\n后文：\n'+'\n'.join(kept))
