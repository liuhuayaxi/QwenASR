from tests import isolation

import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

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


class SessionEntryTests(unittest.IsolatedAsyncioTestCase):
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
