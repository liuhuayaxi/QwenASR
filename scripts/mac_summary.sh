#!/bin/bash
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1
export QWEN_ASR_TESTING=1
REPO=${VGA_SUMMARY_REPO:-"$HOME/AIcreatedapplication/QwenASR"}
PY=${VGA_SUMMARY_PYTHON:-"$REPO/.venv/bin/python"}
ROOT=${VGA_SUMMARY_ROOT:-/tmp/vga_check2}
BRANCH=devin/1795234850-vga-filter-emphasis
fail() { echo "错误：$*" >&2; exit 1; }
[ -x "$PY" ] || fail "找不到 Python：${PY}。"
WORK=$("$PY" - <<'PY'
from pathlib import Path
paths = [p for p in Path('/tmp').glob('qwen-vga-worktree.*') if (p / '.git').is_file()]
if paths:
    print(max(paths, key=lambda p: p.stat().st_mtime))
PY
)
[ -n "$WORK" ] || fail "没有找到 /tmp/qwen-vga-worktree.* 临时工作树。"
COMMON=$(git -C "$WORK" rev-parse --path-format=absolute --git-common-dir)
EXPECTED=$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir)
[ "$COMMON" = "$EXPECTED" ] || fail "临时工作树不属于指定仓库：${WORK}。"
[ -z "$(git -C "$WORK" status --porcelain)" ] || fail "临时工作树有未提交改动，不会切换或删除：${WORK}。"
cleanup() {
  status=$?
  trap - EXIT
  if ! git -C "$REPO" worktree remove "$WORK"; then
    echo "错误：无法删除临时工作树：${WORK}。" >&2
    status=1
  else
    echo "已删除临时工作树：${WORK}。"
  fi
  exit "$status"
}
trap cleanup EXIT
git -C "$WORK" fetch --quiet origin "$BRANCH" || fail "拉取分支失败，请检查 GitHub 权限和网络。"
git -C "$WORK" checkout --quiet --detach FETCH_HEAD
echo "重算版本：$(git -C "$WORK" rev-parse --short HEAD)"
(
cd "$WORK"
"$PY" - "$ROOT" <<'PY'
from collections import deque
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from qwen_asr_app.vga_emphasis import load_transcript, mark_emphasis

try:
    root = Path(sys.argv[1])
    manifests = list(root.glob('artifacts.*/manifest.json'))
    if not manifests:
        raise ValueError('没有找到 artifacts.*/manifest.json')
    manifest_path = max(manifests, key=lambda p: p.stat().st_mtime)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    runtime = root / ('runtime-' + manifest_path.parent.name)
    candidates = list(runtime.rglob('transcript.json'))
    recording_id = manifest.get('recording_id')
    if recording_id:
        matching = []
        for path in candidates:
            session = path.with_name('session.json')
            if session.is_file():
                metadata = json.loads(session.read_text(encoding='utf-8'))
                if metadata.get('id') == recording_id:
                    matching.append(path)
        candidates = matching
    if len(candidates) != 1:
        raise ValueError(f'对应转写必须唯一，实际找到 {len(candidates)} 个；不会猜测或重新转写')
    transcript = candidates[0]
    saved_pages = manifest['pages']
    pages = [SimpleNamespace(occurrences=p.get('occurrences') or [
        {'start_sec': p['start_sec'], 'end_sec': p['end_sec']}]) for p in saved_pages]
    mark_emphasis(SimpleNamespace(pages=pages), load_transcript(transcript))
    lines = [f'清单：{manifest_path}', f'转写：{transcript}',
             f'旧重点页数：{sum(bool(p.get("important")) for p in saved_pages)} / {len(pages)}',
             f'新重点页数：{sum(p.important for p in pages)} / {len(pages)}']
    for i, (old, new) in enumerate(zip(saved_pages, pages), 1):
        windows = ','.join(f'{w["start_sec"]:.2f}-{w["end_sec"]:.2f}' for w in new.occurrences)
        snippets = '；'.join(s['text'] for s in new.matched_snippets)
        snippets = ' '.join(snippets.split())[:60]
        lines.append(f'{i:02} 源页={old.get("source_index", old.get("index", i))} '
                     f'kind={old.get("kind", "slide")} 时间窗=[{windows}] '
                     f'旧={bool(old.get("important"))} 新={new.important} '
                     f'score={new.emphasis_score:.4f} 命中={snippets}')
    lines.append('run.log 最后 20 行：')
    log = manifest_path.with_name('run.log')
    if log.is_file():
        with log.open(encoding='utf-8', errors='replace') as stream:
            lines.extend(line.rstrip() for line in deque(stream, maxlen=20))
    else:
        lines.append('未找到 run.log')
    output = '\n'.join(lines).encode('utf-8')
    if len(output) > 58000:
        output = output[:58000].decode('utf-8', errors='ignore').encode('utf-8')
        output += '\n（输出已截断，限制为 60KB 以内）'.encode('utf-8')
    sys.stdout.buffer.write(output + b'\n')
except (OSError, ValueError, KeyError, TypeError) as exc:
    print(f'错误：{exc}', file=sys.stderr)
    sys.exit(1)
PY
)
