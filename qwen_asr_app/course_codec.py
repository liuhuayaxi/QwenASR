"""Disposable replay codecs with bounded, acknowledged PCM handoff."""
from __future__ import annotations

import multiprocessing as mp
from pathlib import Path


def _codec_worker(connection, request):
    """Do not advance a source cursor until the parent saved the PCM chunk."""
    try:
        def progress(*values):
            connection.send(('progress', values))

        def chunk(pcm, cursor):
            connection.send(('chunk', pcm, cursor))
            if connection.recv() != 'saved':
                raise RuntimeError('Audio checkpoint was not acknowledged')

        if request['operation'] == 'stream':
            from .course_download import _stream_audio_inline
            result = _stream_audio_inline(request['sources'], chunk, progress,
                                          resume=request.get('resume'))
        elif request['operation'] == 'extract':
            from .smartclass import _extract_audio_inline
            result = _extract_audio_inline(request['sources'], Path(request['destination']),
                progress, sample_seconds=request.get('sample_seconds'),
                sample_offset=request.get('sample_offset', 0))
        else:
            raise ValueError('Unknown replay codec operation')
        connection.send(('result', result))
    except Exception as error:
        try:
            connection.send(('error', str(error)))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def run_codec(operation, sources, *, on_chunk=None, progress=lambda *args: None,
              stop=None, **arguments):
    """One chunk in flight; stopping or callback failure always reaps the codec."""
    from .smartclass import SmartClassError
    if stop is not None and stop.is_set():
        raise SmartClassError('Task stopped; completed audio and transcripts are retained')
    context = mp.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_codec_worker,
        args=(child, {'operation': operation, 'sources': sources, **arguments}),
        name='qwen-replay-codec', daemon=True)
    started = False
    try:
        process.start()
        started = True
        child.close()
        while True:
            if stop is not None and stop.is_set():
                raise SmartClassError('Task stopped; completed audio and transcripts are retained')
            if not parent.poll(.1):
                if not process.is_alive():
                    raise SmartClassError('Replay codec exited before completing the audio')
                continue
            try:
                message = parent.recv()
            except (EOFError, OSError) as error:
                raise SmartClassError('Replay codec connection closed unexpectedly') from error
            if message[0] == 'progress':
                progress(*message[1])
            elif message[0] == 'chunk':
                if on_chunk is None:
                    raise SmartClassError('Missing audio checkpoint handler')
                on_chunk(message[1], message[2])
                parent.send('saved')
            elif message[0] == 'error':
                raise SmartClassError(message[1])
            elif message[0] == 'result':
                process.join(timeout=3)
                if process.is_alive() or process.exitcode != 0:
                    raise SmartClassError('Replay codec did not shut down cleanly')
                return message[1]
            else:
                raise SmartClassError('Invalid replay codec response')
    finally:
        parent.close()
        child.close()
        if started:
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=3)
            if not process.is_alive():
                process.close()
        if operation == 'extract' and arguments.get('destination'):
            Path(arguments['destination']).with_suffix('.partial.flac').unlink(missing_ok=True)
