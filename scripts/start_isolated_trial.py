"""Start the review checkout with an independent copy of the existing library.

No inference is submitted here. Library and metadata writes stay in trial data.
Private credentials are referenced, never printed or copied into the checkout.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
STABLE = Path('/Users/liuhuayaxi/AIcreatedapplication/QwenASR')
TRIAL = Path('/Users/liuhuayaxi/.qwenasr-candidates/windows-inference-20261009')
PYTHON = STABLE / '.venv/bin/python'
PRIVATE = Path('/Users/liuhuayaxi/.qwenasr')


def atomic(path, value):
    from qwen_asr_app.durable import atomic_json
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(path, value)


def listening(port):
    with socket.socket() as stream:
        stream.settimeout(.3)
        return stream.connect_ex(('127.0.0.1',port))==0


def main():
    os.umask(0o077)
    # Set the trial root before importing qwen_asr_app.paths/durable. Without
    # this, an atomic settings write could target the formal ~/.qwenasr.
    os.environ['QWEN_ASR_DATA_DIR'] = str(TRIAL)
    os.environ['QWEN_ASR_PORT'] = '8766'
    if listening(8766):
        raise SystemExit('Port 8766 already has a listener; no process was changed.')
    raw = json.loads((STABLE/'app-settings.json').read_text())
    original = Path(raw['storage_root']).expanduser().resolve()
    if not TRIAL.exists():
        TRIAL.mkdir(parents=True, mode=0o700)
        subprocess.run(['/bin/cp','-cR',str(original),str(TRIAL/'library')],check=True)
        for path in (TRIAL/'library').rglob('*'):
            if path.is_symlink():
                raise RuntimeError('Trial library contains a symlink; inspect before launch')
        trial_settings = dict(raw)
        trial_settings.update(storage_root=str(TRIAL/'library'), known_roots=[str(TRIAL/'library')])
        atomic(TRIAL/'app-settings.json',trial_settings)
        # APFS clones have new inode numbers. Update only index stamps in the
        # copy so the scanner does not classify preserved OCR as external edits.
        indices=0
        for index in (TRIAL/'library').glob('*/.qwenasr-files/index.json'):
            values=json.loads(index.read_text());folder=index.parent.parent
            for item in values.values():
                for entry in (item,item.get('conversion',{})):
                    relative=entry.get('relative_path')
                    if not relative:
                        continue
                    source=folder/relative
                    if source.is_file():
                        stat=source.stat()
                        entry['stamp']=[stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns]
            atomic(index,values);indices+=1
        if (STABLE/'recordings').is_dir():
            subprocess.run(['/bin/cp','-cR',str(STABLE/'recordings'),str(TRIAL/'recordings')],check=True)
        private=TRIAL/'private';private.mkdir(mode=0o700)
        for name in ('mineru-settings.json','ai-settings.json'):
            source=PRIVATE/name
            if source.is_file():
                subprocess.run(['/bin/cp','-c',str(source),str(private/name)],check=True)
        token=private/'remote-infer/token';token.parent.mkdir(mode=0o700)
        token.symlink_to(PRIVATE/'remote-infer/token')
        atomic(private/'inference-providers.json',{'mineru_provider':'remote_only','asr_provider':'local',
               'base_url':'http://127.0.0.1:18876','token_file':str(token),
               'connect_timeout_seconds':10,'request_timeout_seconds':1800,'job_timeout_seconds':14400,
               'poll_seconds':2,'network_recovery_seconds':60})
        atomic(TRIAL/'snapshot.json',{'original_library':str(original),'trial_library':str(TRIAL/'library'),
               'copied_indices':indices,'inference_submitted':False,'port':8766})
    if not (TRIAL/'snapshot.json').is_file():
        raise SystemExit('Existing trial preparation is incomplete; no process was started.')
    logs=TRIAL/'logs';logs.mkdir(exist_ok=True,mode=0o700)
    if not listening(18876):
        with (logs/'tunnel.log').open('ab') as log:
            tunnel=subprocess.Popen(['ssh','-N','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes',
                 '-o','ServerAliveInterval=30','-o','ServerAliveCountMax=2',
                 '-L','127.0.0.1:18876:127.0.0.1:8876','liuhuayaxi@100.81.62.69'],
                 stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        atomic(TRIAL/'tunnel.json',{'pid':tunnel.pid,'local_port':18876})
        for _ in range(30):
            if listening(18876):break
            if tunnel.poll() is not None:raise RuntimeError('Candidate SSH tunnel exited')
            time.sleep(.2)
    env=dict(os.environ)
    for key in tuple(env):
        if key.startswith(('AI_','OPENAI_','QWEN_ASR_')):
            env.pop(key)
    env.update(QWEN_ASR_DATA_DIR=str(TRIAL),QWEN_ASR_PORT='8766',
               QWEN_ASR_MODEL_PATH=str(STABLE/'models/Qwen3-ASR-1.7B-MLX-bf16'),
               QWEN_ASR_INSTANCE=uuid.uuid4().hex,PYTHONDONTWRITEBYTECODE='1')
    with (logs/'workbench.log').open('ab') as log:
        process=subprocess.Popen([str(PYTHON),'-m','uvicorn','app:app','--host','127.0.0.1',
                  '--port','8766','--no-access-log'],cwd=ROOT,env=env,
                  stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    atomic(TRIAL/'process.json',{'pid':process.pid,'port':8766,'cwd':str(ROOT),'data_dir':str(TRIAL)})
    with httpx.Client(trust_env=False,timeout=3) as client:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError('Trial process exited; inspect candidate logs')
            try:
                response=client.get('http://127.0.0.1:8766/api/health')
                if response.status_code==200 and response.json().get('pid')==process.pid:
                    print(json.dumps({'ready':True,'pid':process.pid,'url':'http://127.0.0.1:8766/course-files',
                       'library_copy':str(TRIAL/'library'),'automatic_tasks_submitted':0},ensure_ascii=False))
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.25)
    raise RuntimeError('Trial startup did not pass health check')


if __name__=='__main__':
    import sys
    sys.path.insert(0,str(ROOT))
    main()
