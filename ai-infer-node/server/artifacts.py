"""Add gateway-observed resource metrics to a completed worker bundle."""
import json
import os
import zipfile
from pathlib import Path


def atomic_json(path, value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w',encoding='utf-8') as out:
        os.fchmod(out.fileno(), 0o600)
        json.dump(value,out,ensure_ascii=False)
        out.flush();os.fsync(out.fileno())
    os.replace(temporary,path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def finalize(folder, metrics):
    folder=Path(folder)
    atomic_json(folder/'metadata.json',metrics)
    transcript=folder/'transcript.json'
    if transcript.is_file():
        value=json.loads(transcript.read_text(encoding='utf-8'))
        value['metadata']=metrics
        atomic_json(transcript,value)
    temporary=folder/'result.zip.tmp'
    with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob('*')):
            if path.is_file() and path.name not in ('result.zip','result.zip.tmp'):
                archive.write(path,path.relative_to(folder).as_posix())
    os.chmod(temporary, 0o600)
    with temporary.open('rb') as source:os.fsync(source.fileno())
    os.replace(temporary,folder/'result.zip')
