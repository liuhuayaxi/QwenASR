"""Disposable codec workers; audio samples stay in files, not JSON or a resident codec runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from .lazy import lazy_module
from .settings import SAMPLE_RATE

np = lazy_module('numpy')


def audio_operation(operation: str, *, output_fd: int | None = None, **arguments):
    for name in ('source', 'destination'):
        if name in arguments:
            arguments[name] = str(Path(arguments[name]).resolve())
    if 'destinations' in arguments:
        arguments['destinations'] = [str(Path(p).resolve()) for p in arguments['destinations']]
    request = {'operation': operation, **arguments}
    inherited = ()
    if output_fd is not None:
        request['output_fd'] = output_fd
        inherited = (output_fd,)
    result = subprocess.run([sys.executable, '-m', 'qwen_asr_app.native_audio'],
        input=json.dumps(request), capture_output=True, text=True, timeout=600,
        pass_fds=inherited, cwd=Path(__file__).resolve().parent.parent)
    try:
        response = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError('Audio worker did not return a valid result') from None
    if result.returncode:
        raise ValueError(response.get('error') or 'Audio worker failed')
    return response['result']


def _decode_pcm(path: Path, output_fd: int) -> int:
    """Decode common recording formats to mono float32 PCM at 16 kHz."""
    import av

    # mmap bounds resident memory and avoids concatenating an entire lecture.
    # The anonymous temporary file is removed when this mapping is released.
    with os.fdopen(output_fd, 'wb', closefd=False) as scratch:
        samples = 0
        with av.open(str(path)) as container:
            audio_stream = next((stream for stream in container.streams if stream.type == "audio"), None)
            if audio_stream is None:
                raise ValueError("文件中没有找到音轨")
            if audio_stream.duration is not None and audio_stream.time_base and float(audio_stream.duration * audio_stream.time_base) > 7201:
                raise ValueError("单个录音最长支持 2 小时")
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=SAMPLE_RATE)
            def write(outputs):
                nonlocal samples
                for output in outputs:
                    chunk = output.to_ndarray().reshape(-1).astype(np.float32, copy=False)
                    samples += len(chunk)
                    if samples > SAMPLE_RATE * 7200:
                        raise ValueError("单个录音最长支持 2 小时")
                    scratch.write(chunk.tobytes())
            for frame in container.decode(audio_stream):
                write(resampler.resample(frame))
            write(resampler.resample(None))
        if not samples:
            raise ValueError("录音中没有可解码的音频")
        scratch.flush()
        return samples



def convert_audio_to_aac(
    source_path: Path, destination_path: Path, *, bit_rate: int = 64_000
) -> None:
    """Transcode audio to mono 16 kHz AAC/ADTS with an atomic replace.

    ``bit_rate`` is configurable for export targets with a smaller attachment
    limit. The normal recording archive keeps the existing 64 kbps setting;
    IdeaShell can request a smaller speech-oriented copy without changing the
    user's source recording.
    """
    import av

    temp_path = destination_path.with_suffix(".aac.tmp")
    temp_path.unlink(missing_ok=True)
    try:
        with av.open(str(source_path)) as source, av.open(
            str(temp_path), mode="w", format="adts"
        ) as target:
            input_stream = next((stream for stream in source.streams if stream.type == "audio"), None)
            if input_stream is None:
                raise ValueError("文件中没有找到音轨")
            output_stream = target.add_stream("aac", rate=SAMPLE_RATE)
            output_stream.layout = "mono"
            output_stream.bit_rate = int(bit_rate)
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=SAMPLE_RATE)
            decoded_samples = 0
            for frame in source.decode(input_stream):
                for output in resampler.resample(frame):
                    decoded_samples += output.samples
                    for packet in output_stream.encode(output):
                        target.mux(packet)
            for output in resampler.resample(None):
                decoded_samples += output.samples
                for packet in output_stream.encode(output):
                    target.mux(packet)
            if decoded_samples == 0:
                silence = av.AudioFrame.from_ndarray(
                    np.zeros((1, 1024), dtype=np.float32), format="fltp", layout="mono"
                )
                silence.sample_rate = SAMPLE_RATE
                for packet in output_stream.encode(silence):
                    target.mux(packet)
            for packet in output_stream.encode(None):
                target.mux(packet)
        temp_path.replace(destination_path)
    finally:
        temp_path.unlink(missing_ok=True)



def split_audio_to_aac(
    source_path: Path, destination_paths: list[Path], *, bit_rate: int = 32_000
) -> None:
    """Split one recording into equal-duration mono AAC parts atomically.

    The source is decoded twice so the split is based on actual decoded sample
    count rather than a potentially inaccurate container duration. Each part
    gets its own AAC stream and can therefore be uploaded as one attachment.
    """
    import contextlib
    import tempfile
    import av

    if len(destination_paths) < 2:
        raise ValueError("录音拆分至少需要两个目标文件")
    destinations = [Path(path) for path in destination_paths]
    temporary = [path.with_suffix(path.suffix + ".tmp") for path in destinations]
    for path in temporary:
        path.unlink(missing_ok=True)

    def decoded_sample_count() -> int:
        total = 0
        with av.open(str(source_path)) as source:
            input_stream = next((stream for stream in source.streams if stream.type == "audio"), None)
            if input_stream is None:
                raise ValueError("文件中没有找到音轨")
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=SAMPLE_RATE)
            for frame in source.decode(input_stream):
                total += sum(output.samples for output in resampler.resample(frame))
            total += sum(output.samples for output in resampler.resample(None))
        if not total:
            raise ValueError("录音中没有可解码的音频")
        return total

    total_samples = decoded_sample_count()
    thresholds = [round(total_samples * index / len(destinations)) for index in range(1, len(destinations))]
    try:
        with av.open(str(source_path)) as source, contextlib.ExitStack() as stack:
            input_stream = next((stream for stream in source.streams if stream.type == "audio"), None)
            if input_stream is None:
                raise ValueError("文件中没有找到音轨")
            targets = [stack.enter_context(av.open(str(path), "w", format="adts")) for path in temporary]
            output_streams = []
            for target in targets:
                stream = target.add_stream("aac", rate=SAMPLE_RATE)
                stream.layout = "mono"
                stream.bit_rate = int(bit_rate)
                output_streams.append(stream)
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=SAMPLE_RATE)
            part = 0
            written = 0

            def encode_frame(frame: Any) -> None:
                nonlocal part, written
                array = frame.to_ndarray()
                offset = 0
                while offset < frame.samples:
                    if part < len(thresholds) and written >= thresholds[part]:
                        for packet in output_streams[part].encode(None):
                            targets[part].mux(packet)
                        part += 1
                        written = 0
                    remaining = (thresholds[part] - (sum(thresholds[:part]) + written)) if part < len(thresholds) else frame.samples - offset
                    take = max(1, min(frame.samples - offset, remaining))
                    chunk = array[:, offset:offset + take]
                    encoded = av.AudioFrame.from_ndarray(chunk, format="fltp", layout="mono")
                    encoded.sample_rate = SAMPLE_RATE
                    for packet in output_streams[part].encode(encoded):
                        targets[part].mux(packet)
                    written += take
                    offset += take

            for frame in source.decode(input_stream):
                for output in resampler.resample(frame):
                    encode_frame(output)
            for output in resampler.resample(None):
                encode_frame(output)
            for index in range(part, len(output_streams)):
                for packet in output_streams[index].encode(None):
                    targets[index].mux(packet)
        for temp, destination in zip(temporary, destinations):
            temp.replace(destination)
    finally:
        for temp in temporary:
            temp.unlink(missing_ok=True)



def split_audio_to_aac_lossless(
    source_path: Path, destination_paths: list[Path]
) -> None:
    """Split an ADTS AAC file by packet bytes without re-encoding.

    This is the preferred IdeaShell path: the AAC frames are copied exactly,
    so the split changes duration grouping only and does not add generation
    loss. It deliberately rejects non-ADTS inputs and lets the caller use the
    speech bitrate fallback for those files.
    """
    import av

    if len(destination_paths) < 2:
        raise ValueError("录音拆分至少需要两个目标文件")
    with av.open(str(source_path)) as container:
        stream = next((item for item in container.streams if item.type == "audio"), None)
        if stream is None or stream.codec_context.name != "aac":
            raise ValueError("当前录音不是可无损拆分的 AAC 音频")
        packets = [packet for packet in container.demux(stream) if packet.dts is not None]
    if len(packets) < len(destination_paths):
        raise ValueError("录音数据不足，无法拆分")
    temporary = [Path(path).with_suffix(Path(path).suffix + ".tmp") for path in destination_paths]
    for path in temporary:
        path.unlink(missing_ok=True)
    try:
        size = len(packets) // len(destination_paths)
        for index, destination in enumerate(destination_paths):
            begin = index * size
            end = len(packets) if index == len(destination_paths) - 1 else (index + 1) * size
            Path(temporary[index]).write_bytes(b"".join(bytes(packet) for packet in packets[begin:end]))
        for temp, destination in zip(temporary, destination_paths):
            Path(temp).replace(destination)
    finally:
        for temp in temporary:
            Path(temp).unlink(missing_ok=True)



def make_playback_audio(source_path, output_path):
    """Remux archived AAC into a seekable MP4 container without re-encoding."""
    import av
    import uuid
    temporary = output_path.with_name('.playback-' + uuid.uuid4().hex + '.m4a')
    try:
        with av.open(str(source_path)) as source, av.open(str(temporary), 'w', format='mp4') as target:
            audio = next(stream for stream in source.streams if stream.type == 'audio')
            if audio.codec_context.name == 'aac':
                stream = target.add_stream_from_template(audio)
                for packet in source.demux(audio):
                    if packet.dts is None:
                        continue
                    packet.stream = stream
                    target.mux(packet)
            else:
                stream = target.add_stream('aac', rate=SAMPLE_RATE)
                stream.layout = 'mono'
                stream.bit_rate = 64000
                resampler = av.AudioResampler(format='fltp', layout='mono', rate=SAMPLE_RATE)
                for frame in source.decode(audio):
                    for converted in resampler.resample(frame):
                        for packet in stream.encode(converted):
                            target.mux(packet)
                for frame in resampler.resample(None):
                    for packet in stream.encode(frame):
                        target.mux(packet)
                for packet in stream.encode(None):
                    target.mux(packet)
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)



def _execute(request):
    operation = request['operation']
    source = Path(request['source'])
    if operation == 'decode':
        return _decode_pcm(source, int(request['output_fd']))
    if operation == 'aac':
        return convert_audio_to_aac(source, Path(request['destination']), bit_rate=request['bit_rate'])
    if operation == 'split':
        return split_audio_to_aac(source, [Path(p) for p in request['destinations']], bit_rate=request['bit_rate'])
    if operation == 'split_lossless':
        return split_audio_to_aac_lossless(source, [Path(p) for p in request['destinations']])
    if operation == 'playback':
        return make_playback_audio(source, Path(request['destination']))
    raise ValueError('Unknown audio operation')


if __name__ == '__main__':
    try:
        result = _execute(json.loads(sys.stdin.read()))
        print(json.dumps({'result': result}))
    except Exception as exc:
        print(json.dumps({'error': str(exc)}))
        sys.exit(1)
