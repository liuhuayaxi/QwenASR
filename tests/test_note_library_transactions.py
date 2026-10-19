from tests import isolation  # noqa: F401

import asyncio
import copy
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from qwen_asr_app import durable
from qwen_asr_app.note_library import NoteLibrary, note_library_router


def library_at(root):
    settings = SimpleNamespace(storage_root=root / 'library', categories=['未分类'])
    recordings = SimpleNamespace(list_records=lambda: [])
    with patch('qwen_asr_app.note_library.NOTE_STATE_PATH', root / 'catalog.json'):
        return NoteLibrary(settings, recordings)


def add_note(library, root, name):
    path = root / (name + '.pdf')
    path.write_bytes(b'%PDF-' + name.encode())
    return library.add('未分类', name, path.name, [], path)


class NoteTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlapping_uploads_and_deletes_survive_restart(self):
        for operation in ('upload', 'delete'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                library = library_at(root)
                entries = [add_note(library, root, value) for value in ('alpha', 'beta')] if operation == 'delete' else []
                app = FastAPI()
                app.include_router(note_library_router(library.settings, library.recordings, library=library))
                started, release, second = threading.Event(), threading.Event(), threading.Event()
                count = 0
                original_write, original_move = durable.atomic_bytes, shutil.move

                def checkpoint():
                    nonlocal count
                    count += 1
                    if count == 1:
                        started.set()
                        if not release.wait(5):
                            raise TimeoutError('test did not release first operation')
                    else:
                        second.set()

                def write(path, content):
                    if path == library.state_path:
                        checkpoint()
                    return original_write(path, content)

                def move(*args, **kwargs):
                    result = original_move(*args, **kwargs)
                    checkpoint()
                    return result

                hook = patch('qwen_asr_app.durable.atomic_bytes', side_effect=write) if operation == 'upload' else \
                    patch('qwen_asr_app.note_library.shutil.move', side_effect=move)
                with hook, patch('qwen_asr_app.note_library.PRIVATE_ROOT', root / 'private'), \
                        patch('qwen_asr_app.note_library.TRASH_ROOT', root / 'trash'):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                        async def request(index):
                            if operation == 'delete':
                                return await client.post(f"/api/notes/{entries[index]['id']}/delete")
                            name = ('alpha', 'beta')[index]
                            return await client.post('/api/notes/upload', data={'category': '未分类', 'title': name},
                                                     files={'file': (name + '.pdf', b'%PDF-' + name.encode(), 'application/pdf')})

                        first = asyncio.create_task(request(0))
                        try:
                            self.assertTrue(await asyncio.to_thread(started.wait, 3))
                            other = asyncio.create_task(request(1))
                            # Let the second request contend while the first
                            # is between its file operation and catalog commit.
                            await asyncio.to_thread(second.wait, .2)
                        finally:
                            release.set()
                        responses = await asyncio.gather(first, other)
                self.assertEqual([r.status_code for r in responses], [200, 200])
                restarted = library_at(root)
                self.assertEqual(len(restarted.notes()), 2 if operation == 'upload' else 0)
                if operation == 'upload':
                    self.assertEqual({item['title'] for item in restarted.notes()}, {'alpha', 'beta'})
                    self.assertTrue(all(item['exists'] for item in restarted.notes()))
                else:
                    self.assertEqual(len(list((root / 'trash').rglob('*.pdf'))), 2)
                    self.assertFalse(list(library.storage_root.rglob('*.pdf')))

    def test_failed_catalog_commit_rolls_back_files_and_memory_even_after_replace(self):
        for operation in ('add', 'remove', 'template', 'reclassify'):
            for after_replace in (False, True):
                with self.subTest(operation=operation, after_replace=after_replace), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    library = library_at(root)
                    note = add_note(library, root, 'original')
                    before, saved = copy.deepcopy(library.state), library.state_path.read_bytes()
                    upload = root / 'new.pdf'
                    upload.write_bytes(b'%PDF-new')

                    def fail(path, value):
                        if after_replace:
                            durable.atomic_json(path, value)
                        raise OSError('synthetic catalog failure')

                    actions = {
                        'add': lambda: library.add('未分类', 'new', 'new.pdf', [], upload),
                        'remove': lambda: library.remove(note['id']),
                        'template': lambda: library.set_template('new template'),
                        'reclassify': lambda: library.reclassify('未分类', '新课程'),
                    }
                    with patch('qwen_asr_app.note_library.atomic_json', side_effect=fail), \
                            patch('qwen_asr_app.note_library.TRASH_ROOT', root / 'trash'), self.assertRaises(OSError):
                        actions[operation]()
                    self.assertEqual(library.state, before)
                    self.assertEqual(library.state_path.read_bytes(), saved)
                    self.assertEqual(Path(note['path']).read_bytes(), b'%PDF-original')
                    self.assertEqual(upload.read_bytes(), b'%PDF-new')
                    self.assertEqual(len(library_at(root).notes()), 1)
                    self.assertEqual(len(list(library.storage_root.rglob('*.pdf'))), 1)

    def test_directory_failure_preserves_upload_and_catalog(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library = library_at(root)
            before = library.state_path.read_bytes()
            upload = root / 'input.pdf'
            upload.write_bytes(b'%PDF-input')
            with patch('qwen_asr_app.note_library.Path.mkdir', side_effect=PermissionError('directory denied')), \
                    self.assertRaises(PermissionError):
                library.add('未分类', 'input', 'input.pdf', [], upload)
            self.assertEqual(upload.read_bytes(), b'%PDF-input')
            self.assertEqual(library.state_path.read_bytes(), before)
            self.assertFalse(library.notes())

    async def test_cancelled_upload_drains_writer_before_cleaning_its_input(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library = library_at(root)
            app = FastAPI()
            app.include_router(note_library_router(library.settings, library.recordings, library=library))
            started, release = threading.Event(), threading.Event()
            original_move = shutil.move

            def move(*args, **kwargs):
                started.set()
                if not release.wait(5):
                    raise TimeoutError('test did not release upload')
                return original_move(*args, **kwargs)

            with patch('qwen_asr_app.note_library.PRIVATE_ROOT', root / 'private'), \
                    patch('qwen_asr_app.note_library.shutil.move', side_effect=move):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    request = asyncio.create_task(client.post('/api/notes/upload', data={'category': '未分类'},
                                                files={'file': ('note.pdf', b'%PDF-cancelled', 'application/pdf')}))
                    try:
                        self.assertTrue(await asyncio.to_thread(started.wait, 3))
                        request.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(request.done())
                    finally:
                        release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await request
            restarted = library_at(root)
            self.assertEqual(len(restarted.notes()), 1)
            self.assertEqual(Path(restarted.notes()[0]['path']).read_bytes(), b'%PDF-cancelled')
            self.assertFalse(list((root / 'private').glob('note-upload-*')))

