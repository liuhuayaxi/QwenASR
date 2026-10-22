"""Read-only Windows host CPU and NVIDIA GPU occupancy; no model operations."""
import asyncio
import contextlib
import json
import math
from pathlib import Path
import shutil
import time


def percent(value):
    try:
        number = float(value)
        return round(number, 1) if not isinstance(value, bool) and math.isfinite(number) and 0 <= number <= 100 else None
    except (TypeError, ValueError):
        return None


async def output(*args):
    process = None
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await asyncio.wait_for(process.communicate(), 2.5)
        return stdout.decode('utf-8-sig').strip() if process.returncode == 0 else ''
    except (OSError, UnicodeError, asyncio.TimeoutError):
        return ''
    finally:
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def observe():
    powershell = shutil.which('powershell.exe')
    standard = Path('/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
    if not powershell and standard.is_file():
        powershell = str(standard)
    command = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
               "Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter \"Name='_Total'\" "
               "| Select-Object -ExpandProperty PercentProcessorTime | ConvertTo-Json -Compress")
    async def cpu():
        if not powershell:
            return None
        try:
            return percent(json.loads(await output(powershell, '-NoProfile', '-NonInteractive', '-Command', command)))
        except ValueError:
            return None
    async def gpu():
        binary = shutil.which('nvidia-smi') or '/usr/lib/wsl/lib/nvidia-smi'
        # The gateway schedules GPU 0; never substitute memory usage for load.
        return percent(await output(binary, '-i', '0', '--query-gpu=utilization.gpu', '--format=csv,noheader,nounits'))
    cpu_value, gpu_value = await asyncio.gather(cpu(), gpu())
    return {'device': 'Windows', 'scope': 'host', 'cpu_percent': cpu_value,
            'gpu_percent': gpu_value, 'sampled_at': time.time()}


def install(app):
    lock, cache = asyncio.Lock(), []
    @app.get('/v1/resources/usage')
    async def usage():
        async with lock:
            if not cache or time.monotonic() - cache[0] >= 5:
                cache[:] = [time.monotonic(), await observe()]
        return {'ok': True, 'data': dict(cache[1])}
