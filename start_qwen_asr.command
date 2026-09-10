#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${QWEN_ASR_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
MODEL_DIR="$SCRIPT_DIR/models/Qwen3-ASR-1.7B-MLX-bf16"
SERVER_PID=""

say_line() {
  printf '\n  %s\n' "$1"
}

finish() {
  code=$?
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
  fi
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

start_course_companion() {
  if [ -x "$PYTHON" ] && "$PYTHON" -c 'import cryptography' >/dev/null 2>&1 && ! curl -fsS http://127.0.0.1:8766/api/status >/dev/null 2>&1; then
    nohup "$PYTHON" -m qwen_asr_app.smartclass_service >> "$SCRIPT_DIR/course-sync.log" 2>&1 &
  fi
}

if curl -fsS "$URL/api/health" >/dev/null 2>&1; then
  start_course_companion
  say_line "服务已经在运行，正在打开网页…"
  open "$URL"
  trap - EXIT
  exit 0
fi

if [ ! -x "$PYTHON" ] || ! "$PYTHON" -c "import mlx_audio, fastapi, uvicorn, av, multipart, mcp, httpx, cryptography" >/dev/null 2>&1; then
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
  "$UV" pip install --python "$PYTHON" -r requirements.txt
fi

start_course_companion

if [ ! -f "$MODEL_DIR/model.safetensors" ]; then
  say_line "首次运行：正在下载适配 Apple Silicon 的 Qwen3-ASR-1.7B BF16 模型（约 4GB）…"
  mkdir -p "$MODEL_DIR"
  HF_HUB_DOWNLOAD_TIMEOUT=120 HF_HUB_ETAG_TIMEOUT=60 "$SCRIPT_DIR/.venv/bin/hf" download \
    mlx-community/Qwen3-ASR-1.7B-bf16 --local-dir "$MODEL_DIR" --max-workers 2
fi

say_line "正在启动本地服务；模型将在点击转写时按需加载…"
"$PYTHON" -m uvicorn app:app --host 127.0.0.1 --port "$PORT" > server.log 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 180); do
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    echo "服务意外退出："
    tail -n 80 server.log
    exit 1
  fi
  HEALTH="$(curl -fsS "$URL/api/health" 2>/dev/null || true)"
  if printf '%s' "$HEALTH" | grep -Eq '"status":"(ready|idle)"'; then
    say_line "服务已就绪，正在打开网页…"
    open "$URL"
    echo ""
    echo "服务运行中：$URL"
    echo "关闭此终端窗口即可停止服务。"
    wait "$SERVER_PID"
    exit 0
  fi
  if printf '%s' "$HEALTH" | grep -q '"status":"error"'; then
    echo "模型加载失败："
    tail -n 80 server.log
    exit 1
  fi
  sleep 1
done

echo "等待模型加载超时。最近日志："
tail -n 80 server.log
exit 1
