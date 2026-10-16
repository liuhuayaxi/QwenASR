"""Release task-local native caches without unloading active inference work."""
from __future__ import annotations

import asyncio
import ctypes
import gc
import os
import subprocess
import sys

_pending=None
last_reclaim={}


def rss_bytes():
    try:
        if sys.platform=='linux':
            with open('/proc/self/statm') as source:return int(source.read().split()[1])*os.sysconf('SC_PAGE_SIZE')
        return int(subprocess.check_output(['/bin/ps','-o','rss=','-p',str(os.getpid())],text=True).strip())*1024
    except (OSError,ValueError,IndexError):return None


def release_pdf_cache():
    # MuPDF's global store otherwise keeps decoded images after documents and
    # pixmaps are closed. Never race a native PDF operation on another thread.
    from .review_materials import PDF_LOCK
    with PDF_LOCK:
        module=sys.modules.get('fitz') or sys.modules.get('pymupdf')
        tools=getattr(module,'TOOLS',None)
        if tools is None:return 0
        size=tools.store_size
        before=size() if callable(size) else size
        tools.store_shrink(100)
        after=size() if callable(size) else tools.store_size
        # PyMuPDF 1.28 can empty the store but does not expose its byte count.
        return max(0,before-after) if isinstance(before,(int,float)) and isinstance(after,(int,float)) else None


def reclaim_idle_memory(clear_cache=lambda:None):
    before=rss_bytes()
    clear_cache()
    pdf_bytes=release_pdf_cache()
    gc.collect()
    # Ask only this process's allocator to return free pages. Live objects and
    # other apps are unaffected; persistent checkpoints stay on disk.
    try:
        library=ctypes.CDLL(None)
        if sys.platform=='darwin':
            relief=library.malloc_zone_pressure_relief
            relief.argtypes=[ctypes.c_void_p,ctypes.c_size_t];relief.restype=ctypes.c_size_t
            relief(None,0)
        elif sys.platform=='linux':library.malloc_trim(0)
    except (OSError,AttributeError):pass
    last_reclaim.clear()
    last_reclaim.update(rss_before=before,rss_after=rss_bytes(),pdf_cache_released_bytes=pdf_bytes)
    return dict(last_reclaim)


def schedule_idle_reclaim(is_idle,clear_cache=lambda:None):
    """Run after finished coroutine frames are released; debounce sibling jobs."""
    global _pending
    try:loop=asyncio.get_running_loop()
    except RuntimeError:return
    if _pending is not None:_pending.cancel()
    def finish():
        global _pending
        _pending=None
        if is_idle():reclaim_idle_memory(clear_cache)
    _pending=loop.call_later(.5,finish)
