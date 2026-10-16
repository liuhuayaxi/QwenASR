"""Optional bounded candidate scoring; never required for classroom capture."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from ..paths import ROOT
from ..local_compute import metal_lease
from .embeddings import close_embedder
from .settings import knowledge_settings

MODEL_ID = 'Qwen/Qwen3-Reranker-4B'
REVISION = '22e683669bc0f0bd69640a1354a6d0aebcfeede5'
CONTRACT = {'model_id':MODEL_ID,'revision':REVISION,'weights':'official_unquantized',
            'dtype':'float16','scoring':'yes_no_logits_v1','max_length':2048,'batch_size':2}
_guard = threading.Lock()


def rank_candidates(query, results, *, timeout=45):
    """Return results + measured diagnostics, or an explicit budget error."""
    cfg = knowledge_settings.read()
    from ..inference_providers import inference_settings
    routing=inference_settings.for_task('knowledge')
    if routing.get('knowledge_provider','local') != 'local':
        from ..remote_classroom import capabilities, request
        if not capabilities(routing).get('reranker'):
            raise RuntimeError('Windows 节点未准备重排模型，保留排名融合；未启动 Mac 重排')
        import uuid
        selected=results[:8]
        if not selected:return results, {'strategy':'empty'}
        reply=request(routing,'/v1/classroom/compute',{'kind':'rerank','operation_id':'rerank-'+uuid.uuid4().hex,
            'contract':CONTRACT,'query':query,'documents':[r['text'] for r in selected]},timeout=timeout)
        if reply.get('contract')!=CONTRACT or len(reply.get('scores',[]))!=len(selected):raise RuntimeError('Windows 重排回执契约不一致')
        ranked=[{**row,'rerank_score':score} for row,score in zip(selected,reply['scores'])]
        ranked.sort(key=lambda r:-r['rerank_score'])
        return ranked+results[8:],{k:v for k,v in reply.items() if k!='scores'}
    folder = Path(cfg['model_root']) / 'Qwen3-Reranker-4B'
    try:
        identity = json.loads((folder/'qwenasr-model-identity.json').read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError('重排模型尚未准备，当前仍使用排名融合') from exc
    if identity.get('model_id') != MODEL_ID or identity.get('revision') != REVISION:
        raise RuntimeError('重排模型版本不符合已核验契约')
    selected = results[:8]
    if not selected:
        return results, {'strategy':'empty'}
    payload = {'query':query,'documents':[r['text'] for r in selected],'folder':str(folder),'device':cfg['device'],'contract':CONTRACT}
    with _guard:
        # Both large text models cannot be resident with ASR unconditionally.
        close_embedder()
        env={**os.environ,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','TOKENIZERS_PARALLELISM':'false','PYTHONPATH':str(ROOT)}
        process=subprocess.run([sys.executable,'-m','qwen_asr_app.knowledge.reranking','worker'],
                                   input=json.dumps(payload,ensure_ascii=False), text=True,
                                   capture_output=True, timeout=timeout, cwd=ROOT, env=env)
        if process.returncode:
            raise RuntimeError('本地重排未完成，当前保留排名融合结果')
        response=json.loads(process.stdout)
        if response.get('contract') != CONTRACT or len(response.get('scores',[])) != len(selected):
            raise RuntimeError('重排回执的任务或模型契约不一致')
        ranked=[{**row,'rerank_score':score} for row,score in zip(selected,response['scores'])]
        ranked.sort(key=lambda x:-x['rerank_score'])
        return ranked+results[8:], {k:v for k,v in response.items() if k!='scores'}


def worker():
    import time
    import resource
    request=json.load(sys.stdin)
    output=sys.stdout
    sys.stdout=sys.stderr
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    torch.set_num_threads(2)
    started=time.monotonic()
    tok=AutoTokenizer.from_pretrained(request['folder'],padding_side='left',local_files_only=True)
    with metal_lease(timeout=180):
        model=AutoModelForCausalLM.from_pretrained(request['folder'],local_files_only=True,dtype=torch.float16,attn_implementation='sdpa').to(request['device']).eval()
    load_seconds=time.monotonic()-started
    prefix='<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
    suffix='<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
    prefix_ids=tok.encode(prefix,add_special_tokens=False)
    suffix_ids=tok.encode(suffix,add_special_tokens=False)
    task='Given a question about a university course, retrieve passages which provide relevant evidence. A similar topic alone does not imply the passage answers the question.'
    queries=request.get('queries') or [{'query':request['query'],'documents':request['documents']}]
    measured=[]
    for row in queries:
        scores=[];start=time.monotonic()
        for offset in range(0,len(row['documents']),2):
            ids=[]
            for doc in row['documents'][offset:offset+2]:
                text=f"<Instruct>: {task}\n<Query>: {row['query']}\n<Document>: {doc}"
                tokens=prefix_ids+tok.encode(text,add_special_tokens=False)+suffix_ids
                if len(tokens)>CONTRACT['max_length']:
                    raise ValueError('候选超过重排输入预算，未截断原始证据')
                ids.append(tokens)
            inputs=tok.pad({'input_ids':ids},padding=True,return_tensors='pt').to(request['device'])
            with metal_lease(timeout=180), torch.inference_mode():
                # Calculate only the final-token vocabulary projection.
                states=model.model(**inputs).last_hidden_state[:,-1,:]
                logits=model.lm_head(states).float()
                no=logits[:,tok.convert_tokens_to_ids('no')]
                yes=logits[:,tok.convert_tokens_to_ids('yes')]
                scores.extend((yes-no).cpu().tolist())
        measured.append({'scores':scores,'seconds':time.monotonic()-start})
    report={'contract':CONTRACT,'load_seconds':load_seconds,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'metal_driver_bytes':torch.mps.driver_allocated_memory() if request['device']=='mps' else 0}
    if request.get('queries'):
        report['results']=measured
    else:
        report.update(measured[0])
    output.write(json.dumps(report)+'\n');output.flush()


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='prepare':
        from huggingface_hub import snapshot_download
        from ..durable import atomic_json
        cfg=knowledge_settings.read()
        target=Path(cfg['model_root'])/'Qwen3-Reranker-4B'
        snapshot_download(MODEL_ID,revision=REVISION,local_dir=target,allow_patterns=['*.json','*.safetensors','*.txt','*.model','*.jinja'],max_workers=2)
        atomic_json(target/'qwenasr-model-identity.json',{'model_id':MODEL_ID,'revision':REVISION,'quantization':'none'})
        print(str(target))
    else:
        worker()
