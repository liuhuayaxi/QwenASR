"""Real task timeout and systemd crash recovery; restore config in finally."""
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from client.remote import RemoteClient
from server.config import configuration
cfg=configuration();host=cfg['server']['host'];endpoint=f"http://{'127.0.0.1' if host=='0.0.0.0' else host}:{cfg['server']['port']}"
client=RemoteClient({**cfg['client'],'base_url':endpoint,'token_file':cfg['server']['token_file']})
output=ROOT/'benchmarks'/('recovery-'+time.strftime('%Y%m%d-%H%M%S'));output.mkdir(parents=True)
results={}
def get(path):
    with client.session() as s:return client.data(s.get(path))
def healthy():
    for attempt in range(100):
        try:client.health();return
        except Exception:time.sleep(.5)
    raise TimeoutError('Gateway did not recover')
def idle():
    for attempt in range(1200):
        try:
            if get('/v1/status')['state']=='IDLE':return
        except Exception:pass
        time.sleep(.5)
    raise TimeoutError('Existing tasks did not finish')
def restart():
    subprocess.run(['/mnt/c/Windows/System32/wsl.exe','-d','Ubuntu-24.04','-u','root','--exec','systemctl','restart','ai-infer-gateway'],check=True)
    healthy()
def submit(name):
    with client.session() as s,(ROOT/'fixtures'/name).open('rb') as f:
        return client.data(s.post('/v1/jobs',params={'kind':'asr','filename':name},headers={'Idempotency-Key':uuid.uuid4().hex},content=iter(lambda:f.read(1024*1024),b'')))['id']
def wait(ident,states):
    for attempt in range(600):
        try:
            value=get('/v1/jobs/'+ident)
            if value['status'] in states:return value
        except Exception:pass
        time.sleep(.5)
    raise TimeoutError('Recovery test task timed out')
idle();path=ROOT/'config.toml';original=path.read_bytes()
try:
    modified=re.sub(r'(?m)^job_timeout_seconds\s*=\s*\d+','job_timeout_seconds = 12',original.decode())
    path.write_text(modified);restart()
    ident=submit('mixed-5min.wav');value=wait(ident,{'failed','completed'})
    assert value['status']=='failed' and value['error']=='JOB_TIMEOUT'
    assert value['metrics']['gpu_after']['used_mib']<=100
    results['timeout']=value;print('Real task timeout passed',flush=True)
finally:
    path.write_bytes(original);restart()
results['timeout_history_after_restart']=get('/v1/jobs/'+ident)
active=submit('mixed-5min.wav');queued=submit('asr_zh.wav')
wait(active,{'running'})
node=get('/v1/status');assert node['current_job']==active
pid=int(subprocess.check_output(['systemctl','show','ai-infer-gateway','-p','MainPID','--value'],text=True))
os.kill(pid,signal.SIGKILL);healthy()
results['gateway_crash']=wait(active,{'failed'})
assert results['gateway_crash']['error']=='GATEWAY_RESTARTED'
results['queued_after_gateway_crash']=wait(queued,{'completed','failed'})
assert results['queued_after_gateway_crash']['status']=='completed'
results['final_idle']=get('/v1/status')
(output/'report.json').write_text(json.dumps(results,indent=2))
print('Systemd automatic restart and queued task recovery passed:',output,flush=True)
