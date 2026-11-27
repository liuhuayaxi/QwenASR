"""Regenerating notes re-retrieves courseware and recovers from deleted pins."""
from tests import isolation  # noqa: F401

import asyncio
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import app
from qwen_asr_app import fast_note, review_materials as rm, review_notes as rn, smartclass_service as sc
from qwen_asr_app.course_files import CourseFiles
from qwen_asr_app.storage import RecordingRecord, RecordingStore
from tests.test_live_recovery import store_for


class RegenerateMaterialTests(unittest.IsolatedAsyncioTestCase):
    def make_record(self, root: Path, *, with_note: bool = True, review_material_ids=None):
        rec = RecordingRecord(root / 'rec', {
            'id': 'rec-regen-mat', 'title': '8259A中断响应机制', 'category': '微机原理',
            'status': 'completed', 'started_at': '2026-11-24T13:45:00+08:00',
            'sample_count': 16000,
        })
        if with_note:
            rec.metadata['ai_note_summary'] = {'summary': '# 旧笔记', 'abstract': '旧摘要', 'source': 'post_recording'}
        if review_material_ids is not None:
            rec.metadata['review_material_ids'] = list(review_material_ids)
        rec.segments = [{'id': 1, 'start': 0, 'end': 10, 'raw': '8259A 中断响应机制与中断向量编程。'}]
        rec.save_all()
        return rec

    def make_course_settings(self, root: Path):
        settings = copy.copy(app.app_settings)
        settings.storage_root = root / 'library'
        settings.categories = ['未分类', '微机原理']
        settings.library_layout = ''
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        return settings

    async def test_regenerate_clears_old_material_pin_and_re_retrieves(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            old_id = 'a' * 32
            rec = self.make_record(root, with_note=True, review_material_ids=[old_id])
            store = store_for(rec)
            seen = []

            async def fake_generate(ident, payload):
                current = store.find(ident)
                seen.append(current.metadata.get('review_material_ids'))
                return {'summary': {'summary': '# 新笔记', 'pdf_ready': True}}

            with patch.object(app, 'recording_store', store), \
                 patch.object(app.ai_preferences, 'task_config', return_value={}), \
                 patch.object(app, 'generate_library_summary', side_effect=fake_generate):
                await app.create_summary_job(rec.id, {'config': {}, 'regenerate': True})
                await asyncio.wait_for(app.background_summary_tasks[rec.id], 2)

            self.assertEqual(seen, [None])
            self.assertNotIn('review_material_ids', store.find(rec.id).metadata)

    async def test_selected_files_mode_keeps_valid_pin_and_manual_note_dialog_consumes_once(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = self.make_course_settings(root)
            files = CourseFiles(settings)
            src = root / '第08章.pdf'
            src.write_bytes(b'%PDF-ch8')
            item, _ = files.import_file('微机原理', src, '第08章.pdf', '课件')
            rec = self.make_record(root, with_note=True)
            store = store_for(rec)
            seen = []

            async def fake_generate(ident, payload):
                seen.append((
                    store.find(ident).metadata.get('review_material_ids'),
                    store.find(ident).metadata['summary_job']['material_coverage'],
                ))
                return {'summary': {'summary': '# 新笔记', 'pdf_ready': True}}

            with patch.object(app, 'app_settings', settings), \
                 patch.object(app, 'recording_store', store), \
                 patch.object(app.ai_preferences, 'task_config', return_value={}), \
                 patch.object(app, 'generate_library_summary', side_effect=fake_generate):
                # User manually picks a file in 补选本节课件, then clicks 生成课后复习笔记.
                await app.save_review_material_choices(rec.id, {'material_ids': [item['id']]})
                await app.create_summary_job(rec.id, {
                    'config': {}, 'regenerate': True, 'preserve_pending_materials': True,
                })
                await asyncio.wait_for(app.background_summary_tasks[rec.id], 2)
                # Explicit 完整覆盖所选课件 also retains the manual choice.
                await app.save_review_material_choices(rec.id, {'material_ids': [item['id']]})
                await app.create_summary_job(rec.id, {
                    'config': {}, 'regenerate': True, 'material_coverage': 'selected_files',
                })
                await asyncio.wait_for(app.background_summary_tasks[rec.id], 2)
                # Subsequent normal 重新生成复习笔记 clears the prior pin so retrieval runs fresh.
                await app.create_summary_job(rec.id, {'config': {}, 'regenerate': True})
                await asyncio.wait_for(app.background_summary_tasks[rec.id], 2)

            self.assertEqual(seen, [
                ([item['id']], 'lesson'),
                ([item['id']], 'selected_files'),
                (None, 'lesson'),
            ])

    async def test_task_center_regenerate_resets_selected_files_and_is_advertised(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rec = self.make_record(root, with_note=True, review_material_ids=['b' * 32])
            rec.metadata['summary_job'] = {
                'status': 'failed', 'error': '补选课件不属于本课程、已过期或无法读取',
                'material_coverage': 'selected_files', 'custom_prompt': '',
            }
            rec.save_metadata()
            store = store_for(rec)
            with patch.object(app, 'recording_store', store):
                task = next(t for t in app._build_compact_ai_tasks() if t['type'] == 'summary')
                self.assertIn('regenerate', task['actions'])

            with patch.object(app, 'recording_store', store), \
                 patch.object(app, '_saved_ai_task_config', return_value={}), \
                 patch.object(app.ai_preferences, 'task_config', return_value={}), \
                 patch.object(app, 'generate_library_summary', AsyncMock(return_value={'summary': {'summary': '新', 'pdf_ready': True}})):
                res = await app.ai_task_action({'recording_id': rec.id, 'type': 'summary', 'action': 'regenerate'})
                self.assertTrue(res['accepted'])
                await asyncio.wait_for(app.background_summary_tasks[rec.id], 2)

            saved = store.find(rec.id)
            self.assertNotIn('review_material_ids', saved.metadata)
            self.assertEqual(saved.metadata['summary_job']['material_coverage'], 'lesson')

    async def test_smartclass_preserve_flag_keeps_freshly_extracted_vga(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            vga_id = 'c' * 32
            rec = self.make_record(root, with_note=True, review_material_ids=[vga_id])
            store = store_for(rec)
            seen = []

            async def fake_generate(ident, payload):
                seen.append(store.find(ident).metadata.get('review_material_ids'))
                return {'summary': {'summary': '# 新笔记', 'pdf_ready': True}}

            with patch.object(app, 'recording_store', store), \
                 patch.object(app.ai_preferences, 'task_config', return_value={}), \
                 patch.object(app, 'generate_library_summary', side_effect=fake_generate):
                await app.create_summary_job(rec.id, {'config': {}, 'preserve_review_materials': True})
                await asyncio.wait_for(app.background_summary_tasks[rec.id], 2)

            self.assertEqual(seen, [[vga_id]])

    async def test_deleted_courseware_pin_is_pruned_on_continue_and_fast_note(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = self.make_course_settings(root)
            files = CourseFiles(settings)
            ch8 = root / '第08章_8259A中断.pdf'
            ch8.write_bytes(b'%PDF-ch8')
            item, _ = files.import_file('微机原理', ch8, '第08章_8259A中断.pdf', '课件')
            deleted_id = 'd' * 32
            rec = self.make_record(root, with_note=False, review_material_ids=[deleted_id])

            # GET /review-materials hides deleted selection and returns auto-match (None).
            with patch.object(app, 'app_settings', settings), \
                 patch.object(app, 'recording_store', store_for(rec)):
                choices = await app.review_material_choices(rec.id)
            self.assertIsNone(choices['selected'])
            self.assertEqual([m['id'] for m in choices['materials']], [item['id']])

            seen_selection = []
            def fake_collect(r, folder, selected=None, **kw):
                seen_selection.append(selected)
                pkg = {
                    'category': '微机原理', 'lesson_date': '2026-11-24',
                    'sources': [{'source_id': item['id'], 'name': '第08章_8259A中断.pdf', 'sha256': item['sha256'],
                                 'pages': [{'page': 1, 'text': '8259A 中断响应机制'}]}],
                    'relevant_pages': [{'source_id': item['id'], 'page': 1, 'score': 5.0}],
                    'material_selection': {'files': [{'source_id': item['id'], 'name': '第08章_8259A中断.pdf', 'chosen': True}],
                                           'summary': '按转写检索选用 1/1 份课件'},
                }
                (folder / 'materials.json').write_text(json.dumps(pkg), encoding='utf-8')
                return pkg

            pdf_path = root / 'out.pdf'
            pdf_path.write_bytes(b'%PDF-out')
            with patch('qwen_asr_app.storage.app_settings', settings), \
                 patch('qwen_asr_app.batch_ai._yuketang_questions', AsyncMock(return_value=[])), \
                 patch.object(fast_note, 'collect', side_effect=fake_collect), \
                 patch.object(fast_note.review_scope, 'prepare', AsyncMock(return_value={'enabled': False})), \
                 patch.object(fast_note, 'request_ai_text', AsyncMock(return_value='## 8259A\n\n中断向量。')), \
                 patch('qwen_asr_app.note_pdf.export_pdf', return_value=(pdf_path, {'pdf_sha256': 'pdfsha'})), \
                 patch('qwen_asr_app.review_notes.disabled_review', return_value={'status': 'skipped'}), \
                 patch('qwen_asr_app.chapters.update_automatic_chapters'):
                await fast_note.generate_fast(rec, {})

            self.assertEqual(seen_selection, [None])
            self.assertNotIn('review_material_ids', rec.metadata)

    def test_sanitize_selection_preserves_explicit_empty_list(self):
        items = [{'id': 'a' * 32}, {'id': 'b' * 32, 'status': 'deleted'}]
        self.assertEqual(rm.sanitize_selection([], items), [])
        self.assertIsNone(rm.sanitize_selection(['b' * 32], items))
        self.assertIsNone(rm.sanitize_selection(['c' * 32], items))
        self.assertEqual(rm.sanitize_selection(['a' * 32, 'c' * 32], items), ['a' * 32])

    def test_finder_unhashed_file_is_collected_without_false_change_error(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = self.make_course_settings(root)
            pdf = root / 'finder.pdf'
            pdf.write_bytes(b'%PDF-finder')
            item = {'id': 'e' * 32, 'name': '第08章_课件.pdf', 'sha256': '', 'conversion': {'status': 'none'}}
            files = SimpleNamespace(list=lambda c: [item], file=lambda c, i, v: (item, pdf))
            rec = self.make_record(root, with_note=False)
            with patch.object(rm, 'CourseFiles', return_value=files), \
                 patch.object(rm, 'native_page_cache', return_value=[{'page': 1, 'text': '8259A 中断控制器', 'visual_source': True, 'image_primary': False}]), \
                 patch('qwen_asr_app.knowledge.ingest.ensure_course_id', return_value='cid'), \
                 patch('qwen_asr_app.knowledge.store.KnowledgeStore'), \
                 patch.object(rm, 'transcript_blocks', return_value=[{'start': 0, 'end': 5, 'text': '8259A 中断控制器'}]), \
                 patch('qwen_asr_app.classroom_intelligence.integrations.saved_notes_markdown', return_value=''), \
                 patch('qwen_asr_app.material_scope.lesson_title_for', return_value=''):
                pkg = rm.collect(rec, root / 'out', settings=settings)
            self.assertEqual([s['source_id'] for s in pkg['sources']], ['e' * 32])
            self.assertEqual(pkg['sources'][0]['sha256'], hashlib.sha256(b'%PDF-finder').hexdigest())

    async def test_review_notes_stage_cache_is_isolated_across_regenerations(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = self.make_course_settings(root)
            rec = self.make_record(root, with_note=False)
            pkg = {
                'format': rm.FORMAT, 'recording_id': rec.id, 'course_id': 'cid', 'category': '微机原理',
                'lesson_date': '2026-11-24', 'sources': [], 'excluded': [], 'associations': [],
                'relevant_pages': [], 'material_selection': {'files': [], 'summary': '无'},
                'history_count': 0, 'history_scope': '', 'warnings': [], 'learning_scope_candidates': [],
            }
            calls = 0

            async def fake_plan(instructions, payload, config, source_ids, package, folder, index):
                nonlocal calls
                calls += 1
                return {'knowledge': [{'id': 'k1', 'topic': '8259A', 'kind': 'first_introduction', 'sources': ['S1']}],
                        'figures': [], 'omitted_sources': [], 'warnings': [], 'figures_required': False}

            def fake_collect(r, folder, selected=None, **kw):
                (folder / 'history.json').write_text('[]')
                (folder / 'accepted-notes.md').write_text('')
                (folder / 'materials.json').write_text(json.dumps(pkg))
                return copy.deepcopy(pkg)

            pdf = root / 'note.pdf'
            pdf.write_bytes(b'%PDF-note')
            cfg = {'ai': {'summary_provider': {'transport': 'agy_cli', 'cli_model': 'mock', 'context_window': 262144}}}
            with patch('qwen_asr_app.storage.app_settings', settings), \
                 patch('qwen_asr_app.course_files.CourseFiles.list', return_value=[]), \
                 patch('qwen_asr_app.batch_ai._yuketang_questions', AsyncMock(return_value=[])), \
                 patch.object(rn, 'collect', side_effect=fake_collect), \
                 patch.object(rn, 'validated_plan_part', side_effect=fake_plan), \
                 patch.object(rn, 'request_ai_text', AsyncMock(return_value='## 8259A\n\n正文。')), \
                 patch.object(rn, 'export_note_pdf', AsyncMock(return_value=((pdf, {'pdf_sha256': 'x', 'pages': 1}), {}))), \
                 patch('qwen_asr_app.chapters.update_automatic_chapters'), \
                 patch('qwen_asr_app.ai.summarize_note_abstract', AsyncMock(return_value='摘要')):
                await rn.generate(rec, {**cfg, '_note_generation_id': 'round-1'})
                self.assertEqual(calls, 1)
                await rn.generate(rec, {**cfg, '_note_generation_id': 'round-2'})
                self.assertEqual(calls, 2)

    async def test_import_vga_drops_deleted_saved_asset_id_and_re_extracts(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            deleted_asset = 'f' * 32
            new_asset = '1' * 32
            (root / '课程来源.json').write_text(json.dumps({'vga': {'status': 'completed', 'asset_id': deleted_asset}}))
            rec = SimpleNamespace(metadata={}, save_metadata=Mock())
            with patch.object(sc.app.state, 'recording_store', SimpleNamespace(find=lambda _: rec), create=True), \
                 patch.object(sc.app.state, 'course_stream_manager', SimpleNamespace(jobs={}), create=True), \
                 patch('qwen_asr_app.course_files.CourseFiles.file', side_effect=ValueError('文件已删除')), \
                 patch.object(sc.client, 'vga_sources', return_value=[{'url': 'vga'}]), \
                 patch.object(sc, 'extract_courseware', AsyncMock(return_value=new_asset)) as extract:
                await sc.import_vga({'id': 'g1'}, {'id': 'lesson', 'directory': str(root)}, '微机原理', {})
            extract.assert_awaited_once()
            self.assertEqual(rec.metadata['review_material_ids'], [new_asset])

    async def test_continue_and_retry_recover_from_figure_or_material_deadlock(self):
        for action in ('continue', 'retry'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                old_id = 'd' * 32
                rec = self.make_record(root, with_note=True, review_material_ids=[old_id])
                rec.metadata['summary_job'] = {
                    'status': 'failed',
                    'error': '本节需要教学配图，但尚未取得合适原图；成果保留待补选',
                    'material_coverage': 'selected_files',
                    'generation_id': 'dead-gen-1',
                    'checkpoint': {'review_task': 'dead-task', 'phase': '准备配图'},
                    'custom_prompt': '',
                }
                rec.save_metadata()
                store = store_for(rec)
                seen = []

                async def fake_generate(ident, payload):
                    cur = store.find(ident)
                    seen.append((
                        cur.metadata.get('review_material_ids'),
                        cur.metadata['summary_job']['material_coverage'],
                        cur.metadata['summary_job']['generation_id'],
                        cur.metadata['summary_job']['checkpoint'],
                    ))
                    return {'summary': {'summary': '# 8086与8259A', 'pdf_ready': True}}

                with patch.object(app, 'recording_store', store), \
                     patch.object(app, '_saved_ai_task_config', return_value={}), \
                     patch.object(app.ai_preferences, 'task_config', return_value={}), \
                     patch.object(app, 'generate_library_summary', side_effect=fake_generate):
                    res = await app.ai_task_action({'recording_id': rec.id, 'type': 'summary', 'action': action})
                    self.assertTrue(res['accepted'])
                    await asyncio.wait_for(app.background_summary_tasks[rec.id], 2)

                ids, coverage, gen_id, checkpoint = seen[0]
                self.assertIsNone(ids)
                self.assertEqual(coverage, 'lesson')
                self.assertNotEqual(gen_id, 'dead-gen-1')
                self.assertEqual(checkpoint, {})

    async def test_continue_keeps_newly_saved_manual_selection_even_after_figure_deadlock(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = self.make_course_settings(root)
            files = CourseFiles(settings)
            src = root / '第08章_新版.pdf'
            src.write_bytes(b'%PDF-ch8-new')
            item, _ = files.import_file('微机原理', src, '第08章_新版.pdf', '课件')
            old_id, replacement_id = 'd' * 32, item['id']
            rec = self.make_record(root, with_note=True, review_material_ids=[old_id])
            rec.metadata['summary_job'] = {
                'status': 'failed',
                'error': '本节需要教学配图，但尚未取得合适原图；成果保留待补选',
                'material_coverage': 'selected_files',
                'generation_id': 'dead-gen-2',
                'checkpoint': {'review_task': 'dead-task', 'phase': '准备配图'},
                'custom_prompt': '',
            }
            rec.save_metadata()
            store = store_for(rec)
            seen = []

            async def fake_generate(ident, payload):
                cur = store.find(ident)
                seen.append((
                    cur.metadata.get('review_material_ids'),
                    cur.metadata['summary_job']['material_coverage'],
                    cur.metadata['summary_job']['generation_id'],
                    cur.metadata['summary_job']['checkpoint'],
                ))
                return {'summary': {'summary': '# 8086补选', 'pdf_ready': True}}

            with patch.object(app, 'app_settings', settings), \
                 patch.object(app, 'recording_store', store), \
                 patch.object(app, '_saved_ai_task_config', return_value={}), \
                 patch.object(app.ai_preferences, 'task_config', return_value={}), \
                 patch.object(app, 'generate_library_summary', side_effect=fake_generate):
                await app.save_review_material_choices(rec.id, {'material_ids': [replacement_id]})
                res = await app.ai_task_action({'recording_id': rec.id, 'type': 'summary', 'action': 'continue'})
                self.assertTrue(res['accepted'])
                await asyncio.wait_for(app.background_summary_tasks[rec.id], 2)

            ids, coverage, gen_id, checkpoint = seen[0]
            self.assertEqual(ids, [replacement_id])
            self.assertEqual(coverage, 'selected_files')
            self.assertNotEqual(gen_id, 'dead-gen-2')
            self.assertEqual(checkpoint, {})

    async def test_continue_on_network_error_preserves_selected_files_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            kept_id = '1' * 32
            rec = self.make_record(root, with_note=True, review_material_ids=[kept_id])
            rec.metadata['summary_job'] = {
                'status': 'failed',
                'error': '与 AI 通道的连接中断',
                'material_coverage': 'selected_files',
                'generation_id': 'live-gen-1',
                'checkpoint': {'review_task': 'keep-task', 'phase': '编写笔记'},
                'custom_prompt': '',
            }
            rec.save_metadata()
            store = store_for(rec)
            seen = []

            async def fake_generate(ident, payload):
                cur = store.find(ident)
                seen.append((
                    cur.metadata.get('review_material_ids'),
                    cur.metadata['summary_job']['material_coverage'],
                    cur.metadata['summary_job']['generation_id'],
                    cur.metadata['summary_job']['checkpoint'],
                ))
                return {'summary': {'summary': '# 恢复', 'pdf_ready': True}}

            with patch.object(app, 'recording_store', store), \
                 patch.object(app, '_saved_ai_task_config', return_value={}), \
                 patch.object(app.ai_preferences, 'task_config', return_value={}), \
                 patch.object(app, 'generate_library_summary', side_effect=fake_generate):
                res = await app.ai_task_action({'recording_id': rec.id, 'type': 'summary', 'action': 'continue'})
                self.assertTrue(res['accepted'])
                await asyncio.wait_for(app.background_summary_tasks[rec.id], 2)

            self.assertEqual(seen[0], ([kept_id], 'selected_files', 'live-gen-1', {'review_task': 'keep-task', 'phase': '编写笔记'}))

    async def test_review_notes_does_not_persist_empty_images_cache_when_figures_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = self.make_course_settings(root)
            rec = self.make_record(root, with_note=False)
            pkg = {
                'format': rm.FORMAT, 'recording_id': rec.id, 'course_id': 'cid', 'category': '微机原理',
                'lesson_date': '2026-11-19', 'sources': [], 'excluded': [], 'associations': [],
                'relevant_pages': [], 'material_selection': {'files': [], 'summary': '无'},
                'history_count': 0, 'history_scope': '', 'warnings': [], 'learning_scope_candidates': [],
            }

            async def fake_plan(*args, **kwargs):
                return {'knowledge': [{'id': 'k1', 'topic': '8259A', 'kind': 'first_introduction', 'sources': ['S1']}],
                        'figures': [], 'omitted_sources': [], 'warnings': [], 'figures_required': True}

            def fake_collect(r, folder, selected=None, **kw):
                (folder / 'history.json').write_text('[]')
                (folder / 'accepted-notes.md').write_text('')
                (folder / 'materials.json').write_text(json.dumps(pkg))
                return copy.deepcopy(pkg)

            cfg = {'ai': {'summary_provider': {'transport': 'agy_cli', 'cli_model': 'mock', 'context_window': 262144}}}
            with patch('qwen_asr_app.storage.app_settings', settings), \
                 patch('qwen_asr_app.course_files.CourseFiles.list', return_value=[]), \
                 patch('qwen_asr_app.batch_ai._yuketang_questions', AsyncMock(return_value=[])), \
                 patch.object(rn, 'collect', side_effect=fake_collect), \
                 patch.object(rn, 'validated_plan_part', side_effect=fake_plan):
                with self.assertRaisesRegex(ValueError, '本节需要教学配图'):
                    await rn.generate(rec, {**cfg, '_note_generation_id': 'fig-miss'})

            note_dirs = [p for p in (rec.directory / '.review-notes').iterdir() if p.name != '.stage-cache']
            self.assertEqual(len(note_dirs), 1)
            self.assertFalse((note_dirs[0] / 'images.json').exists())


if __name__ == '__main__':
    unittest.main()
