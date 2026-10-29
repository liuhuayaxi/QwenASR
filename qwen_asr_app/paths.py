"""Where QwenASR keeps its private state.

Default: ``<project>/local-data`` (gitignored). Overrides:
- ``QWEN_ASR_PRIVATE_DIR``: explicit private-state directory.
- ``QWEN_ASR_DATA_DIR``: isolated data root (tests, previews) -> ``<root>/private``.
Older installs kept this state in ``~/.qwenasr``; it is moved here once.
"""
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ["QWEN_ASR_DATA_DIR"]).expanduser() if os.getenv("QWEN_ASR_DATA_DIR") else None
LEGACY_PRIVATE_ROOT = Path.home() / ".qwenasr"
DEFAULT_PRIVATE_ROOT = ROOT / "local-data"
if os.getenv("QWEN_ASR_PRIVATE_DIR"):
    PRIVATE_ROOT = Path(os.environ["QWEN_ASR_PRIVATE_DIR"]).expanduser()
elif DATA_ROOT:
    PRIVATE_ROOT = DATA_ROOT / "private"
else:
    PRIVATE_ROOT = DEFAULT_PRIVATE_ROOT

# Regenerated or stale entries that are not worth carrying over.
_LEGACY_JUNK = {"compute-leases", "diagnostics", "review-materials-cache", "agy-cli",
                "ideashell-chronological-repush.lock", ".DS_Store"}


def migrate_legacy_private_root(legacy: Path = LEGACY_PRIVATE_ROOT, target: Path = DEFAULT_PRIVATE_ROOT) -> list[str]:
    """Move ``~/.qwenasr`` into the project once. Never overwrites: an entry that
    already exists in the target stays in the legacy folder for manual review."""
    if not legacy.is_dir() or legacy.resolve() == target.resolve():
        return []
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    moved = []
    for entry in sorted(legacy.iterdir()):
        destination = target / entry.name
        if entry.name in _LEGACY_JUNK:
            junk = target / ".legacy-junk"
            junk.mkdir(exist_ok=True, mode=0o700)
            if not (junk / entry.name).exists():
                shutil.move(str(entry), str(junk / entry.name))
            continue
        if destination.exists():
            continue
        shutil.move(str(entry), str(destination))
        moved.append(entry.name)
    if moved:
        _rewrite_paths(target, str(legacy), str(target))
    try:
        legacy.rmdir()
    except OSError:
        pass
    return moved


def _rewrite_paths(root: Path, old: str, new: str) -> None:
    """State files store absolute paths (tokens, upload jobs); point them here."""
    skip = {"backups", "note-backups", "runtimes", "mineru4", "local-git-backups", "browser-profile"}
    for path in root.rglob("*"):
        if path.suffix not in {".json", ".jsonl", ".plist"} or any(part in skip for part in path.relative_to(root).parts):
            continue
        try:
            data = path.read_bytes()
            if old.encode() in data:
                path.write_bytes(data.replace(old.encode(), new.encode()))
        except OSError:
            continue


if PRIVATE_ROOT == DEFAULT_PRIVATE_ROOT and not os.getenv("QWEN_ASR_TESTING"):
    migrate_legacy_private_root()


def data_path(name, legacy):
    return DATA_ROOT / name if DATA_ROOT else Path(legacy)
