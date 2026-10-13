"""Serialize capture ownership changes on the service's event loop."""
import asyncio
import weakref

_locks = weakref.WeakKeyDictionary()


def control_lock():
    return _locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
