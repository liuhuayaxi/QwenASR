"""Explicit operator provisioning only; classroom requests never download models."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workers.classroom_worker import EMBEDDING_REVISIONS,RERANK_CONTRACT


def main():
    models={key:value[0] for key,value in EMBEDDING_REVISIONS.items()}
    models[RERANK_CONTRACT['model_id']]=RERANK_CONTRACT['revision']
    parser=argparse.ArgumentParser(description='在 Windows WSL 中提前准备课程向量/重排模型')
    parser.add_argument('--model',choices=models,default='Qwen/Qwen3-Embedding-0.6B')
    parser.add_argument('--root',type=Path,default=Path.home()/'ai-infer-node/models/knowledge')
    args=parser.parse_args();folder=args.root.expanduser()/args.model.split('/')[-1]
    from huggingface_hub import snapshot_download
    snapshot_download(args.model,revision=models[args.model],local_dir=folder,max_workers=2,
        allow_patterns=['*.json','*.safetensors','*.txt','*.model','*.jinja'])
    identity={'model_id':args.model,'revision':models[args.model],'quantization':'none'}
    temporary=folder/'qwenasr-model-identity.json.part';temporary.write_text(json.dumps(identity));os.replace(temporary,folder/'qwenasr-model-identity.json')
    manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file() and p.suffix in {'.json','.safetensors','.txt','.model','.jinja'}}
    temporary=folder/'provisioned-manifest.json.part';temporary.write_text(json.dumps({'identity':identity,'sha256':manifest},sort_keys=True,indent=2));os.replace(temporary,folder/'provisioned-manifest.json')
    print(json.dumps({'prepared':True,**identity,'files':len(manifest)},ensure_ascii=False))


if __name__=='__main__':main()
