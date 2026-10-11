"""Run real acceptance through the public Tailnet entry, with public fixtures."""
import argparse
import json
import re
import sys
import time
import uuid
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from client.remote import RemoteClient,safe_extract

parser=argparse.ArgumentParser()
parser.add_argument('--fixtures',type=Path,required=True)
parser.add_argument('--report',type=Path,required=True)
parser.add_argument('--endpoint',default='http://100.81.62.69:8765')
parser.add_argument('--token-file',default=str(Path.home()/'.qwenasr/remote-infer/token'))
args=parser.parse_args()
client=RemoteClient({'base_url':args.endpoint,'token_file':args.token_file,'connect_timeout_seconds':5,
    'request_timeout_seconds':1800,'job_timeout_seconds':14400,'poll_seconds':1,'network_recovery_seconds':60})
args.report.mkdir(parents=True,exist_ok=True)
report={'endpoint':args.endpoint,'checks':{}}
checks=report['checks']
def save():
    (args.report/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
def get(path):
    with client.session() as session:return client.data(session.get(path))
def submit(kind,name,key=None,options=None):
    path=args.fixtures/name
    with client.session() as session,path.open('rb') as content:
        job=client.data(session.post('/v1/jobs',params={'kind':kind,'filename':name,'options':json.dumps(options or {})},
            headers={'Idempotency-Key':key or uuid.uuid4().hex},content=iter(lambda:content.read(1024*1024),b'')))
    print('accepted',name,job['id'],flush=True)
    return job['id']
def wait(ident,states):
    last=''
    deadline=time.monotonic()+1800
    while time.monotonic()<deadline:
        job=get('/v1/jobs/'+ident)
        if last!=job['status']:print(ident,job['status'],flush=True);last=job['status']
        if job['status'] in states:return job
        time.sleep(.5)
    raise TimeoutError('Acceptance exceeded 30 minutes')
def download(ident,name):
    folder=args.report/name;folder.mkdir()
    with client.session() as session,session.stream('GET',f'/v1/jobs/{ident}/result') as response:
        response.raise_for_status()
        with (folder/'result.zip').open('wb') as out:
            for chunk in response.iter_bytes(1024*1024):out.write(chunk)
    safe_extract(folder/'result.zip',folder)
    return folder
try:
    checks['health']=client.health();checks['initial_idle']=get('/v1/status')
    with client.session() as session:
        bad=session.get('/v1/health',headers={'Authorization':'Bearer invalid'})
        assert bad.status_code==401;checks['unauthorized']=bad.status_code
        bad=session.post('/v1/jobs',params={'kind':'asr','filename':'../hidden.wav'},content=b'fixture',headers={'Idempotency-Key':uuid.uuid4().hex})
        assert bad.status_code==400;checks['path_traversal']=bad.status_code
    key=uuid.uuid4().hex
    first=submit('asr','mixed-5min.wav',key)
    same=submit('asr','mixed-5min.wav',key);assert same==first;checks['idempotency_same_job']=True
    wait(first,{'loading','running'})
    pdf=submit('mineru','native-formula-table-12pages.pdf')
    assert get('/v1/jobs/'+pdf)['status']=='queued';checks['shared_gpu_queue']=True;save()
    value=wait(first,{'completed','failed'});checks['asr_5min']=value
    assert value['status']=='completed'
    folder=download(first,'asr-5min');transcript=json.loads((folder/'transcript.json').read_text())
    assert '甚至' in transcript['text'] and 'other people' in transcript['text']
    assert abs(transcript['segments'][-1]['end']-300)<.01
    assert transcript['metadata']['gpu_after']['used_mib']<=100
    checks['asr_5min_quality']={'Chinese':True,'English':True,'segments':len(transcript['segments']),'coverage_seconds':transcript['segments'][-1]['end']};save()
    value=wait(pdf,{'completed','failed'});checks['pdf_12pages']=value;assert value['status']=='completed'
    folder=download(pdf,'pdf-12pages');middle=json.loads((folder/'middle_json.json').read_text())
    md=(folder/'markdown.md').read_text();assert value['metrics']['pages']==12 and value['metrics']['tier']=='advanced'
    assert '\\nu' in md and '| Quantity |' in md and '第 12 页' in md
    metadata=json.loads((folder/'metadata.json').read_text());assert metadata['gpu_after']['used_mib']<=100
    checks['pdf_output']={'pages':12,'all_pages':True,'formulas':True,'tables':True,'assets':len(list((folder/'images').glob('*')))};save()
    scan=submit('mineru','scanned-wave-table.pdf');value=wait(scan,{'completed','failed'});checks['scanned_pdf']=value;assert value['status']=='completed'
    folder=download(scan,'scanned-pdf');md=(folder/'markdown.md').read_text();links=re.findall(r'!\[[^\]]*\]\(([^)]+)\)',md)
    assert links and all((folder/name).is_file() for name in links);checks['scan_image_paths_valid']=True;save()
    cancel=submit('asr','mixed-5min.wav');wait(cancel,{'running'})
    with client.session() as session:client.data(session.post('/v1/jobs/'+cancel+'/cancel'))
    checks['cancel']=wait(cancel,{'canceled'});assert checks['cancel']['metrics']['gpu_after']['used_mib']<=100;save()
    long_job=submit('asr','mixed-30min.wav');value=wait(long_job,{'completed','failed'});checks['asr_30min']=value;assert value['status']=='completed'
    folder=download(long_job,'asr-30min');value=json.loads((folder/'transcript.json').read_text())
    assert '甚至' in value['text'] and 'other people' in value['text'] and abs(value['segments'][-1]['end']-1800)<.01
    checks['asr_30min_quality']={'Chinese':True,'English':True,'segments':len(value['segments']),'coverage_seconds':value['segments'][-1]['end']}
    checks['final_idle']=get('/v1/status');assert checks['final_idle']['worker_pid'] is None and checks['final_idle']['gpu']['used_mib']<=100
    report['passed']=True;print('Final native Tailnet acceptance passed',args.report,flush=True)
finally:save()
