"""Shared worker helpers, deliberately importable without ML dependencies."""
import argparse
import ctypes
import json
import os
import signal
import sys
import time
import zipfile
from pathlib import Path


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as out:
        os.fchmod(out.fileno(), 0o600)
        json.dump(value, out, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def request():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads(args.request.read_text(encoding="utf-8"))
    # Linux kills the worker if its gateway is killed outside systemd as well.
    if os.name == "posix" and Path("/proc/self").exists():
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)
        if os.getppid() != cfg["parent_pid"]:
            raise RuntimeError("Parent gateway exited before worker startup")
    return cfg


def event(cfg, state, progress=0):
    atomic_json(Path(cfg["job_dir"]) / "event.json", {"state": state, "progress": progress, "time": time.time()})


def finish(cfg, metrics):
    event(cfg, "saving", 98)
    output = Path(cfg["job_dir"]) / "pending-result"
    atomic_json(output / "metadata.json", metrics)
    with zipfile.ZipFile(output / "result.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file() and path.name != "result.zip":
                archive.write(path, path.relative_to(output).as_posix())
    os.chmod(output, 0o700)
    os.chmod(output / "result.zip", 0o600)
    for path in output.rglob('*'):
        if path.is_file():
            with path.open('rb') as inp:
                os.fsync(inp.fileno())
    # Visibility of the whole result bundle changes atomically on the same FS.
    os.replace(output, Path(cfg["job_dir"]) / "result")
    fd = os.open(cfg['job_dir'], os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def offline():
    os.environ["PATH"] = str(Path(sys.prefix) / "bin") + os.pathsep + os.environ.get("PATH", "")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                      DO_NOT_TRACK="1", TOKENIZERS_PARALLELISM="false")


def run(main):
    cfg = request()
    offline()
    try:
        main(cfg)
    except Exception as exc:
        # Never expose upstream exception strings, which can contain input text.
        code = type(exc).__name__
        if str(exc) == 'ASR_TOKEN_LIMIT':
            code = 'ASR_TOKEN_LIMIT'
        if "out of memory" in str(exc).lower():
            code = "GPU_OUT_OF_MEMORY"
        atomic_json(Path(cfg["job_dir"]) / "failure.json", {"code": code})
        import traceback
        # Keep frame locations for diagnosis, omit exception values and source
        # line strings that could contain document/audio/prompt content.
        frames = [{"module": Path(frame.filename).name, "line": frame.lineno,
                   "function": frame.name} for frame in traceback.extract_tb(exc.__traceback__)]
        print(json.dumps({"error": code, "frames": frames}), file=sys.stderr, flush=True)
        raise SystemExit(1)
