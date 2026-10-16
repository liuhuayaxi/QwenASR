"""Additive protocol for bounded classroom leases and rebuildable vector replicas.

Uses the gateway's existing authentication and GPU lock. It is intentionally
installable on protocol-1 nodes without replacing their durable job database.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
import time

from fastapi import HTTPException, Request

VERSION = 'classroom-1'
CLASSROOM_ASR_MAX_NEW_TOKENS = 8192
IDENT = re.compile(r'^[a-zA-Z0-9._:-]{8,128}$')
LOADED_SOURCE_SHA256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


class VectorReplica:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS vectors (course TEXT, contract TEXT, id TEXT, version TEXT, lesson TEXT, kind TEXT, derived INTEGER, vector BLOB, PRIMARY KEY(course,contract,id))')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def manifest(self, course, contract):
        with self.connect() as db:
            return dict(db.execute('SELECT id,version FROM vectors WHERE course=? AND contract=?', (course, canonical(contract))).fetchall())

    def sync(self, request):
        course, contract = request['course_id'], request['contract']
        rows, deleted = request.get('upsert', []), request.get('delete', [])
        if not isinstance(course, str) or not course or len(course) > 200 or len(rows) > 128 or len(deleted) > 10000:
            raise ValueError('INVALID_VECTOR_BATCH')
        values = []
        for row in rows:
            vector = row['vector']
            if len(vector) != contract['dimension'] or not all(isinstance(x, (float,int)) and math.isfinite(x) for x in vector):
                raise ValueError('VECTOR_CONTRACT_MISMATCH')
            if abs(sum(x*x for x in vector)-1) > .003:
                raise ValueError('VECTOR_NOT_NORMALIZED')
            values.append((course,canonical(contract),row['id'],row['version'],row.get('lesson_id'),row['source_type'],int(row.get('derived',False)),struct.pack('<'+'f'*len(vector), *vector)))
        with self.connect() as db:
            db.executemany('DELETE FROM vectors WHERE course=? AND contract=? AND id=?',[(course,canonical(contract),x) for x in deleted])
            db.executemany('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?,?,?,?,?)',values)
        return {'upserted':len(values),'deleted':len(deleted),'index_version':self.version(course,contract)}

    def version(self, course, contract):
        return hashlib.sha256(canonical(self.manifest(course,contract)).encode()).hexdigest()

    def search(self, request):
        course, contract = request['course_id'], request['contract']
        q = request['vector']
        if len(q) != contract['dimension'] or not all(math.isfinite(x) for x in q) or abs(sum(x*x for x in q)-1)>.003:
            raise ValueError('QUERY_CONTRACT_MISMATCH')
        sql='SELECT * FROM vectors WHERE course=? AND contract=?'
        params=[course,canonical(contract)]
        if request.get('lesson_id'):
            sql+=' AND lesson=?';params.append(request['lesson_id'])
        if request.get('source_types'):
            sql+=' AND kind IN ('+','.join('?' for _ in request['source_types'])+')';params.extend(request['source_types'])
        if not request.get('include_derived'):
            sql+=" AND (derived=0 OR kind='recording_refined')"
        with self.connect() as db:
            rows=db.execute(sql,params).fetchall()
        scored=[]
        for row in rows:
            vec=struct.unpack('<'+'f'*len(q), row['vector'])
            score=sum(a*b for a,b in zip(q,vec))
            if score>=.35:scored.append({'chunk_id':row['id'],'version':row['version'],'score':score})
        scored.sort(key=lambda x:-x['score'])
        return {'matches':scored[:max(1,min(int(request.get('limit',24)),100))], 'course_id':course,
                'contract':contract,'index_version':self.version(course,contract),'device':'Windows CPU'}


class ClassroomCompute:
    def __init__(self, node):
        self.node=node
        self.root=node.data/'classroom'
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.replica=VectorReplica(self.root/'vectors.sqlite3')
        self.guard=asyncio.Lock()
        self.close_guard=asyncio.Lock()
        self.process=None
        self.gpu_lock=None
        self.external_lock=None
        self.lease_until=0
        self.sessions={}
        self.last_use=0
        self.timer=None
        self.log=None

    def reserved(self):
        now=time.monotonic()
        self.sessions={k:v for k,v in self.sessions.items() if v>now}
        return now<self.lease_until or bool(self.sessions)

    async def close(self):
        async with self.close_guard:await self._close()

    async def _close(self):
        process,self.process=self.process,None
        if process:
            try:
                process.stdin.write(b'{"type":"stop"}\n');await process.stdin.drain()
                await asyncio.wait_for(process.wait(),5)
            except (OSError,asyncio.TimeoutError,ConnectionError):
                with contextlib.suppress(ProcessLookupError):process.terminate()
                try:await asyncio.wait_for(process.wait(),5)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):process.kill()
                    await process.wait()
        if self.gpu_lock:self.gpu_lock.close();self.gpu_lock=None
        if self.external_lock:self.external_lock.close();self.external_lock=None
        if self.log:self.log.close();self.log=None

    async def reap(self):
        try:
            while True:
                await asyncio.sleep(2)
                if not self.reserved() and time.monotonic()-self.last_use>30 and not self.guard.locked():
                    async with self.guard:await self.close()
        except asyncio.CancelledError:
            await self.close()

    async def start(self, needs_gpu=True):
        if getattr(self.node,'isolation',None) or getattr(self.node,'scheduler_error',None):raise HTTPException(409,'NODE_RESOURCE_ISOLATION')
        alive=self.process and self.process.returncode is None
        if not alive:await self.close()
        # Respect both the legacy and current gateway's sole GPU queue.
        lockroot=getattr(self.node,'coordination',self.node.data)
        if needs_gpu and self.gpu_lock is None:
            self.gpu_lock=(lockroot/'gpu.lock').open('a')
            external=self.node.cfg['server'].get('external_gpu_lock_file')
            if external:self.external_lock=Path(external).open('rb')
            deadline=time.monotonic()+8
            while True:
                try:
                    fcntl.flock(self.gpu_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    if self.external_lock:fcntl.flock(self.external_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic()>deadline:
                        self.gpu_lock.close();self.gpu_lock=None
                        raise HTTPException(409,'WINDOWS_BUSY_AUDIO_SAVED_RETRY')
                    await asyncio.sleep(.15)
        if alive:return
        self.log=(self.root/'worker.log').open('ab')
        self.process=await asyncio.create_subprocess_exec(self.node.cfg['asr']['python'],'-m','workers.classroom_worker',
            str(self.node.cfg['asr']['model_path']),str(self.node.cfg.get('knowledge',{}).get('model_root',Path.home()/'ai-infer-node/models/knowledge')),
            cwd=str(Path(__file__).resolve().parents[1]),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
            stderr=self.log,env={**os.environ,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','TOKENIZERS_PARALLELISM':'false','OMP_NUM_THREADS':'4'},limit=4*1024*1024)
        if not self.timer:self.timer=asyncio.create_task(self.reap())

    async def run(self, payload):
        ident=payload.get('operation_id','')
        if not IDENT.fullmatch(ident):raise HTTPException(400,'INVALID_OPERATION_ID')
        fingerprint=hashlib.sha256(canonical(payload).encode()).hexdigest()
        folder=self.root/ident
        folder.mkdir(exist_ok=True,mode=0o700)
        try:await asyncio.wait_for(self.guard.acquire(),8)
        except asyncio.TimeoutError:raise HTTPException(409,'WINDOWS_QUEUE_BUDGET_AUDIO_SAVED') from None
        try:
            request=folder/'request.json'
            if request.exists() and json.loads(request.read_text())['fingerprint']!=fingerprint:
                raise HTTPException(409,'IDEMPOTENCY_CONFLICT')
            if (folder/'canceled').exists():raise HTTPException(409,'OPERATION_CANCELED')
            result=folder/'result.json'
            if result.exists():return json.loads(result.read_text())
            from .artifacts import atomic_json
            atomic_json(request,{'fingerprint':fingerprint,'kind':payload['kind'],'operation_id':ident})
            if payload['kind']=='asr':self.lease_until=time.monotonic()+90
            self.last_use=time.monotonic()
            try:await self.start(needs_gpu=payload['kind']=='asr')
            except BaseException:
                await self.close();raise
            self.process.stdin.write((canonical(payload)+'\n').encode());await self.process.stdin.drain()
            try:
                line=await asyncio.wait_for(self.process.stdout.readline(),180)
                reply=json.loads(line)
                if reply.get('operation_id')!=ident:raise ValueError('WORKER_IDENTITY_MISMATCH')
                if reply.get('error'):
                    if reply['error'] in {'OutOfMemoryError','RuntimeError'}:await self.close()
                    raise HTTPException(409,reply['error'])
                if (folder/'canceled').exists():raise HTTPException(409,'OPERATION_CANCELED')
                reply.update(node_version=VERSION,operation_id=ident,provider='remote',fallback=False)
                atomic_json(result,reply)
                return reply
            except (ValueError,asyncio.TimeoutError,ConnectionError):
                await self.close()
                raise HTTPException(503,'WINDOWS_WORKER_RETRY_SAME_OPERATION') from None
            finally:self.last_use=time.monotonic()
        finally:self.guard.release()


def install(app):
    def compute():
        node=app.state.gateway
        if not hasattr(node,'classroom'):node.classroom=ClassroomCompute(node)
        return node.classroom

    @app.get('/v1/capabilities')
    async def capabilities():
        node=app.state.gateway
        root=Path(node.cfg.get('knowledge',{}).get('model_root',Path.home()/'ai-infer-node/models/knowledge')).expanduser()
        identity=root/'Qwen3-Embedding-0.6B'/'qwenasr-model-identity.json'
        service=getattr(node,'classroom',None)
        return {'ok':True,'data':{'version':VERSION,'asr_session':True,'vectors':True,'embedding_prepared':identity.is_file(),
            'reranker_supported':True,'reranker':(root/'Qwen3-Reranker-4B/qwenasr-model-identity.json').is_file(),
            'worker_loaded':bool(getattr(node,'classroom',None) and node.classroom.process and node.classroom.process.returncode is None),
            'embedding_device':'Windows CPU float16','asr_device':'Windows CUDA BF16',
            'gpu_concurrency':1,'model_idle_seconds':30,'lease_seconds':90,
            'file_asr_max_new_tokens':8192,'file_asr_recovery_by_key':True,
            'classroom_asr_max_new_tokens':CLASSROOM_ASR_MAX_NEW_TOKENS,
            'classroom_reserved':bool(service and service.reserved()),
            'classroom_busy':bool(service and service.guard.locked()),
            'classroom_session_count':len(service.sessions) if service else 0,
            'source_sha256':LOADED_SOURCE_SHA256,
            'source_matches_loaded':LOADED_SOURCE_SHA256==hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'gateway_source_sha256':hashlib.sha256(Path(__file__).with_name('app.py').read_bytes()).hexdigest(),
            'worker_source_sha256':hashlib.sha256((Path(__file__).resolve().parents[1]/'workers/classroom_worker.py').read_bytes()).hexdigest()}}

    @app.get('/v1/operations/{key}')
    async def operation_by_key(key:str):
        if not IDENT.fullmatch(key):raise HTTPException(400,'INVALID_OPERATION_KEY')
        node=app.state.gateway
        row=node.store.by_key(key)
        if not row:raise HTTPException(404,'Unknown operation')
        from .store import public_job
        return {'ok':True,'data':public_job(row)}

    @app.post('/v1/classroom/compute')
    async def execute(request:Request):
        if int(request.headers.get('content-length','0'))>20*1024*1024:raise HTTPException(413,'INPUT_TOO_LARGE')
        raw=await request.body()
        if len(raw)>20*1024*1024:raise HTTPException(413,'INPUT_TOO_LARGE')
        try:payload=json.loads(raw)
        except ValueError:raise HTTPException(400,'INVALID_JSON') from None
        if payload.get('kind') not in {'asr','embedding','rerank'}:raise HTTPException(400,'UNSUPPORTED_COMPUTE')
        return {'ok':True,'data':await compute().run(payload)}

    @app.post('/v1/classroom/end')
    async def end(request:Request):
        raw=await request.body();payload=json.loads(raw) if raw else {}
        service=compute()
        if payload.get('session_id'):service.sessions.pop(payload['session_id'],None)
        if service.sessions:return {'ok':True,'data':{'released':False,'other_sessions':len(service.sessions)}}
        service.lease_until=0
        async with service.guard:await service.close()
        return {'ok':True,'data':{'released':True}}

    @app.post('/v1/classroom/begin')
    async def begin(request:Request):
        payload=await request.json();ident=payload.get('session_id','')
        if not IDENT.fullmatch(ident):raise HTTPException(400,'INVALID_SESSION_ID')
        service=compute();service.sessions[ident]=time.monotonic()+60
        return {'ok':True,'data':{'reserved':True,'background_job':service.node.current,
            'message':'已有后台任务正在安全边界完成，音频会排队保留' if service.node.current else '课堂优先，后台 GPU 任务等待课堂结束'}}

    @app.post('/v1/classroom/cancel/{ident}')
    async def cancel(ident:str):
        if not IDENT.fullmatch(ident):raise HTTPException(400,'INVALID_OPERATION_ID')
        folder=compute().root/ident;folder.mkdir(exist_ok=True);(folder/'canceled').touch()
        return {'ok':True,'data':{'canceled':True}}

    @app.post('/v1/vectors/{action}')
    async def vectors(action:str, request:Request):
        raw=await request.body()
        if len(raw)>16*1024*1024:raise HTTPException(413,'INPUT_TOO_LARGE')
        try:
            payload=json.loads(raw);store=compute().replica
            if action=='manifest':value={'versions':await asyncio.to_thread(store.manifest,payload['course_id'],payload['contract'])}
            elif action=='sync':value=await asyncio.to_thread(store.sync,payload)
            elif action=='search':value=await asyncio.to_thread(store.search,payload)
            else:raise HTTPException(404,'UNKNOWN_ACTION')
            return {'ok':True,'data':value}
        except (ValueError,KeyError,TypeError,struct.error):raise HTTPException(400,'INVALID_VECTOR_REQUEST') from None
