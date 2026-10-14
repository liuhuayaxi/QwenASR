"""Cross-process Metal lease; realtime ASR gets the next batch boundary."""
from __future__ import annotations

import fcntl
import os
import threading
import time
import uuid
from contextlib import contextmanager

from .paths import PRIVATE_ROOT


@contextmanager
def metal_lease(*, realtime=False, timeout=180):
    folder = PRIVATE_ROOT / 'compute-leases'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = folder / f'realtime-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}'
    deadline = time.monotonic() + timeout
    if realtime:
        marker.touch(mode=0o600)
    try:
        with (folder / 'metal.lock').open('a') as lock:
            while True:
                waiting = False
                if not realtime:
                    for path in folder.glob('realtime-*'):
                        try:
                            os.kill(int(path.name.split('-')[1]), 0)
                            waiting = True
                        except ProcessLookupError:
                            path.unlink(missing_ok=True)
                        except (ValueError, OSError):
                            waiting = True
                if not waiting:
                    try:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        pass
                if time.monotonic() >= deadline:
                    raise TimeoutError('本机模型资源繁忙，任务进度已保留')
                time.sleep(.025)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        if realtime:
            marker.unlink(missing_ok=True)
