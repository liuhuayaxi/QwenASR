"""Real local-node acceptance. Uses only public fixtures and records all results."""
import json
import os
import signal
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from client.remote import RemoteClient
from server.config import configuration

cfg=configuration()
host=cfg['server']['host'];endpoint=f"http://{'127.0.0.1' if host=='0.0.0.0' else host}:{cfg['server']['port']}"
client=RemoteClient({**cfg['client'],'base_url':endpoint,'token_file':cfg['server']['token_file'],'poll_seconds':.5})
report=ROOT/'benchmarks'/('gateway-e2e-'+time.strftime('%Y%m%d-%H%M%S'))
report.mkdir(parents=True,mode=0o700)
results={}

def save():
    (report/'report.json').write_text(json.dumps(results,indent=2,ensure_ascii=False))

def submit(kind,name):
    path=ROOT/'fixtures'/name
    key=uuid.uuid4().hex
    with client.session() as session,path.open('rb') as source:
        job=client.data(session.post('/v1/jobs',params={'kind':kind,'filename':name},headers={'Idempotency-Key':key},content=iter(lambda:source.read(1024*1024),b'')))
    print('accepted',name,job['id'],flush=True)
    return job['id']

def status(ident):
    with client.session() as session:
        return client.data(session.get('/v1/jobs/'+ident))

def node_status():
    with client.session() as session:return client.data(session.get('/v1/status'))

def wait(ident,states,timeout=900):
    deadline=time.monotonic()+timeout
    last=''
    while time.monotonic()<deadline:
        job=status(ident)
        if job['status']!=last:print(ident,job['status'],flush=True);last=job['status']
        if job['status'] in states:return job
        time.sleep(.5)
    raise TimeoutError('Acceptance job timeout')

def download(ident,name):
    destination=report/name
    destination.mkdir()
    with client.session() as session,session.stream('GET',f'/v1/jobs/{ident}/result') as response:
        response.raise_for_status()
        with (destination/'result.zip').open('wb') as out:
            for chunk in response.iter_bytes(1024*1024):out.write(chunk)
    with zipfile.ZipFile(destination/'result.zip') as bundle:bundle.extractall(destination)
    return destination

try:
    for attempt in range(60):
        try:
            client.health()
            break
        except Exception:
            if attempt == 59:raise
            time.sleep(.5)
    results['initial_idle']=node_status()
    assert results['initial_idle']['state']=='IDLE'
    assert results['initial_idle']['worker_pid'] is None
    first=submit('asr','mixed-5min.wav')
    wait(first,{'loading','running'})
    pdf=submit('mineru','scanned-wave-table.pdf')
    assert status(pdf)['status']=='queued'
    results['real_gpu_queue']=True;save()
    results['asr_5min']=wait(first,{'completed','failed'})
    assert results['asr_5min']['status']=='completed'
    out=download(first,'asr-5min')
    transcript=json.loads((out/'transcript.json').read_text())
    assert '甚至' in transcript['text'] and 'other people' in transcript['text']
    assert abs(transcript['segments'][-1]['end']-300)<.01
    results['asr_5min_quality']={'Chinese':True,'English':True,'segments':len(transcript['segments'])};save()
    results['pdf_scanned']=wait(pdf,{'completed','failed'})
    assert results['pdf_scanned']['status']=='completed'
    out=download(pdf,'scanned-pdf')
    import re
    md=(out/'markdown.md').read_text()
    links=re.findall(r'!\[[^\]]*\]\(([^)]+)\)',md)
    assert links and all((out/name).is_file() for name in links)
    assert '\\nu' in md and '| Quantity |' in md
    results['pdf_assets_valid']=True;save()
    # Kill the real MinerU parent after vLLM created its GPU engine descendant.
    crash=submit('mineru','native-formula-table-12pages.pdf')
    next_job=submit('asr','asr_zh.wav')
    wait(crash,{'running'},timeout=180)
    node=node_status();assert node['current_job']==crash
    os.kill(node['worker_pid'],signal.SIGKILL)
    results['killed_worker']=wait(crash,{'failed'})
    assert results['killed_worker']['metrics']['gpu_after']['used_mib']<=100
    results['after_killed_worker']=wait(next_job,{'completed','failed'})
    assert results['after_killed_worker']['status']=='completed';save()
    canceled=submit('asr','mixed-5min.wav')
    wait(canceled,{'running'})
    with client.session() as session:client.data(session.post('/v1/jobs/'+canceled+'/cancel'))
    results['canceled_worker']=wait(canceled,{'canceled'})
    assert results['canceled_worker']['metrics']['gpu_after']['used_mib']<=100;save()
    long_job=submit('asr','mixed-30min.wav')
    results['asr_30min']=wait(long_job,{'completed','failed'},timeout=900)
    assert results['asr_30min']['status']=='completed'
    out=download(long_job,'asr-30min')
    value=json.loads((out/'transcript.json').read_text())
    assert abs(value['segments'][-1]['end']-1800)<.01
    assert '甚至' in value['text'] and 'other people' in value['text']
    results['asr_30min_quality']={'Chinese':True,'English':True,'segments':len(value['segments']),
                               'Chinese_anchor_count':value['text'].count('甚至'),'English_anchor_count':value['text'].count('other people')}
    # Other authorized clients may have submitted a job during the benchmark.
    # Wait for the shared queue to drain before measuring idle resources.
    for attempt in range(1200):
        results['final_idle']=node_status()
        if results['final_idle']['state']=='IDLE':break
        time.sleep(.5)
    save()
    assert results['final_idle']['worker_pid'] is None
    assert results['final_idle']['gpu']['used_mib']<=100
    print('Real gateway acceptance passed:',report,flush=True)
finally:
    save()
