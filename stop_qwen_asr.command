#!/bin/bash
set -euo pipefail
COURSE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$COURSE_DIR"
if ! "$COURSE_DIR/.venv/bin/python" -m qwen_asr_app.service_control stop; then
  if [ -t 0 ]; then read -r -p "按回车关闭窗口…" _; fi
  exit 1
fi
