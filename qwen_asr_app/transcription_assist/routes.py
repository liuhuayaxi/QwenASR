"""Resolve scientific correction candidates with source-bound, durable decisions."""
from __future__ import annotations

import copy
import hashlib
import json

from fastapi import APIRouter, Body, HTTPException

from ..durable import atomic_json
from ..note_content import transcript_blocks
from ..storage import local_now


def _identity(level, holder, candidate):
    value=[level,holder.get('segment_ids', [holder.get('id')]),
           {key:holder.get(key) for key in ('raw','corrected','start','end')},candidate]
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _holders(record):
    # Match transcript_blocks precedence exactly, including identical proposals
    # repeated in older layers. Resolving a shadowed holder leaves the visible
    # candidate pending forever.
    global_result=record.metadata.get('ai_global_rewrite') or {}
    groups=[('全局精修',global_result.get('parts') or [])] if global_result.get('complete') else []
    groups.append(('阶段精修',record.metadata.get('ai_reviews') or []))
    for level,parts in groups:
        for part in parts:
            if part.get('correction_review') and not part.get('ai_error') and str(part.get('corrected') or '').strip():
                yield level,part
    for segment in record.segments:
        if segment.get('correction_review'):yield '片段精修',segment


def active_candidates(record):
    # Use the same precedence as the displayed transcript. Older proposals
    # shadowed by an accepted/global result must not inflate the review count.
    visible=[block for block in transcript_blocks(record) if block.get('correction_review')]
    items=[]
    for block in visible:
        ids=list(block.get('segment_ids',[]));candidate=block['correction_review']
        owner=next(((level,holder) for level,holder in _holders(record)
            if holder.get('segment_ids',[holder.get('id')])==ids and holder['correction_review']==candidate),None)
        if owner is None:continue
        level,holder=owner
        items.append({**copy.deepcopy(candidate),'candidate_id':_identity(level,holder,candidate),
            'segment_ids':ids,'start':block['start'],'end':block['end'],'level':level})
    return items


def transcription_assist_router(store, require_available=None):
    router=APIRouter()

    def record_for(recording_id):
        record=store.find(recording_id)
        if record is None:raise HTTPException(404,'没有找到课堂记录')
        return record

    @router.get('/api/recordings/{recording_id}/correction-review')
    async def candidates(recording_id:str):
        record=record_for(recording_id)
        return {'recording_id':record.id,'candidates':active_candidates(record),
            'notice':'对照原录音选择采用建议、保留原文或手动修改；每次确认会保存决定，并更新待核对数量。'}

    @router.post('/api/recordings/{recording_id}/correction-review')
    async def resolve(recording_id:str,payload:dict=Body(...)):
        if require_available is not None:require_available(recording_id)
        record=record_for(recording_id)
        candidate_id=str(payload.get('candidate_id') or '')
        decision=payload.get('decision')
        if decision not in {'accept','keep','edit'} or len(candidate_id)!=64:
            raise HTTPException(400,'请选择一条当前建议及核对方式')
        text=payload.get('text') if decision=='edit' else None
        if decision=='edit' and (not isinstance(text,str) or not text.strip() or len(text)>100000):
            raise HTTPException(400,'修改后的文字不能为空或超过长度限制')
        requested={'candidate_id':candidate_id,'decision':decision,'text':text}
        request_hash=hashlib.sha256(json.dumps(requested,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        previous=next((item for item in record.metadata.get('correction_decisions',[]) if item.get('candidate_id')==candidate_id),None)
        if previous:
            if previous.get('request_sha256')!=request_hash:raise HTTPException(409,'这条建议已经核对，请刷新后查看最新内容')
            pending=next((item for item in active_candidates(record) if item['candidate_id']==candidate_id),None)
            if pending is not None:
                journal=json.loads((record.directory/'.correction-decisions'/(candidate_id+'.json')).read_text())
                if journal.get('request_sha256')!=request_hash:raise HTTPException(409,'核对检查点不匹配，原内容已保留')
                _,holder=next((level,holder) for level,holder in _holders(record)
                    if _identity(level,holder,holder['correction_review'])==candidate_id)
                holder.update(corrected=journal['chosen'],ai_pending=False,human_reviewed_at=journal['created_at'],human_review_decision=decision)
                holder.pop('correction_review',None);holder.pop('ai_error',None)
                record.save_all()
            return {'saved':True,'reused':True,'remaining':len(active_candidates(record)),'session':record.public()}
        candidate=next((item for item in active_candidates(record) if item['candidate_id']==candidate_id),None)
        if candidate is None:raise HTTPException(409,'原文或建议已经更新，本次未覆盖新内容，请刷新后核对')
        level,holder=next((level,holder) for level,holder in _holders(record)
            if _identity(level,holder,holder['correction_review'])==candidate_id)
        chosen=candidate['after'] if decision=='accept' else candidate['before'] if decision=='keep' else text.strip()
        journal={'candidate':candidate,'decision':decision,'text':text,'chosen':chosen,
                 'before_holder':copy.deepcopy(holder),'request_sha256':request_hash,'created_at':local_now()}
        # Preserve the complete displaced proposal before changing visible text.
        # The original acoustic ASR text remains unchanged.
        atomic_json(record.directory/'.correction-decisions'/(candidate_id+'.json'),journal)
        holder['corrected']=chosen
        holder.pop('correction_review',None)
        holder.pop('ai_error',None)
        holder['ai_pending']=False
        holder['human_reviewed_at']=journal['created_at']
        holder['human_review_decision']=decision
        record.metadata.setdefault('correction_decisions',[]).append({
            'candidate_id':candidate_id,'request_sha256':request_hash,'decision':decision,
            'segment_ids':candidate['segment_ids'],'created_at':journal['created_at']})
        record.metadata['updated_at']=journal['created_at']
        record.save_all()
        return {'saved':True,'remaining':len(active_candidates(record)),'session':record.public()}

    return router
