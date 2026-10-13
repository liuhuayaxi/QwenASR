"""Fail-closed library settings and explicit, offline recovery commands."""
import argparse
import json
import os
import uuid
from pathlib import Path

from .durable import atomic_bytes, atomic_json
from .paths import ROOT, data_path
from .settings import DEFAULT_ASR_PARAMS, normalize_asr_params, normalize_live_params
from .terminology import normalize_terminology


class SettingsRecoveryError(ValueError):
    pass


def validate(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('storage_root'), str):
        raise ValueError('缺少资料库路径')
    if not Path(payload['storage_root']).expanduser().is_absolute():
        raise ValueError('资料库路径必须为绝对路径')
    for key in ('categories', 'known_roots'):
        if key in payload and (not isinstance(payload[key], list) or
                               any(not isinstance(item, str) for item in payload[key])):
            raise ValueError(f'{key} 必须为字符串列表')
    if any(not Path(root).expanduser().is_absolute() for root in payload.get('known_roots', [])):
        raise ValueError('已知资料库路径必须为绝对路径')
    profiles = payload.get('category_profiles', {})
    if not isinstance(profiles, dict) or any(not isinstance(value, dict) for value in profiles.values()):
        raise ValueError('课程配置结构无效')
    for profile in profiles.values():
        normalize_asr_params(profile.get('asr_params'), DEFAULT_ASR_PARAMS['max_tokens'])
        normalize_live_params(profile.get('live_params'))
        normalize_terminology(profile.get('terminology'))
    if not isinstance(payload.get('library_layout', 'legacy'), str) or payload.get('library_layout', 'legacy') not in {'legacy', 'shared-courses-v1', ''}:
        raise ValueError('资料库布局无效')
    try:
        if int(payload.get('terminology_seed_version', 0)) < 0:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('术语版本无效') from None
    if payload.get('schema_version', 1) != 1:
        raise ValueError('不支持的资料库配置版本')
    return payload


def read_settings(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if backup_path(path).exists():
            raise SettingsRecoveryError('资料库配置丢失但仍有可用备份；请运行 python -m qwen_asr_app.settings_recovery --restore-backup')
        raise
    except OSError as exc:
        raise SettingsRecoveryError('资料库配置暂时无法读取，原文件未修改；请检查权限或同步状态后重试') from exc
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise SettingsRecoveryError('资料库配置 JSON 损坏，原文件未修改；请运行 python -m qwen_asr_app.settings_recovery --restore-backup 或 --root 已有资料库路径') from exc
    try:
        return validate(payload)
    except ValueError as exc:
        raise SettingsRecoveryError('资料库配置结构无效，原文件未修改；请使用 settings_recovery 恢复：' + str(exc)) from exc


def backup_path(path):
    return path.with_name(path.name + '.last-good')


def save_settings(path, payload):
    validate(payload)
    try:
        previous = read_settings(path)
    except FileNotFoundError:
        previous = None
    # Never turn a read failure into an overwrite, including failures after boot.
    if previous is not None:
        atomic_json(backup_path(path), previous)
    atomic_json(path, payload)
    if previous is None:
        atomic_json(backup_path(path), payload)


def recover(path, root=None):
    payload = read_settings(backup_path(path)) if root is None else None
    if root is not None:
        root = Path(root).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise ValueError('请选择已存在的资料库绝对路径；恢复不会移动文件')
        # Keep known roots and profiles when a usable backup exists.
        try:
            payload = read_settings(backup_path(path))
        except (FileNotFoundError, SettingsRecoveryError):
            courses = [folder.name for folder in root.iterdir() if folder.is_dir() and
                       ((folder / '.qwenasr-course.json').is_file() or (folder / '上课录音').is_dir())]
            payload = {'categories': list(dict.fromkeys(['未分类', *courses])), 'known_roots': [],
                       'library_layout': 'shared-courses-v1' if any((root / name / '上课录音').is_dir() for name in courses) else 'legacy'}
        payload.update(storage_root=str(root.resolve()), schema_version=1)
        payload['known_roots'] = list(dict.fromkeys([*payload.get('known_roots', []), str(root.resolve())]))
    validate(payload)
    # Preserve the exact broken file before explicit replacement. Read errors
    # still abort recovery; the user can retry when the filesystem is available.
    if path.exists():
        atomic_bytes(path.with_name(path.name + '.recovery-' + uuid.uuid4().hex), path.read_bytes())
    atomic_json(path, payload)
    return payload


def main():
    parser = argparse.ArgumentParser(description='恢复资料库配置（不移动录音或资料）')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--restore-backup', action='store_true')
    group.add_argument('--root', help='重新关联已存在的资料库')
    args = parser.parse_args()
    path = Path(os.getenv('QWEN_ASR_SETTINGS_PATH', data_path('app-settings.json', ROOT / 'app-settings.json')))
    recover(path, args.root)
    print('资料库配置已恢复；原文件快照保留。可以重新启动服务。')


if __name__ == '__main__':
    main()
