"""Cooperative cancellation that keeps the serial slot until workers exit."""
import asyncio
from threading import Event


class ConversionStopped(Exception):
    pass


def check_stopped(stop: Event | None):
    if stop is not None and stop.is_set():
        raise ConversionStopped("识别已停止，未完成的文件可重新识别")


async def conversion_worker(function, *args, stop: Event, **kwargs):
    worker = asyncio.create_task(asyncio.to_thread(function, *args, stop=stop, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        stop.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise
