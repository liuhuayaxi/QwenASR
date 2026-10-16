"""Resident CUDA ASR; Windows CPU embedding avoids concurrent GPU model loads."""
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import time

QUERY_INSTRUCTION='Given a question about a Chinese university course, retrieve relevant passages that answer the question, including English terminology and mathematical definitions.'
EMBEDDING_REVISIONS={'Qwen/Qwen3-Embedding-0.6B':('97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3',1024),
    'Qwen/Qwen3-Embedding-4B':('5cf2132abc99cad020ac570b19d031efec650f2b',2560)}
RERANK_CONTRACT={'model_id':'Qwen/Qwen3-Reranker-4B','revision':'22e683669bc0f0bd69640a1354a6d0aebcfeede5',
    'weights':'official_unquantized','dtype':'float16','scoring':'yes_no_logits_v1','max_length':2048,'batch_size':2}


def asr_token_limit(value):
    # Match the durable file worker's explicit ceiling. Never silently reduce
    # a saved 4096/8192-token request when it uses the resident model instead.
    if type(value) is not int or not 128 <= value <= 8192:
        raise ValueError('INVALID_ASR_TOKEN_LIMIT')
    return value


def verify_contract(contract):
    spec=EMBEDDING_REVISIONS.get(contract.get('model_id'))
    if not spec or type(contract.get('max_length')) is not int or not 512<=contract['max_length']<=8192:raise ValueError('EMBEDDING_CONTRACT_MISMATCH')
    expected={'model_id':contract['model_id'],'revision':spec[0],'dimension':spec[1],
        'pooling':'last_non_padding_token','normalization':'l2_float32','query_instruction':QUERY_INSTRUCTION,
        'document_instruction':'','max_length':contract['max_length'],'weights':'official_unquantized',
        'compute_dtype':'float16','implementation':'qwenasr-transformers-v1'}
    if contract!=expected:raise ValueError('EMBEDDING_CONTRACT_MISMATCH')


def rerank(request, root):
    import torch
    from transformers import AutoTokenizer,AutoModelForCausalLM
    if request['contract']!=RERANK_CONTRACT:raise ValueError('RERANK_CONTRACT_MISMATCH')
    folder=root/'Qwen3-Reranker-4B'
    identity=json.loads((folder/'qwenasr-model-identity.json').read_text())
    if identity.get('model_id')!=RERANK_CONTRACT['model_id'] or identity.get('revision')!=RERANK_CONTRACT['revision']:raise ValueError('RERANK_NOT_PREPARED')
    if len(request['documents'])>8:raise ValueError('RERANK_BATCH_TOO_LARGE')
    available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
    if available<12*1024**3:raise ValueError('RERANK_MEMORY_BUDGET_INSUFFICIENT')
    tok=AutoTokenizer.from_pretrained(folder,padding_side='left',local_files_only=True)
    model=AutoModelForCausalLM.from_pretrained(folder,dtype=torch.float16,local_files_only=True,attn_implementation='sdpa').to('cpu').eval()
    prefix='<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
    suffix='<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
    task='Given a question about a university course, retrieve passages which provide relevant evidence. A similar topic alone does not imply the passage answers the question.'
    scores=[]
    for doc in request['documents']:
        ids=tok.encode(prefix,add_special_tokens=False)+tok.encode(f"<Instruct>: {task}\n<Query>: {request['query']}\n<Document>: {doc}",add_special_tokens=False)+tok.encode(suffix,add_special_tokens=False)
        if len(ids)>2048:raise ValueError('RERANK_INPUT_TOO_LONG')
        batch=tok.pad({'input_ids':[ids]},padding=True,return_tensors='pt')
        with torch.inference_mode():
            logits=model.lm_head(model.model(**batch).last_hidden_state[:,-1,:]).float()
            scores.extend((logits[:,tok.convert_tokens_to_ids('yes')]-logits[:,tok.convert_tokens_to_ids('no')]).tolist())
    del model
    return {'contract':RERANK_CONTRACT,'scores':scores,'device':'Windows CPU','dtype':'float16'}


def main():
    output=sys.stdout;sys.stdout=sys.stderr
    import torch
    torch.set_num_threads(4)
    asr_path=Path(sys.argv[1]).expanduser()
    model_root=Path(sys.argv[2]).expanduser()
    asr=None;processor=None;embedding=None;tokenizer=None;loaded_contract=None
    for line in sys.stdin:
        request=json.loads(line)
        if request.get('type')=='stop':break
        started=time.monotonic();cold=False
        try:
            if request['kind']=='asr':
                limit=asr_token_limit(request.get('max_new_tokens',512))
                import soundfile as sf
                from transformers import AutoProcessor,AutoModelForMultimodalLM
                if asr is None:
                    cold=True
                    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():raise ValueError('CUDA_BF16_REQUIRED')
                    if torch.cuda.mem_get_info()[0]<5*1024**3:raise ValueError('CLASSROOM_GPU_MEMORY_BUDGET_WAIT')
                    processor=AutoProcessor.from_pretrained(asr_path,local_files_only=True)
                    asr=AutoModelForMultimodalLM.from_pretrained(asr_path,dtype=torch.bfloat16,device_map={'':'cuda:0'},local_files_only=True,attn_implementation='sdpa').eval()
                audio,sr=sf.read(io.BytesIO(base64.b64decode(request['audio'],validate=True)),dtype='float32')
                if sr!=16000 or audio.ndim!=1 or not 0<len(audio)<=16000*90:raise ValueError('INVALID_CLASSROOM_AUDIO')
                inputs=processor.apply_transcription_request(audio=audio,sampling_rate=16000,
                    language=request.get('language') or None,prompt=request.get('prompt') or None).to(asr.device,asr.dtype)
                with torch.inference_mode():ids=asr.generate(**inputs,max_new_tokens=limit,do_sample=False)[:,inputs['input_ids'].shape[1]:]
                eos=asr.generation_config.eos_token_id;eos=eos if isinstance(eos,list) else [eos]
                if ids.shape[1]>=limit and int(ids[0,-1]) not in eos:raise ValueError('ASR_TOKEN_LIMIT')
                value=processor.decode(ids,return_format='parsed')[0]
                result={'text':value['transcription'].strip(),'language':value.get('language',''),'duration':len(audio)/sr,
                    'device':torch.cuda.get_device_name(0),'dtype':'bfloat16','cold_start':cold,'pid':__import__('os').getpid()}
            elif request['kind']=='rerank':
                result=rerank(request,model_root)
            else:
                from transformers import AutoModel,AutoTokenizer
                contract=request['contract'];folder=model_root/contract['model_id'].split('/')[-1]
                verify_contract(contract)
                identity=json.loads((folder/'qwenasr-model-identity.json').read_text())
                if identity.get('model_id')!=contract['model_id'] or identity.get('revision')!=contract['revision']:raise ValueError('EMBEDDING_CONTRACT_MISMATCH')
                if contract.get('pooling')!='last_non_padding_token' or contract.get('normalization')!='l2_float32' or contract.get('compute_dtype')!='float16':raise ValueError('UNSUPPORTED_EMBEDDING_CONTRACT')
                if loaded_contract!=contract:
                    cold=True;embedding=None
                    tokenizer=AutoTokenizer.from_pretrained(folder,padding_side='left',local_files_only=True)
                    embedding=AutoModel.from_pretrained(folder,dtype=torch.float16,local_files_only=True,attn_implementation='sdpa').to('cpu').eval();loaded_contract=contract
                texts=request['texts']
                if not isinstance(texts,list) or not 1<=len(texts)<=4 or any(not isinstance(t,str) for t in texts):raise ValueError('INVALID_BATCH')
                if request.get('query'):texts=[f"Instruct: {contract['query_instruction']}\nQuery:{t}" for t in texts]
                batch=tokenizer(texts,padding=True,truncation=False,return_tensors='pt')
                if batch['input_ids'].shape[1]>contract['max_length']:raise ValueError('EMBEDDING_INPUT_TOO_LONG')
                with torch.inference_mode():vectors=torch.nn.functional.normalize(embedding(**batch).last_hidden_state[:,-1].float(),p=2,dim=1).tolist()
                result={'vectors':vectors,'contract':contract,'device':'Windows CPU','dtype':'float16','cold_start':cold,'pid':__import__('os').getpid()}
            result.update(seconds=time.monotonic()-started,operation_id=request['operation_id'])
        except Exception as exc:
            # No transcripts, prompts, credentials or library paths in diagnostics.
            result={'operation_id':request.get('operation_id'),'error':str(exc) if isinstance(exc,ValueError) else type(exc).__name__}
        output.write(json.dumps(result,ensure_ascii=False)+'\n');output.flush()


if __name__=='__main__':main()
