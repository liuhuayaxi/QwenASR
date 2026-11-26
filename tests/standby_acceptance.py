"""Real macOS socket/re-exec acceptance with synthetic data and no accounts."""
import asyncio
import json
import http.client
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request,urlopen
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=r'''
import asyncio,json,os,time
from pathlib import Path
from starlette.applications import Starlette
from starlette.responses import JSONResponse,StreamingResponse
from starlette.routing import Route,WebSocketRoute
books=Path(os.environ['FIXTURE_BOOKS']);state=books/'state.json'
saved=json.loads(state.read_text())
if saved.get('wake_at') and saved['wake_at']<=time.time():
 saved.update(wake_at=None,wake_fired=True);state.write_text(json.dumps(saved))
class Watch:
 def __init__(self):self.listeners=set();self.revision=time.time_ns();self.draining=False
 def publish(self):
  self.revision=time.time_ns()
  for channel in self.listeners:
   if channel.empty():channel.put_nowait(self.revision)
library_watch=Watch()
def read():return json.loads(state.read_text())
def can_enter_standby():return not read().get('busy')
def standby_context():return {'watch_dirs':[str(books)],'deadlines':{},'wake_at':read().get('wake_at')}
async def health(request):return JSONResponse({'pid':os.getpid(),'instance':os.getenv('QWEN_ASR_INSTANCE'),'status':'idle','memory':{}})
async def library(request):return JSONResponse({'pid':os.getpid(),**read()})
async def change(request):
 data=await request.json();state.write_text(json.dumps(data));library_watch.publish();return JSONResponse({'pid':os.getpid(),**data})
async def events(request):
 channel=asyncio.Queue();library_watch.listeners.add(channel)
 async def stream():
  try:
   yield f'data: {library_watch.revision}\n\n'
   while not library_watch.draining and not await request.is_disconnected():
    try:await asyncio.wait_for(channel.get(),.1)
    except asyncio.TimeoutError:continue
    if not library_watch.draining:yield f'data: {library_watch.revision}\n\n'
  finally:library_watch.listeners.discard(channel)
 return StreamingResponse(stream(),media_type='text/event-stream')
async def websocket(ws):
 await ws.accept()
 try:
  while True:await ws.send_text(await ws.receive_text())
 except Exception:pass
app=Starlette(routes=[Route('/api/health',health),Route('/api/session',library),Route('/api/tasks',library),
 Route('/api/config',library),Route('/api/library',library),Route('/course/api/status',health),
 Route('/api/change',change,methods=['POST']),Route('/api/library/events',events),WebSocketRoute('/ws',websocket)])
'''


def wait_for(test,timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            if value:=test():return value
        except (OSError,ValueError,KeyError):pass
        time.sleep(.05)
    raise AssertionError('Standby acceptance timed out')


def main():
    if not hasattr(__import__('select'),'kqueue'):
        raise SystemExit('This native acceptance requires macOS kqueue')
    import websockets
    output=ROOT/'output/standby';output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='qwen-standby-acceptance-') as raw:
        directory=Path(raw);books=directory/'books';books.mkdir()
        (books/'state.json').write_text('{"value":"first","busy":false}')
        (directory/'synthetic_standby.py').write_text(FIXTURE)
        listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1];listener.close()
        environment={**os.environ,'QWEN_ASR_DATA_DIR':str(directory/'private-data'),
             'QWEN_ASR_STANDBY':'1','QWEN_ASR_STANDBY_SECONDS':'.5',
             'QWEN_ASR_INSTANCE':'synthetic-standby-instance','FIXTURE_BOOKS':str(books),
             'PYTHONPATH':str(directory)+os.pathsep+str(ROOT)}
        command=[sys.executable,'-m','qwen_asr_app.server','synthetic_standby:app','--host','127.0.0.1','--port',str(port)]
        log=(output/'acceptance.log').open('w')
        process=subprocess.Popen(command,cwd=ROOT,env=environment,stdout=log,stderr=log,start_new_session=True)
        mode=directory/'private-data/private/services/mode.json'
        base=f'http://127.0.0.1:{port}'
        def phase():return json.loads(mode.read_text()).get('mode')
        def get(path,body=None):
            request=Request(base+path,data=json.dumps(body).encode() if body is not None else None,
                            headers={'Content-Type':'application/json'})
            with urlopen(request,timeout=15) as response:return json.load(response)
        try:
            wait_for(lambda:get('/api/health'))
            initial=get('/api/library');assert initial['pid']==process.pid
            # Keep the same HTTP stream open across a full -> light transition.
            stream=urlopen(base+'/api/library/events',timeout=15)
            stream.readline();stream.readline()
            held=http.client.HTTPConnection('127.0.0.1',port,timeout=15)
            held.request('GET','/api/library');held.getresponse().read()
            client_address=held.sock.getsockname()
            wait_for(lambda:phase()=='standby')
            held.request('GET','/api/library')
            assert held.sock.getsockname()==client_address
            assert json.loads(held.getresponse().read())['value']=='first'
            held.close()
            assert get('/api/library')['value']=='first'
            assert get('/api/health')['pid']==process.pid
            for headers in ({'Host':'rebinding.invalid'}, {'Origin':'https://foreign.invalid'}):
                try:
                    urlopen(Request(base+'/api/library',headers=headers),timeout=3)
                except HTTPError as error:
                    assert error.code==403
                else:
                    raise AssertionError('Standby bypassed the request origin/host guard')
            assert phase()=='standby'
            # Resuming must preserve the already-peeked HTTP body exactly.
            payload='第二版：R=0.9'+'x'*100000
            changed=get('/api/change',{'value':payload,'busy':True})
            assert changed['value']==payload and changed['pid']==process.pid
            time.sleep(2);assert phase()=='active','An active job was suspended'
            get('/api/change',{'value':'third','busy':False})
            wait_for(lambda:phase()=='standby')
            assert get('/api/library')['value']=='third'
            async def check_websocket():
                async with websockets.connect(f'ws://127.0.0.1:{port}/ws',open_timeout=15) as ws:
                    await ws.send('same-socket PCM/control boundary')
                    assert await ws.recv()=='same-socket PCM/control boundary'
                    await asyncio.sleep(2)
                    assert phase()=='active','A live WebSocket was suspended'
            asyncio.run(check_websocket())
            wait_for(lambda:phase()=='standby')
            # A Finder-side change wakes the host without any browser request.
            (books/'state.json').write_text('{"value":"filesystem update","busy":false}')
            wait_for(lambda:phase()=='active')
            wait_for(lambda:phase()=='standby')
            assert get('/api/library')['value']=='filesystem update'
            assert phase()=='standby','A cached background read woke the heavy runtime'
            get('/api/change',{'value':'timer','busy':False,'wake_at':time.time()+3})
            wait_for(lambda:phase()=='standby')
            wait_for(lambda:phase()=='active')
            wait_for(lambda:phase()=='standby')
            assert get('/api/library')['wake_fired'] is True
            # The original EventSource still has a valid chunked connection.
            assert stream.readline().startswith(b'data:')
            stream.close()
            summary=subprocess.check_output(['vmmap','-summary',str(process.pid)],text=True,stderr=subprocess.DEVNULL)
            match=re.search(r'Physical footprint:\s+([\d.]+)([KMG])',summary)
            memory=float(match[1])*{'K':1/1024,'M':1,'G':1024}[match[2]]
            assert memory<20,('Idle memory target exceeded',memory)
            started=time.monotonic();process.send_signal(signal.SIGTERM);process.wait(timeout=3)
            report={'synthetic':True,'same_pid':True,'cached_reads':True,'http_body_handoff':True,
              'websocket_handoff':True,'active_work_preserved':True,'event_stream_preserved':True,
              'filesystem_wakeup':True,'keepalive_handoff':True,'scheduled_wakeup':True,
              'origin_and_host_guards':True,
              'idle_physical_footprint_MiB':memory,'stop_seconds':time.monotonic()-started}
            (output/'acceptance.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
        finally:
            if process.poll() is None:
                process.kill();process.wait()
            log.close()


if __name__=='__main__':main()
