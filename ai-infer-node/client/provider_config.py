"""Mac CLI for the same settings used by the workbench UI."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from qwen_asr_app.inference_providers import inference_settings, MODES

parser = argparse.ArgumentParser()
parser.add_argument('--mineru', choices=MODES)
parser.add_argument('--asr', choices=MODES)
parser.add_argument('--url')
args = parser.parse_args()
value = inference_settings.read()
for flag, key in ((args.mineru, 'mineru_provider'), (args.asr, 'asr_provider'), (args.url, 'base_url')):
    if flag:
        value[key] = flag
if args.mineru or args.asr or args.url:
    value = inference_settings.save(value)
print(json.dumps(value, ensure_ascii=False, indent=2))
