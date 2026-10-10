from tests import isolation

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from qwen_asr_app.mineru_settings import DEFAULT_MINERU_SETTINGS, MinerUSettings, normalize_mineru_settings, runtime_info
from qwen_asr_app.pdf_jobs import PDFJobs
from qwen_asr_app.pdf_ai import _mineru_progress
from qwen_asr_app.course_file_routes import CourseFileAPI
from qwen_asr_app.course_file_conversion import ENGINE
from qwen_asr_app.course_files import digest_file


class SettingsTests(unittest.TestCase):
    def test_progress_stays_within_current_window_until_packaging(self):
        first = 'Hybrid processing window 1/2: pages 1-1/2\nOCR-rec Predict: 100%'
        self.assertLess(_mineru_progress(first)[1], 50)
        second = first + '\nHybrid processing window 2/2: pages 2-2/2\n'
        self.assertIsNone(_mineru_progress(second))
        finished = _mineru_progress(second + 'OCR-rec Predict: 100%')
        self.assertLessEqual(finished[1], 74)
        self.assertIn('第 2–2 页', finished[0])

    def test_all_tiers_round_trip_and_invalid_values_preserve_saved_file(self):
        with tempfile.TemporaryDirectory() as raw:
            store = MinerUSettings(Path(raw) / 'settings.json')
            self.assertEqual(store.read(), DEFAULT_MINERU_SETTINGS)
            for tier in ('flash', 'basic', 'standard', 'advanced'):
                settings = store.save({'tier': tier, 'ocr_mode': 'ocr', 'processing_window_size': 8})
                self.assertEqual(MinerUSettings(store.path).read(), settings)
            before = store.path.read_bytes()
            for invalid in ({'tier': 'ultra'}, {'vlm_concurrency': 0}, {'vlm_concurrency': True},
                            {'image_analysis': 'false'}, {'processing_window_size': 1.5}, {'remote': True}):
                with self.assertRaises(ValueError): store.save(invalid)
                self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_configuration_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'settings.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError): MinerUSettings(path).read()
            self.assertEqual(path.read_text(), '{broken')

    def test_runtime_version_is_read_without_loading_models(self):
        with tempfile.TemporaryDirectory() as raw:
            environment = Path(raw)
            binary = environment / 'bin/mineru-kit'
            binary.parent.mkdir(); binary.touch()
            (environment / 'lib/python3.12/site-packages/mineru-4.0.10.dist-info').mkdir(parents=True)
            self.assertEqual(runtime_info(binary)['version'], '4.0.10')
            self.assertTrue(runtime_info(binary)['compatible'])
            self.assertFalse(runtime_info(None)['compatible'])


class JobSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_job_and_retry_keep_frozen_profile(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'sample.pdf'; source.write_bytes(b'%PDF-test')
            jobs = PDFJobs(asyncio.Lock())
            old = normalize_mineru_settings({'tier': 'basic'})
            new = normalize_mineru_settings({'tier': 'advanced'})
            with patch.object(jobs, 'save'), patch.object(jobs, 'schedule'), \
                 patch('qwen_asr_app.pdf_jobs.find_mineru_binary', return_value=Path('/fake/mineru-kit')), \
                 patch('qwen_asr_app.pdf_jobs.mineru_settings.read', return_value=old):
                result = jobs.start([str(source)])
                self.assertEqual(result['mineru_settings'], old)
            job = jobs.jobs[result['id']]
            output = Path(raw) / 'sample(AI).pdf'
            output.write_bytes(b'%PDF-generated')
            with patch.object(jobs, 'save'), \
                 patch('qwen_asr_app.pdf_jobs.conversion_worker', new_callable=AsyncMock, return_value=output) as worker, \
                 patch('qwen_asr_app.pdf_jobs.mineru_settings.read', return_value=new):
                await jobs.run(job, __import__('threading').Event())
                self.assertEqual(worker.call_args.kwargs['settings'], old)
            output.unlink()
            job['files'][0]['status'] = 'error'
            job['status'] = 'failed'
            with patch.object(jobs, 'save'), patch.object(jobs, 'schedule'), \
                 patch('qwen_asr_app.pdf_jobs.mineru_settings.read', return_value=new):
                updated = jobs.action(job['id'], 'retry')
            self.assertEqual(updated['mineru_settings'], old)

    async def test_course_result_reuses_same_parameters_but_reparses_changed_profile(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'source.pdf'; source.write_bytes(b'%PDF-source')
            output = Path(raw) / 'output.pdf'; output.write_bytes(b'%PDF-output')
            old = normalize_mineru_settings({'tier': 'basic'})
            new = normalize_mineru_settings({'tier': 'advanced'})
            item = {'id': 'file', 'conversion': {'status': 'completed', 'engine': ENGINE,
                    'sha256': digest_file(output), 'output_sha256': digest_file(output), 'source_sha256': digest_file(source),
                    'runtime_fingerprint': 'verified-local-fixture', 'mineru_settings': old}}
            api = CourseFileAPI.__new__(CourseFileAPI)
            api.lock = asyncio.Lock(); api.tasks = {}; api.controls = {}; api.store = Mock()
            api.store.file.side_effect = lambda category, ident, *variant: (item, output if variant else source)
            api.store.get.return_value = (item, Path(raw))
            api.run_conversion = AsyncMock()
            with patch('qwen_asr_app.course_file_routes.mineru_settings.read', return_value=old), patch('qwen_asr_app.inference_providers.runtime_fingerprint', return_value='verified-local-fixture'):
                reused = await api.start_conversion('course', 'file')
            self.assertTrue(reused['reused']); api.run_conversion.assert_not_awaited()
            with patch('qwen_asr_app.course_file_routes.mineru_settings.read', return_value=new):
                created = await api.start_conversion('course', 'file')
                await api.tasks[('course', 'file')]
            self.assertNotIn('reused', created)
            self.assertEqual(item['conversion']['mineru_settings'], new)
            api.run_conversion.assert_awaited_once()


if __name__ == '__main__': unittest.main()
