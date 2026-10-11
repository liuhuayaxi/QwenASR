"""Explicit fake cgroup for portable lifecycle tests; no hardware claim."""
from pathlib import Path
import tempfile


class FakeScope:
    def __init__(self, root, ident):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-cgroup-')
        self.path = Path(self.temp.name)
        (self.path/'cgroup.procs').touch()
    def stop(self):
        import contextlib, os, signal
        value = (self.path/'cgroup.procs').read_text().strip()
        if value:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(int(value), signal.SIGKILL)
    def observation(self):
        return {'state': 'observed', 'populated': False, 'pids': [], 'test_double': True}
    def close(self): self.temp.cleanup()


def config(raw):
    raw['server']['gpu_coordination_dir'] = str(Path(raw['server']['data_dir'])/'synthetic-coordination')
    raw.setdefault('asr', {}).update(model_id='Qwen/Qwen3-ASR-1.7B-hf', dtype='bfloat16')
    raw.setdefault('mineru', {}).update(tier='advanced', vlm_engine='vllm')
    return raw
