"""Join the owned cgroup before executing any model or child process."""
import os
import sys
from pathlib import Path

if __name__ == '__main__':
    scope, module, *args = sys.argv[1:]
    (Path(scope)/'cgroup.procs').write_text(str(os.getpid()))
    os.execv(sys.executable, [sys.executable, '-m', module, *args])
