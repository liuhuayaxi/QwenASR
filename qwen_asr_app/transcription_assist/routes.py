"""Reviewable risky edits remain separate from the acoustic ASR record."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException


def transcription_assist_router(store):
    router=APIRouter()

    @router.get('/api/recordings/{recording_id}/correction-review')
    async def candidates(recording_id:str):
        record=store.find(recording_id)
        if record is None:
            raise HTTPException(404,'没有找到课堂记录')
        items=[]
        for segment in record.segments:
            if segment.get('correction_review'):
                items.append({**segment['correction_review'],'segment_ids':[segment['id']],
                              'start':segment.get('start'),'end':segment.get('end'),'level':'片段精修'})
        for kind,parts in [('阶段精修',record.metadata.get('ai_reviews') or []),
                           ('全局精修',(record.metadata.get('ai_global_rewrite') or {}).get('parts') or [])]:
            for part in parts:
                if part.get('correction_review'):
                    items.append({**part['correction_review'],'segment_ids':part.get('segment_ids',[]),
                                  'start':part.get('start'),'end':part.get('end'),'level':kind})
        return {'recording_id':record.id,'candidates':items,'notice':'这些修改涉及数字、单位、否定词或公式条件，精修显示仍保留原内容。请回听后核对。'}

    return router
