from tests import isolation

import json
import tempfile
import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

import httpx

from qwen_asr_app.course_replay import replay_sources, replay_target
from qwen_asr_app.smartclass import CAS, SmartClassClient, SmartClassError, LoginRequired
from qwen_asr_app.replay_bridge import replay_bridge_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


FIRST, SECOND = 'a' * 32, 'b' * 32


class ReplayClockTests(unittest.TestCase):
    def record(self, directory, sources, **stream):
        return SimpleNamespace(id='lesson-test', directory=Path(directory), metadata={
            'title': '课堂笔记', 'course_stream': {'sources': sources, **stream}})

    def sources(self):
        return [{'video_id': FIRST, 'segment_id': 101, 'duration': 2829, 'title': '上半节'},
                {'video_id': SECOND, 'segment_id': 102, 'duration': 2805, 'title': '下半节'}]

    def test_time_below_second_period_and_exact_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            record = self.record(directory, self.sources())
            first = replay_target(record, 60)
            second = replay_target(record, 5371)  # Screenshot: 01:29:31.
            self.assertEqual(first['seconds'], 60)
            self.assertEqual(second['seconds'], 2542)
            self.assertEqual(second['video_id'], SECOND)
            self.assertEqual(replay_target(record, 2829)['seconds'], 0)
            self.assertEqual(parse_qs(urlsplit(second['url']).query)['SegementID'], ['102'])
            self.assertEqual(urlsplit(second['url']).fragment, '')

    def test_decoded_lengths_override_estimated_platform_durations(self):
        with tempfile.TemporaryDirectory() as directory:
            chunks = [{'samples': 2800 * 16000, 'cursor': {'source': 1, 'samples': 0}},
                      {'samples': 2780 * 16000, 'cursor': {'source': 2, 'samples': 0}}]
            record = self.record(directory, self.sources(), chunks=chunks)
            self.assertEqual(replay_sources(record)[1]['offset_seconds'], 2800)
            self.assertEqual(replay_target(record, 2820)['seconds'], 20)

    def test_manifest_imports_and_actual_source_lengths(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = {'sources': [{'video_id': FIRST, 'segment_id': 101,
                        'duration_seconds': 50, 'source_offset_seconds': 10,
                        'source_title': '旧导入'}]}
            (Path(directory) / '课程来源.json').write_text(json.dumps(manifest))
            record = SimpleNamespace(id='old', directory=Path(directory), metadata={})
            self.assertEqual(replay_target(record, 20)['seconds'], 30)
            self.assertEqual(replay_sources(record)[0]['title'], '旧导入')

    def test_completed_manifest_wins_over_estimated_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            record = self.record(directory, self.sources())
            (Path(directory) / '课程来源.json').write_text(json.dumps({'completed_sources': [
                {'video_id': FIRST, 'segment_id': 101, 'duration_seconds': 2800}]}))
            self.assertEqual(replay_target(record, 2805)['seconds'], 5)

    def test_legacy_supplement_keeps_full_manifest_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            chunks = [{'samples': 120 * 16000} for _ in range(23)]
            chunks += [{'samples': 40 * 16000, 'cursor': {'source': 1, 'samples': 0}},
                       {'samples': 2780 * 16000, 'cursor': {'source': 2, 'samples': 0}}]
            record = self.record(directory, self.sources(), chunks=chunks)
            (Path(directory) / '课程来源.json').write_text(json.dumps({'sources': [
                {'video_id': FIRST, 'segment_id': 101, 'duration_seconds': 2800}]}))
            self.assertEqual(replay_sources(record)[0]['duration_seconds'], 2800)
            self.assertEqual(replay_target(record, 2820)['seconds'], 20)
            self.assertEqual(replay_sources(record)[1]['duration_seconds'], 2780)

    def test_incomplete_or_discontinuous_chunks_do_not_override_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            cases = [
                [{'samples': 40 * 16000}],
                [{'samples': 40 * 16000, 'cursor': {'source': 0, 'samples': 2800 * 16000}}],
                [{'samples': 60 * 16000, 'cursor': {'source': 0, 'samples': 60 * 16000}},
                 {'samples': 60 * 16000, 'cursor': {'source': 0, 'samples': 180 * 16000}},
                 {'samples': 40 * 16000, 'cursor': {'source': 1, 'samples': 0}}],
                [{'samples': 60 * 16000, 'cursor': {'source': 0, 'samples': 60 * 16000}}],
            ]
            for chunks in cases:
                with self.subTest(chunks=chunks):
                    record = self.record(directory, self.sources(), chunks=chunks)
                    self.assertEqual(replay_sources(record)[1]['offset_seconds'], 2829)

    def test_complete_multichunk_source_uses_decoded_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            chunks = [{'samples': 120 * 16000, 'cursor': {'source': 0, 'samples': i * 120 * 16000}}
                      for i in range(1, 24)]
            chunks += [{'samples': 40 * 16000, 'cursor': {'source': 1, 'samples': 0}}]
            record = self.record(directory, self.sources(), chunks=chunks)
            self.assertEqual(replay_target(record, 2820)['seconds'], 20)

    def test_multiple_media_segments_still_have_their_own_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            sources = [{'video_id': FIRST, 'segment_id': 101, 'duration': 60},
                       {'video_id': FIRST, 'segment_id': 102, 'duration': 90},
                       {'video_id': SECOND, 'segment_id': 103, 'duration': 120}]
            target = replay_target(self.record(directory, sources), 70)
            self.assertEqual(target['segment_id'], '102')
            self.assertEqual(target['seconds'], 10)

    def test_invalid_or_missing_source_and_clock_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            record = self.record(directory, self.sources())
            for value in (-1, float('nan'), float('inf'), 7000):
                with self.assertRaises(ValueError): replay_target(record, value)
            for value in ([], [{'video_id': '//evil', 'segment_id': 101, 'duration': 50}]):
                with self.assertRaises(ValueError): replay_target(self.record(directory, value), 0)


class BrowserLoginTicketTests(unittest.TestCase):
    def test_ticket_is_not_consumed_by_backend(self):
        seen = []
        def handler(request):
            seen.append((request.url.host, request.url.path))
            if request.url.path == '/cas/login':
                return httpx.Response(302, headers={'location': CAS + '/cas/granting'})
            if request.url.path == '/cas/granting':
                return httpx.Response(302, headers={'location':
                    'https://njupt.smartclass.cn/SystemSpace/Redirect.aspx?ticket=ST-test'})
            raise AssertionError('Backend consumed browser ticket')
        client = SmartClassClient(transport=httpx.MockTransport(handler))
        try:
            ticket = client.browser_login_ticket()
        finally: client.close()
        self.assertEqual(parse_qs(urlsplit(ticket).query)['ticket'], ['ST-test'])
        self.assertEqual(len(seen), 2)

    def test_unapproved_host_and_non_ticket_redirect_rejected(self):
        for destination in ('https://evil.test/?ticket=secret',
                            'https://njupt.smartclass.cn/other?ticket=secret'):
            client = SmartClassClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(302, headers={'location': destination})))
            try:
                with self.assertRaises(SmartClassError): client.browser_login_ticket()
            finally: client.close()


class ReplayAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_login_finishes_without_loading_courses_or_resuming_queue(self):
        from qwen_asr_app import smartclass_service as service
        with patch.object(service, 'status', {}), patch.object(service, 'client') as client, \
                patch.object(service, 'load_courses', AsyncMock()) as load, \
                patch.object(service, 'defaults', AsyncMock()) as defaults, \
                patch.object(service, 'reconcile_imports', AsyncMock()) as reconcile, \
                patch.object(service, 'run_queue', AsyncMock()) as run:
            client.login.return_value = {'UserID': 123}
            await service.connect('test', 'test-password', False, replay_only=True)
            self.assertTrue(service.status['authenticated'])
            self.assertEqual(service.status['identity'], {'UserID': 123})
            load.assert_not_awaited()
            defaults.assert_not_awaited()
            reconcile.assert_not_awaited()
            run.assert_not_awaited()

    async def test_failed_replay_login_clears_previous_authentication(self):
        from qwen_asr_app import smartclass_service as service
        with patch.object(service, 'status', {'authenticated':True, 'identity':{'UserID':123}}), \
                patch.object(service, 'client') as client:
            client.login.side_effect = SmartClassError('登录失败')
            await service.connect('test', 'bad-password', False, replay_only=True)
            self.assertFalse(service.status['authenticated'])
            self.assertIsNone(service.status['identity'])


class BrowserHandoffRoutesTests(unittest.TestCase):
    def app(self, directory, expired=False):
        client = SimpleNamespace(state_dir=Path(directory),
            request=Mock(side_effect=LoginRequired('expired')) if expired else Mock(return_value=SimpleNamespace(
                text='var userInfo=[{"UserID":123}];')),
            login=Mock(), browser_login_ticket=Mock(return_value=
                'https://njupt.smartclass.cn/SystemSpace/Redirect.aspx?ticket=ST-browser'))
        app = FastAPI()
        app.include_router(replay_bridge_router(client, asyncio.Lock()))
        return TestClient(app), client

    def test_existing_server_session_yields_a_fresh_browser_ticket(self):
        with tempfile.TemporaryDirectory() as directory:
            api, client = self.app(directory)
            response = api.post('/api/browser-login')
            self.assertEqual(response.status_code, 200)
            client.login.assert_not_called()
            client.browser_login_ticket.assert_called_once()

    def test_expired_server_login_restored_without_starting_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'credentials.json').write_text(json.dumps({'username':'test', 'password':'test-password'}))
            api, client = self.app(directory, expired=True)
            response = api.post('/api/browser-login')
            self.assertEqual(response.status_code, 200)
            client.login.assert_called_once_with('test', 'test-password')
            self.assertNotIn('test-password', response.text)

    def test_missing_credentials_requires_login(self):
        with tempfile.TemporaryDirectory() as directory:
            api, client = self.app(directory, expired=True)
            self.assertEqual(api.post('/api/browser-login').status_code, 401)
            client.login.assert_not_called()

    def test_userscript_is_served_as_installable_source(self):
        with tempfile.TemporaryDirectory() as directory:
            api, client = self.app(directory)
            response = api.get('/replay-script.user.js')
            self.assertEqual(response.status_code, 200)
            self.assertIn('// ==UserScript==', response.text)
            self.assertIn('text/javascript', response.headers['content-type'])
            self.assertIn('inline', response.headers['content-disposition'])
            self.assertNotIn('GM_xmlhttpRequest', response.text)
            self.assertNotIn('@connect', response.text)


if __name__ == '__main__':
    unittest.main()
