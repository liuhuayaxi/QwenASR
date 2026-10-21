from tests import isolation

import asyncio
import copy
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from qwen_asr_app.course_file_routes import CourseFileAPI
from qwen_asr_app.course_files import digest_file
from qwen_asr_app.mineru_settings import normalize_mineru_settings
from qwen_asr_app.mineru_timing import MineruTiming, TimingHistory
from qwen_asr_app.pdf_jobs import PDFJobs


class TimingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'sample.pdf'
        self.source.write_bytes(b'%PDF-synthetic-source')
        self.settings = normalize_mineru_settings({'processing_window_size': 4})
        self.history = TimingHistory(self.root/'timing.json')
        self.now = 0

    def factory(self, **kwargs):
        return MineruTiming(**kwargs, history=self.history, clock=lambda: self.now)

    async def emit(self, callback):
        for seconds, label in [(0, 'Mac · 准备页面与模型'),
            (8, 'MinerU · 版面分析 · 阶段 100% · 第 1–4 页/8 · 窗口 1/2'),
            (40, 'MinerU · 版面分析 · 阶段 100% · 第 5–8 页/8 · 窗口 2/2'),
            (60, 'MinerU · 保存结构化结果'), (63, '正在写入隐藏文字层'),
            (68, '转换完成')]:
            self.now = seconds
            callback(label, None if seconds < 68 else 100)
            await asyncio.sleep(0)
        self.now = 69

    async def test_pdf_worker_persists_estimate_before_result_then_learns_once(self):
        jobs = PDFJobs(asyncio.Lock()); jobs.jobs.clear()
        with patch.object(jobs, 'save'), patch.object(jobs, 'schedule'), \
             patch('qwen_asr_app.pdf_jobs.find_mineru_binary', return_value=Path('/fake/mineru')), \
             patch('qwen_asr_app.pdf_jobs.mineru_settings.read', return_value=self.settings):
            task = jobs.start([str(self.source)], {'mineru_provider': 'local'})
        job = jobs.jobs[task['id']]
        snapshots = []
        async def worker(fn, source, **kwargs):
            await self.emit(kwargs['progress'])
            output = source.with_name('sample(AI).pdf'); output.write_bytes(b'%PDF-result')
            return output
        with patch.object(jobs, 'save', side_effect=lambda: snapshots.append(copy.deepcopy(job))), \
             patch('qwen_asr_app.pdf_jobs.MineruTiming', side_effect=self.factory), \
             patch('qwen_asr_app.pdf_jobs.conversion_worker', side_effect=worker):
            await jobs.run(job, Event())
        running = [s for s in snapshots if s['status'] == 'running' and s.get('progress_estimate')]
        self.assertTrue(running)
        self.assertTrue(all(s['progress'] < 100 for s in running))
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['files'][0]['progress_estimate']['percent'], 100)
        self.assertEqual(sum(map(len, self.history.read()['profiles'].values())), 1)

    async def test_course_worker_preserves_estimate_in_saved_conversion(self):
        item = {'id':'asset', 'name':'sample.pdf', 'conversion':{
            'status':'queued','operation_id':'run-fixture','mineru_settings':self.settings,
            'inference_settings':{'mineru_provider':'local'}, 'source_sha256':digest_file(self.source)}}
        api = CourseFileAPI.__new__(CourseFileAPI)
        api.store = Mock(); api.store.get.return_value = (item,self.root)
        api.conversion_lock = asyncio.Lock(); api.controls = {('course','asset'):{'action':'interrupted'}}
        snapshots = []
        api.store.save.side_effect = lambda *args: snapshots.append(copy.deepcopy(item['conversion']))
        async def worker(fn, source, output, callback, **kwargs):
            await self.emit(callback)
            output.parent.mkdir(parents=True,exist_ok=True); output.write_bytes(b'%PDF-result')
            return {'pages':9,'source_pages':8,'inference':{'provider':'local'}}
        with patch('qwen_asr_app.course_file_routes.shared', return_value=False), \
             patch('qwen_asr_app.course_file_routes.MineruTiming', side_effect=self.factory), \
             patch('qwen_asr_app.course_file_routes.conversion_worker', side_effect=worker):
            await api.run_conversion('course','asset',self.source,Event())
        self.assertEqual(item['conversion']['status'],'completed')
        self.assertFalse(item['conversion']['progress_estimate']['estimated'])
        self.assertTrue(all(s['progress'] < 100 for s in snapshots if s['status']=='running'))
        self.assertEqual(sum(map(len,self.history.read()['profiles'].values())),1)

    async def test_remote_generation_keeps_clean_published_name_and_preserves_previous_bytes(self):
        output = self.root / 'PDF' / 'sample(AI).pdf'
        output.parent.mkdir()
        output.write_bytes(b'%PDF-old-version')
        item = {'id': 'asset', 'name': 'sample.pdf', 'conversion': {
            'status': 'queued', 'operation_id': 'a' * 32, 'mineru_settings': self.settings,
            'inference_settings': {'mineru_provider': 'local'}, 'source_sha256': digest_file(self.source),
            'operation': {'blob_path': str(self.source), 'receipt_path': str(self.root / 'receipt.json')}}}
        api = CourseFileAPI.__new__(CourseFileAPI)
        api.store = Mock()
        api.store.get.return_value = (item, self.root)
        api.conversion_lock = asyncio.Lock()
        api.controls = {('course', 'asset'): {'action': 'interrupted'}}
        async def worker(fn, source, destination, callback, **kwargs):
            self.assertTrue(destination.name.startswith('.'))
            self.assertEqual(output.read_bytes(), b'%PDF-old-version')
            destination.write_bytes(b'%PDF-new-version')
            return {'pages': 8, 'source_pages': 8, 'inference': {'provider': 'local'}}
        with patch('qwen_asr_app.course_file_routes.shared', return_value=False), \
             patch('qwen_asr_app.course_file_routes.saved_config', return_value={'mineru_provider': 'local'}), \
             patch('qwen_asr_app.course_file_routes.receipt', return_value={}), \
             patch('qwen_asr_app.course_file_routes.MineruTiming', side_effect=self.factory), \
             patch('qwen_asr_app.course_file_routes.conversion_worker', side_effect=worker), \
             patch('qwen_asr_app.remote_operations.discard_completed_payloads'):
            await api.run_conversion('course', 'asset', self.source, Event())
        self.assertEqual(item['conversion']['status'], 'completed')
        self.assertEqual(item['conversion']['name'], 'sample(AI).pdf')
        self.assertEqual(output.read_bytes(), b'%PDF-new-version')
        self.assertEqual(Path(item['conversion']['legacy_output_backup']).read_bytes(), b'%PDF-old-version')


if __name__ == '__main__':
    unittest.main()
