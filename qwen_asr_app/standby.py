"""Small stdlib-only idle host. Re-exec the full workbench on real work.

The same PID, listening socket and instance token survive the transition.
Cached reads and static assets do not load FastAPI, inference or PDF modules.
"""
import hashlib
import json
import os
import resource
import select
import signal
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from .request_security import http_request_allowed

TYPES = {'.html':'text/html; charset=utf-8', '.js':'text/javascript', '.mjs':'text/javascript',
         '.css':'text/css', '.json':'application/json', '.png':'image/png', '.webp':'image/webp',
         '.svg':'image/svg+xml', '.woff':'font/woff', '.woff2':'font/woff2', '.ttf':'font/ttf',
         '.wasm':'application/wasm', '.ico':'image/x-icon'}


def mode(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'mode':value, 'pid':os.getpid(), 'at':time.time()}))
    temporary.replace(path)


def wake(listener, state, accepted=None, streams=(), idle=()):
    descriptor = listener.fileno()
    os.set_inheritable(descriptor, True)
    environment = dict(os.environ)
    environment.pop('QWEN_ASR_STANDBY_STATE', None)
    environment['QWEN_ASR_PRIOR_CACHE']=str(state)
    environment['QWEN_ASR_LISTEN_FD'] = str(descriptor)
    metadata=json.loads(state.read_text())
    environment['QWEN_ASR_WAKE_DEADLINES']=json.dumps(metadata.get('deadlines',{}))
    environment['QWEN_ASR_EVENT_FDS'] = ','.join(str(s.fileno()) for s in streams)
    for stream in streams:
        os.set_inheritable(stream.fileno(), True)
    idle=[s for s in idle if accepted is None or s.fileno()!=accepted.fileno()]
    for stream in idle:os.set_inheritable(stream.fileno(),True)
    environment['QWEN_ASR_IDLE_FDS']=','.join(str(s.fileno()) for s in idle)
    if accepted is not None:
        os.set_inheritable(accepted.fileno(), True)
        environment['QWEN_ASR_ACCEPTED_FD'] = str(accepted.fileno())
    else:
        environment.pop('QWEN_ASR_ACCEPTED_FD', None)
    os.execve(sys.executable, [sys.executable, '-m', 'qwen_asr_app.server', *sys.argv[1:]], environment)


def reply(connection, status, headers, body=b'', file=None, head=False):
    reasons = {200:'OK', 206:'Partial Content', 304:'Not Modified', 400:'Bad Request', 403:'Forbidden', 404:'Not Found', 503:'Service Unavailable'}
    prefix = f'HTTP/1.1 {status} {reasons.get(status,"Response")}\r\n'
    excluded = {'connection','transfer-encoding','content-length','content-encoding'}
    pairs = [(k,v) for k,v in headers if k.lower() not in excluded]
    length = file.stat().st_size if file is not None else len(body)
    pairs += [('Content-Length',str(0 if status == 304 else length)), ('Connection','close')]
    connection.settimeout(2)
    connection.sendall((prefix+''.join(k+': '+v+'\r\n' for k,v in pairs)+'\r\n').encode('latin-1'))
    if head or status == 304:
        return
    if file is not None:
        with file.open('rb') as source:
            while data := source.read(16384):
                connection.sendall(data)
    else:
        connection.sendall(body)


def health_body(path):
    data = json.loads(path.read_text())
    data.update(pid=os.getpid(), status='idle', detail='Qwen3-ASR 已待命 · 操作时自动恢复',
                local_model_loaded=False, asr_worker_pid=None,
                cpu_seconds=time.process_time(),
                standby={'mode':'standby','blockers':[]})
    # proc_pidinfo reports current resident bytes without importing the full
    # runtime-memory module (which intentionally imports asyncio).
    try:
        import ctypes
        buffer = ctypes.create_string_buffer(96)
        library = ctypes.CDLL('/usr/lib/libproc.dylib')
        if library.proc_pidinfo(os.getpid(),4,0,buffer,96) == 96:
            import struct
            resident = struct.unpack_from('Q',buffer.raw,8)[0]
            data['memory'] = {'rss_bytes':resident, 'asr_worker_rss_bytes':0}
    except (OSError,AttributeError):
        pass
    return json.dumps(data,ensure_ascii=False).encode()


def run(args):
    state = Path(os.environ['QWEN_ASR_STANDBY_STATE'])
    metadata = json.loads(state.read_text())
    root = Path(metadata['root'])
    cache = state.parent/'http-cache'
    listener = socket.socket(fileno=int(os.environ.pop('QWEN_ASR_LISTEN_FD')))
    listener.setblocking(False)
    os.set_inheritable(listener.fileno(), False)
    queue = select.kqueue()
    clients, events, watched = {}, {}, []
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
        raise SystemExit(0)
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,stop)
    queue.control([select.kevent(listener.fileno(),filter=select.KQ_FILTER_READ,flags=select.KQ_EV_ADD)],0,0)
    for descriptor in filter(None,os.environ.pop('QWEN_ASR_IDLE_FDS','').split(',')):
        connection=socket.socket(fileno=int(descriptor));connection.setblocking(False)
        os.set_inheritable(connection.fileno(),False);clients[connection.fileno()]=connection
        queue.control([select.kevent(connection.fileno(),filter=select.KQ_FILTER_READ,flags=select.KQ_EV_ADD)],0,0)
    # Existing chunked SSE connections retain their TCP and HTTP state.
    for descriptor in filter(None,os.environ.pop('QWEN_ASR_EVENT_FDS','').split(',')):
        stream = socket.socket(fileno=int(descriptor));stream.setblocking(False)
        os.set_inheritable(stream.fileno(),False);events[stream.fileno()] = stream
        queue.control([select.kevent(stream.fileno(),filter=select.KQ_FILTER_READ,flags=select.KQ_EV_ADD)],0,0)
    changed = False
    soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
    needed=len(metadata.get('watch',[]))+256
    if soft<needed:
        limit=needed if hard==resource.RLIM_INFINITY else min(needed,hard)
        resource.setrlimit(resource.RLIMIT_NOFILE,(limit,hard))
    for path,stamp in metadata.get('watch',[]):
        try:
            descriptor = os.open(path,getattr(os,'O_EVTONLY',os.O_RDONLY))
            stat = os.fstat(descriptor)
            changed |= [stat.st_ino,stat.st_mtime_ns] != stamp
            watched.append(descriptor)
            queue.control([select.kevent(descriptor,filter=select.KQ_FILTER_VNODE,flags=select.KQ_EV_ADD|select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_WRITE|select.KQ_NOTE_RENAME|select.KQ_NOTE_DELETE)],0,0)
        except OSError:
            changed = True
    mode(state.parent/'mode.json','standby')
    if changed:
        wake(listener,state,streams=events.values(),idle=clients.values())
    last_heartbeat = time.monotonic()
    try:
        while not stopped:
            deadline=metadata.get('wake_at')
            if deadline is not None and time.time()>=deadline:
                wake(listener,state,streams=events.values(),idle=clients.values())
            timeout=max(.1,15-(time.monotonic()-last_heartbeat)) if events else None
            if deadline is not None:
                timeout=min(timeout,max(.1,deadline-time.time())) if timeout is not None else max(.1,deadline-time.time())
            try:
                notices = queue.control(None,256,timeout)
            except InterruptedError:
                continue
            if any(event.filter == select.KQ_FILTER_VNODE for event in notices):
                wake(listener,state,streams=events.values(),idle=clients.values())
            for event in notices:
                descriptor = event.ident
                if descriptor == listener.fileno():
                    while True:
                        try:connection,_ = listener.accept()
                        except BlockingIOError:break
                        connection.setblocking(False);clients[connection.fileno()] = connection
                        queue.control([select.kevent(connection.fileno(),filter=select.KQ_FILTER_READ,flags=select.KQ_EV_ADD)],0,0)
                    continue
                if descriptor in events:
                    stream = events[descriptor]
                    # EventSource clients never send more request data. Any
                    # readable EOF or pipelined input closes this stream.
                    events.pop(descriptor).close()
                    continue
                connection = clients.get(descriptor)
                if connection is None:continue
                try:
                    raw = connection.recv(65536,socket.MSG_PEEK)
                    if not raw:raise OSError('closed')
                    if b'\r\n\r\n' not in raw:
                        wake(listener,state,accepted=connection,streams=events.values(),idle=clients.values())
                    first,*lines = raw.split(b'\r\n\r\n',1)[0].decode('latin-1').split('\r\n')
                    method,url,_ = first.split(' ',2)
                    headers = {k.lower():v.strip() for line in lines if ':' in line for k,v in [line.split(':',1)]}
                    path = urlsplit(url).path
                    request=SimpleNamespace(base_url='http://'+headers.get('host',''),headers=headers,
                                            method=method,url=SimpleNamespace(path=path))
                    allowed=http_request_allowed(request,os.getenv('QWEN_ASR_ALLOWED_ORIGINS',''),
                                                 testing=bool(os.getenv('QWEN_ASR_DATA_DIR')))
                    control=(bool(os.getenv('QWEN_ASR_INSTANCE')) and path.startswith('/api/service/') and method=='POST'
                             and headers.get('x-qwenasr-control')==os.getenv('QWEN_ASR_INSTANCE'))
                    handled=(not allowed or control or
                             (method in {'GET','HEAD'} and (path=='/api/library/events' or
                              path.startswith('/static/') or url in metadata['urls'])))
                    if handled:
                        # A TCP close with unread request bytes can reset the
                        # connection and discard an otherwise valid response.
                        remaining=raw.index(b'\r\n\r\n')+4+min(4096,int(headers.get('content-length','0')))
                        connection.settimeout(2)
                        while remaining:
                            data=connection.recv(min(remaining,16384))
                            if not data:raise OSError('incomplete request')
                            remaining-=len(data)
                    if not allowed:
                        reply(connection,403,[('Content-Type','application/json')],b'{"detail":"Untrusted request origin"}')
                    elif control:
                        reply(connection,200,[('Content-Type','application/json')],b'{"ready":true}')
                        if path.endswith('/request-stop'):stopped = True
                    elif path == '/api/library/events' and method == 'GET':
                        connection.settimeout(2)
                        payload=f'data: {metadata["revision"]}\n\n'.encode()
                        connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nTransfer-Encoding: chunked\r\n\r\n'+f'{len(payload):x}\r\n'.encode()+payload+b'\r\n')
                        connection.setblocking(False);events[descriptor] = clients.pop(descriptor)
                        continue
                    elif method in {'GET','HEAD'} and path.startswith('/static/'):
                        file = (root/unquote(path).lstrip('/')).resolve()
                        if not file.is_relative_to(root/'static') or not file.is_file():
                            reply(connection,404,[],b'Not found')
                        else:
                            stat=file.stat();etag=f'W/"{stat.st_size}-{stat.st_mtime_ns}"'
                            status=304 if headers.get('if-none-match') == etag else 200
                            reply(connection,status,[('Content-Type',TYPES.get(file.suffix,'application/octet-stream')),('ETag',etag)],file=file,head=method=='HEAD')
                    elif method in {'GET','HEAD'} and url in metadata['urls']:
                        key=hashlib.sha256(url.encode()).hexdigest();entry=json.loads((cache/(key+'.json')).read_text())
                        fields=entry['headers'];etag=next((v for k,v in fields if k.lower()=='etag'),None)
                        status=304 if etag and headers.get('if-none-match')==etag else entry['status']
                        if path=='/api/health':reply(connection,200,fields,health_body(cache/(key+'.body')),head=method=='HEAD')
                        else:reply(connection,status,fields,file=cache/(key+'.body'),head=method=='HEAD')
                    else:
                        # No bytes were consumed: HTTP bodies and WebSocket
                        # handshakes continue in the fully restored ASGI host.
                        wake(listener,state,accepted=connection,streams=events.values(),idle=clients.values())
                except (OSError,ValueError,KeyError):
                    pass
                clients.pop(descriptor,None)
                connection.close()
            if events and time.monotonic()-last_heartbeat>=15:
                for descriptor,stream in list(events.items()):
                    try:stream.sendall(b'd\r\n: keepalive\n\n\r\n')
                    except OSError:events.pop(descriptor).close()
                last_heartbeat=time.monotonic()
    finally:
        for stream in [*clients.values(),*events.values()]:stream.close()
        for descriptor in watched:os.close(descriptor)
        listener.close();queue.close()
        mode(state.parent/'mode.json','stopped')
