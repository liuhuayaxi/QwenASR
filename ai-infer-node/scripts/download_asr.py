"""Download official weights before inference; pin the downloaded commit."""
import hashlib
import json
import os
import time
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[1]
with (root / 'config.toml').open('rb') as source_config:
    asr_config = tomllib.load(source_config)['asr']
model = asr_config['model_id']
if model not in {'Qwen/Qwen3-ASR-1.7B', 'Qwen/Qwen3-ASR-1.7B-hf'}:
    raise ValueError('Only official Qwen3-ASR 1.7B is allowed')
folder = Path(asr_config['model_path']).expanduser().resolve()
folder.mkdir(parents=True, exist_ok=True)
source = os.environ.get("AI_INFER_MODEL_SOURCE", "huggingface")
if source not in ("huggingface", "modelscope"):
    raise ValueError("AI_INFER_MODEL_SOURCE must be huggingface or modelscope")
revision = ""
expected = {}
if source == "huggingface":
    try:
        from huggingface_hub import HfApi, snapshot_download
        previous = root / "models/asr-manifest.json"
        pinned = os.environ.get("AI_INFER_MODEL_REVISION")
        if not pinned and previous.exists():
            saved = json.loads(previous.read_text())
            if saved.get("source") == "huggingface" and isinstance(saved.get("revision"),str):
                pinned = saved["revision"]
        info = HfApi().model_info(model, revision=pinned, files_metadata=True)
        revision = info.sha
        expected = {entry.rfilename: entry.lfs.sha256 for entry in info.siblings if entry.lfs}
        snapshot_download(model, revision=revision, local_dir=folder, max_workers=4)
    except Exception as exc:
        print("Hugging Face unavailable: " + type(exc).__name__ + "; using Qwen official ModelScope repository", flush=True)
        source = "modelscope"
if source == "modelscope":
    base = "https://modelscope.cn/api/v1/models/" + model
    listing = json.load(urllib.request.urlopen(base + "/repo/files?Revision=master&Recursive=true", timeout=30))
    if not listing.get("Success"):
        raise RuntimeError("ModelScope file listing unavailable")
    (root / "models/asr-source-manifest.json").write_text(json.dumps(listing, ensure_ascii=False, indent=2))
    for item in listing["Data"]["Files"]:
        if item["Type"] != "blob":
            continue
        name = item["Path"]
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise RuntimeError("Invalid model repository path")
        expected[name] = item["Sha256"]
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            digest = hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
            if digest == item["Sha256"]:
                continue
        url = base + "/repo?" + urllib.parse.urlencode({"Revision": item["Revision"], "FilePath": name})
        partial = path.with_suffix(path.suffix + ".part")
        for attempt in range(5):
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {"Range": "bytes=" + str(offset) + "-"} if offset else {}
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                    mode = "ab" if offset and response.status == 206 else "wb"
                    with partial.open(mode) as output:
                        while block := response.read(4 * 1024 * 1024):
                            output.write(block)
                if partial.stat().st_size != item["Size"]:
                    raise RuntimeError("Incomplete model file")
                digest = hashlib.file_digest(partial.open("rb"), "sha256").hexdigest()
                if digest != item["Sha256"]:
                    partial.unlink()
                    raise RuntimeError("Model checksum mismatch")
                os.replace(partial, path)
                print("Verified " + name + " (" + str(item["Size"]) + " bytes)", flush=True)
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(2)
    revision = {item["Path"]: item["Revision"] for item in listing["Data"]["Files"] if item["Type"] == "blob"}
files = {}
for path in sorted(folder.rglob("*")):
    if path.is_file() and ".cache" not in path.parts and not path.name.endswith(".part"):
        digest = hashlib.sha256()
        with path.open("rb") as content:
            while block := content.read(4 * 1024 * 1024):
                digest.update(block)
        name = path.relative_to(folder).as_posix()
        if expected and name in expected and digest.hexdigest() != expected[name]:
            raise RuntimeError("Model checksum mismatch")
        files[name] = {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}
(root / "models" / "asr-manifest.json").write_text(json.dumps({"model": model, "source": source, "revision": revision, "files": files}, indent=2))
print(json.dumps({"model": model, "source": source, "files": len(files)}))
