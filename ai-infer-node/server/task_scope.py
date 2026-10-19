"""Delegated cgroup v2 ownership survives process-group/setsid changes."""
import os
from pathlib import Path


class TaskScope:
    def __init__(self, root, ident):
        if not root or not ident or any(c not in '0123456789abcdef' for c in ident):
            raise RuntimeError('WORKER_CGROUP_DELEGATION_REQUIRED')
        entries = Path('/proc/self/cgroup').read_text().splitlines()
        relative = next(line.split(':', 2)[2] for line in entries if line.startswith('0::'))
        own = (Path('/sys/fs/cgroup')/relative.lstrip('/')).resolve()
        root = own if root == 'auto' else Path(root).resolve()
        if (root != own or not root.name.endswith('.service') or root.stat().st_uid != os.getuid()
                or not (root/'cgroup.controllers').is_file()):
            raise RuntimeError('WORKER_CGROUP_NOT_OWNED_BY_SERVICE')
        self.path = root / ('ai-job-' + ident)
        self.path.mkdir(exist_ok=False)
        if not (self.path/'cgroup.kill').exists():
            raise RuntimeError('WORKER_CGROUP_KILL_UNAVAILABLE')

    def stop(self):
        (self.path/'cgroup.kill').write_text('1')

    def observation(self):
        values = dict(line.split() for line in (self.path/'cgroup.events').read_text().splitlines())
        pids = [int(x) for x in (self.path/'cgroup.procs').read_text().split()]
        return {'state': 'observed', 'path': str(self.path), 'populated': values.get('populated') != '0', 'pids': pids}

    def close(self):
        if self.observation()['populated']:
            raise RuntimeError('WORKER_CGROUP_STILL_POPULATED')
        self.path.rmdir()


def observe_saved(path):
    if not path:
        return {'state': 'unknown', 'populated': True}
    root = Path(path)
    try:
        events = dict(line.split() for line in (root/'cgroup.events').read_text().splitlines())
        return {'state': 'observed', 'populated': events.get('populated') != '0'}
    except FileNotFoundError:
        # A removed cgroup has no live members, only if its parent is observable.
        if root.parent.is_dir() and (root.parent/'cgroup.events').is_file():
            return {'state': 'observed', 'populated': False}
        return {'state': 'unknown', 'populated': True}
    except OSError:
        return {'state': 'unknown', 'populated': True}
