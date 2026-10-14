#!/bin/zsh
set -e
QWEN_CLASSROOM_DIR="${0:A:h}"
/Users/liuhuayaxi/AIcreatedapplication/QwenASR/.venv/bin/python "$QWEN_CLASSROOM_DIR/scripts/run_classroom_candidate.py" "$@"
