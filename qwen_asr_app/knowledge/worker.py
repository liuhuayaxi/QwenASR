"""One local embedding model, no CUDA, bounded batches, idle process exit."""
from __future__ import annotations

import json
import queue
import sys
import threading
import time


def main():
    # The protocol owns stdout; library diagnostics go to stderr.
    protocol = sys.stdout
    sys.stdout = sys.stderr
    cfg = json.loads(sys.argv[1])
    from .settings import embedding_contract, model_directory, prepared, QUERY_INSTRUCTION
    contract = embedding_contract(cfg)
    if not prepared(cfg):
        raise RuntimeError('本地向量模型尚未准备，请在知识检索设置中准备模型')
    import torch
    from transformers import AutoModel, AutoTokenizer
    torch.set_num_threads(2)
    device = cfg['device']
    if device == 'mps' and not torch.backends.mps.is_available():
        raise RuntimeError('Apple GPU 不可用，请明确选择 CPU 模式后重建索引')
    started = time.monotonic()
    folder = str(model_directory(cfg))
    tokenizer = AutoTokenizer.from_pretrained(folder, padding_side='left', local_files_only=True)
    model = AutoModel.from_pretrained(folder, local_files_only=True,
                                     dtype=torch.float16, attn_implementation='sdpa').to(device).eval()
    def memory():
        import resource
        return {'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'metal_allocated_bytes':torch.mps.current_allocated_memory() if device=='mps' else 0,
                'metal_driver_bytes':torch.mps.driver_allocated_memory() if device=='mps' else 0}
    def reply(value):
        protocol.write(json.dumps(value, ensure_ascii=False) + '\n')
        protocol.flush()
    reply({'type': 'ready', 'contract': contract, 'load_seconds': time.monotonic()-started, 'memory':memory()})
    inbox = queue.Queue(maxsize=4)
    def read():
        for line in sys.stdin:
            inbox.put(line)
        inbox.put(None)
    threading.Thread(target=read, daemon=True).start()
    while True:
        try:
            line = inbox.get(timeout=cfg['idle_seconds'])
        except queue.Empty:
            break
        if line is None:
            break
        request = json.loads(line)
        if request.get('type') == 'stop':
            break
        ident = request.get('id')
        try:
            if request.get('contract') != contract:
                raise ValueError('向量请求模型契约不一致')
            texts = request['texts']
            if not isinstance(texts, list) or len(texts) > cfg['batch_size'] or any(not isinstance(x, str) for x in texts):
                raise ValueError('向量批次无效')
            if request.get('query'):
                texts = [f'Instruct: {QUERY_INSTRUCTION}\nQuery:{text}' for text in texts]
            batch = tokenizer(texts, padding=True, truncation=False, return_tensors='pt')
            if batch['input_ids'].shape[1] > cfg['max_length']:
                raise ValueError('知识块超过模型输入预算，未截断公式或题目；请调整分块或输入预算')
            batch = batch.to(device)
            started = time.monotonic()
            with torch.inference_mode():
                states = model(**batch).last_hidden_state[:, -1].float()
                vectors = torch.nn.functional.normalize(states, p=2, dim=1).cpu().tolist()
            reply({'type': 'result', 'id': ident, 'contract': contract,
                   'vectors': vectors, 'seconds': time.monotonic()-started, 'memory':memory()})
        except Exception as exc:
            reply({'type': 'error', 'id': ident, 'message': str(exc)})


if __name__ == '__main__':
    main()
