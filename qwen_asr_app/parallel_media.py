"""Seekable, disk-backed HTTP media with four bounded download workers."""
from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
import threading
from collections import deque
from contextlib import contextmanager

import httpx

from .smartclass import PORTAL, SmartClassError

BLOCK_BYTES = 8 * 1024 * 1024
PROBE_BYTES = 1024 * 1024
DOWNLOAD_WORKERS = 4


def range_info(response):
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
    return tuple(map(int, match.groups())) if match else None


def read_block(response, start, end, total, stop, abort=None):
    if response.status_code != 206 or range_info(response) != (start, end, total):
        raise SmartClassError("视频服务器分段响应发生变化，已保存音频可继续")
    if response.headers.get("content-encoding", "identity") != "identity":
        raise SmartClassError("视频服务器返回了无法校验的压缩分段")
    data = bytearray()
    for part in response.iter_bytes(64 * 1024):
        if stop.is_set() or (abort and abort.is_set()):
            raise SmartClassError("已暂停下载，已接收音频与转写保留")
        data.extend(part)
        if len(data) > end - start + 1:
            raise SmartClassError("视频服务器分段长度校验失败")
    if len(data) != end - start + 1:
        raise httpx.ReadError("incomplete media range")
    return data


class ParallelMediaReader(io.RawIOBase):
    def __init__(self, client, url, total, first, stop, validator=None):
        super().__init__()
        self.client, self.url, self.total, self.stop = client, url, total, stop
        self.validator = validator
        self.file = tempfile.TemporaryFile(prefix="qwenasr-media-")
        self.file.truncate(total)
        if os.pwrite(self.file.fileno(), first, 0) != len(first):
            self.file.close()
            raise OSError("incomplete cached media write")
        self.position = 0
        self.prefix_bytes = len(first)
        self.condition = threading.Condition()
        self.abort = threading.Event()
        self.error = None
        self.ready = {0} if len(first) >= min(total, BLOCK_BYTES) else set()
        self.pending = deque(range(1 if self.ready else 0, (total + BLOCK_BYTES - 1) // BLOCK_BYTES))
        self.threads = [threading.Thread(target=self._worker, name="course-media-download", daemon=True)
                        for _ in range(min(DOWNLOAD_WORKERS, len(self.pending)))]
        for worker in self.threads:
            worker.start()

    def _worker(self):
        try:
            while not self.abort.is_set() and not self.stop.is_set():
                with self.condition:
                    if not self.pending:
                        return
                    index = self.pending.popleft()
                start = self.prefix_bytes if index == 0 else index * BLOCK_BYTES
                end = min(self.total, (index + 1) * BLOCK_BYTES) - 1
                headers = {"Range": f"bytes={start}-{end}"}
                if self.validator:
                    headers["If-Range"] = self.validator
                for attempt in range(3):
                    try:
                        with self.client.stream("GET", self.url, headers=headers) as response:
                            data = read_block(response, start, end, self.total, self.stop, self.abort)
                        if os.pwrite(self.file.fileno(), data, start) != len(data):
                            raise OSError("incomplete cached media write")
                        del data
                        with self.condition:
                            self.ready.add(index)
                            self.condition.notify_all()
                        break
                    except httpx.HTTPError:
                        if attempt == 2:
                            raise SmartClassError("视频分段连续连接失败，已下载音频与转写保留") from None
                        if self.abort.wait(attempt + 1) or self.stop.is_set():
                            return
        except Exception as exc:
            with self.condition:
                if not self.abort.is_set():
                    self.error = exc
                    self.abort.set()
                self.condition.notify_all()

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        position = offset if whence == io.SEEK_SET else self.position + offset if whence == io.SEEK_CUR else self.total + offset if whence == io.SEEK_END else -1
        if position < 0:
            raise ValueError("invalid media seek")
        self.position = position
        return position

    def read(self, size=-1):
        self._checkClosed()
        if self.position >= self.total or size == 0:
            return b""
        index = self.position // BLOCK_BYTES
        with self.condition:
            # MP4 metadata may be at the end; prioritize decoder seeks over prefetch.
            if index in self.pending:
                self.pending.remove(index)
                self.pending.appendleft(index)
            while index not in self.ready and self.position >= self.prefix_bytes:
                if self.error:
                    raise self.error
                if self.stop.is_set() or self.abort.is_set():
                    raise SmartClassError("已暂停下载，已接收音频与转写保留")
                self.condition.wait(.2)
            if self.stop.is_set():
                raise SmartClassError("已暂停下载，已接收音频与转写保留")
        available = min(self.total - self.position, BLOCK_BYTES - self.position % BLOCK_BYTES)
        if index not in self.ready and self.position < self.prefix_bytes:
            available = min(available, self.prefix_bytes - self.position)
        data = os.pread(self.file.fileno(), available if size < 0 else min(size, available), self.position)
        if not data:
            raise OSError("missing cached media range")
        self.position += len(data)
        return data

    def close(self):
        if self.closed:
            return
        self.abort.set()
        with self.condition:
            self.condition.notify_all()
        for worker in self.threads:
            worker.join()
        self.file.close()
        super().close()


@contextmanager
def parallel_media(url, stop):
    """Yield a streaming range reader, or the URL if Range is unsupported."""
    if not url.startswith("https://"):
        yield url
        return
    with httpx.Client(timeout=httpx.Timeout(20, connect=15), trust_env=False,
                      headers={"User-Agent": "Mozilla/5.0", "Referer": f"{PORTAL}/", "Accept-Encoding": "identity"},
                      limits=httpx.Limits(max_connections=DOWNLOAD_WORKERS + 1)) as client:
        with client.stream("GET", url, headers={"Range": f"bytes=0-{PROBE_BYTES - 1}"}) as response:
            info = range_info(response)
            if response.status_code != 206 or not info:
                # Close without consuming a full video when the server ignores Range.
                reader = None
            else:
                start, end, total = info
                if start != 0 or end != min(total, PROBE_BYTES) - 1 or total <= 0:
                    raise SmartClassError("视频服务器返回了无效的分段大小")
                if shutil.disk_usage(tempfile.gettempdir()).free < total + 256 * 1024**2:
                    raise SmartClassError("磁盘空间不足，无法缓存并行下载的视频")
                first = read_block(response, start, end, total, stop)
                validator = response.headers.get("etag") or response.headers.get("last-modified")
                if validator and validator.startswith("W/"):
                    validator = response.headers.get("last-modified")
                reader = ParallelMediaReader(client, url, total, first, stop, validator)
        try:
            yield reader if reader is not None else url
        finally:
            if reader is not None:
                reader.close()
