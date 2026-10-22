"""Provision private config and pin locally verified official model manifests."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import tomllib

NODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE_ROOT))
from server.config import verify_model_manifests


def write_private(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def set_values(text, changes):
    """Update only named scalar keys, retaining unrelated operator settings."""
    for section, values in changes.items():
        lines = text.splitlines(keepends=True)
        headers = [(i, re.fullmatch(r'\s*\[([^\]]+)\]\s*(?:#.*)?', line.strip()))
                   for i, line in enumerate(lines)]
        start = next((i for i, match in headers if match and match[1] == section), None)
        if start is None:
            if lines and not lines[-1].endswith('\n'):
                lines[-1] += '\n'
            lines += ['\n', '[' + section + ']\n']
            start = len(lines) - 1
        end = next((i for i, match in headers if match and i > start), len(lines))
        for key, value in values.items():
            encoded = json.dumps(str(value), ensure_ascii=False)
            pattern = r'\s*(?:' + '|'.join(re.escape(item) for item in
                (key, '"' + key + '"', "'" + key + "'")) + r')\s*='
            index = next((i for i in range(start + 1, end) if re.match(pattern, lines[i])), None)
            line = key + ' = ' + encoded + '\n'
            if index is None:
                if end and not lines[end - 1].endswith('\n'):
                    lines[end - 1] += '\n'
                lines.insert(end, line)
                end += 1
            else:
                lines[index] = line
        text = ''.join(lines)
    tomllib.loads(text)  # Refuse ambiguous or invalid edits before replacing config.
    return text


def initialize(root):
    root = Path(root).resolve()
    path = root / 'config.toml'
    new = not path.exists()
    text = (root / 'config.example.toml' if new else path).read_text()
    cfg = tomllib.loads(text)
    changes = {}
    if new:
        changes = {
            'server': {'data_dir': root / 'data', 'token_file': root / 'private/token'},
            'asr': {'python': root / 'venvs/asr/bin/python',
                    'model_path': root / 'models/Qwen3-ASR-1.7B-hf'},
            'mineru': {'python': root / 'venvs/mineru/bin/python', 'home': root / 'mineru-home'},
        }
    if not cfg['server'].get('worker_cgroup_root'):
        changes.setdefault('server', {})['worker_cgroup_root'] = 'auto'
    text = set_values(text, changes)
    cfg = tomllib.loads(text)
    write_private(path, text)
    token = Path(cfg['server']['token_file']).expanduser()
    if not token.exists():
        write_private(token, secrets.token_urlsafe(48) + '\n')
    token.chmod(0o600)
    return cfg


def provision(root):
    root = Path(root).resolve()
    path = root / 'config.toml'
    text = path.read_text()
    cfg = tomllib.loads(text)
    home = Path(cfg['mineru']['home']).expanduser().resolve()
    python = Path(cfg['mineru']['python']).expanduser().absolute()
    # Only record official-mineru provenance after its own verifier succeeds.
    subprocess.run([str(python.with_name('mineru-kit')), 'models', 'verify',
                    '--tier', 'standard', '--small-backend', cfg['mineru']['small_backend'],
                    '--vlm-engine', cfg['mineru']['vlm_engine']],
                   env={**os.environ, 'MINERU_HOME': str(home)}, check=True)
    files = {}
    model_root = home / 'models'
    for model in sorted(model_root.rglob('*')):
        if not model.is_file() or '.cache' in model.parts or model.name.endswith('.part'):
            continue
        if not model.resolve().is_relative_to(model_root.resolve()):
            raise ValueError('MODEL_MANIFEST_PATH_INVALID')
        with model.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        files[model.relative_to(model_root).as_posix()] = {'bytes': model.stat().st_size, 'sha256': digest}
    mineru_manifest = root / 'models/mineru-manifest.json'
    write_private(mineru_manifest, json.dumps({'source': 'official-mineru',
        'version': cfg['mineru']['version'], 'files': files}, ensure_ascii=False, indent=2) + '\n')
    changes = {}
    for section, manifest in (('asr', root / 'models/asr-manifest.json'), ('mineru', mineru_manifest)):
        changes[section] = {'manifest_file': str(manifest),
            'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest()}
    candidate = set_values(text, changes)
    verified_cfg = tomllib.loads(candidate)
    verified_cfg['asr']['model_path'] = str(Path(cfg['asr']['model_path']).expanduser().resolve())
    verified_cfg['mineru']['home'] = str(home)
    verified = verify_model_manifests(verified_cfg)
    write_private(path, candidate)
    return verified


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('initialize', 'provision'))
    parser.add_argument('--node-root', type=Path, default=NODE_ROOT)
    args = parser.parse_args()
    if args.action == 'initialize':
        initialize(args.node_root)
    else:
        print(json.dumps({'verified_manifests': provision(args.node_root)}))
