#!/bin/zsh
set -e
QWEN_CLASSROOM_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
/Users/liuhuayaxi/AIcreatedapplication/QwenASR/.venv/bin/python "$QWEN_CLASSROOM_DIR/scripts/run_classroom_candidate.py"
