"""Run isolated regression tests and verify that real user data was untouched."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def snapshot():
    settings = ROOT / 'app-settings.json'
    library_roots = [ROOT / 'recordings']
    if settings.exists():
        config = json.loads(settings.read_text())
        library_roots += [Path(p).expanduser() for p in config.get('known_roots', [])]
        if config.get('storage_root'):
            library_roots.append(Path(config['storage_root']).expanduser())
    files = {settings} if settings.exists() else set()
    private = Path.home() / '.qwenasr'
    if private.exists():
        files.update(p for p in private.rglob('*.json') if p.is_file())
    # Match the library's own inventory depth, never traverse unrelated Documents.
    for root in library_roots:
        patterns = ('*/session.json',) if root == ROOT / 'recordings' else ('*/*/session.json', '*/上课录音/*/session.json')
        for pattern in patterns:
            for meta in root.glob(pattern):
                files.update(p for p in meta.parent.rglob('*') if p.is_file())
        for pattern in ('*/.qwenasr-course.json', '*/.qwenasr-files/index.json', '*/课程资料/*/asset.json'):
            files.update(p for p in root.glob(pattern) if p.is_file())
    result = {}
    for path in files:
        # Do not read key values into logs or test processes.
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        result[str(path)] = digest
    return result


if __name__ == '__main__':
    before = snapshot()
    result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], cwd=ROOT)
    after = snapshot()
    changed = [p for p in before.keys() | after.keys() if before.get(p) != after.get(p)]
    if changed:
        print('FAIL: real user data changed:', len(changed), file=sys.stderr)
        sys.exit(1)
    print(f'Real user data unchanged: {len(before)} files; tests use temporary data and mocked network.')
    sys.exit(result.returncode)
