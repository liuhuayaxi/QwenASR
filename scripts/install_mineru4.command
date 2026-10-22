#!/bin/bash
set -euo pipefail

PRIVATE_DIR="${QWEN_ASR_PRIVATE_DIR:-$(cd "$(dirname "$0")/.." && pwd)/local-data}"
RUNTIME_DIR="$PRIVATE_DIR/runtimes/mineru4"
MODEL_HOME="$PRIVATE_DIR/mineru4"
UV="$(command -v uv || true)"
if [ -z "$UV" ] && [ -x /opt/homebrew/bin/uv ]; then UV=/opt/homebrew/bin/uv; fi
if [ -z "$UV" ]; then echo '请先安装 uv：https://docs.astral.sh/uv/'; exit 1; fi
if [ ! -x "$RUNTIME_DIR/bin/python" ]; then "$UV" venv --python 3.12 "$RUNTIME_DIR"; fi
"$UV" pip install --python "$RUNTIME_DIR/bin/python" 'mineru==4.0.10'
MINERU_HOME="$MODEL_HOME" MINERU_MODEL_SMALL_BACKEND=auto MINERU_MODEL_VLM_ENGINE=llama-cpp \
  "$RUNTIME_DIR/bin/mineru-kit" models download --tier standard --source modelscope
MINERU_HOME="$MODEL_HOME" MINERU_MODEL_SMALL_BACKEND=auto MINERU_MODEL_VLM_ENGINE=llama-cpp \
  "$RUNTIME_DIR/bin/mineru-kit" models verify --tier standard
echo 'MinerU 4.0.10 已就绪。打开工作台的设置 → MinerU 文件识别，选择质量档位与参数。'
