from __future__ import annotations

from pathlib import Path

import numpy as np

from .settings import SAMPLE_RATE


def decode_audio_file(path: Path) -> np.ndarray:
    """Decode common recording formats to mono float32 PCM at 16 kHz."""
    import av

    import tempfile
    # mmap bounds resident memory and avoids concatenating an entire lecture.
    # The anonymous temporary file is removed when this mapping is released.
    with tempfile.TemporaryFile(prefix="qwenasr-pcm-") as scratch:
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
        return np.memmap(scratch, dtype=np.float32, mode="r", shape=(samples,))


def vad_split_ranges(
    audio: np.ndarray,
    start_sample: int = 0,
    target_seconds: float = 90.0,
    min_seconds: float = 55.0,
    max_seconds: float = 120.0,
) -> list[tuple[int, int]]:
    """Split long audio near low-energy frames while keeping bounded chunks."""
    total = len(audio)
    start_sample = max(0, min(int(start_sample), total))
    if total - start_sample <= int(max_seconds * SAMPLE_RATE):
        return [(start_sample, total)] if total > start_sample else []

    frame_samples = int(0.10 * SAMPLE_RATE)
    usable = total - (total % frame_samples)
    if usable:
        framed = np.asarray(audio[:usable], dtype=np.float32).reshape(-1, frame_samples)
        energies = np.concatenate([np.mean(np.square(framed[i:i + 1000]), axis=1, dtype=np.float64) for i in range(0, len(framed), 1000)])
    else:
        energies = np.empty(0, dtype=np.float64)

    ranges: list[tuple[int, int]] = []
    cursor = start_sample
    min_samples = int(min_seconds * SAMPLE_RATE)
    target_samples = int(target_seconds * SAMPLE_RATE)
    max_samples = int(max_seconds * SAMPLE_RATE)
    search_radius = int(25 * SAMPLE_RATE)
    while total - cursor > max_samples:
        desired = cursor + target_samples
        low = max(cursor + min_samples, desired - search_radius)
        high = min(cursor + max_samples, desired + search_radius, total)
        low_frame = max(0, low // frame_samples)
        high_frame = min(len(energies), max(low_frame + 1, high // frame_samples))
        if high_frame > low_frame:
            boundary_frame = low_frame + int(np.argmin(energies[low_frame:high_frame]))
            boundary = min(total, (boundary_frame + 1) * frame_samples)
        else:
            boundary = min(total, desired)
        if boundary <= cursor:
            boundary = min(total, cursor + target_samples)
        ranges.append((cursor, boundary))
        cursor = boundary
    if cursor < total:
        if ranges and total - cursor < int(20 * SAMPLE_RATE):
            previous_start, _ = ranges.pop()
            ranges.append((previous_start, total))
        else:
            ranges.append((cursor, total))
    return ranges


def convert_audio_to_aac(source_path: Path, destination_path: Path) -> None:
    """Transcode an audio file to mono 16 kHz AAC/ADTS with an atomic replace."""
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
            output_stream.bit_rate = 64_000
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
