"""Node-owned file advice and residency observations, never host RAM proof."""
import ctypes
import mmap
import os
from pathlib import Path


def resident_bytes(fd, size):
    if not __import__('sys').platform.startswith('linux') or not size:
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    page = mmap.PAGESIZE
    count = (size + page - 1)//page
    address = libc.mmap(None, size, mmap.PROT_READ, mmap.MAP_PRIVATE, fd, 0)
    if address == ctypes.c_void_p(-1).value:
        return None
    try:
        vector = (ctypes.c_ubyte * count)()
        if libc.mincore(address, size, vector):
            return None
        return min(size, sum(v & 1 for v in vector)*page)
    finally:
        libc.munmap(address, size)


def reclaim_model_cache(cfg):
    if not cfg['server'].get('reclaim_model_file_cache', True) or not hasattr(os, 'posix_fadvise'):
        return {'enabled': False, 'files': 0, 'bytes_advised': 0, 'host_release_state': 'unknown'}
    count, size, errors, observed_before, observed_after, unknown = 0, 0, 0, 0, 0, 0
    roots = [(Path(cfg['asr'].get('model_path', '/nonexistent')), 'model_weights'),
             (Path(cfg['mineru'].get('home', '/nonexistent'))/'models', 'model_weights')]
    # Runtime-library advice is disabled until dedicated-environment ownership
    # and cold-load tradeoffs have independent acceptance. pyvenv.cfg is not proof.
    scopes = {}
    seen = set()
    for root, scope in roots:
        if not root.is_dir():
            continue
        observed = scopes.setdefault(scope, {'files': 0, 'resident_bytes_before': 0,
                                            'resident_bytes_after': 0, 'unknown_files': 0})
        for path in root.rglob('*'):
            eligible = ('.so' in path.name if scope == 'runtime_libraries' else
                        path.suffix.lower() in {'.safetensors', '.bin', '.pt', '.pth', '.onnx', '.gguf', '.pdiparams'})
            if not eligible or path.is_symlink():
                continue
            try:
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino)
                if (not path.is_file() or stat.st_size < 4*1024**2 or identity in seen
                        or not path.resolve().is_relative_to(root.resolve())):
                    continue
                seen.add(identity)
                with path.open('rb') as content:
                    length = os.fstat(content.fileno()).st_size
                    before = resident_bytes(content.fileno(), length)
                    os.posix_fadvise(content.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                    after = resident_bytes(content.fileno(), length)
                count += 1
                observed['files'] += 1
                size += length
                if before is None or after is None:
                    unknown += 1
                    observed['unknown_files'] += 1
                else:
                    observed_before += before
                    observed_after += after
                    observed['resident_bytes_before'] += before
                    observed['resident_bytes_after'] += after
            except OSError:
                errors += 1
    return {'enabled': True, 'files': count, 'bytes_advised': size, 'advice_errors': errors,
            'resident_bytes_before': observed_before, 'resident_bytes_after': observed_after,
            'residency_unknown_files': unknown, 'scope': 'node_model_files_and_private_runtime_libraries',
            'by_scope': scopes,
            'host_release_state': 'unknown', 'bytes_advised_is_reclaimed_bytes': False}


async def bounded_reclaim(cfg):
    import asyncio
    import json
    import sys
    proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(json.dumps(cfg).encode()), 15)
        if proc.returncode:
            raise OSError('CACHE_ADVICE_PROCESS_FAILED')
        return json.loads(out)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


if __name__ == '__main__':
    import json
    import sys
    print(json.dumps(reclaim_model_cache(json.load(sys.stdin))))
