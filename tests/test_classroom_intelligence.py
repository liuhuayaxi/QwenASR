try:
    from tests import isolation  # noqa: F401
except ImportError:
    import isolation  # noqa: F401

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from qwen_asr_app.ai_preferences import AIPreferences
from qwen_asr_app.ai_secrets import AISecretStore
from qwen_asr_app.classroom_chat import (
    DEFAULT_PROVIDER,
    build_question,
    chat_settings,
    classroom_chat_router,
)
from qwen_asr_app.classroom_intelligence import (
    DEFAULT_INTELLIGENCE_PROVIDER,
    ClassroomIntelligenceService,
    ClassroomNoteStore,
    classroom_intelligence_router,
    configure_intelligence,
    get_intelligence_settings,
    on_stable_transcript,
    save_intelligence_settings,
    verify_claims,
)
from qwen_asr_app.classroom_intelligence.export import export_notes_markdown
from qwen_asr_app.classroom_intelligence.store import read_notes_doc, save_notes_doc


class ClassroomIntelligenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

        self.preferences = AIPreferences(self.root / 'settings.json')
        self.secrets = AISecretStore(self.root / 'keys.json')

        self.record = SimpleNamespace(
            id='lecture-intel-001',
            directory=self.root / 'lecture-intel-001',
            duration=300,
            metadata={
                'title': '光通信原理',
                'category': '专业必修课',
                'started_at': '2026-10-14 10:00:00',
                'course_settings': {'asr_prompt': '全反射与纤芯折射率', 'terminology': ['全反射', '纤芯']},
                'ai_reviews': [
                    {'segment_ids': [1], 'corrected': '我们上一节讲了折射，今天讲全反射。老师说全反射临界角是 45 度。'}
                ],
                'ai_summaries': [{'summary': '讲授全反射原理与临界角'}],
            },
            segments=[
                {'id': 1, 'start': 0, 'end': 60, 'raw': '折射与全反射，临界角是 45 度'},
                {'id': 2, 'start': 60, 'end': 120, 'raw': '纤芯折射率 n1 大于包层折射率 n2，这是光纤传光基本条件'},
                {'id': 3, 'start': 240, 'end': 300, 'raw': '最后我们来看数值孔径 NA 的定义公式'},
            ],
        )
        self.record.directory.mkdir(parents=True, exist_ok=True)

        self.empty_record = SimpleNamespace(
            id='lecture-intel-empty',
            directory=self.root / 'lecture-intel-empty',
            duration=0,
            metadata={'title': '空课堂', 'category': '专业必修课'},
            segments=[],
        )
        self.empty_record.directory.mkdir(parents=True, exist_ok=True)

        self.store = SimpleNamespace(
            find=lambda ident: {
                self.record.id: self.record,
                self.empty_record.id: self.empty_record,
            }.get(ident),
            list_records=lambda: [self.record, self.empty_record],
            list_recordings=lambda: [self.record, self.empty_record],
        )

        self.knowledge = SimpleNamespace(
            search=lambda category, query, limit=6, lesson_id=None, **kwargs: [
                {
                    'citation_id': 'K1',
                    'source_id': 'textbook-optics-01',
                    'title': '现代光纤通信技术',
                    'heading': '全反射临界条件',
                    'page': 48,
                    'text': '光纤通信中，石英玻璃光纤的实际临界角通常约为 48.6 度，数值孔径 NA 为 0.2。',
                    'revision': 'rev-hash-k1',
                    'stale': False,
                }
            ],
            validate_evidence=lambda ev: copy.deepcopy(ev),
        )

        self.live_sessions = {
            self.record.id: SimpleNamespace(
                recording=self.record,
                chat_previews={
                    4: {'id': 4, 'start': 300, 'end': 320, 'text': '滚动预览文本（未定稿）', 'source': 'unfinalized_preview'}
                },
            )
        }

        # Build app with both intelligence and chat routers
        app = FastAPI()
        app.include_router(
            classroom_intelligence_router(
                self.store,
                self.knowledge,
                self.preferences,
                self.secrets,
                self.live_sessions,
            )
        )
        app.include_router(
            classroom_chat_router(
                self.store,
                self.preferences,
                self.secrets,
                self.live_sessions,
                knowledge=self.knowledge,
            )
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)

    # 1. Raw scribble immediate atomic save, time range, anchors, and disconnect protection
    async def test_scribble_creation_immediate_atomic_save_and_anchors(self):
        # Anchor: current
        res_curr = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '老师刚讲到数值孔径公式', 'anchor': 'current'},
        )
        self.assertEqual(res_curr.status_code, 200, res_curr.text)
        note_curr = res_curr.json()['note']
        self.assertEqual(note_curr['raw'], '老师刚讲到数值孔径公式')
        self.assertEqual(note_curr['content'], '老师刚讲到数值孔径公式')
        self.assertEqual(note_curr['revision'], 1)
        self.assertFalse(note_curr['locked'])
        self.assertEqual(note_curr['status'], 'idle')
        # Time range should match latest stable block (start 240, end 300)
        self.assertAlmostEqual(note_curr['start'], 240.0)
        self.assertAlmostEqual(note_curr['end'], 300.0)

        # Immediate atomic save: verify file exists on disk
        notes_file = self.record.directory / 'classroom-notes.json'
        self.assertTrue(notes_file.exists())
        self.assertEqual(notes_file.stat().st_mode & 0o777, 0o600)
        saved = json.loads(notes_file.read_text(encoding='utf-8'))
        self.assertEqual(len(saved['notes']), 1)
        self.assertEqual(saved['notes'][0]['id'], note_curr['id'])

        # Anchor: previous
        res_prev = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '刚才说的纤芯与包层折射率关系', 'anchor': 'previous'},
        )
        self.assertEqual(res_prev.status_code, 200)
        note_prev = res_prev.json()['note']
        # The second-to-last block is id=2 (start 60, end 120)
        self.assertAlmostEqual(note_prev['start'], 60.0)
        self.assertAlmostEqual(note_prev['end'], 120.0)

        # Anchor: manual with explicit range
        res_man = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '课后补充速记', 'anchor': 'manual', 'start': 10.0, 'end': 50.0},
        )
        self.assertEqual(res_man.status_code, 200)
        note_man = res_man.json()['note']
        self.assertAlmostEqual(note_man['start'], 10.0)
        self.assertAlmostEqual(note_man['end'], 50.0)

        # GET scribbles lists all 3 notes with context
        res_list = await self.client.get(f'/api/recordings/{self.record.id}/scribbles')
        self.assertEqual(res_list.status_code, 200)
        doc = res_list.json()
        self.assertEqual(len(doc['notes']), 3)
        self.assertEqual(doc['context']['recording_id'], self.record.id)
        self.assertEqual(doc['context']['note_count'], 3)

    # 2. Regression: Stale manual edits & optimistic concurrency (suggestion only, never overwrites content)
    async def test_stale_manual_edits_optimistic_concurrency(self):
        res = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '学生课堂草稿内容', 'anchor': 'current'},
        )
        note = res.json()['note']
        note_id = note['id']

        ai_response = json.dumps({
            'enhanced_content': '整理后的笔记：纤芯折射率大于包层折射率。',
            'claims': {
                'classroom': [
                    {'statement': '纤芯折射率大于包层折射率', 'citation_id': 'C1', 'quote': '纤芯折射率 n1 大于包层折射率 n2'}
                ],
                'material': [],
                'ai_explanation': [],
                'check': {},
            },
        }, ensure_ascii=False)

        # User starts enhancement (reading input_revision = 1)
        # While AI is thinking, user manually edits the note before AI finishes
        async def slow_ai(**kwargs):
            patch_res = await self.client.patch(
                f'/api/scribbles/{note_id}',
                json={'expected_revision': 1, 'content': '用户在课上手动补充修正后的笔记'},
            )
            self.assertEqual(patch_res.status_code, 200)
            self.assertEqual(patch_res.json()['note']['revision'], 2)
            return ai_response

        service = ClassroomIntelligenceService(self.store, self.knowledge, self.preferences, self.secrets, self.live_sessions)
        with patch('qwen_asr_app.classroom_intelligence.service.request_ai_text', slow_ai):
            await service._run_enhancement(self.record, note_id, force=True)

        # Check note status: content MUST NOT be overwritten! It stays suggestion only.
        store = ClassroomNoteStore(self.record)
        updated_note = await store.get_note(note_id)
        self.assertEqual(updated_note['content'], '用户在课上手动补充修正后的笔记')
        self.assertEqual(updated_note['revision'], 2)
        self.assertIsNotNone(updated_note['suggestion'])
        self.assertTrue(updated_note['suggestion']['stale'])
        self.assertEqual(updated_note['suggestion']['input_revision'], 1)

        # Attempting to accept a stale suggestion must be rejected to protect user edits
        accept_res = await self.client.post(
            f'/api/scribbles/{note_id}/action',
            json={'action': 'accept', 'expected_revision': 2, 'suggestion_id': updated_note['suggestion']['id']},
        )
        self.assertEqual(accept_res.status_code, 409)
        self.assertIn('被手动修改', accept_res.text)

    # 3. Undo does not destroy raw
    async def test_undo_does_not_destroy_raw(self):
        res = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '原始速记原话', 'anchor': 'manual', 'start': 0, 'end': 30},
        )
        note_id = res.json()['note']['id']

        # Edit 1
        await self.client.patch(
            f'/api/scribbles/{note_id}',
            json={'expected_revision': 1, 'content': '第一次手动整理'},
        )
        # Edit 2
        await self.client.patch(
            f'/api/scribbles/{note_id}',
            json={'expected_revision': 2, 'content': '第二次手动整理'},
        )

        store = ClassroomNoteStore(self.record)
        current = await store.get_note(note_id)
        self.assertEqual(current['content'], '第二次手动整理')
        self.assertEqual(current['revision'], 3)
        self.assertEqual(current['raw'], '原始速记原话')

        # Action: undo
        undo_res = await self.client.post(
            f'/api/scribbles/{note_id}/action',
            json={'action': 'undo'},
        )
        self.assertEqual(undo_res.status_code, 200)
        note_after_undo = undo_res.json()['note']
        self.assertEqual(note_after_undo['content'], '第一次手动整理')
        self.assertEqual(note_after_undo['raw'], '原始速记原话')  # RAW PRESERVED!

        # Second undo restores initial content
        await self.client.post(f'/api/scribbles/{note_id}/action', json={'action': 'undo'})
        note_after_undo2 = await store.get_note(note_id)
        self.assertEqual(note_after_undo2['content'], '原始速记原话')
        self.assertEqual(note_after_undo2['raw'], '原始速记原话')

    # 4. Regression: Unsupported entity & preview exclusion (rejected invalid facts not presented as verified)
    def test_unsupported_entity_and_preview_exclusion(self):
        classroom_evidence = [
            {'citation_id': 'C1', 'type': 'classroom', 'time': '00:00:00–00:01:00',
             'text': '老师说全反射临界角是 45 度', 'source': 'refined'},
            {'citation_id': 'C2_PREVIEW', 'type': 'classroom', 'time': '00:05:00–00:05:20',
             'text': '预览文本未定稿', 'source': 'unfinalized_preview'},  # Preview excluded!
        ]
        material_evidence = [
            {'citation_id': 'K1', 'type': 'material', 'title': '光纤通信',
             'text': '光纤通信中，数值孔径 NA 为 0.2。', 'page': 48, 'stale': False},
            {'citation_id': 'K2_STALE', 'type': 'material', 'title': '旧教材',
             'text': '过时的旧内容', 'page': 10, 'stale': True},  # Stale material!
        ]

        # Case A: Unsupported citation ID
        res_unsupported_id = verify_claims(
            {
                'enhanced_content': '测试',
                'claims': {
                    'classroom': [{'statement': '编造的课堂事实', 'citation_id': 'C99_UNKNOWN', 'quote': '编造'}],
                },
            },
            classroom_evidence,
            material_evidence,
        )
        self.assertEqual(len(res_unsupported_id['validation']['verified_claims']), 0)
        self.assertEqual(len(res_unsupported_id['validation']['rejected_claims']), 1)
        self.assertIn('不在允许的课堂转写范围内', res_unsupported_id['validation']['rejected_claims'][0]['reason'])

        # Case B: Preview cited as classroom evidence -> rejected!
        res_preview = verify_claims(
            {
                'enhanced_content': '测试',
                'claims': {
                    'classroom': [{'statement': '预览事实', 'citation_id': 'C2_PREVIEW', 'quote': '预览文本未定稿'}],
                },
            },
            classroom_evidence,
            material_evidence,
        )
        self.assertEqual(len(res_preview['validation']['verified_claims']), 0)
        self.assertEqual(len(res_preview['validation']['rejected_claims']), 1)
        self.assertIn('预览不可作为已核实课堂依据', res_preview['validation']['rejected_claims'][0]['reason'])

        # Case C: Stale material cited -> rejected!
        res_stale = verify_claims(
            {
                'enhanced_content': '测试',
                'claims': {
                    'material': [{'statement': '旧内容', 'citation_id': 'K2_STALE', 'quote': '过时的旧内容'}],
                },
            },
            classroom_evidence,
            material_evidence,
        )
        self.assertEqual(len(res_stale['validation']['rejected_claims']), 1)
        self.assertIn('已过期失效', res_stale['validation']['rejected_claims'][0]['reason'])

        # Case D: Quote not contained in evidence text
        res_not_contained = verify_claims(
            {
                'enhanced_content': '测试',
                'claims': {
                    'classroom': [{'statement': '全反射', 'citation_id': 'C1', 'quote': '老师完全没讲过的捏造句子'}],
                },
            },
            classroom_evidence,
            material_evidence,
        )
        self.assertEqual(len(res_not_contained['validation']['rejected_claims']), 1)
        self.assertIn('未在课堂转写原文中精确包含', res_not_contained['validation']['rejected_claims'][0]['reason'])

        # Case E: AI explanation explicitly marked to review
        res_ai = verify_claims(
            {
                'enhanced_content': '测试',
                'claims': {
                    'ai_explanation': [{'statement': '推导结论：在折射率差较小时可作近似处理'}],
                },
            },
            classroom_evidence,
            material_evidence,
        )
        self.assertEqual(len(res_ai['validation']['verified_claims']), 0)
        self.assertEqual(len(res_ai['validation']['needs_review']), 1)
        self.assertTrue(res_ai['validation']['needs_review'][0]['needs_review'])
        self.assertFalse(res_ai['validation']['needs_review'][0]['verified'])

    # 5. Regression: Current-vs-Textbook numbers check
    def test_current_vs_textbook_numbers(self):
        classroom_evidence = [
            {'citation_id': 'C1', 'type': 'classroom', 'time': '00:00:00–00:01:00',
             'text': '老师说全反射临界角是 45 度。', 'source': 'refined'},
        ]
        material_evidence = [
            {'citation_id': 'K1', 'type': 'material', 'title': '光纤通信',
             'text': '教材指出，实际光纤全反射临界角为 48.6 度。', 'page': 48, 'stale': False},
        ]

        # Model claims classroom quote has 48.6° (mixing up textbook number into classroom claim)
        invalid_json = {
            'enhanced_content': '课堂笔记',
            'claims': {
                'classroom': [
                    {'statement': '老师说临界角是 48.6 度', 'citation_id': 'C1', 'quote': '老师说全反射临界角是 45 度'}
                ],
            },
        }
        res_invalid = verify_claims(invalid_json, classroom_evidence, material_evidence)
        # Should be rejected by numeric check!
        self.assertEqual(len(res_invalid['validation']['rejected_claims']), 1)
        self.assertIn('数值核查失败', res_invalid['validation']['rejected_claims'][0]['reason'])
        self.assertIn('48.6', res_invalid['validation']['rejected_claims'][0]['reason'])

        # Correct claim: cites classroom with 45° and material with 48.6°
        valid_json = {
            'enhanced_content': '课堂笔记：老师课堂采用理想角 45°，教材指出实际角为 48.6°。',
            'claims': {
                'classroom': [
                    {'statement': '老师在课堂上讲解的临界角为 45 度', 'citation_id': 'C1', 'quote': '老师说全反射临界角是 45 度'}
                ],
                'material': [
                    {'statement': '教材指出实际光纤临界角为 48.6 度', 'citation_id': 'K1', 'quote': '实际光纤全反射临界角为 48.6 度'}
                ],
            },
        }
        res_valid = verify_claims(valid_json, classroom_evidence, material_evidence)
        self.assertEqual(len(res_valid['validation']['rejected_claims']), 0)
        self.assertEqual(len(res_valid['validation']['verified_claims']), 2)

    # 6. Empty evidence gracefully retains note and requests clarification
    async def test_empty_evidence_gracefully_retains_note_and_requests_clarification(self):
        # empty_record has no transcript segments and mock search returns empty when category is unknown
        no_k_service = SimpleNamespace(
            search=lambda category, query, limit=6, lesson_id=None: [],
            validate_evidence=lambda ev: ev,
        )
        service = ClassroomIntelligenceService(
            self.store,
            no_k_service,
            self.preferences,
            self.secrets,
            {},
        )

        res = await self.client.post(
            f'/api/recordings/{self.empty_record.id}/scribbles',
            json={'text': '完全无转写和资料的速记内容', 'anchor': 'manual', 'start': 0, 'end': 0},
        )
        note_id = res.json()['note']['id']

        # Run enhancement with no evidence available
        await service._run_enhancement(self.empty_record, note_id, force=True)

        store = ClassroomNoteStore(self.empty_record)
        updated = await store.get_note(note_id)
        # Note content retained!
        self.assertEqual(updated['content'], '完全无转写和资料的速记内容')
        self.assertEqual(updated['raw'], '完全无转写和资料的速记内容')
        self.assertEqual(updated['status'], 'clarification_needed')
        self.assertTrue(updated['suggestion']['validation']['clarification_requested'])

    # 7. Lock suppression
    async def test_lock_suppression(self):
        res = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '需要锁定的重要笔记', 'anchor': 'current'},
        )
        note_id = res.json()['note']['id']

        # Lock the note
        lock_res = await self.client.post(
            f'/api/scribbles/{note_id}/action',
            json={'action': 'lock'},
        )
        self.assertEqual(lock_res.status_code, 200)
        self.assertTrue(lock_res.json()['note']['locked'])

        # Attempt to patch content -> 409
        patch_res = await self.client.patch(
            f'/api/scribbles/{note_id}',
            json={'expected_revision': 1, 'content': '尝试篡改'},
        )
        self.assertEqual(patch_res.status_code, 409)

        # Attempt to enhance -> 409
        enhance_res = await self.client.post(f'/api/scribbles/{note_id}/enhance')
        self.assertEqual(enhance_res.status_code, 409)

        # Unlock
        unlock_res = await self.client.post(
            f'/api/scribbles/{note_id}/action',
            json={'action': 'unlock'},
        )
        self.assertEqual(unlock_res.status_code, 200)
        self.assertFalse(unlock_res.json()['note']['locked'])

    # 8. Regression: Reload retry after service restart interruption
    async def test_reload_retry(self):
        # Simulate an in-flight pending note before crash
        notes_doc = {
            'version': 1,
            'recording_id': self.record.id,
            'notes': [
                {
                    'id': 'note-crash-001',
                    'raw': '崩溃前的速记',
                    'content': '崩溃前的速记',
                    'revision': 1,
                    'start': 0.0,
                    'end': 60.0,
                    'anchor': 'current',
                    'locked': False,
                    'status': 'pending',  # Was pending when process died
                    'created_at': '2026-10-14T10:00:00+08:00',
                    'updated_at': '2026-10-14T10:00:00+08:00',
                    'suggestion': None,
                    'evidence': [],
                    'history': [],
                }
            ],
        }
        save_notes_doc(self.record, notes_doc)

        # After reload, service.start() recovers pending notes across process restart
        service = ClassroomIntelligenceService(self.store, self.knowledge, self.preferences, self.secrets, self.live_sessions)
        await service.start()

        store = ClassroomNoteStore(self.record)
        notes, _ = await store.list_notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]['status'], 'pending')
        self.assertIn('中断，可以重试', notes[0]['error'])
        self.assertEqual(notes[0]['raw'], '崩溃前的速记')

        # Retry via regenerate action
        ai_result = json.dumps({
            'enhanced_content': '重试生成的整理笔记',
            'claims': {
                'classroom': [{'statement': '全反射', 'citation_id': 'C1', 'quote': '老师说全反射临界角是 45 度'}],
                'material': [],
                'ai_explanation': [],
                'check': {},
            },
        }, ensure_ascii=False)

        with patch('qwen_asr_app.classroom_intelligence.service.request_ai_text', AsyncMock(return_value=ai_result)):
            regen_res = await self.client.post(
                f'/api/scribbles/{notes[0]["id"]}/action',
                json={'action': 'regenerate'},
            )
            self.assertEqual(regen_res.status_code, 200)
            # Give background task a moment to finish
            await asyncio.sleep(0.05)

        refreshed = await store.get_note(notes[0]['id'])
        self.assertEqual(refreshed['status'], 'enhanced')
        self.assertIn('全反射', refreshed['suggestion']['content'])
        self.assertIn('老师说全反射临界角是 45 度', refreshed['suggestion']['content'])

    # 9. Independent settings default gemini-3.8-flash-high and PUT validation
    async def test_independent_settings(self):
        settings_file = self.root / 'custom-intel-settings.json'
        with patch('qwen_asr_app.classroom_intelligence.settings.intelligence_settings_path', return_value=settings_file):
            initial = get_intelligence_settings()
            self.assertEqual(initial['provider']['model'], 'gemini-3.8-flash-high')
            self.assertEqual(initial['provider']['transport'], 'agy_cli')

            # GET settings API
            res_get = await self.client.get('/api/intelligence/settings')
            self.assertEqual(res_get.status_code, 200)
            self.assertEqual(res_get.json()['provider']['model'], 'gemini-3.8-flash-high')

            # PUT settings API
            res_put = await self.client.put(
                '/api/intelligence/settings',
                json={'provider': {'model': 'claude-sonnet-4-6', 'cli_model': 'claude-sonnet-4-6'}},
            )
            self.assertEqual(res_put.status_code, 200)
            self.assertEqual(res_put.json()['provider']['model'], 'claude-sonnet-4-6')

            saved = get_intelligence_settings()
            self.assertEqual(saved['provider']['model'], 'claude-sonnet-4-6')

            # Invalid PUT
            res_invalid = await self.client.put(
                '/api/intelligence/settings',
                json={'provider': {'model': 'invalid model with spaces'}},
            )
            self.assertEqual(res_invalid.status_code, 400)

    # 10. Markdown export
    async def test_markdown_export(self):
        # Create a note
        await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '测试导出速记', 'anchor': 'current'},
        )
        export_res = await self.client.get(f'/api/recordings/{self.record.id}/scribbles/export')
        self.assertEqual(export_res.status_code, 200)
        self.assertIn('text/markdown', export_res.headers['content-type'])
        self.assertIn('光通信原理', export_res.text)
        self.assertIn('测试导出速记', export_res.text)

    # 11. Related materials endpoint
    async def test_related_materials_endpoint(self):
        res = await self.client.get(
            f'/api/intelligence/related?session_id={self.record.id}&heading=全反射临界条件'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data['results']), 1)
        self.assertEqual(data['results'][0]['citation_id'], 'K1')
        self.assertIn('last_stable_blocks', data['context'])

    # 12. Regression: Classroom chat scope and ambiguity (missing question, clarification prompt)
    async def test_classroom_chat_scopes_and_ambiguity(self):
        # Missing / invalid question
        res_empty_q = await self.client.post(
            f'/api/recordings/{self.record.id}/chat',
            json={'question': '   ', 'request_id': 'req-chat-err-01'},
        )
        self.assertEqual(res_empty_q.status_code, 400)

        # Scope: recent (only last 180s)
        # Record duration is 300, cutoff is 300 - 180 = 120s.
        # Blocks are at [0-60], [60-120], [240-300]. Block at 240-300 is within last 180s.
        _, payload_recent, info_recent = build_question(
            self.record,
            {'turns': []},
            '刚才那一步是什么',
            chat_settings(self.preferences),
            scope='recent',
            knowledge=self.knowledge,
        )
        self.assertEqual(info_recent['scope'], 'recent')
        self.assertEqual(len(payload_recent['transcript']), 1)
        self.assertEqual(payload_recent['transcript'][0]['text'], '最后我们来看数值孔径 NA 的定义公式')

        # Scope: course on empty recording (course allows no transcript if evidence exists)
        instructions_c, payload_c, info_c = build_question(
            self.empty_record,
            {'turns': []},
            '光纤临界角是多少？',
            chat_settings(self.preferences),
            scope='course',
            knowledge=self.knowledge,
        )
        self.assertEqual(info_c['scope'], 'course')
        self.assertTrue(len(payload_c['evidence']) > 0)
        self.assertIn('严禁猜测题目', instructions_c)

        # If scope is NOT course on empty recording -> raises ValueError
        with self.assertRaisesRegex(ValueError, '还没有可用转写'):
            build_question(
                self.empty_record,
                {'turns': []},
                '光纤临界角是多少？',
                chat_settings(self.preferences),
                scope='lesson',
                knowledge=self.knowledge,
            )

    # 13. Regression: lesson_id filter does NOT hide course materials
    async def test_lesson_id_filter_does_not_hide_course_materials(self):
        recorded_calls = []

        def tracking_search(category, query, limit=6, lesson_id=None):
            recorded_calls.append({'category': category, 'lesson_id': lesson_id})
            # Chunks from course files (textbook) have lesson_id=None
            return [
                {
                    'citation_id': 'K1',
                    'source_id': 'course-pdf-01',
                    'title': '教材-光纤通信',
                    'heading': '折射率分布',
                    'page': 12,
                    'text': '纤芯折射率高于包层折射率是形成全反射传光的必要条件。',
                    'revision': 'rev-k1',
                    'stale': False,
                    'lesson_id': None,
                }
            ]

        custom_k = SimpleNamespace(
            search=tracking_search,
            validate_evidence=lambda ev: ev,
        )
        service = ClassroomIntelligenceService(
            self.store,
            custom_k,
            self.preferences,
            self.secrets,
            self.live_sessions,
        )

        res = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '光纤传光必要条件', 'anchor': 'current'},
        )
        note_id = res.json()['note']['id']

        ai_res = json.dumps({
            'enhanced_content': '无用正文',
            'claims': {
                'classroom': [],
                'material': [
                    {'statement': '纤芯折射率高于包层折射率是形成全反射传光的必要条件', 'citation_id': 'K1',
                     'quote': '纤芯折射率高于包层折射率是形成全反射传光的必要条件'}
                ],
                'ai_explanation': [],
                'check': {},
            },
        }, ensure_ascii=False)

        with patch('qwen_asr_app.classroom_intelligence.service.request_ai_text', AsyncMock(return_value=ai_res)):
            await service._run_enhancement(self.record, note_id, force=True)

        # Verify lesson_id=None was used, not lesson_id=record_id
        self.assertTrue(len(recorded_calls) > 0)
        self.assertIsNone(recorded_calls[0]['lesson_id'])

        store = ClassroomNoteStore(self.record)
        note = await store.get_note(note_id)
        self.assertIsNotNone(note['suggestion'])
        self.assertTrue(len(note['suggestion']['claims']['material']) > 0)
        self.assertEqual(note['suggestion']['claims']['material'][0]['citation_id'], 'K1')

    # 14. Regression: Unchecked enhanced_content hallucinations are blocked & display reconstructed
    def test_unchecked_enhanced_content_hallucinations_blocked_and_reconstructed(self):
        classroom_evidence = [
            {'citation_id': 'C1', 'type': 'classroom', 'time': '00:00:00–00:01:00',
             'text': '老师说全反射临界角是 45 度。', 'source': 'refined'},
        ]
        material_evidence = []

        # Model returns hallucinated raw enhanced_content with fake entities and claims
        hallucinated_response = {
            'enhanced_content': '爱因斯坦在1905年发明了光纤，老师说全反射临界角是 45 度，纤芯折射率小于包层折射率。',
            'claims': {
                'classroom': [
                    {'statement': '老师说全反射临界角是 45 度', 'citation_id': 'C1', 'quote': '老师说全反射临界角是 45 度'}
                ],
                'material': [],
                'ai_explanation': [
                    {'statement': '补充：折射率差异决定数值孔径大小'}
                ],
                'check': {'notes': '数值一致'},
            },
        }

        res = verify_claims(hallucinated_response, classroom_evidence, material_evidence, raw_text='临界角草稿')
        display_content = res['enhanced_content']

        # Verified claim is present in display
        self.assertIn('老师说全反射临界角是 45 度', display_content)
        # Hallucinations from raw enhanced_content MUST NOT be in display
        self.assertNotIn('爱因斯坦', display_content)
        self.assertNotIn('纤芯折射率小于包层折射率', display_content)
        # AI explanation is explicitly tagged
        self.assertIn('AI 知识补充与推导（待学生复核）', display_content)

    # 15. Regression: list_notes does NOT mark currently running work failed during polling
    async def test_list_notes_does_not_fail_pending_work_during_polling(self):
        res = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '正在生成的速记', 'anchor': 'current'},
        )
        note_id = res.json()['note']['id']

        # Mark note as pending (simulating background task actively running)
        store = ClassroomNoteStore(self.record)
        doc = read_notes_doc(self.record)
        target = next(n for n in doc['notes'] if n['id'] == note_id)
        target['status'] = 'pending'
        save_notes_doc(self.record, doc)

        # Client polls GET scribbles multiple times during active generation
        poll_res1 = await self.client.get(f'/api/recordings/{self.record.id}/scribbles')
        self.assertEqual(poll_res1.status_code, 200)
        p_note1 = next(n for n in poll_res1.json()['notes'] if n['id'] == note_id)
        self.assertEqual(p_note1['status'], 'pending')  # MUST STAY PENDING!

        poll_res2 = await self.client.get(f'/api/recordings/{self.record.id}/scribbles')
        self.assertEqual(poll_res2.status_code, 200)
        p_note2 = next(n for n in poll_res2.json()['notes'] if n['id'] == note_id)
        self.assertEqual(p_note2['status'], 'pending')

        # Only on lifecycle start (when no task is in memory) does it recover orphaned pending notes
        service = ClassroomIntelligenceService(self.store, self.knowledge, self.preferences, self.secrets, self.live_sessions)
        await service.start()

        recovered_note = await store.get_note(note_id)
        self.assertEqual(recovered_note['status'], 'pending')
        self.assertIn('中断，可以重试', recovered_note['error'])

    # 16. Regression: Malformed settings raise error and do not silently fallback or overwrite
    async def test_malformed_settings_raise_and_do_not_fallback_or_overwrite(self):
        settings_file = self.root / 'corrupted-intel-settings.json'
        settings_file.write_text('{"provider": "NOT_A_DICT_MALFORMED_DATA}', encoding='utf-8')

        with patch('qwen_asr_app.classroom_intelligence.settings.intelligence_settings_path', return_value=settings_file):
            res = await self.client.get('/api/intelligence/settings')
            self.assertEqual(res.status_code, 500)
            self.assertIn('已保留原配置', res.text)
            # Disk file must NOT have been silently overwritten
            self.assertEqual(settings_file.read_text(encoding='utf-8'), '{"provider": "NOT_A_DICT_MALFORMED_DATA}')

    # 17. Regression: API key is never written to settings JSON on disk
    async def test_api_key_never_stored_on_disk(self):
        settings_file = self.root / 'secure-intel-settings.json'
        with patch('qwen_asr_app.classroom_intelligence.settings.intelligence_settings_path', return_value=settings_file):
            res = await self.client.put(
                '/api/intelligence/settings',
                json={
                    'provider': {
                        'transport': 'api',
                        'model': 'gpt-4o',
                        'base_url': 'https://api.openai.com/v1',
                        'api_key': 'secret-super-key-999',
                    }
                },
            )
            self.assertEqual(res.status_code, 200)
            # Response must not contain the api_key
            self.assertNotIn('secret-super-key-999', res.text)

            # File on disk must NOT contain the api_key
            disk_content = settings_file.read_text(encoding='utf-8')
            self.assertNotIn('secret-super-key-999', disk_content)
            self.assertNotIn('api_key', disk_content)

            # Secret store must have it bound to chat mode
            from qwen_asr_app.classroom_intelligence.settings import intelligence_secret_store
        self.assertEqual(intelligence_secret_store.get('chat', 'https://api.openai.com/v1'), 'secret-super-key-999')
        self.assertEqual(self.secrets.get('chat', 'https://api.openai.com/v1'), '')

    # 18. Live transcript event debounces enhancement for unlocked recent notes
    async def test_on_transcript_debounces_enhancement(self):
        res = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '当前课堂速记进行中', 'anchor': 'current'},
        )
        note_id = res.json()['note']['id']

        service = ClassroomIntelligenceService(self.store, self.knowledge, self.preferences, self.secrets, self.live_sessions)
        await service.on_transcript(self.record)

        # Debounce task should be scheduled
        self.assertIn(note_id, service._debounce_tasks)
        await service.close()

    # 19. Router exposes intelligence_service with lifecycle start and close
    def test_router_exposes_intelligence_service_with_lifecycle(self):
        router = classroom_intelligence_router(self.store, self.knowledge, self.preferences, self.secrets, self.live_sessions)
        self.assertTrue(hasattr(router, 'intelligence_service'))
        service = router.intelligence_service
        self.assertTrue(callable(getattr(service, 'start', None)))
        self.assertTrue(callable(getattr(service, 'close', None)))

    # 20. Scoped intelligence related alias /api/recordings/{session_id}/intelligence/related
    async def test_scoped_intelligence_related_alias(self):
        res = await self.client.get(
            f'/api/recordings/{self.record.id}/intelligence/related?heading=全反射临界条件'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(len(data['results']) > 0)
        self.assertEqual(data['results'][0]['citation_id'], 'K1')

    # 21. Source freshness check before acceptance
    async def test_source_freshness_check_before_accept(self):
        res = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '待接受建议的速记', 'anchor': 'manual', 'start': 0.0, 'end': 60.0},
        )
        note_id = res.json()['note']['id']

        ai_res = json.dumps({
            'enhanced_content': '整理笔记',
            'claims': {
                'classroom': [
                    {'statement': '临界角 45 度', 'citation_id': 'C1', 'quote': '老师说全反射临界角是 45 度。'}
                ],
                'material': [
                    {'statement': '教材临界角 48.6 度', 'citation_id': 'K1', 'quote': '实际临界角通常约为 48.6 度'}
                ],
                'ai_explanation': [],
                'check': {},
            },
        }, ensure_ascii=False)

        service = ClassroomIntelligenceService(self.store, self.knowledge, self.preferences, self.secrets, self.live_sessions)
        with patch('qwen_asr_app.classroom_intelligence.service.request_ai_text', AsyncMock(return_value=ai_res)):
            await service._run_enhancement(self.record, note_id, force=True)

        store = ClassroomNoteStore(self.record)
        note = await store.get_note(note_id)
        suggestion_id = note['suggestion']['id']

        # Case A: Material evidence became stale after generation
        stale_knowledge = SimpleNamespace(
            validate_evidence=lambda ev: [{'citation_id': 'K1', 'stale': True}],
        )
        with self.assertRaisesRegex(ValueError, '资料版本已发生变化'):
            await store.apply_action(note_id, 'accept', expected_revision=1, suggestion_id=suggestion_id, knowledge=stale_knowledge)

        # Case B: Classroom transcript changed after generation
        modified_record = copy.deepcopy(self.record)
        modified_record.metadata['ai_reviews'][0]['corrected'] = '修正后的新文本，原临界角论述已删除。'
        store_modified = ClassroomNoteStore(modified_record)
        with self.assertRaisesRegex(ValueError, '课堂转写在生成后已发生变更'):
            await store_modified.apply_action(note_id, 'accept', expected_revision=1, suggestion_id=suggestion_id, knowledge=self.knowledge)

    # 22. Production RecordingStore uses list_records() exclusively
    async def test_start_with_actual_list_records(self):
        prod_store = SimpleNamespace(
            list_records=lambda: [self.record],
            find=lambda ident: self.record if ident == self.record.id else None,
        )
        self.assertFalse(hasattr(prod_store, 'list_recordings'))

        notes_doc = {
            'version': 1,
            'recording_id': self.record.id,
            'notes': [
                {
                    'id': 'note-rec-prod-01',
                    'raw': '待恢复生产笔记',
                    'content': '待恢复生产笔记',
                    'revision': 1,
                    'start': 0.0,
                    'end': 60.0,
                    'anchor': 'current',
                    'locked': False,
                    'status': 'pending',
                    'created_at': '2026-10-14T10:00:00+08:00',
                    'updated_at': '2026-10-14T10:00:00+08:00',
                    'suggestion': None,
                    'evidence': [],
                    'history': [],
                }
            ],
        }
        save_notes_doc(self.record, notes_doc)

        service = ClassroomIntelligenceService(prod_store, self.knowledge, self.preferences, self.secrets, self.live_sessions)
        await service.start()

        store = ClassroomNoteStore(self.record)
        notes, _ = await store.list_notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]['status'], 'pending')
        self.assertIn('中断，可以重试', notes[0]['error'])
        self.assertTrue(notes[0]['job']['retryable'])
        await service.close()

    # 23. Cancelled debounce awaits and does not leak or execute after cancellation
    async def test_cancelled_debounce(self):
        res = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '防抖测试笔记', 'anchor': 'current'},
        )
        note_id = res.json()['note']['id']
        service = ClassroomIntelligenceService(self.store, self.knowledge, self.preferences, self.secrets, self.live_sessions)

        # Enqueue debounce 1 with 10s delay
        task1 = await service.enqueue_enhancement(self.record, note_id, debounce_seconds=10.0)
        self.assertIn(note_id, service._debounce_tasks)

        # Enqueue debounce 2: task1 must be cancelled
        task2 = await service.enqueue_enhancement(self.record, note_id, debounce_seconds=5.0)
        self.assertTrue(task1.cancelled() or task1.done())
        self.assertIs(service._debounce_tasks[note_id], task2)

        # Explicit cancel_task cancels debounce task and awaits it
        cancelled = await service.cancel_task(note_id)
        self.assertTrue(cancelled)
        self.assertTrue(task2.cancelled() or task2.done())
        self.assertNotIn(note_id, service._debounce_tasks)
        await service.close()

    # 24. Anchor patch invalidates in-flight or late suggestion
    async def test_anchor_patch_invalidates_late_result(self):
        res = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '测试锚点修改', 'anchor': 'current'},
        )
        note_id = res.json()['note']['id']

        ai_response = json.dumps({
            'enhanced_content': 'AI增强结果',
            'claims': {
                'classroom': [{'statement': '临界角 45 度', 'citation_id': 'C1', 'quote': '老师说全反射临界角是 45 度'}],
                'material': [],
                'ai_explanation': [],
                'check': {},
            },
        }, ensure_ascii=False)

        # While AI is running, user patches anchor and timestamps
        async def slow_ai(**kwargs):
            patch_res = await self.client.patch(
                f'/api/scribbles/{note_id}',
                json={'expected_revision': 1, 'anchor': 'manual', 'start': 0.0, 'end': 30.0},
            )
            self.assertEqual(patch_res.status_code, 200)
            self.assertEqual(patch_res.json()['note']['revision'], 2)
            self.assertEqual(patch_res.json()['note']['anchor'], 'manual')
            return ai_response

        service = ClassroomIntelligenceService(self.store, self.knowledge, self.preferences, self.secrets, self.live_sessions)
        with patch('qwen_asr_app.classroom_intelligence.service.request_ai_text', slow_ai):
            await service._run_enhancement(self.record, note_id, force=True)

        store = ClassroomNoteStore(self.record)
        note = await store.get_note(note_id)
        # Late result should be marked stale!
        self.assertTrue(note['suggestion']['stale'])
        self.assertEqual(note['suggestion']['input_revision'], 1)
        self.assertEqual(note['revision'], 2)

        # Trying to accept stale suggestion raises 409
        accept_res = await self.client.post(
            f'/api/scribbles/{note_id}/action',
            json={'action': 'accept', 'expected_revision': 2, 'suggestion_id': note['suggestion']['id']},
        )
        self.assertEqual(accept_res.status_code, 409)
        self.assertIn('该建议已过期', accept_res.text)

    # 25. Preserved manual edits plus latest source timestamp & chunk metadata
    async def test_preserved_manual_edits_plus_latest_source_timestamp(self):
        res = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '原始课堂速记不可破坏', 'anchor': 'current'},
        )
        note_id = res.json()['note']['id']

        # Edit 1
        p1 = await self.client.patch(
            f'/api/scribbles/{note_id}',
            json={'expected_revision': 1, 'content': '手动修改第一版'},
        )
        self.assertEqual(p1.status_code, 200)
        # Edit 2
        p2 = await self.client.patch(
            f'/api/scribbles/{note_id}',
            json={'expected_revision': 2, 'content': '手动修改第二版'},
        )
        self.assertEqual(p2.status_code, 200)

        store = ClassroomNoteStore(self.record)
        n = await store.get_note(note_id)
        self.assertEqual(n['raw'], '原始课堂速记不可破坏')
        self.assertEqual(n['content'], '手动修改第二版')
        self.assertEqual(n['revision'], 3)

        # Run AI enhancement for revision 3
        ai_res = json.dumps({
            'enhanced_content': 'AI增强第二版',
            'claims': {
                'classroom': [{'statement': '老师说全反射临界角是 45 度', 'citation_id': 'C1', 'quote': '老师说全反射临界角是 45 度'}],
                'material': [],
                'ai_explanation': [],
                'check': {},
            },
        }, ensure_ascii=False)

        service = ClassroomIntelligenceService(self.store, self.knowledge, self.preferences, self.secrets, self.live_sessions)
        with patch('qwen_asr_app.classroom_intelligence.service.request_ai_text', AsyncMock(return_value=ai_res)):
            await service._run_enhancement(self.record, note_id, force=True)

        n_enhanced = await store.get_note(note_id)
        self.assertEqual(n_enhanced['content'], '手动修改第二版')  # Suggestion only, does not overwrite!
        self.assertIsNotNone(n_enhanced['suggestion'])

        # Verify classroom source open_url and chunk metadata
        ev = n_enhanced['suggestion']['evidence'][0]
        self.assertTrue(ev['open_url'].startswith(f"/?recording={self.record.id}&start="))
        self.assertNotIn('/index.html?session', ev['open_url'])
        self.assertIn('chunk_id', ev)
        self.assertIn('content_hash', ev)

        # Accept suggestion with correct revision
        accept_res = await self.client.post(
            f'/api/scribbles/{note_id}/action',
            json={'action': 'accept', 'expected_revision': 3, 'suggestion_id': n_enhanced['suggestion']['id']},
        )
        self.assertEqual(accept_res.status_code, 200)
        n_accepted = await store.get_note(note_id)
        self.assertEqual(n_accepted['status'], 'enhanced')
        self.assertEqual(n_accepted['raw'], '原始课堂速记不可破坏')  # Raw still preserved!

    # 26. Free paraphrase not labeled verified, displayed with pending interpretation
    def test_free_paraphrase_not_labeled_verified(self):
        classroom_evidence = [
            {'citation_id': 'C1', 'type': 'classroom', 'time': '00:00:00–00:01:00',
             'text': '老师说全反射临界角是 45 度', 'source': 'refined', 'content_hash': 'h1', 'revision': 'r1',
             'source_id': 's1', 'open_url': '/?recording=rec&start=0'},
        ]
        material_evidence = [
            {'citation_id': 'K1', 'type': 'material', 'title': '光纤通信',
             'text': '光纤通信中，数值孔径 NA 为 0.2。', 'page': 48, 'stale': False},
        ]

        response_with_paraphrase = {
            'enhanced_content': '自由概括笔记',
            'claims': {
                'classroom': [
                    {'statement': '课堂上老师强调了发生全反射现象所要求的最低入射临界角是 45 度', 'citation_id': 'C1', 'quote': '老师说全反射临界角是 45 度'}
                ],
                'material': [
                    {'statement': '教材中指出光纤的聚光能力参数 NA 为 0.2', 'citation_id': 'K1', 'quote': '数值孔径 NA 为 0.2'}
                ],
                'ai_explanation': [
                    {'statement': 'AI 推论：临界角与光纤数值孔径存在三角函数关联'}
                ],
                'check': {'notes': '参数核验完毕'},
            },
        }

        res = verify_claims(response_with_paraphrase, classroom_evidence, material_evidence, raw_text='临界角草稿')
        validation = res['validation']

        # Faithful quotes are verified; free paraphrases are marked interpretation_pending
        self.assertEqual(len(validation['verified_claims']), 2)
        for c in validation['verified_claims']:
            self.assertTrue(c['verified'])
        self.assertEqual(len(validation['interpretation_pending']), 2)
        for p in validation['interpretation_pending']:
            self.assertFalse(p['verified'])
            self.assertEqual(p['status'], 'interpretation_pending')

        # Reconstructed content has both verified points and pending interpretation
        content = res['enhanced_content']
        self.assertIn('课堂讲解要点（已核对课堂转写）', content)
        self.assertIn('语义解读（待复核）', content)
        self.assertIn('AI 知识补充与推导（待学生复核）', content)

        # Missing claims schema raises ValueError
        with self.assertRaisesRegex(ValueError, '缺少 claims 架构'):
            verify_claims({'enhanced_content': '无 claims'}, classroom_evidence, material_evidence)

    # 27. Reject cancels future automatic repeats until new evidence
    async def test_reject_prevents_automatic_repeats(self):
        res = await self.client.post(
            f'/api/recordings/{self.record.id}/scribbles',
            json={'text': '测试拒绝防重试', 'anchor': 'current'},
        )
        note_id = res.json()['note']['id']

        ai_res = json.dumps({
            'enhanced_content': '建议内容',
            'claims': {
                'classroom': [{'statement': '全反射', 'citation_id': 'C1', 'quote': '老师说全反射临界角是 45 度'}],
                'material': [],
                'ai_explanation': [],
                'check': {},
            },
        }, ensure_ascii=False)

        service = ClassroomIntelligenceService(self.store, self.knowledge, self.preferences, self.secrets, self.live_sessions)
        with patch('qwen_asr_app.classroom_intelligence.service.request_ai_text', AsyncMock(return_value=ai_res)):
            await service._run_enhancement(self.record, note_id, force=True)

        store = ClassroomNoteStore(self.record)
        note = await store.get_note(note_id)
        suggestion_id = note['suggestion']['id']

        # User rejects suggestion
        reject_res = await self.client.post(
            f'/api/scribbles/{note_id}/action',
            json={'action': 'reject', 'expected_revision': 1, 'suggestion_id': suggestion_id},
        )
        self.assertEqual(reject_res.status_code, 200)
        note_after_reject = await store.get_note(note_id)
        self.assertIsNone(note_after_reject['suggestion'])
        self.assertIsNotNone(note_after_reject.get('rejected_fingerprint'))

        # Live transcript triggers on_transcript with same evidence: enhancement must be suppressed!
        call_count = 0
        def count_call(**kwargs):
            nonlocal call_count
            call_count += 1
            return ai_res

        with patch('qwen_asr_app.classroom_intelligence.service.request_ai_text', count_call):
            await service._run_enhancement(self.record, note_id, force=False)

        self.assertEqual(call_count, 0)  # Suppressed!
        note_still_idle = await store.get_note(note_id)
        self.assertIsNone(note_still_idle['suggestion'])

    # 28. Root hook configure_intelligence and on_stable_transcript
    async def test_configure_intelligence_and_on_stable_transcript_hook(self):
        service = ClassroomIntelligenceService(self.store, self.knowledge, self.preferences, self.secrets, self.live_sessions)
        configure_intelligence(service)

        # Call module-level hook on_stable_transcript
        await on_stable_transcript(self.record)
        await service.close()


if __name__ == '__main__':
    unittest.main()
