#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${QWEN_ASR_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
SERVER_PID=""

say_line() {
  printf '\n  %s\n' "$1"
}

finish() {
  code=$?
  if [ "$code" -ne 0 ]; then
    printf '\n启动失败（错误码 %s）。请保留本窗口中的信息。\n' "$code"
    if [ -t 0 ]; then
      read -r -p "按回车键关闭窗口…" _
    fi
  fi
}
trap finish EXIT
trap 'exit 0' INT TERM HUP

printf '\033c'
printf '╭──────────────────────────────────────╮\n'
printf '│       Qwen3-ASR 本地语音工作台       │\n'
printf '╰──────────────────────────────────────╯\n'

if [ ! -x "$PYTHON" ] || ! "$PYTHON" "$SCRIPT_DIR/scripts/check_environment.py" >/dev/null 2>&1; then
  say_line "首次运行：正在创建独立 Python 环境…"
  if [ -x /opt/homebrew/bin/uv ]; then
    UV=/opt/homebrew/bin/uv
  elif command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
  else
    echo "未找到 uv。请先安装：https://docs.astral.sh/uv/"
    exit 1
  fi
  if [ ! -x "$PYTHON" ]; then
    "$UV" venv --python 3.12 .venv
  fi
  REQUIREMENTS="$("$PYTHON" -m qwen_asr_app.model_setup requirements)"
  "$UV" pip install --python "$PYTHON" -r "$REQUIREMENTS"
fi


"$PYTHON" -m qwen_asr_app.model_setup check

say_line "正在启动本地服务；模型将在点击转写时按需加载…"
"$PYTHON" -m qwen_asr_app.service_control start
if [ "${WTP_NO_OPEN:-0}" != "1" ]; then
  open "$URL"
fi
say_line "关闭本窗口不会中断录音或课程任务。停止服务请运行 stop_qwen_asr.command。"
