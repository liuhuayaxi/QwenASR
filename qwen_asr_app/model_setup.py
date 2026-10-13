"""Shared launch/runtime model policy; downloads are an explicit operation."""
import argparse
import os
from pathlib import Path

from .paths import ROOT
from .inference_providers import inference_settings


def model_path():
    path = Path(os.getenv('QWEN_ASR_MODEL_PATH', ROOT / 'models/Qwen3-ASR-1.7B-MLX-bf16')).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


def launch_plan():
    mode = inference_settings.for_task('asr')['asr_provider']
    path = model_path()
    return {'mode': mode, 'path': path, 'local_required': mode != 'remote_only',
            'ready': (path / 'model.safetensors').is_file() or
                     (path / 'model.safetensors.index.json').is_file()}


def requirements_path():
    return ROOT / ('requirements-core.lock' if launch_plan()['mode'] == 'remote_only' else 'requirements.lock')


def prepare_model(download=None):
    plan = launch_plan()
    if not plan['local_required'] or plan['ready']:
        return plan
    if download is None:
        from huggingface_hub import snapshot_download
        download = snapshot_download
    download(repo_id='mlx-community/Qwen3-ASR-1.7B-bf16', local_dir=str(plan['path']), max_workers=2)
    return launch_plan()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['check', 'prepare', 'requirements'])
    args = parser.parse_args()
    if not os.getenv('QWEN_ASR_DATA_DIR'):
        try:
            from dotenv import load_dotenv
        except ImportError:
            pass  # dependency selection also runs inside a fresh venv
        else:
            load_dotenv(ROOT / '.env')
    if args.action == 'requirements':
        print(requirements_path())
        return
    plan = prepare_model() if args.action == 'prepare' else launch_plan()
    if not plan['local_required']:
        print('仅远程 ASR：无需 Mac 本地模型，直接启动网页服务。')
    elif plan['ready']:
        print(f"本地模型已准备：{plan['path']}；实际转写时加载。")
    else:
        print('网页服务可以启动；本地转写前请运行 .venv/bin/python -m qwen_asr_app.model_setup prepare。')


if __name__ == '__main__':
    main()
