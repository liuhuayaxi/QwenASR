#!/bin/bash
set -euo pipefail
COURSE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$COURSE_DIR"
COURSE_PYTHON="$COURSE_DIR/.venv/bin/python"
if [ ! -x "$COURSE_PYTHON" ]; then
  echo "请先运行 start_qwen_asr.command 完成首次安装。"
  exit 1
fi
if ! "$COURSE_PYTHON" -c 'import cryptography' >/dev/null 2>&1; then
  /opt/homebrew/bin/uv pip install --python "$COURSE_PYTHON" 'cryptography>=44,<48'
fi
if ! curl -fsS http://127.0.0.1:8765/api/health >/dev/null 2>&1; then
  nohup "$COURSE_PYTHON" -m uvicorn app:app --host 127.0.0.1 --port 8765 >> "$COURSE_DIR/server.log" 2>&1 &
fi
if ! curl -fsS http://127.0.0.1:8766/api/status >/dev/null 2>&1; then
  nohup "$COURSE_PYTHON" -m qwen_asr_app.smartclass_service >> "$COURSE_DIR/course-sync.log" 2>&1 &
fi
for attempt in {1..30}; do
  if curl -fsS http://127.0.0.1:8766/api/status >/dev/null 2>&1; then
    open http://127.0.0.1:8766/
    echo "课程回放服务已启动。关闭此窗口不会中断已提交的任务。"
    exit 0
  fi
  sleep 1
done
echo "启动未完成，请查看 course-sync.log。"
exit 1
