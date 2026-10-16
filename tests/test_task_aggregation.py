"""Lesson aggregation retains owner actions and verifies recovered errors."""
from tests import isolation  # noqa: F401

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from qwen_asr_app.knowledge.ingest import CourseIngestor
from qwen_asr_app.knowledge.store import KnowledgeStore
from qwen_asr_app.knowledge.task_adapter import IntelligenceTasks
from qwen_asr_app.task_center import group_tasks, needs_attention, task_counts, task_center_router


def task(ident, status, **extra):
    return {'id': ident, 'type': 'summary', 'label': '课后笔记', 'title': '第一堂课',
            'category': '数学', 'recording_id': 'lesson', 'status': status,
            'stage': '编写笔记', 'updated_at': '2026-10-15T12:00:00+08:00',
            'actions': ['continue', 'retry'], **extra}


class LessonGroupingTests(unittest.TestCase):
    def test_duplicate_queue_error_and_summary_are_one_lesson_and_one_error(self):
        summary = task('summary:lesson', 'failed', error='连接中断')
        queue = task('course_queue', 'running', type='course_queue', recording_id='',
            course_items=[{'id': 'course-lesson', 'recording_id': 'lesson', 'title': '第一堂课',
                           'course': '数学', 'state': 'interrupted', 'message': '笔记尚未完成',
                           'progress': 99, 'phases': [{'key': 'asr', 'state': 'completed'},
                                                     {'key': 'summary', 'state': 'error'}]}],
            actions=['pause'])
        original = copy.deepcopy([queue, summary])
        groups, counts = group_tasks([queue, summary])
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group['id'], 'recording:lesson')
        self.assertEqual((group['status'], len(group['errors'])), ('failed', 1))
        self.assertEqual([t['id'] for t in group['current_tasks']], ['summary:lesson'])
        self.assertEqual(group['context_tasks'][0]['id'], 'course_queue')
        self.assertEqual(group['context_tasks'][0]['actions'], ['pause'])
        self.assertEqual(counts['attention'], 1)
        self.assertEqual([queue, summary], original)

    def test_retry_owner_overrides_stale_queue_failure_without_hiding_phase_evidence(self):
        queue = task('course_queue', 'running', type='course_queue', recording_id='',
            course_items=[{'id': 'queue-lesson', 'recording_id': 'lesson', 'course': '数学',
                           'state': 'interrupted', 'message': '旧失败',
                           'phases': [{'key': 'summary', 'state': 'error', 'detail': '旧失败'}]}])
        groups, counts = group_tasks([queue, task('summary:lesson', 'running')])
        self.assertEqual((groups[0]['status'], counts['attention'], counts['active']), ('running', 0, 1))
        self.assertEqual(groups[0]['context_tasks'][0]['course_item']['phases'][0]['detail'], '旧失败')

    def test_unstarted_review_is_separate_from_errors_and_completed_history(self):
        review = task('refinement:lesson', 'pending', type='refinement', error='', attention_kind='review')
        recovered = task('knowledge:old', 'failed', type='knowledge_index', recovered=True,
                         display_status='recovered', error='旧错误', recovery_reason='已恢复')
        done = task('summary:lesson', 'completed')
        groups, counts = group_tasks([review, recovered, done])
        self.assertEqual(groups[0]['status'], 'pending')
        self.assertEqual((counts['attention'], counts['pending'], counts['review'], counts['recovered']), (0, 1, 1, 1))
        self.assertEqual(len(groups[0]['history']), 2)
        self.assertFalse(needs_attention(review))
        self.assertFalse(needs_attention(recovered))
        self.assertEqual(task_counts([review, recovered, done])['attention'], 0)
        self.assertTrue(needs_attention({**review, 'error': '实际失败'}))

    def test_same_title_different_recordings_stay_separate_and_queue_keeps_original_action_id(self):
        queue = task('course_queue', 'running', type='course_queue', recording_id='',
                     course_items=[{'id': 'a', 'recording_id': 'first', 'title': '同名', 'state': 'running'},
                                   {'id': 'b', 'recording_id': 'second', 'title': '同名', 'state': 'queued'}])
        groups, counts = group_tasks([queue])
        self.assertEqual({g['id'] for g in groups}, {'recording:first', 'recording:second'})
        self.assertEqual({t['id'] for g in groups for t in g['tasks']}, {'course_queue'})
        self.assertEqual(counts['active'], 2)

    def test_running_work_remains_counted_with_another_failed_stage(self):
        groups, counts = group_tasks([task('summary:lesson', 'running'),
                                     task('refinement:lesson', 'failed', type='refinement', error='未完成')])
        self.assertEqual(groups[0]['status'], 'failed')
        self.assertEqual((counts['attention'], counts['active'], groups[0]['active_count']), (1, 1, 1))

    def test_unimported_lessons_are_not_merged_by_course_or_identical_title(self):
        queue = task('course_queue', 'running', type='course_queue', recording_id='',
                     course_items=[{'id': 'first', 'course': '数学', 'title': '同名课', 'state': 'running'},
                                   {'id': 'second', 'course': '数学', 'title': '同名课', 'state': 'queued'}])
        index = task('knowledge:index', 'completed', type='knowledge_index', recording_id='')
        groups, counts = group_tasks([queue, index])
        self.assertEqual({g['id'] for g in groups}, {'course-lesson:first', 'course-lesson:second', 'course:数学'})
        self.assertEqual((counts['all'], counts['active']), (3, 2))

    def test_waiting_retry_and_stopping_are_not_renamed_running_or_queued(self):
        for status in ('waiting_retry', 'stopping'):
            with self.subTest(status=status):
                groups, counts = group_tasks([task('summary:lesson', status)])
                self.assertEqual(groups[0]['status'], status)
                self.assertEqual(counts['active'], 1)


class ExactRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = KnowledgeStore(self.root / 'knowledge.db')
        self.store.upsert_course('course', '数学', str(self.root))
        self.path = self.root / 'transcript.json'
        self.path.write_text('真实来源文本', encoding='utf-8')
        self.source_id = 'rec_lesson_raw'
        contract = {'model': 'test', 'dimension': 2}
        embedder = SimpleNamespace(contract=contract, encode_documents=lambda texts: [[1., 0.] for _ in texts])
        from qwen_asr_app.knowledge.chunking import Chunk
        ingestor = CourseIngestor(SimpleNamespace(), None, None, self.store)
        ingestor._sync_single_source({'source_id': self.source_id, 'course_id': 'course', 'category': '数学',
            'lesson_id': 'lesson', 'title': '转写', 'kind': '录音', 'origin': 'original',
            'source_type': 'recording_raw', 'file_path': str(self.path), 'open_url': '/?recording=lesson',
            'loader': lambda: ('真实来源文本', [Chunk('真实来源文本')])}, contract,
            json.dumps(contract, sort_keys=True), embedder, None)
        self.jobs = [
            {'job_id': 'old', 'category': '数学', 'status': 'failed', 'stage': 'failed', 'progress': 0,
             'error': f'Embedding failure for source {self.source_id}: Windows queue budget',
             'created_at': '2020-01-01T00:00:00Z', 'updated_at': '2020-01-01T00:00:08Z'},
            {'job_id': 'new', 'category': '数学', 'status': 'completed', 'stage': 'completed', 'progress': 1,
             'error': None, 'message': '完成',
             'created_at': self.store.get_source(self.source_id)['updated_at'],
             'updated_at': self.store.get_source(self.source_id)['updated_at']}]
        record = SimpleNamespace(id='lesson', metadata={'title': '第一堂课'}, directory=self.root)
        self.adapter = IntelligenceTasks(SimpleNamespace(store=self.store, jobs=lambda: self.jobs), None,
                                         SimpleNamespace(list_records=lambda: [record]))

    def old_task(self):
        return next(t for t in self.adapter.tasks() if t['job_id'] == 'old')

    def test_proven_recovery_keeps_failed_receipt_and_original_actions(self):
        with self.store._connect() as db:
            before = '\n'.join(db.iterdump())
        task = self.old_task()
        self.assertEqual((task['status'], task['display_status']), ('failed', 'recovered'))
        self.assertEqual(task['recording_id'], 'lesson')
        self.assertEqual(task['actions'], ['retry'])
        self.assertEqual(task['recovery']['source_id'], self.source_id)
        self.assertEqual(task['recovery']['job_id'], 'new')
        self.assertEqual(task['recovery']['chunks'], 1)
        self.assertEqual(self.jobs[0]['status'], 'failed')
        with self.store._connect() as db:
            self.assertEqual('\n'.join(db.iterdump()), before)

    def test_later_success_does_not_hide_stale_file_or_unready_source(self):
        self.path.write_text('原文已经变化而未重建索引', encoding='utf-8')
        self.assertNotIn('recovered', self.old_task())

    def test_later_success_does_not_hide_missing_vector_for_active_revision(self):
        with self.store._connect() as db:
            db.execute('DELETE FROM chunk_vectors')
        self.assertNotIn('recovered', self.old_task())

    def test_later_success_does_not_hide_error_or_lexical_only_source(self):
        for column, value in [('error', '仍有错误'), ('needs_vectors', 1), ('status', 'stale')]:
            with self.subTest(column=column):
                with self.store._connect() as db:
                    db.execute(f'UPDATE sources SET {column}=? WHERE source_id=?', (value, self.source_id))
                self.assertNotIn('recovered', self.old_task())
                with self.store._connect() as db:
                    db.execute("UPDATE sources SET error=NULL, needs_vectors=0, status='active' WHERE source_id=?", (self.source_id,))

    def test_other_source_or_older_completed_job_never_proves_recovery(self):
        self.jobs[0]['error'] = 'Embedding failure for source different_source: failure'
        self.assertNotIn('recovered', self.old_task())
        self.jobs[0]['error'] = f'Embedding failure for source {self.source_id}: failure'
        self.jobs[1]['updated_at'] = '2019-01-01T00:00:00Z'
        self.assertNotIn('recovered', self.old_task())

    def test_unrelated_later_success_cannot_claim_another_jobs_source_revision(self):
        self.jobs[1].update(created_at='2099-01-01T00:00:00Z', updated_at='2099-01-01T00:00:08Z')
        self.assertNotIn('recovered', self.old_task())

    def test_completed_job_before_source_was_updated_does_not_prove_recovery(self):
        self.jobs[1].update(created_at='2021-01-01T00:00:00Z', updated_at='2021-01-01T00:00:08Z')
        self.assertNotIn('recovered', self.old_task())


class FilteredFeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_unfiltered_feed_keeps_all_courses_and_owner_action_targets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            courses = SimpleNamespace(store=SimpleNamespace(
                settings=SimpleNamespace(categories=['数学', '物理'], storage_root=root), list=lambda category: []), tasks={})
            tasks = [task('summary:lesson', 'running', recording_title='第一堂课'),
                     task('summary:other', 'failed', category='物理', recording_id='other', recording_title='第二堂课')]
            queue = task('course_queue', 'interrupted', type='course_queue', category='', recording_id='',
                         course_items=[{'id': 'queue-a', 'recording_id': 'lesson', 'course': '数学',
                                        'state': 'interrupted', 'phases': [{'key': 'summary', 'state': 'error'}]}])
            actions = []
            async def ai_action(payload):
                actions.append(payload)
                return {'ok': True}
            api = FastAPI()
            with patch('qwen_asr_app.task_center.PRIVATE_ROOT', root), \
                 patch('qwen_asr_app.task_center.discover_courses'), \
                 patch('qwen_asr_app.task_center.course_tasks', return_value=[queue]):
                api.include_router(task_center_router(lambda **kwargs: tasks, ai_action, courses, SimpleNamespace(jobs={})))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test') as client:
                    feed = (await client.get('/api/tasks')).json()
                    response = await client.post('/api/tasks/action', json={'id': 'summary:lesson', 'action': 'retry'},
                                                 headers={'X-QwenASR-Client': 'task-center'})
            self.assertEqual(feed['counts']['all'], 3)
            self.assertEqual({t['id'] for t in feed['tasks']}, {'summary:lesson', 'summary:other', 'course_queue'})
            self.assertEqual({g['id'] for g in feed['groups']}, {'recording:lesson', 'recording:other'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(actions[0]['recording_id'], 'lesson')

    async def test_category_filter_recomputes_group_and_task_counts(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            courses = SimpleNamespace(store=SimpleNamespace(
                settings=SimpleNamespace(categories=['数学', '物理'], storage_root=root), list=lambda category: []), tasks={})
            tasks = [task('summary:lesson', 'running', recording_title='第一堂课'),
                     task('summary:other', 'failed', category='物理', recording_id='other', recording_title='第二堂课')]
            queue = task('course_queue', 'running', type='course_queue', recording_id='', category='',
                         course_items=[{'id': 'queue-a', 'recording_id': 'lesson', 'course': '数学', 'state': 'running'},
                                       {'id': 'queue-b', 'recording_id': 'other', 'course': '物理', 'state': 'interrupted'}])
            api = FastAPI()
            with patch('qwen_asr_app.task_center.PRIVATE_ROOT', root), \
                 patch('qwen_asr_app.task_center.discover_courses'), \
                 patch('qwen_asr_app.task_center.course_tasks', return_value=[queue]):
                api.include_router(task_center_router(lambda **kwargs: tasks, None, courses, SimpleNamespace(jobs={})))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test') as client:
                    response = await client.get('/api/tasks', params={'category': '数学'})
            self.assertEqual(response.status_code, 200)
            feed = response.json()
            self.assertEqual(feed['counts']['all'], 2)
            self.assertEqual(feed['group_counts']['all'], 1)
            self.assertEqual(feed['group_counts']['attention'], 0)
            self.assertEqual(feed['groups'][0]['id'], 'recording:lesson')
            self.assertEqual(len(next(t for t in feed['tasks'] if t['type'] == 'course_queue')['course_items']), 1)


class RetryCurrentFailuresTests(unittest.IsolatedAsyncioTestCase):
    async def request_retry(self, root, tasks, ai_action, queue=None, intelligence=None):
        courses = SimpleNamespace(store=SimpleNamespace(
            settings=SimpleNamespace(categories=['数学', '物理'], storage_root=root), list=lambda category: []), tasks={})
        api = FastAPI()
        with patch('qwen_asr_app.task_center.PRIVATE_ROOT', root), \
             patch('qwen_asr_app.task_center.discover_courses'), \
             patch('qwen_asr_app.task_center.course_tasks', return_value=queue or []):
            api.include_router(task_center_router(lambda **kwargs: tasks, ai_action, courses,
                                                 SimpleNamespace(jobs={}), intelligence_tasks=intelligence))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test') as client:
                return await client.post('/api/tasks/retry-failed', headers={'X-QwenASR-Client': 'task-center'})

    async def test_only_actual_failure_retries_among_review_pending_recovered_and_queue_context(self):
        failed = task('summary:lesson', 'failed', error='连接中断', recording_title='第一堂课')
        review = task('refinement:lesson', 'pending', type='refinement', error='',
                      attention_kind='review', recording_title='第一堂课')
        pending = task('summary:unstarted', 'pending', recording_id='unstarted', error='', recording_title='未开始')
        blocked = task('summary:blocked', 'failed', recording_id='blocked', error='仅支持取消',
                       actions=['cancel'], recording_title='无重试操作')
        recovered = task('knowledge:old', 'failed', type='knowledge_index', recovered=True,
                         display_status='recovered', error='历史错误', recovery_reason='已恢复')
        queue = task('course_queue', 'interrupted', type='course_queue', recording_id='',
                     actions=['continue'], course_items=[{'id': 'queue-lesson', 'recording_id': 'lesson',
                         'course': '数学', 'state': 'interrupted',
                         'phases': [{'key': 'summary', 'state': 'error', 'detail': '同一笔记错误'}]}])
        inputs = [failed, review, pending, blocked]
        before = copy.deepcopy([inputs, recovered, queue])
        ai_action, knowledge_action, course_action = AsyncMock(return_value={'ok': True}), AsyncMock(), AsyncMock()
        with tempfile.TemporaryDirectory() as raw, patch('qwen_asr_app.task_center.course_action', course_action):
            response = await self.request_retry(Path(raw), inputs, ai_action, [queue],
                SimpleNamespace(tasks=lambda: [recovered], action=knowledge_action))
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()['requested'], response.json()['accepted']), (1, 1))
        self.assertEqual(response.json()['results'][0]['id'], 'summary:lesson')
        ai_action.assert_awaited_once()
        self.assertEqual(ai_action.await_args.args[0]['recording_id'], 'lesson')
        self.assertEqual(ai_action.await_args.args[0]['action'], 'retry')
        knowledge_action.assert_not_awaited()
        course_action.assert_not_awaited()
        self.assertEqual([inputs, recovered, queue], before)

    async def test_failure_becoming_review_during_batch_is_not_submitted(self):
        first = task('summary:first', 'failed', error='失败', recording_id='first', recording_title='第一堂课',
                     updated_at='2026-10-15T13:00:00+08:00')
        second = task('refinement:second', 'failed', type='refinement', error='失败',
                      recording_id='second', recording_title='第二堂课')
        async def action(payload):
            second.update(status='pending', error='', attention_kind='review')
            return {'ok': True}
        ai_action = AsyncMock(side_effect=action)
        with tempfile.TemporaryDirectory() as raw:
            response = await self.request_retry(Path(raw), [first, second], ai_action)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()['requested'], response.json()['accepted']), (2, 1))
        ai_action.assert_awaited_once()
        self.assertEqual(second['status'], 'pending')
        self.assertIn('当前无需重试', response.json()['results'][1]['error'])

    async def test_uncovered_queue_is_resumed_once_using_its_original_owner_id(self):
        queue = task('course_queue', 'interrupted', type='course_queue', recording_id='', actions=['continue'],
                     course_items=[{'id': 'first', 'course': '数学', 'state': 'interrupted'},
                                   {'id': 'second', 'course': '数学', 'state': 'interrupted'}])
        course_action = AsyncMock(return_value={'started': True})
        with tempfile.TemporaryDirectory() as raw, patch('qwen_asr_app.task_center.course_action', course_action):
            response = await self.request_retry(Path(raw), [], AsyncMock(), [queue])
        self.assertEqual((response.json()['requested'], response.json()['accepted']), (1, 1))
        self.assertEqual(response.json()['results'][0]['id'], 'course_queue')
        course_action.assert_awaited_once_with('continue')


if __name__ == '__main__':
    unittest.main()
