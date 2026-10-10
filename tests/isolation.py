"""All tests use disposable settings, credentials, libraries and OAuth tokens."""
import atexit
import os
import tempfile

_root = tempfile.TemporaryDirectory(prefix="qwenasr-tests-")
os.environ["QWEN_ASR_DATA_DIR"] = _root.name
os.environ["QWEN_ASR_TESTING"] = "1"
for key in ("QWEN_ASR_STORAGE_ROOT", "QWEN_ASR_SETTINGS_PATH", "QWEN_ASR_LEGACY_RECORDINGS_DIR"):
    os.environ.pop(key, None)
for key in tuple(os.environ):
    if (key.startswith("AI_") or key.startswith("OPENAI_")) and ("KEY" in key or "TOKEN" in key):
        os.environ.pop(key, None)
atexit.register(_root.cleanup)

import socket
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex

def _isolated_connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        raise RuntimeError("Tests must mock network access; real accounts are isolated")
    return _connect(sock, address)

def _isolated_connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        raise RuntimeError("Tests must mock network access; real accounts are isolated")
    return _connect_ex(sock, address)

socket.socket.connect = _isolated_connect
socket.socket.connect_ex = _isolated_connect_ex
