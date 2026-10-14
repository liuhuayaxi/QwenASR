"""Start the reviewed worktree against existing Mac data; keep original code intact."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import signal
import urllib.request
from pathlib import Path

WORKTREE=Path(__file__).resolve().parent.parent
ORIGINAL=Path('/Users/liuhuayaxi/AIcreatedapplication/QwenASR')
BASE='http://127.0.0.1:8765'


def read(path):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(BASE+path,timeout=3) as response:
            return json.load(response)
    except (OSError,ValueError):
        return None


def main():
    interpreter=ORIGINAL/'.venv/bin/python'
    if not interpreter.is_file():
        raise SystemExit('请先准备原 QwenASR 的 Python 环境。原程序未改变。')
    if sys.argv[1:]==['--check']:
        paths={'Python 环境':interpreter,'新版程序':WORKTREE/'app.py',
               '原资料配置':ORIGINAL/'app-settings.json',
               'Mac ASR 模型':ORIGINAL/'models/Qwen3-ASR-1.7B-MLX-bf16',
               '课程向量模型':ORIGINAL/'models/knowledge'}
        checks={name:path.exists() for name,path in paths.items()}
        print(json.dumps(checks,ensure_ascii=False))
        if not all(checks.values()):raise SystemExit(1)
        return
    config=read('/api/config')
    if config and config.get('knowledge_feature_version')=='course-knowledge-v1':
        subprocess.run(['/usr/bin/open',BASE+'/workspace'],check=False)
        print('智能课堂新版已在运行。')
        return
    if config:
        health=read('/api/health') or {}
        session=read('/api/session') or {}
        tasks=read('/api/tasks') or {}
        if session.get('active') or (tasks.get('counts') or {}).get('active'):
            raise SystemExit('原版正在录音或处理任务。请完成或暂停任务后再启动新版；原版继续运行。')
        print('将安全停止空闲的原版服务，启动独立工作区中的新版。原目录代码不变。',flush=True)
        clean=os.environ.copy()
        for key in ('QWEN_ASR_DATA_DIR','QWEN_ASR_PREVIEW','PYTHONPATH'):
            clean.pop(key,None)
        clean['QWEN_ASR_PORT']='8765'
        subprocess.run([str(interpreter),'-m','qwen_asr_app.service_control','stop'],cwd=ORIGINAL,env=clean,check=True)
        # The original controller verifies identity and idle state before SIGTERM.
        # Its old Uvicorn may keep an idle SSE open indefinitely after that.
        stopped_pid=health.get('pid')
        if stopped_pid:
            for attempt in range(100):
                try:os.kill(int(stopped_pid),0)
                except ProcessLookupError:break
                if attempt==50:
                    os.kill(int(stopped_pid),signal.SIGINT)
                time.sleep(.1)
            else:
                raise SystemExit('原版仍在退出中，请关闭原版网页后重新启动；未启动第二个业务服务。')
    env=os.environ.copy()
    for key in ('QWEN_ASR_DATA_DIR','QWEN_ASR_PREVIEW'):
        env.pop(key,None)
    env.update(QWEN_ASR_PORT='8765',QWEN_ASR_MODEL_PATH=str(ORIGINAL/'models/Qwen3-ASR-1.7B-MLX-bf16'),
               QWEN_ASR_CLASSROOM_EDITION='1',
               QWEN_ASR_KNOWLEDGE_MODEL_ROOT=str(ORIGINAL/'models/knowledge'),
               QWEN_ASR_SETTINGS_PATH=str(ORIGINAL/'app-settings.json'),
               QWEN_ASR_LEGACY_RECORDINGS_DIR=str(ORIGINAL/'recordings'),
               PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=str(WORKTREE))
    # Read existing dotenv in memory only; no copied credential file in the worktree.
    from dotenv import dotenv_values
    for key,value in dotenv_values(ORIGINAL/'.env').items():
        if value is not None and key not in {'QWEN_ASR_DATA_DIR','QWEN_ASR_SETTINGS_PATH','QWEN_ASR_MODEL_PATH','QWEN_ASR_PORT'}:
            env.setdefault(key,value)
    subprocess.run([str(interpreter),'-m','qwen_asr_app.service_control','start'],cwd=WORKTREE,env=env,check=True)
    for _ in range(20):
        current=read('/api/config')
        if current and current.get('knowledge_feature_version')=='course-knowledge-v1':
            subprocess.run(['/usr/bin/open',BASE+'/workspace'],check=False)
            print('新版已启动，复用现有课程资料、录音、模型和授权。',flush=True)
            return
        time.sleep(.2)
    raise SystemExit('新版状态尚未确认，请查看工作区 server.log；原目录代码未修改。')


if __name__=='__main__':
    main()
