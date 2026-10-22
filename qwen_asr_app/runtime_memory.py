"""Release task-local native caches without unloading active inference work."""
from __future__ import annotations

import asyncio
import ctypes
import gc
import os
import sys
from functools import lru_cache

_pending=None
last_reclaim={}


class _ProcTaskInfo(ctypes.Structure):
    # Apple's public proc_taskinfo ABI (xnu/bsd/sys/proc_info.h).
    _fields_ = [(name, ctypes.c_uint64) for name in
                ('virtual_size', 'resident_size', 'total_user', 'total_system', 'threads_user', 'threads_system')]
    _fields_ += [(name, ctypes.c_int32) for name in
                 ('policy', 'faults', 'pageins', 'cow_faults', 'messages_sent', 'messages_received',
                  'syscalls_mach', 'syscalls_unix', 'context_switches', 'threads', 'running_threads', 'priority')]


@lru_cache(maxsize=1)
def _darwin_proc_info():
    function = ctypes.CDLL('/usr/lib/libproc.dylib').proc_pidinfo
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    return function


def _darwin_rss_bytes(pid=None):
    info = _ProcTaskInfo()
    count = _darwin_proc_info()(os.getpid() if pid is None else pid, 4, 0, ctypes.byref(info), ctypes.sizeof(info))
    return int(info.resident_size) if count == ctypes.sizeof(info) else None


def rss_bytes(pid=None):
    try:
        if sys.platform=='linux':
            path = '/proc/self/statm' if pid is None else f'/proc/{int(pid)}/statm'
            with open(path) as source:return int(source.read().split()[1])*os.sysconf('SC_PAGE_SIZE')
        if sys.platform == 'darwin':
            return _darwin_rss_bytes(pid)
        return None
    except (OSError,AttributeError,ValueError,IndexError):return None


def release_pdf_cache():
    # MuPDF's global store otherwise keeps decoded images after documents and
    # pixmaps are closed. Never race a native PDF operation on another thread.
    # Do not import PDF infrastructure just to clean up an idle server.
    module=sys.modules.get('fitz') or sys.modules.get('pymupdf')
    if module is None:return 0
    from .native_pdf import PDF_LOCK
    with PDF_LOCK:
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
