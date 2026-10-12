from tests import isolation

import asyncio
import json
import threading
import unittest
import tempfile
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch
import httpx

from fastapi import HTTPException

from qwen_asr_app import smartclass_service as service
from qwen_asr_app.smartclass import SmartClassError


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.005)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.groups = {ident: {'id': ident, 'title': ident, 'videos': [{'NewID': ident}]}
                       for ident in 'ABCDE'}
        self.stack = ExitStack()
        for name, value in {
            'job': None, 'catalog_job': None, 'status': {'state': 'connected', 'identity': {'name': 'test'}},
            'imports': {}, 'group_rows': self.groups, 'queue_state': {'items': [], 'paused': False},
            'options': {'summary_provider': {'model': 'saved'}}, 'stop_event': threading.Event(),
            'queue_wakeup': asyncio.Event(), 'platform_lock': asyncio.Lock(),
        }.items():
            self.stack.enter_context(patch.object(service, name, value))
        self.stack.enter_context(patch.object(service, 'save_imports'))
        self.stack.enter_context(patch.object(service, 'save_queue'))

    async def asyncTearDown(self):
        service.stop_event.set()
        service.queue_wakeup.set()
        if service.job:
            await asyncio.wait_for(service.job, 3)
        if service.catalog_job:
            await asyncio.wait_for(service.catalog_job, 3)
        self.stack.close()

    async def test_append_during_transcription_preserves_runner_options_and_bounds(self):
        downloaded, transcribing = asyncio.Event(), asyncio.Event()
        release = asyncio.Event()
        started, configs = [], {}

        async def run(group, config):
            ident = group['id']
            started.append(ident)
            configs[ident] = config
            if ident == 'A':
                downloaded.set()
                transcribing.set()
            await release.wait()

        with patch.object(service, 'import_group', run):
            await service.start_import(service.Selection(ids=['A']))
            await downloaded.wait()
            runner = service.job
            service.options['summary_provider']['model'] = 'new'
            accepted = await service.start_import(service.Selection(ids=['A', 'B', 'C', 'D']))
            await until(lambda: len(started) == 3)
            self.assertIs(service.job, runner)
            self.assertTrue(transcribing.is_set())
            self.assertEqual(started, ['A', 'B', 'C'])
            self.assertEqual(accepted['added'], ['B', 'C', 'D'])
            self.assertEqual(configs['A']['summary_provider']['model'], 'saved')
            self.assertEqual(configs['B']['summary_provider']['model'], 'new')
            again = await service.start_import(service.Selection(ids=['A', 'B']))
            self.assertTrue(again['already_queued'])
            release.set()
            await runner
            self.assertEqual(started, list('ABCD'))
            self.assertEqual(service.queue_view()['remaining'], 0)

    async def test_running_status_omits_finished_paused_and_failed_history(self):
        service.imports.update({state: {'id': state, 'state': state}
                               for state in ['running', 'queued', 'completed', 'paused', 'interrupted', 'error']})
        status = await service.get_status()
        self.assertEqual([entry['id'] for entry in status['imports']], ['running', 'queued'])

    async def test_pause_stops_admission_then_resume_keeps_unselected_work(self):
        started = []

        async def run(group, config):
            started.append(group['id'])
            await until(service.stop_event.is_set)
            raise SmartClassError('已暂停')

        with patch.object(service, 'import_group', run):
            await service.start_import(service.Selection(ids=list('ABCD')))
            await until(lambda: len(started) == 3)
            await service.stop()
            with self.assertRaises(HTTPException):
                await service.start_import(service.Selection(ids=['E']))
            await service.job
            self.assertEqual(started, list('ABC'))
            self.assertTrue(all(i['state'] == 'paused' for i in service.queue_state['items']))
            self.assertTrue(all(i['state'] == 'paused' for i in service.imports.values()))
        with patch.object(service, 'import_group', AsyncMock()) as run:
            await service.start_import(service.Selection(ids=['E']))
            await service.job
            self.assertEqual(run.await_count, 1)
            self.assertTrue(all(i['state'] == 'paused' for i in service.queue_state['items'][:4]))
            await service.resume_queue()
            await service.job
            self.assertEqual(run.await_count, 5)

    async def test_failed_task_does_not_retry_forever_and_can_be_appended_again(self):
        started, release = [], asyncio.Event()

        async def run(group, config):
            started.append(group['id'])
            if group['id'] == 'A':
                raise SmartClassError('网络中断')
            await release.wait()

        with patch.object(service, 'import_group', run):
            await service.start_import(service.Selection(ids=['A', 'B']))
            await until(lambda: service.queue_state['items'][0]['state'] == 'interrupted')
            await service.start_import(service.Selection(ids=['A']))
            await until(lambda: started.count('A') == 2)
            release.set()
            await service.job
            self.assertEqual(started.count('A'), 2)

    async def test_single_period_and_later_pair_never_download_same_source_together(self):
        self.groups['C']['videos'].append({'NewID': 'A'})
        started, release_first, release_rest = [], asyncio.Event(), asyncio.Event()
        async def run(group, config):
            started.append(group['id'])
            await (release_first if group['id'] == 'A' else release_rest).wait()
        with patch.object(service, 'import_group', run):
            await service.start_import(service.Selection(ids=['A', 'B', 'C']))
            await until(lambda: len(started) == 2)
            self.assertEqual(started, ['A', 'B'])
            release_first.set()
            await until(lambda: len(started) == 3)
            self.assertEqual(started, ['A', 'B', 'C'])
            release_rest.set()
            await service.job

    async def test_refresh_remains_available_while_queue_runs(self):
        release = asyncio.Event()
        async def run(*args):
            await release.wait()
        with patch.object(service, 'import_group', run), \
                patch.object(service.client, 'ensure_login', return_value={'name': 'test'}), \
                patch.object(service.client, 'courses', return_value=[]) as courses:
            await service.start_import(service.Selection(ids=['A']))
            runner = service.job
            await service.refresh_courses()
            await service.catalog_job
            self.assertIs(service.job, runner)
            self.assertEqual(service.status['state'], 'running')
            courses.assert_called_once()
            release.set()
            await runner

    async def test_download_slot_moves_to_next_course_before_asr_finishes(self):
        states, downloaded = {}, []
        release_asr = asyncio.Event()
        with tempfile.TemporaryDirectory() as raw:
            def handler(request):
                if request.method == 'POST' and request.url.path == '/api/course-streams':
                    import json
                    ident = json.loads(request.content)['key']
                    directory = Path(raw) / ident
                    directory.mkdir()
                    states[ident] = {'id': ident, 'directory': str(directory), 'next_index': 0,
                                    'status': 'processing', 'download_complete': False,
                                    'download_seconds': 0, 'asr_seconds': 0,
                                    'corrected_segments': 0, 'segments': 0, 'ai_errors': 0}
                    return httpx.Response(200, json=states[ident])
                ident = request.url.path.split('/')[3]
                if request.url.path.endswith('/finish'):
                    states[ident].update(download_complete=True, download_seconds=100)
                    downloaded.append(ident)
                if release_asr.is_set():
                    states[ident].update(status='completed', asr_seconds=100)
                return httpx.Response(200, json=states[ident])

            def decode(sources, on_chunk, progress, stop, resume):
                on_chunk(bytes(32000), {'source': 1, 'samples': 0})
                return []

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as local:
                with patch.object(service, 'download_slots', asyncio.Semaphore(1)), \
                        patch.object(service.client, 'audio_sources', return_value=[]), \
                        patch.object(service, 'stream_audio', decode):
                    tasks = []
                    for ident in 'ABC':
                        entry = {'id': ident}
                        service.begin_import_progress(entry, {})
                        tasks.append(asyncio.create_task(service.import_stream(local,
                            {'id': ident, 'title': ident, 'duration': 100, 'periods': []}, {}, entry)))
                    try:
                        async with asyncio.timeout(5):
                            while len(downloaded) < 3:
                                await asyncio.sleep(.01)
                        self.assertEqual(downloaded, list('ABC'))
                        self.assertTrue(all(not task.done() for task in tasks))
                        self.assertTrue(all(state['asr_seconds'] == 0 for state in states.values()))
                        release_asr.set()
                        await asyncio.gather(*tasks)
                        service.save_imports.assert_any_call(force=False)
                    finally:
                        service.stop_event.set()
                        await asyncio.gather(*tasks, return_exceptions=True)


class ProgressCheckpointTests(unittest.TestCase):
    def test_progress_checkpoint_is_throttled_and_completion_forces_a_save(self):
        with tempfile.TemporaryDirectory() as raw, \
             patch.object(service, 'STATE_DIR', Path(raw)), \
             patch.object(service, 'imports', {'synthetic': {'progress': 12.7}}), \
             patch.object(service, '_last_import_save', 0), \
             patch.object(service.time, 'monotonic', side_effect=[10, 11, 12, 12.1]):
            service.save_imports(force=False)
            checkpoint = Path(raw) / 'imports.json'
            self.assertEqual(json.loads(checkpoint.read_text())['synthetic']['progress'], 12.7)
            service.imports['synthetic']['progress'] = 48.8
            service.save_imports(force=False)
            self.assertEqual(json.loads(checkpoint.read_text())['synthetic']['progress'], 12.7)
            service.save_imports(force=False)
            self.assertEqual(json.loads(checkpoint.read_text())['synthetic']['progress'], 48.8)
            service.imports['synthetic']['progress'] = 100
            service.save_imports()
            self.assertEqual(json.loads(checkpoint.read_text())['synthetic']['progress'], 100)


if __name__ == '__main__':
    unittest.main()
