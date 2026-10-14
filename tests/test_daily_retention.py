from tests import isolation  # noqa: F401
import asyncio
import hashlib
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
from qwen_asr_app.library_backup import BackupSource, build_backup
from qwen_asr_app.remote_operations import discard_completed_payloads
from scripts.cleanup_daily_data import plan, apply


class TemporaryAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_split_runs_each_time_and_removes_every_temporary_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'recording.aac'
            source.write_bytes(b'original-long-audio')
            original = source.read_bytes()
            record = SimpleNamespace(ensure_aac=lambda: source, metadata={'title':'课堂'}, directory=root)
            generated = []
            def split(src, destinations):
                generated.append(destinations[0].parent)
                destinations[0].write_bytes(b'one')
                destinations[1].write_bytes(b'two')
            with patch('qwen_asr_app.ideashell_attachments.MAX_AUDIO_BYTES', 4), \
                 patch.object(app, 'split_audio_to_aac_lossless', side_effect=split):
                first = await app.ideashell_recording_audio_parts(record)
                second = await app.ideashell_recording_audio_parts(record)
            self.assertEqual([item['data'] for item in first], [b'one', b'two'])
            self.assertEqual([item['sha256'] for item in first], [item['sha256'] for item in second])
            self.assertEqual(len(generated), 2)
            self.assertNotEqual(generated[0], generated[1])
            self.assertTrue(all(not folder.exists() for folder in generated))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual([path.name for path in root.iterdir()], ['recording.aac'])

    async def test_cancellation_drains_split_worker_before_directory_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'recording.aac'; source.write_bytes(b'long-audio')
            record = SimpleNamespace(ensure_aac=lambda: source, metadata={'title':'课堂'}, directory=root)
            entered, release = threading.Event(), threading.Event()
            generated = []
            def split(src, destinations):
                generated.append(destinations[0].parent)
                entered.set(); release.wait(5)
                for path in destinations: path.write_bytes(b'part')
            with patch('qwen_asr_app.ideashell_attachments.MAX_AUDIO_BYTES', 4), \
                 patch.object(app, 'split_audio_to_aac_lossless', side_effect=split):
                task = asyncio.create_task(app.ideashell_recording_audio_parts(record))
                await asyncio.to_thread(entered.wait, 5)
                task.cancel(); await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertTrue(generated[0].exists())
                release.set()
                with self.assertRaises(asyncio.CancelledError): await task
            self.assertFalse(generated[0].exists())
            self.assertTrue(source.exists())

    async def test_failed_split_also_removes_working_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source=root/'recording.aac';source.write_bytes(b'long-audio')
            record=SimpleNamespace(ensure_aac=lambda:source,metadata={'title':'课堂'},directory=root)
            directories=[]
            def fail(src,destinations,**kwargs):
                directories.append(destinations[0].parent)
                destinations[0].write_bytes(b'incomplete')
                raise ValueError('failed')
            with patch('qwen_asr_app.ideashell_attachments.MAX_AUDIO_BYTES',4), \
                 patch.object(app,'split_audio_to_aac_lossless',side_effect=fail), \
                 patch.object(app,'split_audio_to_aac',side_effect=fail):
                with self.assertRaisesRegex(ValueError,'拆分后'):
                    await app.ideashell_recording_audio_parts(record)
            self.assertTrue(all(not folder.exists() for folder in directories))


class DailyRetentionTests(unittest.TestCase):
    def test_daily_zip_has_no_audio_derivatives_or_old_task_directories(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); course=root/'library/物理光学/上课录音/lesson';course.mkdir(parents=True)
            for name in ['recording.aac','.course-source.flac','.playback.m4a','.ideashell-audio-part-1.aac','recording-and-transcript.zip']:
                (course/name).write_bytes(name.encode())
            output=root/'daily.zip'
            build_backup([BackupSource(root/'library','资料库')],output,lambda **kw:None)
            with zipfile.ZipFile(output) as archive:
                self.assertIn('资料库/物理光学/上课录音/lesson/recording.aac',archive.namelist())
                self.assertEqual(sum(not item.is_dir() for item in archive.infolist()),3)
                self.assertEqual(json.loads(archive.read('备份清单.json'))['format'],'qwenasr-daily-backup')

    def test_discard_only_operation_payloads_preserves_receipts_and_other_folders(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);folder=root/'remote-operations/id';folder.mkdir(parents=True)
            for name in ['receipt.json','operation.json','source.pdf','downloaded-result.zip']:
                (folder/name).write_bytes(b'payload')
            unrelated=root/'current';unrelated.mkdir();(unrelated/'notes.pdf').write_bytes(b'keep')
            with patch('qwen_asr_app.remote_operations.PRIVATE_ROOT',root):
                discard_completed_payloads(folder/'receipt.json')
                discard_completed_payloads(unrelated/'receipt.json')
                discard_completed_payloads(root/'remote-operations/receipt.json')
            self.assertEqual({p.name for p in folder.iterdir()},{'receipt.json','operation.json'})
            self.assertTrue((unrelated/'notes.pdf').is_file())

    def test_cleanup_preserves_canonical_recording_and_notes_and_can_roll_back(self):
        for failure in [False,True]:
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as raw:
                root=Path(raw);library=root/'library';record=library/'物理光学/上课录音/lesson';record.mkdir(parents=True)
                (record/'recording.aac').write_bytes(b'canonical')
                (record/'transcript.txt').write_text('课堂内容')
                (record/'.course-source.flac').write_bytes(b'disposable')
                meta={'id':'one','status':'completed','audio_filename':'recording.aac','summary_job':{'status':'completed'}}
                (record/'session.json').write_text(json.dumps(meta))
                settings=root/'app-settings.json';settings.write_text(json.dumps({'storage_root':str(library)}))
                private=root/'private';private.mkdir();project=root/'project';project.mkdir();downloads=root/'downloads';downloads.mkdir()
                _,moves,changes,protected,count=plan(settings,private,project,downloads)
                before=(record/'session.json').read_bytes()
                if failure:
                    with patch('scripts.cleanup_daily_data.shutil.move',side_effect=OSError('denied')):
                        with self.assertRaises(OSError):
                            apply(moves,changes,protected,root/'trash',root/'report.json')
                    self.assertEqual((record/'session.json').read_bytes(),before)
                    self.assertTrue((record/'.course-source.flac').exists())
                else:
                    report=apply(moves,changes,protected,root/'trash',root/'report.json')
                    self.assertTrue(report['complete']);self.assertEqual(count,1)
                    self.assertFalse((record/'.course-source.flac').exists())
                    self.assertNotIn('summary_job',json.loads((record/'session.json').read_text()))
                self.assertEqual((record/'recording.aac').read_bytes(),b'canonical')
                self.assertEqual((record/'transcript.txt').read_text(),'课堂内容')

    def test_incomplete_or_missing_archive_blocks_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);record=root/'library/物理光学/上课录音/lesson';record.mkdir(parents=True)
            settings=root/'settings.json';settings.write_text(json.dumps({'storage_root':str(root/'library')}))
            for state in [{'status':'interrupted'},{'status':'completed','audio_filename':'missing.aac'}]:
                (record/'session.json').write_text(json.dumps(state))
                with self.assertRaises(ValueError):
                    plan(settings,root/'private',root/'project',root/'downloads')
