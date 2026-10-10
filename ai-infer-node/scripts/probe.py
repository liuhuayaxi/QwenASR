import argparse
import json
import sys
import os
import platform
import tomllib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client.remote import RemoteClient

parser = argparse.ArgumentParser()
parser.add_argument('--config', default='config.toml')
parser.add_argument('--kind', choices=['asr', 'mineru'])
parser.add_argument('--input', type=Path)
parser.add_argument('--output', type=Path, default=Path('benchmark-result'))
args = parser.parse_args()
cfg = tomllib.loads(Path(args.config).read_text())
client_cfg = dict(cfg['client'])
if (os.environ.get('WSL_DISTRO_NAME') or 'microsoft' in platform.release().lower()) and 'server' in cfg:
    host = cfg['server']['host']
    client_cfg.update(base_url=f"http://{'127.0.0.1' if host=='0.0.0.0' else host}:{cfg['server']['port']}", token_file=cfg['server']['token_file'])
client = RemoteClient(client_cfg)
if args.input:
    print(json.dumps(client.run(args.kind, args.input, args.output), indent=2))
else:
    print(json.dumps(client.health()))
    with client.session() as session:
        print(json.dumps(client.data(session.get('/v1/status')), indent=2))
