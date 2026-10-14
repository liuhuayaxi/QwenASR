"""Model preparation and diagnostics stay out of the classroom workflow."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, HTTPException

from .embeddings import get_embedder, close_embedder
from .settings import knowledge_settings, embedding_contract, prepared, prepare_models, MODELS


def knowledge_config_router(service):
    router = APIRouter()
    preparing = set()

    @router.get('/api/knowledge/settings')
    async def settings():
        cfg = knowledge_settings.read()
        embedder = get_embedder()
        return {'settings':cfg, 'contract':embedding_contract(cfg), 'prepared':prepared(cfg),
                'models':[{'id':key, **value} for key,value in MODELS.items()],
                'runtime':{'loaded':bool(embedder.process and embedder.process.poll() is None),
                           'load_seconds':embedder.load_seconds,'last_seconds':embedder.last_seconds},
                'strategy':'中文词法＋本机向量召回＋排名融合；重排通过单次明确请求执行，课堂路径使用排名融合。相似度不作为事实置信度',
                'preparing':list(preparing)}

    @router.put('/api/knowledge/settings')
    async def save(payload:dict=Body(...)):
        try:
            cfg = await asyncio.to_thread(knowledge_settings.save, payload.get('settings',payload))
            await asyncio.to_thread(close_embedder)
            return {'settings':cfg, 'contract':embedding_contract(cfg),'prepared':prepared(cfg),
                    'message':'设置已保存。已有词法索引可继续查询；请更新课程索引以启用新向量契约。'}
        except (ValueError, OSError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @router.post('/api/knowledge/models/prepare')
    async def prepare(payload:dict=Body(...)):
        cfg = knowledge_settings.read()
        ident = payload.get('model_id',cfg['model_id'])
        if ident not in MODELS:
            raise HTTPException(400,'模型未列入已核验的准备清单')
        if preparing:
            raise HTTPException(409,'已有模型准备任务，请等待完成')
        # Explicit user action authorizes the download; never start it from search.
        preparing.add(ident)
        try:
            await asyncio.to_thread(prepare_models, ident, cfg['model_root'])
            return {'prepared':True,'model_id':ident}
        finally:
            preparing.discard(ident)

    return router
