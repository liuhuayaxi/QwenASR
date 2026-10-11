"""Real worker benchmark, before installing the persistent service."""
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.config import configuration

parser = argparse.ArgumentParser()
parser.add_argument('--kind', choices=['mineru','asr'], required=True)
parser.add_argument('--input', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--language', default='')
args = parser.parse_args()
cfg = configuration()
data = Path(cfg['server']['data_dir'])
data.mkdir(parents=True, exist_ok=True)
gpu_lock = (data/'gpu.lock').open('a')
fcntl.flock(gpu_lock,fcntl.LOCK_EX)
folder = args.output.resolve()
folder.mkdir(parents=True, exist_ok=False, mode=0o700)
options = {'tier':'advanced','ocr_mode':'auto'} if args.kind == 'mineru' else {'language':args.language,'max_new_tokens':512}
request = {'input':str(args.input.resolve()),'job_dir':str(folder),'parent_pid':os.getpid(),'options':options,'config':cfg}
(folder/'request.json').write_text(json.dumps(request))
binary = '/usr/lib/wsl/lib/nvidia-smi'
def memory():
    return int(subprocess.check_output([binary,'--query-gpu=memory.used','--format=csv,noheader,nounits'],text=True).splitlines()[0])
baseline=memory();peak=baseline;started=time.monotonic()
with (folder/'worker.log').open('wb') as log:
    p=subprocess.Popen([cfg[args.kind]['python'],'-m','workers.'+args.kind+'_worker','--request',str(folder/'request.json')],cwd=ROOT,start_new_session=True,stdout=log,stderr=log)
    last=''
    try:
        while p.poll() is None:
            if time.monotonic()-started>cfg['server']['job_timeout_seconds']:raise TimeoutError()
            peak=max(peak,memory())
            try:
                e=json.loads((folder/'event.json').read_text())
                state=e['state']+':'+str(int(e['progress']))
                if state != last:print(state,flush=True);last=state
            except (OSError,ValueError):pass
            time.sleep(.75)
    finally:
        try:os.killpg(p.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:p.wait(timeout=5)
        except subprocess.TimeoutExpired:pass
        try:os.killpg(p.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        p.wait()
metrics={'returncode':p.returncode,'gpu_baseline_mib':baseline,'peak_gpu_used_mib':peak,'gpu_after_mib':memory(),'wall_seconds':time.monotonic()-started}
if (folder/'result/metadata.json').exists():metrics.update(json.loads((folder/'result/metadata.json').read_text()))
if (folder/'failure.json').exists():metrics.update(json.loads((folder/'failure.json').read_text()))
(folder/'benchmark.json').write_text(json.dumps(metrics,indent=2))
print(json.dumps(metrics,indent=2))
raise SystemExit(0 if p.returncode==0 else 1)
