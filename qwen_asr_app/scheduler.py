"""Non-preemptive GPU scheduling with realtime priority and bounded overtaking."""
import asyncio
import time
from contextlib import asynccontextmanager


class PriorityGate:
    def __init__(self, max_overtakes=8):
        self.waiters = []
        self.busy = False
        self.sequence = 0
        self.overtakes = 0
        self.max_overtakes = max_overtakes

    @asynccontextmanager
    async def slot(self, priority=10):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.sequence += 1
        entry = (priority, self.sequence, time.monotonic(), future)
        self.waiters.append(entry)
        self._grant()
        try:
            await asyncio.shield(future)
        except BaseException:
            if entry in self.waiters:
                self.waiters.remove(entry)
            elif future.done():
                self.busy = False
            self._grant()
            raise
        try:
            yield
        finally:
            self.busy = False
            self._grant()

    def _grant(self):
        if self.busy or not self.waiters:
            return
        oldest = min(self.waiters, key=lambda item: item[1])
        selected = min(self.waiters, key=lambda item: item[:2])
        if self.overtakes >= self.max_overtakes or time.monotonic() - oldest[2] > 30:
            selected = oldest
        self.overtakes = self.overtakes + 1 if selected != oldest else 0
        self.waiters.remove(selected)
        self.busy = True
        selected[3].set_result(None)

    def snapshot(self):
        return {"busy": self.busy, "queued": len(self.waiters),
                "realtime_queued": sum(item[0] < 10 for item in self.waiters)}
