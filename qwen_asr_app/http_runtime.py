"""Full ASGI runtime; its imported modules are released on idle re-exec."""
import asyncio
import hashlib
import importlib
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time

import uvicorn

from .standby_state import Activity,refreshable


class IdleServer(uvicorn.Server):
    def __init__(self, config):
        super().__init__(config)
        self.standby_sockets = []
        self.event_sockets = []
        self.idle_sockets = []
        self.standby_file = None
        self.activity = None
        self.standby_cancelled = False
        self.enabled = (sys.platform == 'darwin' and os.getenv('QWEN_ASR_STANDBY') != '0'
                        and (os.getenv('QWEN_ASR_TESTING') != '1' or os.getenv('QWEN_ASR_STANDBY') == '1'))

    async def startup(self, sockets=None):
        if self.enabled:
            if not self.config.loaded:
                self.config.load()
            self.activity = Activity(self.config.loaded_app)
            prior=os.environ.pop('QWEN_ASR_PRIOR_CACHE','')
            if prior:
                try:
                    metadata=json.loads(Path(prior).read_text())
                    self.activity.urls.update(metadata.get('urls',[]))
                    restore=getattr(self.owner(),'restore_standby_context',None)
                    if restore:restore(metadata)
                except (OSError,ValueError):pass
            self.config.loaded_app = self.activity
        await super().startup(sockets)
        if self.enabled:
            self.activity.last_action = time.monotonic()
            descriptors=list(filter(None,os.environ.pop('QWEN_ASR_IDLE_FDS','').split(',')))
            accepted_descriptor=os.environ.pop('QWEN_ASR_ACCEPTED_FD','')
            if accepted_descriptor:descriptors.append(accepted_descriptor)
            for descriptor in descriptors:
                accepted = socket.socket(fileno=int(descriptor))
                os.set_inheritable(accepted.fileno(), False)
                await asyncio.get_running_loop().connect_accepted_socket(
                    lambda: self.config.http_protocol_class(config=self.config,server_state=self.server_state,
                        app_state=self.lifespan.state,_loop=asyncio.get_running_loop()),accepted)
            for descriptor in filter(None,os.environ.pop('QWEN_ASR_EVENT_FDS','').split(',')):
                stream=socket.socket(fileno=int(descriptor));stream.setblocking(False)
                os.set_inheritable(stream.fileno(),False)
                self.event_sockets.append(stream)
                asyncio.create_task(self.event_stream(stream))

    def owner(self):
        return importlib.import_module(str(self.config.app).split(':',1)[0])

    async def event_stream(self, stream):
        owner=self.owner();watcher=getattr(owner,'library_watch',None)
        channel=asyncio.Queue(maxsize=1)
        if watcher is not None:watcher.listeners.add(channel)
        try:
            while not self.activity.hibernating:
                payload=f'data: {getattr(watcher,"revision",time.time_ns())}\n\n'.encode()
                await asyncio.get_running_loop().sock_sendall(stream,f'{len(payload):x}\r\n'.encode()+payload+b'\r\n')
                try:await asyncio.wait_for(channel.get(),15)
                except asyncio.TimeoutError:pass
        except (OSError,asyncio.CancelledError):
            pass
        finally:
            if watcher is not None:watcher.listeners.discard(channel)
            stream.close()

    def can_standby(self):
        if not self.activity or self.activity.active:
            return False
        for protocol in self.server_state.connections:
            cycle=getattr(protocol,'cycle',None)
            if cycle and not cycle.response_complete and cycle.scope.get('path')!='/api/library/events':
                return False
        predicate=getattr(self.owner(),'can_enter_standby',None)
        return bool(predicate and predicate())

    async def prepare_standby(self):
        from .paths import PRIVATE_ROOT,ROOT
        from .durable import atomic_json
        import httpx
        directory=PRIVATE_ROOT/'services';cache=directory/'http-cache'
        cache.mkdir(parents=True,exist_ok=True);cache.chmod(0o700)
        defaults={'/api/health','/course/api/status','/api/session','/api/tasks','/api/config','/api/library'}
        urls=[*sorted(defaults),*sorted(self.activity.urls-defaults)][:128]
        self.activity.draining=True;self.activity.interrupted=False;self.activity.released.clear()
        source_revision=getattr(getattr(self.owner(),'library_watch',None),'revision',None)
        entries=[]
        try:
            async with httpx.AsyncClient(timeout=3,trust_env=False) as client:
                for url in urls:
                    if not refreshable(url.split('?',1)[0]):
                        key=hashlib.sha256(url.encode()).hexdigest()
                        if (cache/(key+'.json')).exists() and (cache/(key+'.body')).exists():entries.append(url)
                        continue
                    response=await client.get(f'http://127.0.0.1:{self.config.port}'+url,
                        headers={'X-QwenASR-Standby-Warm':os.getenv('QWEN_ASR_INSTANCE',''),
                                 'Origin':f'http://127.0.0.1:{self.config.port}'})
                    if response.status_code not in {200,301,302,307,308} or len(response.content)>2*1024*1024:
                        continue
                    key=hashlib.sha256(url.encode()).hexdigest()
                    (cache/(key+'.body')).write_bytes(response.content)
                    headers=[(k,v) for k,v in response.headers.items() if k.lower() not in {'content-encoding','transfer-encoding','set-cookie'}]
                    atomic_json(cache/(key+'.json'),{'status':response.status_code,'headers':headers})
                    entries.append(url)
                    if self.activity.interrupted:break
            if (self.activity.interrupted or not self.can_standby() or '/api/health' not in entries
                    or getattr(getattr(self.owner(),'library_watch',None),'revision',None)!=source_revision):
                return False
            owner=self.owner()
            context=getattr(owner,'standby_context',lambda:{})()
            watch=[]
            paths=set(map(Path,context.get('watch_dirs',[ROOT])))
            for watch_directory in list(paths):
                if watch_directory.is_dir():
                    paths.update(path for path in watch_directory.iterdir() if path.is_file()
                                 and path.suffix.lower() in {'.json','.pdf','.png','.md','.py','.html'})
            for path in sorted(paths):
                try:
                    stat=Path(path).stat();watch.append([str(path),[stat.st_ino,stat.st_mtime_ns]])
                except OSError:pass
            self.standby_file=directory/'standby.json'
            atomic_json(self.standby_file,{'root':str(ROOT),'urls':entries,'watch':watch,
                        'revision':getattr(getattr(owner,'library_watch',None),'revision',time.time_ns()),
                        'wake_at':context.get('wake_at'),'deadlines':context.get('deadlines',{}),
                        'knowledge_stamps':context.get('knowledge_stamps',{})})
            # Duplicate listeners and already-open SSE sockets. Uvicorn closes
            # its own descriptors; these copies survive the same-PID exec.
            for server in self.servers:
                for descriptor in server.sockets:
                    duplicate=descriptor.dup();os.set_inheritable(duplicate.fileno(),True)
                    self.standby_sockets.append(duplicate)
            self.activity.hibernating=True
            for protocol in list(self.server_state.connections):
                cycle=getattr(protocol,'cycle',None)
                if protocol.transport.is_closing():continue
                try:duplicate=protocol.transport.get_extra_info('socket').dup()
                except OSError:continue
                os.set_inheritable(duplicate.fileno(),True)
                if cycle and cycle.scope.get('path')=='/api/library/events':self.event_sockets.append(duplicate)
                else:self.idle_sockets.append(duplicate)
                # End the ASGI owner without sending an HTTP end-of-message.
                if cycle:
                    cycle.response_complete=True;cycle.disconnected=True
                self.server_state.connections.discard(protocol)
                protocol.transport.close()
            inherited=[]
            for stream in self.event_sockets:
                if stream.fileno()<0:continue
                if stream.get_inheritable():inherited.append(stream)
                else:
                    try:duplicate=stream.dup()
                    except OSError:continue
                    os.set_inheritable(duplicate.fileno(),True);inherited.append(duplicate)
            self.event_sockets=inherited
            watcher=getattr(owner,'library_watch',None)
            if watcher is not None:
                watcher.draining=True
                for channel in tuple(watcher.listeners):
                    if channel.empty():channel.put_nowait(watcher.revision)
            self.should_exit=True
            return True
        except (OSError,httpx.HTTPError):
            return False
        finally:
            if not self.activity.hibernating:
                for stream in self.standby_sockets:stream.close()
                self.standby_sockets=[]
                self.activity.draining=False;self.activity.released.set()

    async def main_loop(self):
        self._exit_event=asyncio.Event();counter=0
        delay=float(os.getenv('QWEN_ASR_STANDBY_SECONDS','5'))
        while not await self.on_tick(counter):
            if (self.enabled and self.started and time.monotonic()-self.activity.last_action>=delay
                    and self.can_standby() and await self.prepare_standby()):
                break
            try:await asyncio.wait_for(self._exit_event.wait(),1.0)
            except asyncio.TimeoutError:pass
            counter=(counter+10)%864000

    def handle_exit(self,sig,frame):
        self.standby_cancelled=True
        super().handle_exit(sig,frame)
        event=getattr(self,'_exit_event',None)
        if event is not None:event.set()


def run(args):
    from .paths import PRIVATE_ROOT
    from .standby import mode
    mode_path=PRIVATE_ROOT/'services/mode.json';mode_path.parent.mkdir(parents=True,exist_ok=True)
    mode(mode_path,'active')
    config=uvicorn.Config(args.app,host=args.host,port=args.port,http='h11',
                          timeout_graceful_shutdown=args.timeout_graceful_shutdown)
    server=IdleServer(config)
    descriptor=os.environ.pop('QWEN_ASR_LISTEN_FD','')
    sockets=[socket.socket(fileno=int(descriptor))] if descriptor else None
    if sockets:
        for stream in sockets:os.set_inheritable(stream.fileno(),False)
    server.run(sockets=sockets)
    if server.standby_file and server.standby_sockets and not server.standby_cancelled:
        environment=dict(os.environ,QWEN_ASR_STANDBY_STATE=str(server.standby_file),
            QWEN_ASR_LISTEN_FD=str(server.standby_sockets[0].fileno()),
            QWEN_ASR_IDLE_FDS=','.join(str(s.fileno()) for s in server.idle_sockets if s.fileno()>=0),
            QWEN_ASR_EVENT_FDS=','.join(str(s.fileno()) for s in server.event_sockets if s.fileno()>=0))
        os.execve(sys.executable,[sys.executable,'-m','qwen_asr_app.server',*sys.argv[1:]],environment)
    mode(mode_path,'stopped')
