"""Incremental video1 audio decoder. Only one bounded PCM block is in memory."""
from __future__ import annotations

import time
import threading

from .lazy import lazy_module

from .smartclass import PORTAL, SmartClassError, media_url
from .parallel_media import parallel_media
import httpx

np = lazy_module("numpy")
av = lazy_module("av")

RATE = 16000


def stream_audio(sources, on_chunk, progress=lambda *args: None, stop=None, resume=None):
    """Emit ~60 second PCM chunks; each callback acknowledges a durable checkpoint.

    Cursor positions count decoded 16 kHz samples inside the original source.
    Seeking includes a preroll and clips precisely at the acknowledged position.
    """
    stop = stop or threading.Event()
    resume = resume or {"source": 0, "samples": 0}
    manifest = []
    for index, item in enumerate(sources):
        if index < resume["source"]:
            continue
        committed = int(resume["samples"]) if index == resume["source"] else 0
        received = committed
        pending = np.empty(0, dtype="<i2")
        last_notice = 0
        # Reconnect only the media read. Login credentials are never sent here.
        for attempt in range(3):
            if stop.is_set():
                raise SmartClassError("已停止下载，已接收音频与转写保留")
            try:
                with parallel_media(media_url(item["url"]), stop) as media, av.open(media, options={
                    "rw_timeout": "20000000", "user_agent": "Mozilla/5.0", "protocol_whitelist": "https,tls,tcp",
                    "headers": f"Referer: {PORTAL}/\r\n",
                }) as source:
                    stream = next(iter(source.streams.audio), None)
                    if stream is None:
                        raise SmartClassError("video1 中没有音轨")
                    available = float(stream.duration * stream.time_base) if stream.duration else float(item["duration"])
                    if available < float(item["duration"]) * .95:
                        raise SmartClassError("video1 音轨仍在处理，请稍后刷新")
                    origin = float((stream.start_time or 0) * stream.time_base)
                    if received:
                        target = max(0, received / RATE - 1)
                        source.seek(int((origin + target) / stream.time_base), stream=stream)
                    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)

                    def consume(frame):
                        nonlocal pending, received, committed, last_notice
                        values = frame.to_ndarray().reshape(-1).astype("<i2", copy=False)
                        position = round((float(frame.pts * frame.time_base) - origin) * RATE) if frame.pts is not None else received
                        trim = max(0, received - position)
                        if trim >= len(values):
                            return
                        if position > received + 4:
                            raise SmartClassError("媒体时间戳不连续，已停止以免丢失音频")
                        values = values[trim:]
                        pending = np.concatenate((pending, values))
                        received += len(values)
                        if len(pending) >= 75 * RATE:
                            # Find a low-energy boundary in 50–70 s, retaining the tail.
                            energy = np.mean(pending[50*RATE:70*RATE].astype(np.float32).reshape(-1, RATE//10)**2, axis=1)
                            cut = 50*RATE + (int(np.argmin(energy)) + 1) * (RATE//10)
                            cursor = {"source": index, "samples": committed + cut}
                            on_chunk(pending[:cut].tobytes(), cursor)
                            committed += cut
                            pending = pending[cut:]
                        if time.monotonic() - last_notice > 1:
                            progress(index, received / RATE, available)
                            last_notice = time.monotonic()

                    for frame in source.decode(stream):
                        if stop.is_set():
                            raise SmartClassError("已停止下载，已接收音频与转写保留")
                        for output in resampler.resample(frame):
                            consume(output)
                    for output in resampler.resample(None):
                        consume(output)
                    if received < (available - .5) * RATE:
                        raise OSError("incomplete media")
                    if len(pending):
                        on_chunk(pending.tobytes(), {"source": index + 1, "samples": 0})
                        committed += len(pending)
                    manifest.append({"video_id": item["video_id"], "segment_id": item["segment_id"],
                                     "duration_seconds": received / RATE, "source_title": item["title"],
                                     "source_start": item["start"]})
                break
            except SmartClassError:
                raise
            except (OSError, httpx.HTTPError, av.error.FFmpegError):
                if attempt == 2:
                    raise SmartClassError("视频服务器连续连接失败；已下载分段保留，恢复校园网络后可继续") from None
                if stop.wait(attempt + 1):
                    raise SmartClassError("已停止下载，已接收音频与转写保留")
    return manifest
