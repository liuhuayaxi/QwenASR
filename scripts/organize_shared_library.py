"""One-time, logged organization of a real library into its shared course folder."""
import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qwen_asr_app.durable import atomic_json
from qwen_asr_app.library_layout import AI_FILES, INDEX, RECORDINGS, SHARED_LAYOUT, recording_path
from qwen_asr_app.course_files import digest_file
from qwen_asr_app.paths import PRIVATE_ROOT
from qwen_asr_app.storage import SETTINGS_PATH, app_settings, recording_store, default_course_profile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    parser.add_argument('--execute', action='store_true', required=True)
    args = parser.parse_args()
    destination = args.destination.expanduser().resolve()
    source = app_settings.storage_root
    if not destination.is_dir() or source == destination:
        raise ValueError('请选择现有的桌面资料文件夹；不重复迁移已启用的目录')
    aliases = {'光电子器件双语': '光电子器件（全英文）', '通信原理': '通信原理 C'}
    categories = sorted(set(aliases.get(name, name) for name in app_settings.categories)
                        | {path.name for path in destination.iterdir() if path.is_dir() and not path.name.startswith('.')})
    records = recording_store.list_records()
    if any(record.metadata.get('status') in {'active', 'processing'} for record in records):
        raise ValueError('仍有录音在处理，不能开始整理')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = PRIVATE_ROOT / 'library-migrations' / stamp
    backup.mkdir(parents=True)
    print(f'先保留原资料库备份：{backup}', flush=True)
    shutil.copy2(SETTINGS_PATH, backup / 'app-settings.json')
    # APFS clones retain a full recovery copy without copying every audio byte.
    result = subprocess.run(['/bin/cp', '-cR', str(source), str(backup / 'library-before')], capture_output=True)
    if result.returncode:
        shutil.copytree(source, backup / 'library-before', dirs_exist_ok=True)
    journal = {'source': str(source), 'destination': str(destination), 'backup': str(backup), 'moves': [], 'status': 'organizing'}
    atomic_json(backup / 'organization.json', journal)
    reserved = set()

    def move(old, target):
        if old == target:
            return target
        if target.exists() or target in reserved:
            if old.is_file() and target.is_file() and digest_file(old) == digest_file(target):
                journal['moves'].append({'source': str(old), 'target': str(target), 'reused_identical': True})
                atomic_json(backup / 'organization.json', journal)
                return target
            base = target
            number = 2
            while target.exists() or target in reserved:
                target = base.with_name(f'{base.stem}（保留版本{number}）{base.suffix}')
                number += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(target))
        reserved.add(target)
        journal['moves'].append({'source': str(old), 'target': str(target)})
        atomic_json(backup / 'organization.json', journal)
        return target

    # Preserve nested original folders (including the social-practice photos).
    moved_ai = 0
    for category in categories:
        folder = destination / category
        (folder / RECORDINGS).mkdir(parents=True, exist_ok=True)
        (folder / AI_FILES).mkdir(exist_ok=True)
        for path in list(folder.iterdir()):
            if path.is_file() and re.search(r'[（(]AI[）)]', path.stem, re.I):
                move(path, folder / AI_FILES / path.name)
                moved_ai += 1
    indexes = {}
    materials = list(source.glob('*/课程资料/*/asset.json'))
    for manifest in materials:
        old_category = manifest.parents[2].name
        category = aliases.get(old_category, old_category)
        folder = destination / category
        item = json.loads(manifest.read_text())
        original = manifest.parent / '原件' / item['name']
        target = move(original, folder / item['name'])
        item.update(name=target.name, relative_path=target.relative_to(folder).as_posix())
        stat = target.stat()
        item.update(stamp=[stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns], missing=False)
        conversion = item.get('conversion') or {}
        pdf = manifest.parent / 'PDF' / str(conversion.get('name') or 'missing')
        if pdf.is_file():
            output = move(pdf, folder / AI_FILES / pdf.name)
            stat = output.stat()
            conversion.update(name=output.name, relative_path=output.relative_to(folder).as_posix(),
                              stamp=[stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns])
        indexes.setdefault(category, {})[item['id']] = item
    for category, items in indexes.items():
        atomic_json(destination / category / INDEX / 'index.json', items)
    record_log = []
    for record in records:
        category = aliases.get(record.metadata.get('category'), record.metadata.get('category'))
        target = recording_path(destination, category, record.metadata)
        target = move(record.directory, target)
        metadata = json.loads((target / 'session.json').read_text())
        metadata['category'] = category
        if record.metadata.get('recorded_at'):
            metadata['recorded_at'] = record.metadata['recorded_at']
        options = metadata.get('batch_options')
        if isinstance(options, dict):
            options['category'] = category
        atomic_json(target / 'session.json', metadata)
        record_log.append({'id': record.id, 'category': category, 'directory': str(target)})
    profiles = {aliases.get(name, name): value for name, value in app_settings.category_profiles.items()}
    for category in categories:
        profile = profiles.setdefault(category, default_course_profile(category))
        atomic_json(destination / category / '.qwenasr-course.json', {'category': category, **profile})
    app_settings.storage_root = destination
    app_settings.library_layout = SHARED_LAYOUT
    app_settings.categories = sorted(categories, key=lambda value: (value != '未分类', value.casefold()))
    app_settings.category_profiles = profiles
    app_settings.add_known_root(destination)
    app_settings.save()
    from qwen_asr_app.shared_files import SharedCourseFiles
    files = SharedCourseFiles(app_settings)
    counts = {category: len(files.scan(category)) for category in categories}
    # Keep the obsolete layout out of future record discovery, with the backup.
    shutil.move(str(source), str(backup / 'old-layout-remainder'))
    journal.update(status='completed', recordings=record_log, files=counts, desktop_ai_moved=moved_ai)
    atomic_json(backup / 'organization.json', journal)
    print(json.dumps({'backup': str(backup), 'recordings': len(record_log), 'files': counts,
                      'desktop_ai_moved': moved_ai, 'storage_root': str(destination)}, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
