"""Broadcast library changes without a polling timer or a lost-wakeup window."""
from __future__ import annotations

import asyncio


class ChangeSignal:
    def __init__(self):
        self.version = 0
        self._event = None
        self._loop = None
        self.callbacks = set()

    def notify(self, paths=None):
        # Codec/import workers may persist metadata on another thread. Deliver
        # callbacks and asyncio wakeups on the owning service loop.
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if self._loop is not None and self._loop.is_running() and current is not self._loop:
            self._loop.call_soon_threadsafe(self.notify, paths)
            return
        self.version += 1
        event, self._event = self._event, None
        if event is not None:
            event.set()
        for callback in tuple(self.callbacks):
            callback(paths)

    async def wait(self, since, timeout=None):
        if since != self.version:
            return
        loop = asyncio.get_running_loop()
        if self._event is None or self._loop is not loop:
            self._event = asyncio.Event()
            self._loop = loop
        try:
            if timeout is None:
                await self._event.wait()
            else:
                await asyncio.wait_for(self._event.wait(), max(0, timeout))
        except asyncio.TimeoutError:
            pass


library_changes = ChangeSignal()
