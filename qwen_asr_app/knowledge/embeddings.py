"""Strict query/document contract with a disposable local model subprocess."""
from __future__ import annotations

import atexit
import collections
import json
import math
import os
import queue
import subprocess
import sys
import threading
import uuid

from ..local_compute import metal_lease
from ..paths import ROOT, PRIVATE_ROOT
from .settings import knowledge_settings, embedding_contract, prepared


class LocalEmbedder:
    def __init__(self, config=None):
        self.config = dict(config or knowledge_settings.read())
        self.contract = embedding_contract(self.config)
        self.lock = threading.RLock()
        self.process = None
        self.replies = None
        self.cache = collections.OrderedDict()
        self.last_memory = None
        self.last_seconds = None
        self.load_seconds = None
        self.log = None

    def _read(self, timeout):
        try:
            value = self.replies.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise TimeoutError('本地向量化超时，已释放模型，可以重试') from None
        if value is None:
            self.close()
            raise RuntimeError('本地向量模型进程退出，请检查诊断记录后重试')
        return value

    def _start(self):
        from ..inference_providers import inference_settings
        if inference_settings.for_task('knowledge').get('knowledge_provider') == 'remote_only':
            raise RuntimeError('课堂省电模式禁止启动 Mac 向量模型')
        if self.process and self.process.poll() is None:
            return
        self.close()
        if not prepared(self.config):
            raise RuntimeError('本地向量模型尚未准备，词法检索仍可用；请在设置中准备模型')
        folder = PRIVATE_ROOT / 'knowledge'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log = (folder / 'embedding-worker.log').open('a', encoding='utf-8')
        env = os.environ.copy()
        env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
                   PYTHONPATH=str(ROOT), PYTORCH_ENABLE_MPS_FALLBACK='0')
        self.process = subprocess.Popen([sys.executable, '-m', 'qwen_asr_app.knowledge.worker', json.dumps(self.config)],
                                        cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.log, text=True, bufsize=1)
        self.replies = inbox = queue.Queue()
        process = self.process
        def read():
            try:
                for line in process.stdout:
                    inbox.put(json.loads(line))
            finally:
                inbox.put(None)
        threading.Thread(target=read, daemon=True).start()
        message = self._read(self.config['timeout_seconds'])
        if message.get('type') != 'ready' or message.get('contract') != self.contract:
            self.close()
            raise RuntimeError('本地向量模型就绪回执不匹配')
        self.load_seconds = message['load_seconds']
        self.last_memory = message.get('memory')

    def _encode(self, texts, query=False):
        if not texts:
            return []
        result = []
        with self.lock:
            for offset in range(0, len(texts), self.config['batch_size']):
                batch = texts[offset:offset+self.config['batch_size']]
                lease = metal_lease(timeout=self.config['timeout_seconds'])
                with lease:
                    self._start()
                    ident = uuid.uuid4().hex
                    request = {'id': ident, 'texts': batch, 'query': query, 'contract': self.contract}
                    try:
                        self.process.stdin.write(json.dumps(request, ensure_ascii=False)+'\n')
                        self.process.stdin.flush()
                    except (BrokenPipeError, OSError):
                        self.close()
                        self._start()
                        self.process.stdin.write(json.dumps(request, ensure_ascii=False)+'\n')
                        self.process.stdin.flush()
                    message = self._read(self.config['timeout_seconds'])
                if message.get('id') != ident:
                    self.close()
                    raise RuntimeError('向量任务回执身份不一致')
                if message.get('type') == 'error':
                    raise RuntimeError(message.get('message', '向量模型未返回结果'))
                if message.get('contract') != self.contract:
                    raise RuntimeError('向量模型版本不一致，禁止混用')
                vectors = message.get('vectors') or []
                if len(vectors) != len(batch):
                    raise RuntimeError('向量批次回执数量不一致')
                for vector in vectors:
                    if len(vector) != self.contract['dimension'] or not all(math.isfinite(x) for x in vector):
                        raise RuntimeError('向量维度或数值无效')
                    if abs(sum(x*x for x in vector)-1) > .002:
                        raise RuntimeError('向量归一化不满足模型契约')
                self.last_seconds = message['seconds']
                self.last_memory = message.get('memory')
                result.extend(vectors)
        return result

    def encode_documents(self, texts):
        return self._encode(texts)

    def encode_query(self, text):
        with self.lock:
            if text in self.cache:
                self.cache.move_to_end(text)
                return list(self.cache[text])
            value = self._encode([text], query=True)[0]
            self.cache[text] = value
            if len(self.cache) > 128:
                self.cache.popitem(last=False)
            return list(value)

    def close(self):
        with self.lock:
            process, self.process = self.process, None
            if process:
                if process.poll() is None:
                    try:
                        process.stdin.write('{"type":"stop"}\n')
                        process.stdin.flush()
                        process.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                for pipe in (process.stdin, process.stdout):
                    if pipe:
                        pipe.close()
            if self.log:
                self.log.close()
                self.log = None


_instance = None
_guard = threading.RLock()


def get_embedder():
    global _instance
    cfg = knowledge_settings.read()
    from ..inference_providers import inference_settings
    routing = inference_settings.for_task('knowledge')
    cfg = {**cfg}
    with _guard:
        remote = routing.get('knowledge_provider', 'local') != 'local'
        if _instance is None or _instance.config != cfg or bool(getattr(_instance,'remote',False)) != remote or (remote and _instance.routing != routing):
            if _instance:
                _instance.close()
            if remote:
                from ..remote_classroom import RemoteEmbedder
                _instance = RemoteEmbedder(cfg, routing)
            else:
                _instance = LocalEmbedder(cfg)
        return _instance


def close_embedder():
    global _instance
    with _guard:
        if _instance:
            _instance.close()
            _instance = None


atexit.register(close_embedder)
