from tests import isolation

import asyncio
import json
import tempfile
import threading
import unittest
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import httpx

from qwen_asr_app.smartclass import SmartClassClient, LoginRequired, SmartClassError, course_key, lesson_groups
from qwen_asr_app import smartclass_service as service


def replay(ident, name='未绑定教学班的课程', teacher=None, start='2026-10-12 13:45:00', stop='2026-10-12 14:30:00'):
    return {'NewID': ident, 'CourseName': name, 'Teachers': teacher, 'Title': name,
            'StartTime': start, 'StopTime': stop, 'Duration': 2820, 'ClassRoomName': '教2-304'}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.client = SmartClassClient()
        self.addCleanup(self.client.close)
        self.client.user = {'UserType': 2, 'SchoolID': 1, 'ID': 2}

    def test_dynamic_teacherless_discovery_and_new_course_on_rescan(self):
        enrolled = {'CourseID': 1, 'ClassID': 2, 'TeacherID': 3, 'CourseName': '已绑定课程',
                    'TeacherName': '老师', 'VideoTotal': 1, 'ClassName': '没有学期编号的班级'}
        feed = [replay('a'), replay('b', start='2026-10-12 14:35:00', stop='2026-10-12 15:20:00'),
                replay('c', '已绑定课程', '老师'), replay('old', '旧学期', start='2026-07-01 13:45:00')]
        def api(path, params):
            if path.endswith('GetCourseLists'):
                return {'Data': [enrolled], 'TotalCount': 1}
            self.assertEqual(params['IncludePublic'], 'false')
            return {'Data': feed, 'TotalCount': len(feed)}
        with patch.object(self.client, 'prepare_api'), patch.object(self.client, 'api', side_effect=api):
            rows = self.client.courses('2026-2027-1')
            self.assertEqual([c['CourseName'] for c in rows], ['已绑定课程', '未绑定教学班的课程'])
            discovered = rows[1]
            self.assertEqual(discovered['VideoTotal'], 2)
            first_key = course_key(discovered)
            groups = lesson_groups(self.client.videos(discovered), discovered)
            self.assertEqual(len(groups), 1)
            self.assertEqual([v['NewID'] for v in groups[0]['videos']], ['a', 'b'])
            feed.append(replay('new', '以后出现的新课程'))
            again = self.client.courses('2026-2027-1')
            self.assertEqual(len(again), 3)
            self.assertEqual(course_key(again[1]), first_key)

    def test_personal_feed_pagination_deduplicates(self):
        feed = [replay(str(i)) for i in range(51)]
        def api(path, params):
            return {'Data': feed[:50] if params['PageNumber'] == 1 else [feed[49], feed[50]], 'TotalCount': 51}
        with patch.object(self.client, 'api', side_effect=api) as api_mock:
            self.assertEqual(len(self.client.my_videos('2026-2027-1')), 51)
            self.assertEqual(api_mock.call_count, 2)
        with patch.object(self.client, 'api', return_value={'Data': [feed[0]], 'TotalCount': 51}):
            with self.assertRaisesRegex(SmartClassError, '分页重复'):
                self.client.my_videos('2026-2027-1')

    def test_teacherless_pair_does_not_merge_different_classrooms(self):
        a, b = replay('a'), replay('b', start='2026-10-12 14:35:00', stop='2026-10-12 15:20:00')
        b['ClassRoomName'] = '教3-310'
        self.assertEqual(len(lesson_groups([a, b], {'CourseName': '课程'})), 2)

    def test_expired_session_restores_once_and_failed_credentials_do_not_loop(self):
        with tempfile.TemporaryDirectory() as raw:
            self.client.state_dir = Path(raw)
            (Path(raw) / 'credentials.json').write_text(json.dumps({'username': 'student', 'password': 'dummy'}))
            with patch.object(self.client, 'request', return_value=Mock(text='var userInfo=[{"UserID":0}];')), \
                    patch.object(self.client, 'login', side_effect=LoginRequired('密码无效')) as login:
                with self.assertRaises(LoginRequired): self.client.ensure_login()
                login.assert_called_once_with('student', 'dummy')
                self.assertEqual(self.client.user, {})

    def test_live_session_does_not_submit_credentials(self):
        with patch.object(self.client, 'request', return_value=Mock(text='var userInfo=[{"UserID":123,"Name":"测试"}];')), \
                patch.object(self.client, 'login') as login:
            self.assertEqual(self.client.ensure_login()['name'], '测试')
            login.assert_not_called()

    def test_cancelled_http_response_does_not_start_retries_or_follow_redirects(self):
        cancelled = threading.Event()
        requests = []
        def handle(request):
            requests.append(request)
            cancelled.set()
            return httpx.Response(302, headers={'location': '/next'})
        client = SmartClassClient(transport=httpx.MockTransport(handle))
        try:
            with client.operation(cancelled, time.monotonic() + 30):
                with self.assertRaisesRegex(SmartClassError, '已取消'):
                    client.request('GET', 'https://njupt.smartclass.cn/start')
            self.assertEqual(len(requests), 1)
            cancelled.clear()
            with client.operation(cancelled, time.monotonic() - 1):
                with self.assertRaisesRegex(SmartClassError, '超时'):
                    client.request('GET', 'https://njupt.smartclass.cn/start')
            self.assertEqual(len(requests), 1)
        finally:
            client.close()


class SessionEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_is_prompt_preserves_catalog_and_serializes_late_worker(self):
        entered, release = threading.Event(), threading.Event()
        def slow_courses():
            entered.set()
            release.wait(2)
            return [{'CourseName': 'late', 'replay_catalog': True, 'term': '2026-2027-1'}]
        with patch.object(service, 'status', {'state': 'connected', 'authenticated': True}), \
                patch.object(service, 'catalog_job', None), patch.object(service, 'job', None), \
                patch.object(service, 'last_session_check', 0), patch.object(service, 'course_rows', {'saved': {}}), \
                patch.object(service, 'platform_lock', asyncio.Lock()), \
                patch.object(service.client, 'ensure_login', return_value={'name': 'test'}), \
                patch.object(service.client, 'courses', side_effect=slow_courses), \
                patch.object(service, 'save_queue') as save:
            try:
                await service.ensure_session()
                async with asyncio.timeout(1):
                    while not entered.is_set(): await asyncio.sleep(.005)
                await asyncio.wait_for(service.cancel_connection(), .2)
                self.assertEqual(service.status['session_check'], 'cancelled')
                self.assertNotIn(service.status['state'], {'connecting', 'syncing'})
                self.assertEqual(service.course_rows, {'saved': {}})
                self.assertTrue(service.platform_lock.locked())
                second = Mock(return_value='next')
                retry = asyncio.create_task(service.platform_call(second))
                await asyncio.sleep(.01)
                second.assert_not_called()
                release.set()
                self.assertEqual(await retry, 'next')
                self.assertEqual(service.course_rows, {'saved': {}})
                save.assert_not_called()
            finally:
                release.set()
                await asyncio.gather(*service.platform_workers, return_exceptions=True)

    async def test_timeout_unlocks_ui_and_does_not_accept_late_catalog(self):
        release = threading.Event()
        with patch.object(service, 'status', {'state': 'connected', 'authenticated': True}), \
                patch.object(service, 'catalog_job', None), patch.object(service, 'last_session_check', 0), \
                patch.object(service, 'course_rows', {'saved': {}}), patch.object(service, 'platform_lock', asyncio.Lock()), \
                patch.object(service, 'PLATFORM_TIMEOUT', .02), \
                patch.object(service.client, 'ensure_login', side_effect=lambda: release.wait(2)):
            try:
                await service.ensure_session()
                await service.catalog_job
                self.assertEqual(service.status['state'], 'error')
                self.assertIn('超时', service.status['message'])
                self.assertEqual(service.course_rows, {'saved': {}})
            finally:
                release.set()
                await asyncio.gather(*service.platform_workers, return_exceptions=True)

    async def test_page_entry_singleflight_preserves_queue_and_replaces_catalog(self):
        with ExitStack() as stack:
            for name, value in {'status': {'state': 'running', 'authenticated': True}, 'catalog_job': None,
                                'last_session_check': 0, 'course_rows': {'old': {}}, 'group_rows': {},
                                'platform_lock': asyncio.Lock()}.items():
                stack.enter_context(patch.object(service, name, value))
            client = stack.enter_context(patch.object(service, 'client'))
            client.ensure_login.return_value = {'name': '测试'}
            client.courses.return_value = [{'CourseName': '新发现', 'TeacherName': '',
                                           'replay_catalog': True, 'term': '2026-2027-1'}]
            run = stack.enter_context(patch.object(service, 'run_queue', AsyncMock()))
            self.assertTrue((await service.ensure_session())['started'])
            first = service.catalog_job
            self.assertFalse((await service.ensure_session())['started'])
            self.assertIs(service.catalog_job, first)
            await first
            self.assertEqual(service.status['state'], 'running')
            self.assertEqual(service.status['session_check'], 'ready')
            self.assertEqual([c['CourseName'] for c in service.course_rows.values()], ['新发现'])
            run.assert_not_awaited()

    async def test_failed_rescan_keeps_catalog_and_clears_expired_identity(self):
        with patch.object(service, 'status', {'state': 'connected', 'authenticated': True, 'identity': {'name': '旧身份'}}), \
                patch.object(service, 'catalog_job', None), patch.object(service, 'last_session_check', 0), \
                patch.object(service, 'course_rows', {'previous': {}}), patch.object(service, 'platform_lock', asyncio.Lock()), \
                patch.object(service.client, 'ensure_login', side_effect=LoginRequired('账号未保存')):
            await service.ensure_session()
            await service.catalog_job
            self.assertEqual(service.course_rows, {'previous': {}})
            self.assertFalse(service.status['authenticated'])
            self.assertIsNone(service.status['identity'])
