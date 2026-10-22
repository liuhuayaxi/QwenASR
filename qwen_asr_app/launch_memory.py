"""Per-service allocator policy, applied before the Python child starts."""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping


def service_environment(source: Mapping[str, str] | None = None,
                        platform: str | None = None) -> dict[str, str]:
    """Return a copy; never change the user's shell or override explicit tuning.

    macOS can retain large *freed* allocations even after pressure_relief().
    These libmalloc launch options prefer returning free pages over caching
    them. CPython's small objects use the same reclaimable native allocator
    instead of pinning separate pymalloc arenas. Live memory is never capped.
    QWEN_ASR_MALLOC_TUNING=0 opts out for comparison/troubleshooting.
    """
    env = dict(os.environ if source is None else source)
    if (sys.platform if platform is None else platform) == 'darwin' and env.get('QWEN_ASR_MALLOC_TUNING') != '0':
        env.setdefault('PYTHONMALLOC', 'malloc')
        env.setdefault('MallocAggressiveMadvise', '1')
        env.setdefault('MallocLargeCache', '0')
    return env
