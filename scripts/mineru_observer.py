"""Observe MinerU stage entry without changing model arguments or results."""
from contextlib import contextmanager
import sys


def install_stage_observer():
    import mineru.utils.timing as timing
    original = timing.stage_timer
    if getattr(original, '_qwen_observer', False):
        return

    @contextmanager
    def observed(stage):
        print('QWENASR_STAGE ' + str(stage), flush=True)
        with original(stage):
            yield

    observed._qwen_observer = True
    # Some runtimes import stage_timer before the parser is constructed.
    for name, module in list(sys.modules.items()):
        if name.startswith('mineru.') and getattr(module, 'stage_timer', None) is original:
            module.stage_timer = observed
    timing.stage_timer = observed
