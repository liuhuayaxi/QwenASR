"""Live request bookkeeping used only by the full ASGI workbench."""
import asyncio
import os
import time

SAFE_GETS = {'/api/health', '/api/config', '/api/library', '/api/session', '/api/tasks',
            '/api/ai-tasks', '/api/ai/settings', '/api/ai/secrets', '/api/inference/settings',
            '/api/mineru/settings', '/api/course-files', '/api/notes', '/course/api/status'}
SAFE_PAGES = {'/', '/workspace', '/library', '/tasks', '/course-files', '/course/'}


def cacheable(path):
    if path in SAFE_GETS or path in SAFE_PAGES:
        return True
    parts = path.strip('/').split('/')
    if (len(parts) in {3, 4} and parts[:2] == ['api', 'recordings']
            and (len(parts) == 3 or parts[-1] in {'summary-status', 'chat'})):
        return True
    return (path.startswith('/api/') or path.startswith('/course/api/')) and not any(
        token in path for token in ('choose-', '/oauth/', '/download', '/playback',
                                   '/events', '/reveal', '/clipboard', '/open-pdf'))


def refreshable(path):
    # A passive page must not make a fresh external model/OAuth probe just
    # because the local workbench is preparing to release its runtime.
    return cacheable(path) and not (path.endswith('/models') or path=='/api/ideashell/status')


def save_response(url,status,headers,body):
    import hashlib
    from .paths import PRIVATE_ROOT
    from .durable import atomic_bytes,atomic_json
    encoding=next((v for k,v in headers if k.lower()=='content-encoding'),'')
    if encoding=='gzip':
        import zlib
        body=zlib.decompressobj(16+zlib.MAX_WBITS).decompress(body,2*1024*1024+1)
    elif encoding:
        return False
    if len(body)>2*1024*1024:return False
    directory=PRIVATE_ROOT/'services/http-cache';directory.mkdir(parents=True,exist_ok=True);directory.chmod(0o700)
    key=hashlib.sha256(url.encode()).hexdigest()
    atomic_bytes(directory/(key+'.body'),body)
    fields=[(k,v) for k,v in headers if k.lower() not in {'content-encoding','transfer-encoding','set-cookie'}]
    atomic_json(directory/(key+'.json'),{'status':status,'headers':fields})
    return True


class Activity:
    def __init__(self, application):
        self.application = application
        self.last_action = time.monotonic()
        self.urls = set()
        self.active = 0
        self.draining = False
        self.hibernating = False
        self.interrupted = False
        self.released = asyncio.Event()
        self.released.set()

    async def __call__(self, scope, receive, send):
        kind = scope['type']
        path = scope.get('path', '')
        if kind not in {'http', 'websocket'}:
            return await self.application(scope, receive, send)
        headers = dict(scope.get('headers', []))
        token = os.getenv('QWEN_ASR_INSTANCE', '')
        warm = bool(token) and headers.get(b'x-qwenasr-standby-warm', b'').decode() == token
        if self.draining and not warm:
            self.interrupted = True
            await self.released.wait()
        events = kind == 'http' and path == '/api/library/events'
        if kind == 'websocket' or (scope.get('method') not in {'GET', 'HEAD', 'OPTIONS'} and not path.startswith('/api/service/')):
            self.last_action = time.monotonic()
        candidate=kind=='http' and scope.get('method')=='GET' and cacheable(path) and not warm
        url=None
        if candidate:
            query = scope.get('query_string', b'').decode('ascii', errors='replace')
            url=scope.get('raw_path', path.encode()).decode('ascii', errors='replace') + ('?' + query if query else '')
        if not events and not warm:
            self.active += 1

        response={};chunks=[];size=0
        async def forwarded(message):
            nonlocal size
            # The TCP stream is handed to the light host without ending its
            # chunked event response. Clients retain the same EventSource.
            if events and self.hibernating and message['type'] == 'http.response.body':
                return
            await send(message)
            if candidate and message['type']=='http.response.start':
                response.update(status=message['status'],headers=[(k.decode('latin-1'),v.decode('latin-1')) for k,v in message.get('headers',[])])
            if candidate and message['type']=='http.response.body':
                block=message.get('body',b'');size+=len(block)
                if size<=2*1024*1024:chunks.append(block)
                if not message.get('more_body',False) and response.get('status')==304:
                    # A browser may retain an ETag from before this runtime.
                    # Warm this URL without conditional headers before sleep,
                    # even when its first observed response has no body.
                    self.urls.add(url)
                if not message.get('more_body',False) and size<=2*1024*1024 and response.get('status')==200:
                    content_type=next((v for k,v in response['headers'] if k.lower()=='content-type'),'')
                    if 'application/json' in content_type or path in SAFE_PAGES:
                        try:
                            if save_response(url,response['status'],response['headers'],b''.join(chunks)):
                                self.urls.add(url)
                        except (OSError,ValueError):pass
        try:
            await self.application(scope, receive, forwarded)
        finally:
            if not events and not warm:
                self.active -= 1


def restore_deadlines(drive):
    import json
    saved=json.loads(os.environ.pop('QWEN_ASR_WAKE_DEADLINES','{}'))
    if saved.get('drive') is not None:
        drive.next_run=time.monotonic()+max(0,saved['drive']-time.time())


def companion_ready(status_path, mode_path, pid):
    """Do not sleep before the publisher acknowledges this particular wake."""
    import json
    try:
        status=json.loads(status_path.read_text())
        mode=json.loads(mode_path.read_text())
    except (OSError,ValueError):
        return False
    return (mode.get('pid')==pid and mode.get('mode')=='active'
            and status.get('qwenPid')==pid and status.get('qwenGeneration')==mode.get('at')
            and status.get('status') in {'waiting_for_change','failed','retry_on_next_change'})
