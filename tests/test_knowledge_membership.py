from tests import isolation  # noqa: F401

import copy
import json
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from qwen_asr_app.categories import change_category
from qwen_asr_app.durable import atomic_json
from qwen_asr_app.knowledge.service import KnowledgeService
from qwen_asr_app.knowledge.store import KnowledgeStore
from qwen_asr_app.library_layout import INDEX, RECORDINGS
from qwen_asr_app.shared_files import SharedCourseFiles
from qwen_asr_app.storage import RecordingRecord, RecordingStore, app_settings


class Embedder:
    contract = {'provider': 'synthetic', 'model': 'membership-test', 'dim': 2}

    def encode_documents(self, texts):
        return [[1., 0.] for _ in texts]

    def encode_query(self, text):
        return [1., 0.]


@contextmanager
def course_library(root, layout='shared-courses-v1'):
    with patch.object(app_settings, 'storage_root', root), \
            patch.object(app_settings, 'known_roots', [str(root)]), \
            patch.object(app_settings, 'categories', ['未分类', '旧课程', '新课程']), \
            patch.object(app_settings, 'category_profiles', {'未分类': {'asr_prompt': '保留目标配置'}}), \
            patch.object(app_settings, 'library_layout', layout), patch.object(app_settings, 'save'):
        record = RecordingRecord.create_live('测试课次', '旧课程', root)
        record.metadata['status'] = 'completed'
        record.segments = [{'id': 1, 'raw': '阈值增益与光场限制因子', 'start': 0, 'end': 10}]
        record.save_all()
        recordings = RecordingStore()
        service = KnowledgeService(app_settings, recordings, course_files=SimpleNamespace(list=lambda category: []),
                                   embedder=Embedder())
        service.sync_course('旧课程')
        yield record, recordings, service


class KnowledgeMembershipTests(unittest.TestCase):
    def test_recording_move_reuses_content_without_leaving_evidence_in_old_course(self):
        for layout in ('legacy', 'shared-courses-v1'):
            for finder_move in ((False, True) if layout == 'shared-courses-v1' else (False,)):
                with self.subTest(layout=layout, finder_move=finder_move), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    with course_library(root, layout) as (record, recordings, service):
                        before = service.search('旧课程', '阈值增益')
                        self.assertTrue(before)
                        if finder_move:
                            target = root / '新课程'
                            if layout == 'shared-courses-v1':
                                target /= RECORDINGS
                            target.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(record.directory), str(target / record.directory.name))
                            recordings.refresh()
                        else:
                            recordings.update(record.id, '测试课次', '新课程')
                        service.sync_course('新课程')
                        after = service.search('新课程', '阈值增益')
                        self.assertTrue(after)
                        self.assertFalse(service.search('旧课程', '阈值增益'))
                        self.assertEqual(after[0]['citation_id'], before[0]['citation_id'])
                        source = service.store.get_source('rec_' + record.id + '_raw')
                        self.assertEqual(source['course_id'], service.course_id('新课程'))
                        with service.store._connect() as conn:
                            chunks = conn.execute('SELECT course_id,category,lesson_id FROM chunks WHERE source_id=?', (source['source_id'],)).fetchall()
                        self.assertTrue(all(tuple(row) == (source['course_id'], '新课程', record.id) for row in chunks))

    def test_unchanged_source_fast_path_updates_metadata_without_parsing(self):
        with tempfile.TemporaryDirectory() as raw:
            with course_library(Path(raw)) as (record, recordings, service):
                source_id = 'rec_' + record.id + '_raw'
                spec = next(item for item in service.ingestor._discover_recordings('旧课程', service.course_id('旧课程'))
                            if item['source_id'] == source_id)
                spec.update(category='新课程', course_id=service.course_id('新课程'), title='调整后的课次名')
                spec['loader'] = lambda: self.fail('unchanged transcript must not be parsed again')
                outcome = service.ingestor._sync_single_source(spec, Embedder.contract,
                          json.dumps(Embedder.contract, sort_keys=True), service.embedder, None)
                self.assertEqual(outcome, (False, 0))
                source = service.store.get_source(source_id)
                self.assertEqual((source['category'], source['title']), ('新课程', '调整后的课次名'))
                row = service.store.search_lexical('新课程', '阈值增益', source_types=['recording_raw'])[0]
                self.assertEqual((row['course_id'], row['title']), (source['course_id'], '调整后的课次名'))

    def test_upgrade_repairs_active_and_historical_membership_without_reembedding(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'index.db'
            store = KnowledgeStore(path)
            meta = {'source_id': 'source', 'course_id': 'old', 'category': '旧课程', 'lesson_id': 'old-lesson',
                    'title': '测试内容', 'kind': '录音', 'origin': 'original', 'source_type': 'recording_raw', 'open_url': '/?recording=record'}
            for revision in ('past', 'current'):
                store.commit_source_revision(meta, revision, revision, 'parser', 'chunker', 'fixture',
                                             [{'chunk_id': revision, 'content_hash': revision, 'text': '阈值增益'}], [[1., 0.]])
            citation = store.get_chunk('current')['citation_id']
            with store._connect() as conn:
                # This is the pre-fix on-disk state: authoritative source
                # migrated, reused and historical chunks did not.
                conn.execute("UPDATE sources SET course_id='new',category='新课程',lesson_id='new-lesson'")
                conn.execute("DELETE FROM knowledge_meta WHERE key='source_membership_version'")
            for _ in range(2):
                store = KnowledgeStore(path)
                for ident in ('past', 'current'):
                    row = store.get_chunk(ident)
                    self.assertEqual((row['course_id'], row['category'], row['lesson_id']), ('new', '新课程', 'new-lesson'))
                self.assertEqual(store.get_chunk('past')['is_active'], 0)
                self.assertEqual(store.get_chunk('current')['citation_id'], citation)
                self.assertFalse(store.search_lexical('旧课程', '阈值增益'))
                self.assertEqual(len(store.search_lexical('新课程', '阈值增益')), 1)
                self.assertFalse(store.search_vectors('旧课程', [1., 0.], 'fixture'))
                self.assertEqual(len(store.search_vectors('新课程', [1., 0.], 'fixture')), 1)

    def test_same_revision_upsert_updates_course_and_lesson(self):
        with tempfile.TemporaryDirectory() as raw:
            store = KnowledgeStore(Path(raw) / 'index.db')
            meta = {'source_id': 'source', 'course_id': 'old', 'category': '旧课程', 'lesson_id': 'old-lesson',
                    'title': '测试内容', 'kind': '录音', 'origin': 'original', 'source_type': 'recording_raw', 'open_url': '/'}
            chunk = {'chunk_id': 'stable', 'content_hash': 'same', 'text': '阈值增益'}
            for course, category, lesson in (('old', '旧课程', 'old-lesson'), ('new', '新课程', 'new-lesson')):
                meta.update(course_id=course, category=category, lesson_id=lesson)
                store.commit_source_revision(meta, 'same', 'same', 'parser', 'chunker', 'fixture', [chunk], [[1., 0.]])
            row = store.get_chunk('stable')
            self.assertEqual((row['course_id'], row['category'], row['lesson_id']), ('new', '新课程', 'new-lesson'))
            self.assertFalse(store.search_lexical('旧课程', '阈值增益'))
            self.assertEqual(len(store.search_lexical('新课程', '阈值增益')), 1)

    def test_indexed_category_merge_preserves_destination_identity_materials_and_citations(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with course_library(root) as (record, recordings, service):
                before = service.search('旧课程', '阈值增益')[0]
                source_id, target_id = service.course_id('旧课程'), service.course_id('未分类')
                (root / '旧课程/source.txt').write_text('source original')
                (root / '未分类/target.txt').write_text('target original')
                atomic_json(root / '旧课程' / INDEX / 'index.json', {'source-asset': {'relative_path': 'source.txt'}})
                atomic_json(root / '未分类' / INDEX / 'index.json', {'target-asset': {'relative_path': 'target.txt'}})
                with patch('qwen_asr_app.storage.TRASH_ROOT', root / 'trash'):
                    result = change_category(app_settings, recordings, '旧课程')
                self.assertTrue(result['deleted'])
                self.assertFalse((root / '旧课程').exists())
                self.assertEqual(service.course_id('未分类'), target_id)
                self.assertNotEqual(source_id, target_id)
                self.assertIsNone(service.store.get_course('旧课程'))
                self.assertEqual((root / '未分类/source.txt').read_text(), 'source original')
                self.assertEqual((root / '未分类/target.txt').read_text(), 'target original')
                self.assertEqual(set(json.loads((root / '未分类' / INDEX / 'index.json').read_text())), {'source-asset', 'target-asset'})
                self.assertEqual(app_settings.category_profiles['未分类']['asr_prompt'], '保留目标配置')
                source = service.store.get_source('rec_' + record.id + '_raw')
                self.assertEqual((source['category'], source['course_id']), ('未分类', target_id))
                self.assertTrue(Path(source['file_path']).is_file())
                self.assertFalse(service.store.search_lexical('旧课程', '阈值增益'))
                self.assertEqual(service.search('未分类', '阈值增益')[0]['citation_id'], before['citation_id'])
                service.sync_course('未分类')
                self.assertEqual(service.search('未分类', '阈值增益')[0]['citation_id'], before['citation_id'])

    def test_failed_merge_rolls_back_files_indexes_sidecars_and_settings(self):
        for failure in ('index', 'settings', 'archive'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                with course_library(root) as (record, recordings, service):
                    source_id, target_id = service.course_id('旧课程'), service.course_id('未分类')
                    before_source = service.store.get_source('rec_' + record.id + '_raw')
                    categories, profiles = list(app_settings.categories), copy.deepcopy(app_settings.category_profiles)
                    (root / '旧课程/source.txt').write_text('source original')
                    atomic_json(root / '未分类' / INDEX / 'index.json', {'keep': {}})
                    target_index = (root / '未分类' / INDEX / 'index.json').read_bytes()
                    original_move = shutil.move

                    def move(old, new, *args, **kwargs):
                        if Path(new).parent == root / 'trash':
                            raise OSError('archive denied')
                        return original_move(old, new, *args, **kwargs)

                    def write(path, value):
                        atomic_json(path, value)
                        if path == root / '未分类' / INDEX / 'index.json':
                            raise OSError('index commit failed after replace')

                    failing = patch('qwen_asr_app.shared_library.atomic_json', side_effect=write) if failure == 'index' else \
                        patch.object(app_settings, 'save', side_effect=[OSError('settings failed'), None]) if failure == 'settings' else \
                        patch('qwen_asr_app.shared_library.shutil.move', side_effect=move)
                    with failing, patch('qwen_asr_app.storage.TRASH_ROOT', root / 'trash'), self.assertRaises(OSError):
                        change_category(app_settings, recordings, '旧课程')
                    self.assertEqual((root / '旧课程/source.txt').read_text(), 'source original')
                    self.assertTrue(record.directory.is_dir())
                    self.assertFalse((root / '未分类/source.txt').exists())
                    self.assertEqual((root / '未分类' / INDEX / 'index.json').read_bytes(), target_index)
                    self.assertEqual(app_settings.categories, categories)
                    self.assertEqual(app_settings.category_profiles, profiles)
                    self.assertEqual(service.course_id('旧课程'), source_id)
                    self.assertEqual(service.course_id('未分类'), target_id)
                    self.assertEqual(service.store.get_source(before_source['source_id']), before_source)
                    self.assertTrue(service.search('旧课程', '阈值增益'))
                    self.assertFalse(service.search('未分类', '阈值增益'))

    def test_merge_rebases_fresh_pdf_proofs_without_reviving_stale_originals(self):
        import pymupdf
        for changed_original in (False, True):
            with self.subTest(changed_original=changed_original), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                with course_library(root) as (record, recordings, service):
                    target_id = service.course_id('未分类')
                    original = root / '旧课程/讲义.txt'
                    original.write_text('阈值增益与光场限制因子', encoding='utf-8')
                    files = SharedCourseFiles(app_settings)
                    item = files.scan('旧课程')[0]
                    converted = files.output_path('旧课程', item)
                    converted.parent.mkdir(parents=True, exist_ok=True)
                    with pymupdf.open() as document:
                        document.new_page()
                        document.embfile_add('mineru-transcript.md', original.read_bytes())
                        document.save(converted)
                    files.cache.clear()
                    service.course_files = service.ingestor.course_files = files
                    service.sync_course('旧课程')
                    before = service.search('旧课程', '阈值增益', source_types=['course_file_pdf'])
                    self.assertTrue(before)
                    if changed_original:
                        original.write_text('原件已更新，转换 PDF 还未更新。', encoding='utf-8')
                    with patch('qwen_asr_app.storage.TRASH_ROOT', root / 'trash'):
                        change_category(app_settings, recordings, '旧课程')
                    source = service.store.get_source('cf_' + item['id'] + '_pdf')
                    self.assertEqual(source['course_id'], target_id)
                    self.assertTrue(Path(source['file_path']).is_file())
                    self.assertTrue(Path(json.loads(source['lineage'])['parent_file_path']).is_file())
                    after = service.search('未分类', '阈值增益', source_types=['course_file_pdf'])
                    if changed_original:
                        self.assertFalse(after)
                    else:
                        self.assertEqual(after[0]['citation_id'], before[0]['citation_id'])
                        service.sync_course('未分类')
                        self.assertEqual(service.search('未分类', '阈值增益', source_types=['course_file_pdf'])[0]['citation_id'], before[0]['citation_id'])

