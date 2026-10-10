"""Expose pip CUDA as a conventional toolkit without changing system drivers."""
from pathlib import Path
import sysconfig

root = Path(__file__).resolve().parents[1]
source = Path(sysconfig.get_paths()["purelib"]) / "nvidia/cu13"
target = root / "cuda-toolkit"
target.mkdir(exist_ok=True)
def link(path, value):
    if path.is_symlink():
        if path.resolve() == value.resolve():return
        path.unlink()
    if path.exists():raise RuntimeError("Refusing to replace existing toolkit file")
    path.symlink_to(value)
for name in ('bin','include','nvvm'):
    if (source/name).exists():link(target/name,source/name)
(target/'lib64').mkdir(exist_ok=True)
link(target/'lib64/libcudart.so',source/'lib/libcudart.so.13')
link(target/'lib64/libcudart.so.13',source/'lib/libcudart.so.13')
link(target/'lib64/libcuda.so',Path('/usr/lib/wsl/lib/libcuda.so'))
print(target)
