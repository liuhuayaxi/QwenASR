"""Run this worktree with its own empty data root and loopback port."""
import argparse
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', type=int, default=8876)
args = parser.parse_args()
if not 1024 <= args.port <= 65535:
    parser.error('port must be between 1024 and 65535')
preview = root / '.workspace-preview'
preview.mkdir(mode=0o700, exist_ok=True)
# Paths and credentials from a shell must not select the formal installation.
for name in tuple(os.environ):
    if ((name.startswith('AI_') or name.startswith('OPENAI_')) and ('KEY' in name or 'TOKEN' in name)) or name in {
        'QWEN_ASR_STORAGE_ROOT', 'QWEN_ASR_SETTINGS_PATH', 'QWEN_ASR_LEGACY_RECORDINGS_DIR',
    }:
        os.environ.pop(name, None)
os.environ['QWEN_ASR_DATA_DIR'] = str(preview)
os.environ['QWEN_ASR_PORT'] = str(args.port)
os.environ['QWEN_ASR_INSTANCE'] = 'course-workspace-preview'
os.chdir(root)
sys.path.insert(0,str(root))
print(f'Course workspace preview: http://127.0.0.1:{args.port}/workspace', flush=True)
print(f'Isolated data: {preview}', flush=True)
import uvicorn
uvicorn.run('app:app', host='127.0.0.1',port=args.port,access_log=False)
