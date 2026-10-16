"""Authenticated classroom protocol. No local model or tensor imports."""
from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
import struct
import time
import uuid

from .durable import atomic_json
from .inference_providers import inference_settings, remote_client


class ClassroomRemoteError(RuntimeError):
    def __init__(self, code):
        self.code = str(code)[:180]
        super().__init__('Windows 暂不可用：' + self.code)


def request(cfg, path, payload=None, timeout=180):
    client=remote_client(cfg)
    with client.session() as session:
        response=session.get(path,timeout=10) if payload is None else session.post(path,json=payload,timeout=timeout)
        if response.status_code==404:
            raise RuntimeError('Windows 节点版本不支持课堂或向量能力，请更新节点；没有启动本机模型')
        if response.status_code>=400:
            try:
                value=response.json()
                code=value.get('error') or value.get('detail') or 'REMOTE_ERROR'
            except ValueError:code='REMOTE_ERROR'
            raise ClassroomRemoteError(code)
        return client.data(response)


def capabilities(cfg):
    return request(cfg,'/v1/capabilities')


def compute(cfg, payload, *, stop=None):
    # The server has one GPU and an eight-second queue admission budget. Two
    # course imports can legitimately exceed that budget while the other clip
    # is running. Retry only transient conditions, always using the durable
    # operation ID: an uncertain response reconciles the same cached result.
    import httpx
    from .remote_operations import ResultNeedsRepair
    started=time.monotonic()
    deadline=started+180
    transient={'WINDOWS_BUSY_AUDIO_SAVED_RETRY','WINDOWS_QUEUE_BUDGET_AUDIO_SAVED',
               'WINDOWS_WORKER_RETRY_SAME_OPERATION'}
    for attempt in range(12):
        if stop is not None and stop.is_set():
            raise ResultNeedsRepair('Windows 操作已停止等待；原始资料已保留')
        remaining=deadline-time.monotonic()
        if remaining<=0:
            raise ResultNeedsRepair('Windows 计算队列等待超时，原始资料已保留')
        try:
            value=request(cfg,'/v1/classroom/compute',payload,timeout=remaining)
            break
        except (ClassroomRemoteError,httpx.TransportError) as exc:
            if isinstance(exc,ClassroomRemoteError) and exc.code not in transient:
                raise
            if attempt==11:
                raise ResultNeedsRepair('Windows 计算暂时繁忙，原始资料已保留') from exc
            delay=min(1.0, max(0,deadline-time.monotonic()))
            if stop is not None:
                stop.wait(delay)
            else:
                time.sleep(delay)
    return {**value,'protocol':'classroom','actual_provider':'remote',
            'client_seconds':round(time.monotonic()-started,3),'admission_retries':attempt}


def transcribe_clip(folder, cfg, source, language, prompt, max_tokens, *, stop=None):
    folder=Path(folder)
    receipt=folder/'classroom-receipt.json'
    fingerprint=hashlib.sha256(source.read_bytes()).hexdigest()
    if receipt.exists():
        state=json.loads(receipt.read_text())
        if state['audio_sha256']!=fingerprint:raise ValueError('课堂音频版本发生变化')
    else:
        state={'operation_id':'live-'+uuid.uuid4().hex,'audio_sha256':fingerprint,'state':'pending'}
        atomic_json(receipt,state)
    if state.get('state')=='completed':return state['result']
    caps=capabilities(cfg)
    if not caps.get('asr_session'):raise RuntimeError('Windows 不支持课堂会话，请更新节点')
    ceiling=caps.get('classroom_asr_max_new_tokens',2048)
    if type(ceiling) is not int or not 128<=max_tokens<=ceiling:
        raise ValueError('Windows 课堂生成上限不支持已保存的参数，请更新节点；音频已保留')
    payload={'kind':'asr','operation_id':state['operation_id'],'audio':base64.b64encode(source.read_bytes()).decode(),
             'language':language or '', 'prompt':prompt or '', 'max_new_tokens':max_tokens}
    value=compute(cfg,payload,stop=stop)
    state.update(state='completed',result=value)
    atomic_json(receipt,state)
    return value


class RemoteEmbedder:
    remote=True
    process=None
    load_seconds=None
    last_seconds=None

    def __init__(self, config, routing):
        from .knowledge.settings import embedding_contract
        self.config=dict(config);self.routing=dict(routing);self.contract=embedding_contract(config)

    def _encode(self, texts, query=False):
        output=[]
        for offset in range(0,len(texts),self.config['batch_size']):
            payload={'operation_id':'embed-'+uuid.uuid4().hex,'kind':'embedding','contract':self.contract,
                     'texts':texts[offset:offset+self.config['batch_size']], 'query':query}
            result=compute(self.routing,payload)
            if result.get('contract')!=self.contract:raise ValueError('Windows 向量模型契约不一致')
            vectors=result.get('vectors',[])
            import math
            if len(vectors)!=len(payload['texts']) or any(len(v)!=self.contract['dimension'] or not all(math.isfinite(x) for x in v) or abs(sum(x*x for x in v)-1)>.003 for v in vectors):
                raise ValueError('Windows 向量回执维度或归一化无效')
            self.last_seconds=result.get('seconds');output.extend(vectors)
        return output

    def encode_documents(self, texts):return self._encode(texts)
    def encode_query(self, text):return self._encode([text],True)[0]
    def close(self):pass

    def search(self, store, course_id, query, **filters):
        """Incremental replica sync; Mac revalidates every returned ID/version."""
        key=json.dumps(self.contract,sort_keys=True)
        with store._connect() as db:
            rows=[dict(r) for r in db.execute('''SELECT c.*,s.source_type,cv.vector,cv.dimension FROM chunks c
                JOIN sources s USING(source_id) JOIN chunk_vectors cv USING(chunk_id)
                WHERE c.course_id=? AND c.is_active=1 AND s.status='active' AND cv.embedding_contract=?''',(course_id,key))]
        active={r['chunk_id']:r for r in rows}
        base={'course_id':course_id,'contract':self.contract}
        manifest=request(self.routing,'/v1/vectors/manifest',base)['versions']
        deleted=list(set(manifest)-set(active))
        changed=[r for r in rows if manifest.get(r['chunk_id'])!=r['content_hash']]
        for start in range(0,max(len(changed),1),64):
            upsert=[]
            for row in changed[start:start+64]:
                vector=struct.unpack('<'+'f'*row['dimension'],row['vector'])
                upsert.append({'id':row['chunk_id'],'version':row['content_hash'],'vector':vector,
                    'lesson_id':row['lesson_id'],'source_type':row['source_type'],'derived':row['origin']=='ai_derived'})
            if upsert or deleted:
                request(self.routing,'/v1/vectors/sync',{**base,'upsert':upsert,'delete':deleted});deleted=[]
        q=self.encode_query(query)
        result=request(self.routing,'/v1/vectors/search',{**base,'vector':q,**filters})
        if result.get('course_id')!=course_id or result.get('contract')!=self.contract:raise ValueError('Windows 返回跨课程或错误契约结果')
        found=[]
        for hit in result['matches']:
            row=active.get(hit['chunk_id'])
            if not row or row['content_hash']!=hit['version']:continue
            if filters.get('lesson_id') and row['lesson_id']!=filters['lesson_id']:continue
            if filters.get('source_types') and row['source_type'] not in filters['source_types']:continue
            if not filters.get('include_derived') and row['origin']=='ai_derived' and row['source_type']!='recording_refined':continue
            current=store.get_chunk(hit['chunk_id']);source=store.get_source(row['source_id'])
            if not current or not current['is_active'] or current['content_hash']!=hit['version'] or not source or source['status']!='active':continue
            row={k:v for k,v in row.items() if k!='vector'}
            row.update(vector_score=hit['score'],remote_index_version=result['index_version'])
            found.append(row)
        return found
