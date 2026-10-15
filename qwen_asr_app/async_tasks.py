"""Await owned work without releasing resources on repeated cancellation."""
from __future__ import annotations

import asyncio
import threading
from typing import TypeVar

_T = TypeVar("_T")


async def await_owned_task(
    task: asyncio.Task[_T], *, stop: threading.Event | None = None
) -> _T:
    """Drain a task before propagating cancellation to its resource owner.

    Cancelling an asyncio wrapper cannot stop its running worker thread.
    Shield *every* wait, including cleanup after a second cancel(), so callers
    keep their locks until the owned work actually finishes. A cooperative
    worker can also observe ``stop``. No other owner should cancel ``task``.

    Once the caller is cancelled, cancellation takes precedence over a later
    worker exception; awaiting the task still retrieves that exception.
    """
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            if stop is not None:
                stop.set()
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        else:
            if cancelled:
                raise asyncio.CancelledError
            return result
