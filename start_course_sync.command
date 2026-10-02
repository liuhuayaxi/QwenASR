#!/bin/bash
set -euo pipefail
COURSE_DIR="$(cd "$(dirname "$0")" && pwd)"
# Compatibility entry point for older bookmarks. Qwen3-ASR is one service on
# one port now; the canonical launcher is start_qwen_asr.command.
WTP_NO_OPEN=1 "$COURSE_DIR/start_qwen_asr.command"
PORT="${QWEN_ASR_PORT:-8765}"
if [ "${WTP_NO_OPEN:-0}" != "1" ]; then
  open "http://127.0.0.1:${PORT}/course/"
fi
echo "课程回放已挂载在 Qwen3-ASR 的 ${PORT} 端口。"
