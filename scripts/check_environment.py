"""Check the exact runtime without importing model code or loading weights."""
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
import sys

root = Path(__file__).resolve().parent.parent
mismatches = []
for line in (root / 'requirements.lock').read_text().splitlines():
    if not line or line.startswith('#'):
        continue
    name, expected = line.split('==', 1)
    try:
        actual = version(name)
    except PackageNotFoundError:
        actual = 'missing'
    if actual != expected:
        mismatches.append(f'{name}: {actual}, expected {expected}')
if mismatches:
    print('\n'.join(mismatches), file=sys.stderr)
sys.exit(bool(mismatches))
