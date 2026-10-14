"""Offline, reversible removal of completed QwenASR task data."""
import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qwen_asr_app.durable import atomic_json
from qwen_asr_app.media_retention import disposable_name

PRIVATE_HISTORY = ('backups', 'compat-backups', 'library-migrations', 'repair-backups',
                   'mineru-upgrade-backups', 'remote-operations', 'tasks', 'uploads',
                   'library-last-index.json')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def files_under(path):
    if path.is_file() or path.is_symlink():
        return [path]
    result = []
    for root, directories, files in os.walk(path, followlinks=False):
        result.extend(Path(root) / name for name in files)
        result.extend(Path(root) / name for name in directories if (Path(root) / name).is_symlink())
    return result


def plan(settings_path, private, project, downloads):
    settings = json.loads(settings_path.read_text())
    root = Path(settings['storage_root']).resolve()
    moves, changes, protected = [], {}, {}
    record_count = 0
    for meta_path in sorted(root.glob('*/上课录音/*/session.json')):
        state = json.loads(meta_path.read_text())
        if state.get('status') != 'completed':
            raise ValueError('仍有未完成课堂记录，请先完成或明确处理后再清理')
        audio = meta_path.parent / state.get('audio_filename', 'recording.wav')
        if not audio.is_file() or audio.is_symlink() or audio.stat().st_size == 0:
            raise ValueError(f'日常录音不存在，停止清理：{meta_path.parent.name}')
        record_count += 1
        for child in meta_path.parent.iterdir():
            working = state.get('working_audio_filename')
            if (disposable_name(child.name) or child.name == working) and child != audio:
                moves.append(child)
        for name in ('summary_job',):
            if state.get(name, {}).get('status') == 'completed':
                state.pop(name)
            elif state.get(name, {}).get('status') == 'cancelled':
                state[name] = {'status': 'cancelled', 'task_hidden': True}
        for kind, job in list(state.get('ai_jobs', {}).items()):
            if job.get('status') == 'completed':
                del state['ai_jobs'][kind]
            elif job.get('status') == 'cancelled':
                state['ai_jobs'][kind] = {'status': 'cancelled', 'task_hidden': True}
        for name in ('inference_attempts', 'batch_progress'):
            state.pop(name, None)
        stream = state.get('course_stream')
        if isinstance(stream, dict):
            for name in ('chunks', 'supplements', 'prefix_cache'):
                stream.pop(name, None)
        changes[meta_path] = state
    for index in root.glob('*/.qwenasr-files/index.json'):
        items = json.loads(index.read_text())
        for item in items.values():
            item.pop('conversion_history', None)
            conversion = item.get('conversion', {})
            if conversion.get('status') == 'completed':
                conversion['task_hidden'] = True
                for name in ('operation', 'operation_id', 'remote_operation', 'legacy_output_backup'):
                    conversion.pop(name, None)
        changes[index] = items
    for name in PRIVATE_HISTORY:
        path = private / name
        if path.exists():
            moves.append(path)
    for name, value in (('smartclass/queue.json', {'items': [], 'paused': True}),
                        ('smartclass/imports.json', {})):
        path = private / name
        if path.exists():
            changes[path] = value
    for name in ('output', 'tmp', 'server.log', 'course-sync.log'):
        path = project / name
        if path.exists():
            moves.append(path)
    for path in downloads.glob('QwenASR完整备份-*'):
        if path.suffix in {'.zip', '.json'}:
            moves.append(path)
    # Canonical data are compared byte for byte after cleanup. Metadata above
    # are audited separately, because only their task fields are rewritten.
    for path in files_under(root):
        if path in changes or path.is_symlink() or any(path == old or path.is_relative_to(old) for old in moves):
            continue
        protected[str(path)] = {'sha256': digest(path), 'bytes': path.stat().st_size}
    for path in private.glob('*.json'):
        if path not in moves and path not in changes:
            protected[str(path)] = {'sha256': digest(path), 'bytes': path.stat().st_size}
    for path in (settings_path, settings_path.with_name(settings_path.name + '.last-good'), project / '.env'):
        if path.is_file():
            protected[str(path)] = {'sha256': digest(path), 'bytes': path.stat().st_size}
    return root, moves, changes, protected, record_count


def apply(moves, changes, protected, trash, report):
    trash.mkdir(parents=True, mode=0o700)
    report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = {'moves': [], 'metadata': [], 'protected': protected, 'complete': False}
    atomic_json(report, journal)
    moved = []
    try:
        for index, (path, value) in enumerate(changes.items()):
            original = trash / '原元数据' / f'{index:04d}.json'
            original.parent.mkdir(exist_ok=True)
            shutil.copy2(path, original)
            journal['metadata'].append({'path': str(path), 'original': str(original), 'sha256_before': digest(path)})
            atomic_json(report, journal)
            atomic_json(path, value)
        for index, source in enumerate(moves):
            destination = trash / '已删除文件' / f'{index:04d}-{source.name}'
            destination.parent.mkdir(exist_ok=True)
            contents = [{'relative': '.' if path == source else str(path.relative_to(source)),
                         'bytes': path.lstat().st_size, 'inode': path.lstat().st_ino}
                        for path in files_under(source)]
            entry = {'source': str(source), 'trash': str(destination), 'files': contents, 'moved': False}
            journal['moves'].append(entry)
            atomic_json(report, journal)
            shutil.move(str(source), str(destination))
            moved.append((source, destination))
            for item in contents:
                target = destination if item['relative'] == '.' else destination / item['relative']
                if (target.lstat().st_size, target.lstat().st_ino) != (item['bytes'], item['inode']):
                    raise ValueError('废纸篓文件核对失败，已停止清理')
            entry['moved'] = True
            atomic_json(report, journal)
        for raw, expected in protected.items():
            path = Path(raw)
            if path.stat().st_size != expected['bytes'] or digest(path) != expected['sha256']:
                raise ValueError(f'保留文件核对失败：{path.name}')
        journal['complete'] = True
        journal['removed_bytes'] = sum(item['bytes'] for entry in journal['moves'] for item in entry['files'])
        journal['removed_files'] = sum(len(entry['files']) for entry in journal['moves'])
        atomic_json(report, journal)
        return journal
    except BaseException:
        for source, destination in reversed(moved):
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(source))
        for entry in journal['metadata']:
            shutil.copy2(entry['original'], entry['path'])
        journal['rolled_back'] = True
        atomic_json(report, journal)
        raise


def main():
    parser = argparse.ArgumentParser(description='清理已完成任务，只保留日常资料；默认仅统计')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    private = Path.home() / '.qwenasr'
    if args.apply:
        with socket.socket() as connection:
            connection.settimeout(1)
            if connection.connect_ex(('127.0.0.1', int(os.getenv('QWEN_ASR_PORT', '8765')))) == 0:
                raise ValueError('请先安全停止 QwenASR，再清理任务数据')
    root, moves, changes, protected, recordings = plan(project / 'app-settings.json', private, project, Path.home() / 'Downloads')
    size = sum(path.lstat().st_size for source in moves for path in files_under(source))
    print(json.dumps({'library': str(root), 'recordings_preserved': recordings,
                      'move_roots': len(moves), 'removed_bytes': size,
                      'retained_files': len(protected)}, ensure_ascii=False), flush=True)
    if args.apply:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        trash = Path.home() / '.Trash' / ('QwenASR日常清理-' + stamp)
        report = Path.home() / 'Downloads' / ('QwenASR日常清理-' + stamp) / '清理清单.json'
        journal = apply(moves, changes, protected, trash, report)
        print(json.dumps({'complete': journal['complete'], 'removed_files': journal['removed_files'],
                          'removed_bytes': journal['removed_bytes'], 'report': str(report), 'trash': str(trash)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
