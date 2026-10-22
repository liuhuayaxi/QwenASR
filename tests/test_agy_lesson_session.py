from tests import isolation  # noqa: F401

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qwen_asr_app import agy_cli
from qwen_asr_app.ai import AIRequestError
from qwen_asr_app.agy_lesson_session import LessonSession, current
from qwen_asr_app.agy_session import last_turn, SessionEvidenceError, read_session

BIG = 'transcript ' * 400
SID = '1a62490d-9818-4545-a84c-f4ecfcf6363a'


class LessonSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def test_identical_fields_are_sent_once_and_state_persists(self):
        session = LessonSession(self.folder)
        self.assertEqual(session.plan('写笔记', {'transcript': BIG}), ('写笔记', {'transcript': BIG}, ''))
        session.record(session_id=SID, prompt='p1', instructions='写笔记', payload={'transcript': BIG, 'batch': 1},
                       response='ok', continued=False)
        text, shaped, conversation = session.plan('排版', {'transcript': BIG, 'batch': 2})
        self.assertEqual(conversation, SID)
        self.assertIn('第 1 轮 INPUT.transcript 完全相同', shaped['transcript'])
        self.assertEqual(shaped['batch'], 2)
        again = LessonSession(self.folder)
        self.assertEqual((again.session_id, again.prompts), (SID, ['p1']))

    def test_budget_restarts_with_full_payload(self):
        session = LessonSession(self.folder, budget_tokens=1000)
        session.record(session_id=SID, prompt='p1', instructions='i', payload={'transcript': BIG}, response='r', continued=False)
        self.assertEqual(session.plan('i', {'transcript': BIG})[2], '')
        self.assertEqual(session.session_id, '')


class LastTurnTests(unittest.TestCase):
    def entries(self, *requests):
        rows = []
        for text in requests:
            rows.append({'type': 'USER_INPUT', 'status': 'DONE', 'content': f'<USER_REQUEST>\n{text}\n</USER_REQUEST>'})
            rows.append({'type': 'PLANNER_RESPONSE', 'status': 'DONE', 'content': 'answer ' + text})
        return rows

    def test_only_final_turn_of_our_own_conversation_is_used(self):
        rows = self.entries('p1', 'p2')
        self.assertEqual(last_turn(rows, ['p1']), rows[2:])
        with self.assertRaises(SessionEvidenceError):
            last_turn(rows, ['other'])
        with self.assertRaises(SessionEvidenceError):
            last_turn(rows, [])  if False else last_turn(rows, ['p1', 'p2'])

    def test_read_session_verifies_continued_turn(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / SID / '.system_generated/logs'
            path.mkdir(parents=True)
            (path / 'transcript_full.jsonl').write_text('\n'.join(json.dumps(e) for e in self.entries('p1', 'p2')))
            self.assertEqual(read_session(SID, 'p2', root=Path(raw), prior_prompts=['p1'])['response'], 'answer p2')
            with self.assertRaises(SessionEvidenceError):
                read_session(SID, 'p2', root=Path(raw))  # two turns but no recorded history


class RequestTextSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_continued_turn_then_fallback_to_fresh_on_session_error(self):
        with tempfile.TemporaryDirectory() as raw:
            session = LessonSession(Path(raw))
            calls = []

            async def once(instructions, payload, provider, conversation='', prior_prompts=()):
                calls.append((conversation, list(prior_prompts), payload))
                if conversation and len(calls) == 3:
                    raise AIRequestError('mismatch', kind='agy_cli_error', retriable=False)
                agy_cli.last_session_id.set(SID if len(calls) < 4 else '2b62490d-9818-4545-a84c-f4ecfcf6363a')
                agy_cli.last_prompt.set(f'prompt{len(calls)}')
                return f'r{len(calls)}'

            token = current.set(session)
            try:
                with patch.object(agy_cli, '_request_text_once', side_effect=once):
                    self.assertEqual(await agy_cli.request_text('i', {'transcript': BIG}, {}), 'r1')
                    self.assertEqual(await agy_cli.request_text('j', {'transcript': BIG, 'b': 2}, {}), 'r2')
                    self.assertEqual(calls[1][0], SID)
                    self.assertEqual(calls[1][1], ['prompt1'])
                    self.assertIn('完全相同', calls[1][2]['transcript'])
                    self.assertEqual(await agy_cli.request_text('k', {'transcript': BIG, 'b': 3}, {}), 'r4')
                    self.assertEqual(calls[3][0], '')            # fresh fallback
                    self.assertEqual(calls[3][2]['transcript'], BIG)  # full payload again
                    self.assertEqual(session.stats['fallbacks'], 1)
                    self.assertEqual(session.prompts, ['prompt4'])
            finally:
                current.reset(token)

    async def test_non_session_errors_are_not_retried_fresh(self):
        with tempfile.TemporaryDirectory() as raw:
            session = LessonSession(Path(raw))
            session.record(session_id=SID, prompt='p', instructions='i', payload={}, response='r', continued=False)
            token = current.set(session)

            async def once(*args, **kwargs):
                raise AIRequestError('region', kind='provider_region', retriable=False)
            try:
                with patch.object(agy_cli, '_request_text_once', side_effect=once) as call:
                    with self.assertRaises(AIRequestError):
                        await agy_cli.request_text('i', {}, {})
                    self.assertEqual(call.call_count, 1)
            finally:
                current.reset(token)


if __name__ == '__main__':
    unittest.main()


class DisplayMathTests(unittest.TestCase):
    def test_bare_aligned_is_wrapped_only_outside_math(self):
        from qwen_asr_app.note_latex import normalize_display_math as fix
        self.assertEqual(fix(r'a \begin{aligned}x&=1\end{aligned} b'), r'a \[\begin{aligned}x&=1\end{aligned}\] b')
        for inside in (r'\[\begin{aligned}x\end{aligned}\]', r'$x=\begin{cases}1\end{cases}$',
                       r'\begin{equation}\begin{aligned}x\end{aligned}\end{equation}'):
            self.assertEqual(fix(inside), inside)


class ChunkSizeTests(unittest.TestCase):
    def test_cjk_prompt_uses_few_large_files_within_view_limit(self):
        import tempfile
        from qwen_asr_app.agy_cli import _file_prompt, FILE_INPUT_CHARS
        prompt = '课堂转写内容' * 22000  # 132k CJK characters (~400 KB)
        with tempfile.TemporaryDirectory() as raw:
            _, request = _file_prompt(prompt, Path(raw))
            files = sorted(request.glob('input-*.json'))
            self.assertLessEqual(len(files), 8)
            self.assertTrue(all(len(f.read_text()) <= FILE_INPUT_CHARS + 800 for f in files))

    def test_same_value_under_another_field_name_is_referenced(self):
        with tempfile.TemporaryDirectory() as raw:
            session = LessonSession(Path(raw))
            session.record(session_id=SID, prompt='p1', instructions='i', payload={'pages': [BIG]}, response='r', continued=False)
            _, shaped, _ = session.plan('w', {'source_pages': [BIG]})
            self.assertIn('INPUT.pages 完全相同', shaped['source_pages'])
