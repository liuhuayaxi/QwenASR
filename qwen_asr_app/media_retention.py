"""Keep canonical media; derived files belong to a single operation."""
import asyncio
import shutil
from pathlib import Path
from fastapi.responses import FileResponse


DISPOSABLE_NAMES = {
    '.course-source.flac', '.course-chunks', '.recording-recovery.wav',
    '.playback.m4a', '.ideashell-audio-parts.json',
    'recording-and-transcript.zip', '.versions',
}


def disposable_name(name):
    return name in DISPOSABLE_NAMES or name.startswith(('.ideashell-audio-part-', '.playback-'))


async def file_worker(function, *args, **kwargs):
    """Drain a cancelled file worker before its caller removes the directory."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        await asyncio.gather(worker, return_exceptions=True)
        raise


class TemporaryMediaResponse(FileResponse):
    def __init__(self, path, *, directory, **kwargs):
        super().__init__(path, **kwargs)
        self.directory = Path(directory)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.to_thread(shutil.rmtree, self.directory, ignore_errors=True)
