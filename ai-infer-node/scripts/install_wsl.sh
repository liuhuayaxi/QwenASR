#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
NODE_ROOT="$PWD"
umask 077
mkdir -p private venvs models mineru-home data
if [[ ! -x bootstrap/bin/uv ]]; then
  python3 -m venv bootstrap
  bootstrap/bin/python -m pip install 'uv==0.12.23'
fi
NODE_UV="$NODE_ROOT/bootstrap/bin/uv"
[[ -f config.toml ]] || cp config.example.toml config.toml
[[ -f private/token ]] || python3 -c 'import secrets;from pathlib import Path;Path("private/token").write_text(secrets.token_urlsafe(48)+"\n")'
chmod 600 private/token config.toml
for environment in gateway mineru asr; do
  [[ -x "venvs/$environment/bin/python" ]] || "$NODE_UV" venv "venvs/$environment" --python python3
done
if [[ -f requirements-gateway.lock ]]; then
  "$NODE_UV" pip install --python venvs/gateway/bin/python -r requirements-gateway.lock
else
  "$NODE_UV" pip install --python venvs/gateway/bin/python -r requirements-gateway.txt
fi
if [[ -f requirements-mineru.lock ]]; then
  "$NODE_UV" pip install --python venvs/mineru/bin/python -r requirements-mineru.lock
else
  "$NODE_UV" pip install --python venvs/mineru/bin/python -c constraints-mineru.txt 'mineru[full]==4.0.10' 'vllm==0.28.0' 'torch==2.13.0' 'nvidia-cuda-nvcc==13.0.88'
fi
venvs/mineru/bin/python scripts/configure_cuda_toolkit.py
if [[ -f requirements-asr.lock ]]; then
  "$NODE_UV" pip install --python venvs/asr/bin/python -r requirements-asr.lock
else
  "$NODE_UV" pip install --python venvs/asr/bin/python 'transformers==5.19.0' 'torch==2.14.1' accelerate soundfile librosa huggingface_hub
fi
command -v ffmpeg >/dev/null || { echo 'Install ffmpeg with the WSL administrator, then rerun.' >&2; exit 1; }
MINERU_HOME="$NODE_ROOT/mineru-home" venvs/mineru/bin/mineru-kit models download --tier standard --small-backend torch --vlm-engine vllm --source modelscope
MINERU_HOME="$NODE_ROOT/mineru-home" venvs/mineru/bin/mineru-kit models verify --tier standard --small-backend torch --vlm-engine vllm
venvs/asr/bin/python scripts/download_asr.py
for environment in gateway mineru asr; do
  "$NODE_UV" pip freeze --python "venvs/$environment/bin/python" > "requirements-$environment.lock"
done
echo 'Runtimes and local weights ready. Install systemd on WSL, then install_tailnet_serve.ps1 on Windows for the stable native Tailnet entry.'
