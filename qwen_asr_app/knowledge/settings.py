"""Durable local retrieval configuration and pinned, reproducible models."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..durable import atomic_json
from ..paths import PRIVATE_ROOT, ROOT

MODELS = {
    'Qwen/Qwen3-Embedding-4B': {
        'revision': '5cf2132abc99cad020ac570b19d031efec650f2b', 'dimension': 2560},
    'Qwen/Qwen3-Embedding-0.6B': {
        'revision': '97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3', 'dimension': 1024},
}
QUERY_INSTRUCTION = 'Given a question about a Chinese university course, retrieve relevant passages that answer the question, including English terminology and mathematical definitions.'
DEFAULTS = {'model_id': 'Qwen/Qwen3-Embedding-4B', 'device': 'mps',
            'model_root': str(Path(os.getenv('QWEN_ASR_KNOWLEDGE_MODEL_ROOT', ROOT / 'models' / 'knowledge'))),
            'batch_size': 2, 'max_length': 2048, 'idle_seconds': 30,
            'timeout_seconds': 180, 'reranker': 'none'}


class KnowledgeSettings:
    def __init__(self, path=None):
        self.path = Path(path or PRIVATE_ROOT / 'knowledge-settings.json')

    def read(self):
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            raw = {}
        return self.validate(raw)

    @staticmethod
    def validate(raw):
        if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
            raise ValueError('知识检索设置格式无效')
        cfg = {**DEFAULTS, **raw}
        if cfg['model_id'] not in MODELS:
            raise ValueError('请选择已核验的 Qwen3 Embedding 模型')
        if cfg['device'] not in {'mps', 'cpu'}:
            raise ValueError('本地知识检索支持 Apple GPU 或 CPU')
        if cfg['reranker'] not in {'none', 'qwen3-4b'}:
            raise ValueError('重排策略无效')
        for key, low, high in [('batch_size', 1, 4), ('max_length', 512, 8192),
                               ('idle_seconds', 5, 300), ('timeout_seconds', 30, 600)]:
            if type(cfg[key]) is not int or not low <= cfg[key] <= high:
                raise ValueError(f'{key} 不在允许范围内')
        root = Path(str(cfg['model_root'])).expanduser()
        if not root.is_absolute():
            raise ValueError('模型目录必须为完整路径')
        cfg['model_root'] = str(root.resolve())
        return cfg

    def save(self, raw):
        cfg = self.validate({**self.read(), **raw})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.path, cfg)
        self.path.chmod(0o600)
        return cfg


knowledge_settings = KnowledgeSettings()


def model_directory(cfg):
    return Path(cfg['model_root']) / cfg['model_id'].split('/')[-1]


def embedding_contract(cfg):
    spec = MODELS[cfg['model_id']]
    return {'model_id': cfg['model_id'], 'revision': spec['revision'],
            'dimension': spec['dimension'], 'pooling': 'last_non_padding_token',
            'normalization': 'l2_float32', 'query_instruction': QUERY_INSTRUCTION,
            'document_instruction': '', 'max_length': cfg['max_length'],
            'weights': 'official_unquantized', 'compute_dtype': 'float16',
            'implementation': 'qwenasr-transformers-v1'}


def prepared(cfg):
    folder = model_directory(cfg)
    try:
        identity = json.loads((folder / 'qwenasr-model-identity.json').read_text())
    except (OSError, ValueError):
        return False
    spec = MODELS[cfg['model_id']]
    return (identity.get('model_id') == cfg['model_id']
            and identity.get('revision') == spec['revision']
            and identity.get('quantization') == 'none'
            and (folder / 'config.json').is_file()
            and any(folder.glob('*.safetensors')))


def prepare_models(model_id, root=None):
    """Explicit preparation only. Capture/search never starts a download."""
    from huggingface_hub import snapshot_download
    cfg = KnowledgeSettings.validate({'model_id': model_id, **({'model_root': str(root)} if root else {})})
    folder = model_directory(cfg)
    snapshot_download(model_id, revision=MODELS[model_id]['revision'], local_dir=folder,
                      allow_patterns=['*.json', '*.safetensors', '*.txt', '*.model', '*.jinja'], max_workers=3)
    atomic_json(folder / 'qwenasr-model-identity.json',
                {'model_id': model_id, 'revision': MODELS[model_id]['revision'], 'quantization': 'none'})
    return folder


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='准备 Mac 本地课程知识模型')
    parser.add_argument('action', choices=['prepare', 'status'])
    parser.add_argument('--model', choices=MODELS, default=DEFAULTS['model_id'])
    parser.add_argument('--root')
    args = parser.parse_args()
    if args.action == 'prepare':
        print(prepare_models(args.model, args.root))
    else:
        cfg = knowledge_settings.read()
        print(json.dumps({'prepared': prepared(cfg), 'contract': embedding_contract(cfg)}, ensure_ascii=False, indent=2))
