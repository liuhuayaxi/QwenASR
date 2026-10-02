"""Atomic, flushed checkpoints for acknowledged work."""
import json
import os
import tempfile
from pathlib import Path


def atomic_bytes(path: Path, value: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def atomic_json(path: Path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode())
