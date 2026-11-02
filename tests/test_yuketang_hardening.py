"""Offline regression cases for PR #7 hardening. No accounts or model calls."""
from tests import isolation

import asyncio
import copy
import hashlib
import io
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from qwen_asr_app.yuketang import parsing, retrieval
from qwen_asr_app.yuketang.client import HOST, LoginRequired, YuketangSession
from qwen_asr_app.yuketang.store import BankStore
from qwen_asr_app.yuketang.sync import ClassroomSync
from qwen_asr_app.yuketang.vision import transcribe_classroom
from tests.test_yuketang import FakeSession, Recording, bank_question, lesson_store


def png():
    from PIL import Image
    stream = io.BytesIO()
    Image.new('RGB', (24, 24), 'black').save(stream, 'PNG')
    return stream.getvalue()


class PromptBudgetTests(unittest.TestCase):
    def test_serialized_budget_and_required_flags(self):
        for count in (1, 10, 100, 500):
            source = [{'id': f'q:{i}', '题号': f'Y{i}', '必含': True, '题干': '长题干' * 1000,
                       '选项': ['A. ' + '长选项' * 300, 'B. 其他'], '正确答案': ['A'], '答案已知': True}
                      for i in range(count)]
            before = copy.deepcopy(source)
            started = time.monotonic()
            fitted = retrieval.fit(source)
            self.assertLessEqual(len(json.dumps(fitted, ensure_ascii=False)), 14000)
            self.assertTrue(all(q.get('必含') for q in fitted))
            self.assertEqual(source, before, 'model fitting must not mutate the full appendix')
            self.assertLess(time.monotonic() - started, 3, 'fitting must not repeatedly rescan all questions')

    def test_prompt_wrapper_counts_towards_budget(self):
        items = [{'题号': f'Y{i}', '必含': True, '题干': 'x' * 260, '正确答案': ['A']} for i in range(100)]
        self.assertLessEqual(len(retrieval.prompt_block(items)), retrieval.MAX_CONTEXT_CHARS)

    def test_compact_never_exports_student_answers_or_scores(self):
        q = bank_question(body='题目', student_answer=['private answer'], score=1, correct=False)
        compact = retrieval.compact(q)
        self.assertFalse({'我的答案', '得分', '是否答对'} & compact.keys())

    def test_single_request_planning_counts_actual_question_block(self):
        from qwen_asr_app.ai import summary_instructions
        from qwen_asr_app.batch_ai import note_summary_chunks
        config = {'ai': {'summary_provider': {'model': 'mock', 'context_window': 8192, 'max_output_tokens': 2048}},
                  '_yuketang_questions': [{'题号': 'Y1', '必含': True, '题干': '题目' * 6000}]}
        # The planner must receive the same full-note options as the real call.
        full = summary_instructions({**config, '_full_note': True})
        self.assertIn('<yuketang_questions>', full)
        chunk = summary_instructions({**config, '_detailed_chunk': True})
        self.assertNotIn('\n<yuketang_questions>\n', chunk)


class CompletenessTests(unittest.TestCase):
    def test_shortened_model_entry_restores_full_stem_options_and_answer(self):
        q = {'id': 'q1', '题号': 'Y1', '必含': True, '题干': '题干前半。' + '必须保留的后半条件。' * 80,
             '选项': ['A. 选项完整条件' * 40, 'B. 另一个选项'], '正确答案': ['A'], '答案已知': True,
             '解析': '教师解析'}
        note = '## 课上习题\n\n### 第1题\n<!-- yuketang:Y1 -->\n题干前半。\n\n**答案**：B\n\n**解析**：模型推导。\n'
        fixed = retrieval.ensure_section(note, [q])
        self.assertIn(q['题干'], fixed)
        self.assertIn(q['选项'][0], fixed)
        self.assertIn('**答案**：A', fixed)
        self.assertIn('模型推导', fixed)
        self.assertEqual(retrieval.ensure_section(fixed, [q]), fixed)
        self.assertEqual(fixed.count('### 第1题'), 1)

    def test_marker_reference_in_another_entry_is_not_presence(self):
        items = [{'id': f'q{i}', '题号': f'Y{i}', '必含': True, '题干': f'第{i}个独立问题的完整题干', '正确答案': ['A']} for i in (1, 2)]
        note = '## 课上习题\n\n### 第1题\n<!-- yuketang:Y1 -->\n第1个独立问题的完整题干\n**答案**：A\n**解析**：可参考〔Y2〕。\n'
        self.assertEqual([q['id'] for q in retrieval.missing_forced(note, items)], ['q2'])

    def test_duplicate_generic_prefix_is_not_an_exact_id_match(self):
        q1 = {'id': 'q:1', '题号': 'Y1', '必含': True, '题干': '这是一个很长的共同题干前缀' * 3 + '不同参数1'}
        q2 = {**q1, 'id': 'q:10', '题号': 'Y2', '题干': q1['题干'][:-1] + '2'}
        note = '## 课上习题\n\n### 第1题\n<!-- yuketang:Y2 -->\n' + q2['题干'] + '\n**答案**：2\n'
        self.assertEqual([q['id'] for q in retrieval.missing_forced(note, [q1, q2])], ['q:1'])

    def test_image_only_fallback_remains_visible_before_attachment(self):
        text = retrieval.appendix([{'题号': 'Y1', '必含': True, '原题图片': 1, '题干': '（题干见雨课堂原题图片）'}])
        self.assertIn('原题图片', text)


class StoreIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = BankStore(Path(self.temp.name) / 'bank')

    def test_corrupted_question_cache_is_not_silently_empty(self):
        self.store.save_questions('1', '1', [bank_question(id='q')])
        (self.store.classroom_dir('1') / 'questions/1.json').write_text('{invalid')
        with self.assertRaises((RuntimeError, ValueError)):
            self.store.questions('1')

    def test_explicit_reimport_repairs_corruption_and_keeps_original_bytes(self):
        self.store.save_questions('1', '1', [bank_question(id='q')])
        path = self.store.classroom_dir('1') / 'questions/1.json'
        path.write_bytes(b'{corrupt original')
        self.store.save_questions('1', '1', [bank_question(id='fresh', body='reimported')])
        self.assertEqual([q['id'] for q in self.store.questions('1')], ['fresh'])
        backups = list(path.parent.glob('*.corrupt-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'{corrupt original')

    def test_changed_image_invalidates_finished_vision_and_never_restores_student_image(self):
        old = bank_question(id='q', body='same', images=[{'kind': 'body', 'source': 'a', 'path': 'images/1/a.png', 'sha256': 'old'},
                                                      {'kind': 'student', 'source': 's', 'path': 'images/1/private.png'}],
                            vision={'status': 'done', 'text': 'stale'})
        self.store.save_questions('1', '1', [old])
        newer = bank_question(id='q', body='same', images=[{'kind': 'body', 'source': 'a', 'path': 'images/1/a.png', 'sha256': 'new'},
                                                        {'kind': 'student', 'source': 's'}])
        self.store.save_questions('1', '1', [newer])
        saved = self.store.questions('1')[0]
        self.assertNotIn('vision', saved)
        self.assertFalse(any(i.get('kind') == 'student' for i in saved['images']))

    def test_raw_scrubbing_removes_all_url_queries_and_named_secrets(self):
        self.store.save_raw('1', '1', 'payload', {'token': 'private-token', 'content': '<img src="https://x.yuketang.cn/a?X-Amz-Signature=private-query&foo=1">'})
        text = json.dumps(self.store.raw('1', '1', 'payload'))
        self.assertNotIn('private-token', text)
        self.assertNotIn('private-query', text)
        self.assertNotIn('?X-Amz', text)


class ImportAndClientTests(unittest.TestCase):
    def test_numeric_string_types_and_boolean_flags(self):
        self.assertEqual(parsing.problem_type('1'), 'single')
        self.assertEqual(parsing.problem_type('2'), 'multiple')
        self.assertIs(parsing.bool_value('0'), False)
        self.assertIs(parsing.bool_value('1'), True)

    def test_json_responses_disposed_on_success_and_failure(self):
        for status, value in ((200, '{"data":{}}'), (401, '{}'), (500, '{}')):
            session = YuketangSession(Path('/unused'), delay=0)
            response = MagicMock(status=status)
            response.text.return_value = value
            session.context = SimpleNamespace(request=SimpleNamespace(get=lambda *a, **kw: response))
            try:
                session.get_json(HOST + '/v2/api/web/userinfo')
            except (RuntimeError, LoginRequired):
                pass
            response.dispose.assert_called_once()

    def test_download_redirect_and_html_are_not_saved_as_images(self):
        for status, body, mime in ((302, b'redirect', 'text/html'), (200, b'<html>login</html>', 'text/html')):
            session = YuketangSession(Path('/unused'), delay=0)
            response = MagicMock(status=status, headers={'content-type': mime})
            response.body.return_value = body
            session.context = SimpleNamespace(request=SimpleNamespace(get=lambda *a, **kw: response))
            with self.assertRaises((RuntimeError, ValueError)):
                session.download('https://a.yuketang.cn/image.png')
            response.dispose.assert_called_once()

    def test_valid_image_download_is_disposed(self):
        session = YuketangSession(Path('/unused'), delay=0)
        response = MagicMock(status=200, headers={'content-type': 'image/png'})
        response.body.return_value = png()
        session.context = SimpleNamespace(request=SimpleNamespace(get=lambda *a, **kw: response))
        self.assertEqual(session.download('https://a.yuketang.cn/image.png'), png())
        response.dispose.assert_called_once()

    def test_stale_accepts_legacy_naive_timestamp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runner = ClassroomSync(FakeSession(), BankStore(Path(temp.name)), '1')
        self.assertTrue(runner._stale({'checked_at': '2026-10-01T12:00:00'}, hours=0.1))

    def test_incomplete_pagination_is_an_error(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runner = ClassroomSync(FakeSession(), BankStore(Path(temp.name)), '1')
        with patch.object(runner, 'get', return_value={'errcode': 0, 'data': {'activities': [{'id': '1', 'type': 2}], 'has_more': True}}), \
             patch('qwen_asr_app.yuketang.sync.MAX_PAGES', 1):
            with self.assertRaises(RuntimeError):
                runner.list_activities()


class LessonIsolationTests(unittest.TestCase):
    def test_unrelated_evening_recording_is_not_matched_to_morning(self):
        lessons = [{'classroom_id': '1', 'activity_id': 'L1', 'title': '课次',
                    'start': datetime(2026, 10, 21, 8), 'end': datetime(2026, 10, 21, 9, 40)}]
        self.assertIsNone(retrieval.lesson_for_recording(lessons, datetime(2026, 10, 21, 20), datetime(2026, 10, 21, 21)))

    def test_optional_quiz_and_cards_have_independent_limit(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = lesson_store(temp.name, problems=16)
        store.save_questions('100', 'extra', [bank_question(id=f'quiz:extra:{i}', activity_id='extra', classroom_id='100',
                                source='quiz', date='2026-10-20', body='HDB3 编码替换规则与极性规律') for i in range(12)])
        items = retrieval.questions_for_record(Recording('2026-10-21T08:02:00', 98), store=store, transcript='HDB3 编码替换规则与极性规律')
        self.assertEqual(sum(not bool(q.get('必含')) for q in items), 8)


class VisionIntegrityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = BankStore(Path(self.temp.name) / 'bank')
        self.image = {'kind': 'body', 'source': 'https://a.yuketang.cn/p.png', **self.store.save_image('1', '1', 'p.png', png())}
        self.store.save_questions('1', '1', [bank_question(id='q1', needs_vision=True, images=[self.image])])
        self.calls = []

    async def request(self, instructions, folder, paths):
        import re
        self.calls.append((instructions, list(paths)))
        ids = re.findall(r'"image_id": "(img\d+)"', instructions)
        return {'images': [{'image_id': i, 'text': '图中题干', 'answer_text': ''} for i in ids]}

    async def test_second_run_has_zero_requests_and_model_change_invalidates(self):
        first = await transcribe_classroom(self.store, '1', provider={'model': 'a'}, request=self.request)
        second = await transcribe_classroom(self.store, '1', provider={'model': 'a'}, request=self.request)
        self.assertEqual((first['done'], second['done'], len(self.calls)), (1, 0, 1))
        third = await transcribe_classroom(self.store, '1', provider={'model': 'b'}, request=self.request)
        self.assertEqual((third['done'], len(self.calls)), (1, 2))

    async def test_shared_image_cache_avoids_second_question_call(self):
        self.store.save_questions('1', '2', [bank_question(id='q2', needs_vision=True, images=[self.image])])
        result = await transcribe_classroom(self.store, '1', request=self.request)
        self.assertEqual((result['done'], len(self.calls)), (2, 1))

    async def test_changed_file_not_stale_metadata_invalidates_cache(self):
        await transcribe_classroom(self.store, '1', request=self.request)
        self.store.image_path('1', self.image['path']).write_bytes(png() + b'changed')
        await transcribe_classroom(self.store, '1', request=self.request)
        self.assertEqual(len(self.calls), 2)

    async def test_mixed_cache_hits_keep_original_image_order(self):
        second = {'kind': 'body', 'source': 'https://a.yuketang.cn/q.png', **self.store.save_image('1', '1', 'q.png', png() + b'q')}
        self.store.save_questions('1', '1', [bank_question(id='q1', needs_vision=True, images=[second])])
        await transcribe_classroom(self.store, '1', request=self.request)
        self.store.save_questions('1', '1', [bank_question(id='q1', needs_vision=True, images=[self.image, second])])
        async def first_only(instructions, folder, paths):
            self.assertEqual(len(paths), 1)
            return {'images': [{'image_id': 'img0', 'text': 'FIRST', 'answer_text': ''}]}
        await transcribe_classroom(self.store, '1', request=first_only)
        self.assertEqual(self.store.questions('1')[0]['vision']['text'], 'FIRST\n图中题干')

    async def test_duplicate_receipts_never_mark_done(self):
        async def duplicate(*args):
            return {'images': [{'image_id': 'img0', 'text': 'a', 'answer_text': ''}, {'image_id': 'img0', 'text': 'b', 'answer_text': ''}]}
        result = await transcribe_classroom(self.store, '1', request=duplicate)
        self.assertEqual((result['done'], result['errors']), (0, 1))

    async def test_empty_receipt_never_marked_complete(self):
        async def empty(*args):
            return {'images': [{'image_id': 'img0', 'text': '', 'answer_text': ''}]}
        result = await transcribe_classroom(self.store, '1', request=empty)
        self.assertEqual((result['done'], result['errors']), (0, 1))

    async def test_concurrent_sync_is_not_overwritten(self):
        async def updated(*args):
            self.store.save_questions('1', '1', [bank_question(id='q1', body='new content', needs_vision=True, images=[self.image])])
            return {'images': [{'image_id': 'img0', 'text': 'stale', 'answer_text': ''}]}
        result = await transcribe_classroom(self.store, '1', request=updated)
        saved = self.store.questions('1')[0]
        self.assertEqual(result['done'], 0)
        self.assertEqual(saved['body'], 'new content')
        self.assertNotEqual((saved.get('vision') or {}).get('text'), 'stale')

    async def test_stop_prevents_model_calls(self):
        from qwen_asr_app.yuketang.sync import SyncAborted
        with self.assertRaises(SyncAborted):
            await transcribe_classroom(self.store, '1', request=self.request, should_stop=lambda: True)
        self.assertEqual(self.calls, [])


class TextNoteImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_pipeline_attaches_original_image_without_extra_model_calls(self):
        import os
        from qwen_asr_app.storage import RecordingRecord
        from qwen_asr_app import batch_ai
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bank = BankStore(root / 'bank')
            bank.bind('test-course', '1')
            image = {'kind': 'body', 'source': 'https://a.yuketang.cn/q.png', **bank.save_image('1', '1', 'q.png', png())}
            question = bank_question(id='quiz:1:1', classroom_id='1', activity_id='1', needs_vision=True, images=[image], _forced=True, _marker='Y1')
            bank.save_questions('1', '1', [question])
            compact = retrieval.compact(question, bank)
            record = RecordingRecord(root / 'record', {'id': 'r', 'category': 'test-course', 'title': 'image lesson', 'started_at': '2026-10-21T08:00:00+08:00'})
            record.segments = [{'id': 1, 'start': 0, 'end': 1, 'raw': '这里是课堂内容。'}]
            record.save_all()
            summary = AsyncMock(return_value={'summary': '## 知识点\n\n正文。'})
            with patch.dict(os.environ, {'QWEN_ASR_YUKETANG_DIR': str(bank.root)}), \
                 patch.object(batch_ai, '_yuketang_questions', AsyncMock(return_value=[compact])), \
                 patch.object(batch_ai, 'summarize_transcript', summary), \
                 patch.object(batch_ai, 'summarize_note_abstract', AsyncMock(return_value='摘要')), \
                 patch('qwen_asr_app.transcription_assist.service.evidence_config', AsyncMock(return_value={})), \
                 patch('qwen_asr_app.classroom_intelligence.integrations.saved_notes_markdown', return_value=''):
                result = await batch_ai._build_text_note_summary(record, {'ai': {'summary_provider': {'model': 'mock', 'context_window': 32768, 'max_output_tokens': 1024}}})
            import re
            links = re.findall(r'\]\((assets/fig-[a-f0-9]{16}\.png)\)', result['summary'])
            self.assertEqual(len(links), 1)
            self.assertTrue((record.directory / result['review_package'] / links[0]).is_file())
            self.assertEqual(summary.await_count, 1)


class FastFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_payload_can_split_and_resume_without_full_bank(self):
        from qwen_asr_app.fast_note import _fallback_payload
        from qwen_asr_app.note_markdown_batches import write_batches
        from qwen_asr_app.agy_cli import last_session_id
        payload = _fallback_payload({'transcript': [{'source': 'S1', 'text': '第一段'}, {'source': 'S2', 'text': '第二段'}],
                                      'yuketang_questions': [{'题干': 'large bank'}]})
        calls = []
        async def write(**kwargs):
            calls.append(kwargs['payload'])
            last_session_id.set('synthetic-writer-' + str(len(calls)))
            return '\n'.join(f'<!-- knowledge:{key} -->课堂正文<!-- /knowledge:{key} -->' for key in kwargs['payload']['required_knowledge_ids'])
        with tempfile.TemporaryDirectory() as temporary:
            with patch('qwen_asr_app.note_markdown_batches.request_ai_text', side_effect=write):
                text, _ = await write_batches('test', payload, {}, Path(temporary))
                again, _ = await write_batches('test', payload, {}, Path(temporary))
            self.assertEqual(text, again)
            self.assertEqual(len(calls), 2)
            self.assertTrue(all('yuketang_questions' not in call for call in calls))


class EmbeddedQuestionTests(unittest.TestCase):
    def test_cards_and_quiz_option_images_are_preserved(self):
        activity = {'id': '1', 'courseware_id': '2', 'type': 2}
        question = {'ProblemID': '3', 'Type': '1', 'Body': '选择正确图像', 'Answer': ['A'],
                    'Options': [{'Key': 'A', 'Value': '<img src="https://a.yuketang.cn/a.png?token=secret">'}]}
        cards = parsing.parse_cards(activity, '1', {'errcode': 0, 'data': {'Slides': [{'SlideID': '3', 'Problem': question}]}})
        self.assertEqual(cards[0]['problem_type'], 'single')
        self.assertTrue(cards[0]['needs_vision'])
        self.assertEqual(cards[0]['images'][0]['source'], 'https://a.yuketang.cn/a.png')
        quiz = parsing.parse_quiz(activity, '1', {'errcode': 0, 'data': {'objective_result_list': [{'problem_id': '3', 'answer': ['A']}]}},
                                  {'3': {'errcode': 0, 'data': question}})
        self.assertTrue(quiz[0]['needs_vision'])
        self.assertEqual(len(quiz[0]['images']), 1)


class WorkerLifecycleTests(unittest.TestCase):
    def test_shutdown_publishes_idle_before_clearing_worker(self):
        from qwen_asr_app.model_runtime import ModelManager
        observed = []

        class ObservedManager(ModelManager):
            def __setattr__(self, name, value):
                if name == 'worker' and value is None and getattr(self, 'observe', False):
                    observed.append((self.status, self.request_queue, self.result_queue))
                super().__setattr__(name, value)

        manager = ObservedManager(Path('/unused'))
        manager.worker = SimpleNamespace(is_alive=lambda: False)
        manager.status = 'ready'
        manager.observe = True
        manager._stop_worker_sync()
        self.assertEqual(observed, [('idle', None, None)])


if __name__ == '__main__':
    unittest.main()
