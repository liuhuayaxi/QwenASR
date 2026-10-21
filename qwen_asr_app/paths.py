"""Cross-platform application data paths."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ["QWEN_ASR_DATA_DIR"]).expanduser() if os.getenv("QWEN_ASR_DATA_DIR") else None
PRIVATE_ROOT = DATA_ROOT / "private" if DATA_ROOT else Path.home() / ".qwenasr"

def data_path(name, legacy):
    return DATA_ROOT / name if DATA_ROOT else Path(legacy)

def secure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return path
