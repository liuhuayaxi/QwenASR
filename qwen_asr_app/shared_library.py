"""Directory discovery and live notifications for the shared course library."""
import asyncio
import copy
import json
import shutil
import time
from pathlib import Path

from .durable import atomic_json
from .library_layout import AI_FILES, AI_NOTES, INDEX, NOTE_LIBRARY_MARKER, RECORDINGS, shared
from .storage import default_course_profile, safe_component, local_now


def discover_courses(settings):
    if not shared(settings) or not settings.storage_root.is_dir():
        return False
    names = []
    changed = False
    for folder in sorted(settings.storage_root.iterdir()):
        if not folder.is_dir() or folder.is_symlink() or folder.name.startswith('.'):
            continue
        # The generated-note catalog lives under the same Desktop materials
        # root but is not a course and must never become a course category.
        if (folder / NOTE_LIBRARY_MARKER).is_file():
            continue
        name = folder.name
        if safe_component(name, '') != name:
            continue
        names.append(name)
        marker = folder / '.qwenasr-course.json'
        try:
            profile = json.loads(marker.read_text())
        except (OSError, ValueError):
            profile = {"category": name, **settings.category_profiles.get(name, default_course_profile(name))}
        old = profile.get('category')
        if old != name and old in settings.category_profiles and not (settings.storage_root / old).exists():
            settings.category_profiles[name] = settings.category_profiles.pop(old)
            changed = True
        settings.category_profiles.setdefault(name, {key: value for key, value in profile.items() if key != 'category'})
        if old != name or not marker.exists():
            atomic_json(marker, {**profile, 'category': name})
        (folder / RECORDINGS).mkdir(exist_ok=True)
    if '未分类' not in names:
        settings.add_category('未分类')
        names.append('未分类')
    names.sort(key=lambda name: (name != '未分类', name.casefold()))
    if names != settings.categories:
        settings.categories = names
        changed = True
    if changed:
        settings.save()
    return changed


class LibraryWatch:
    def __init__(self, settings, recordings):
        self.settings, self.recordings = settings, recordings
        self.listeners = set()
        self.task = None
        self.stop = asyncio.Event()
        self.revision = time.time_ns()
        self.draining = False

    def publish(self):
        self.revision = time.time_ns()
        for queue in tuple(self.listeners):
            if queue.empty():
                queue.put_nowait(self.revision)

    async def start(self):
        if shared(self.settings):
            discover_courses(self.settings)
            self.recordings.refresh()
            self.task = asyncio.create_task(self.run())

    async def close(self):
        self.stop.set()
        if self.task:
            await self.task

    async def run(self):
        from watchfiles import awatch
        while not self.stop.is_set():
            root = self.settings.storage_root
            try:
                async for changes in awatch(root, debounce=200, step=50, stop_event=self.stop,
                                            yield_on_timeout=True, rust_timeout=1000, watch_filter=None):
                    if root != self.settings.storage_root:
                        break
                    relevant = [path for _, path in changes if not any(
                        part.startswith('.') and part not in {INDEX, '.qwenasr-course.json'}
                        for part in Path(path).relative_to(root).parts
                    ) and not path.endswith(('.tmp', '.part'))]
                    if relevant:
                        discover_courses(self.settings)
                        self.recordings.refresh()
                        self.publish()
            except (OSError, ValueError) as exc:
                print(f'资料文件夹监听暂未完成：{exc}', flush=True)
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=1)
                except asyncio.TimeoutError:
                    self.publish()

    async def events(self, request):
        queue = asyncio.Queue(maxsize=1)
        self.listeners.add(queue)
        try:
            yield f'data: {self.revision}\n\n'
            while not self.draining and not await request.is_disconnected():
                try:
                    revision = await asyncio.wait_for(queue.get(), timeout=15)
                    if self.draining:
                        break
                    yield f'data: {revision}\n\n'
                except asyncio.TimeoutError:
                    yield ': keepalive\n\n'
        finally:
            self.listeners.discard(queue)


def change_shared_category(settings, recordings, category, name=None):
    if category == '未分类' or category not in settings.categories:
        raise ValueError('请选择一个可以修改的课程分类')
    target_name = '未分类' if name is None else str(name).strip()
    if safe_component(target_name, '') != target_name or not target_name or '\\' in target_name:
        raise ValueError('请输入有效的分类名称')
    source = settings.storage_root / category
    target = settings.storage_root / target_name
    if source.is_symlink() or target.is_symlink():
        raise ValueError('不能修改符号链接课程目录')
    if target_name == category:
        return {'category': category, 'deleted': False}
    if name is not None and (target.exists() or target_name in settings.categories):
        raise ValueError('已有同名分类，请使用其他名称')
    previous_categories = list(settings.categories)
    previous_profiles = copy.deepcopy(settings.category_profiles)
    if name is not None:
        source.rename(target)
        try:
            profile = settings.category_profiles.get(category, default_course_profile(category))
            atomic_json(target / '.qwenasr-course.json', {'category': target_name, **profile})
            settings.categories = [target_name if value == category else value for value in settings.categories]
            settings.category_profiles[target_name] = settings.category_profiles.pop(category, profile)
            settings.save()
        except BaseException:
            target.rename(source)
            settings.categories, settings.category_profiles = previous_categories, previous_profiles
            atomic_json(source / '.qwenasr-course.json', {'category': category, **previous_profiles.get(category, {})})
            settings.save()
            raise
    else:
        # Merge direct originals, AI outputs, recording directories and stable
        # indexes. Reject collisions before any move; never overwrite a file.
        target.mkdir(exist_ok=True)
        moves = []
        for child in source.iterdir():
            if child.name in {'.qwenasr-course.json', INDEX}:
                continue
            destination = target / child.name
            if child.name in {AI_FILES, AI_NOTES, RECORDINGS} and destination.is_dir():
                moves.extend((item, destination / item.name) for item in child.iterdir())
            else:
                moves.append((child, destination))
        if any(destination.exists() for _, destination in moves):
            raise ValueError('“未分类”中已有同名内容，请先改名后再删除分类')
        def index(folder):
            path = folder / INDEX / 'index.json'
            return json.loads(path.read_text()) if path.exists() else {}
        incoming, existing = index(source), index(target)
        if incoming.keys() & existing.keys():
            raise ValueError('目标分类已有相同文件编号，请先核对重复资料')
        completed = []
        try:
            for old, new in moves:
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(new))
                completed.append((old, new))
            atomic_json(target / INDEX / 'index.json', {**existing, **incoming})
            settings.categories.remove(category)
            settings.category_profiles.pop(category, None)
            settings.save()
        except BaseException:
            for old, new in reversed(completed):
                shutil.move(str(new), str(old))
            atomic_json(target / INDEX / 'index.json', existing)
            settings.categories, settings.category_profiles = previous_categories, previous_profiles
            settings.save()
            raise
        # Retain deleted classification metadata outside the visible library.
        from .storage import TRASH_ROOT
        backup = TRASH_ROOT / ('分类-' + category + '-' + str(time.time_ns()))
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(backup))
    discover_courses(settings)
    recordings.refresh()
    return {'category': target_name, 'deleted': name is None}
