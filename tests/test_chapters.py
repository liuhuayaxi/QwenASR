from tests import isolation  # noqa: F401

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qwen_asr_app.chapters import (
    chapter_fields, infer_chapters, labels_from_metadata, normalize_chapters,
    suggestion_for_record,
)


def source(name, texts, sid='slides'):
    return {'source_id': sid, 'name': name,
            'pages': [{'page': page, 'text': text} for page, text in texts]}


class ChapterTests(unittest.TestCase):
    def test_exercise_intro_is_not_appended_as_chapter_title(self):
        package = {'sources': [source('通信原理 C-第01章-翻转课堂预习题02-答案解析.pdf', [
            (1, '第一章翻转课堂预习题（二）：题目与答案解析\n本节把课程《第一章翻转课堂预习题（二）》第3–15 页的题目完整纳入复习笔记，并按')])]}
        self.assertEqual(infer_chapters(package)['chapters'], ['第1章'])

    def test_slide_version_footer_and_chinese_wrapping_are_not_title(self):
        cases = [('第五章 半导体光放大器（2026lpl）(1) 》 - 1/54页 -', '第5章 半导体光放大器'),
                 ('第一章半导体中光子-电子的互作用(2026lpl)', '第1章 半导体中光子-电子的互作用'),
                 ('第三章 半导体激光器的 工作原理及类型', '第3章 半导体激光器的工作原理及类型')]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(infer_chapters({'sources': [source('slides.pdf', [(1,text)])]})['chapters'], [expected])

    def test_labels_validate_and_deduplicate_without_silent_truncation(self):
        self.assertEqual(normalize_chapters(' 第一章 光学；第二章\n第一章 光学; '), ['第一章 光学', '第二章'])
        self.assertEqual(normalize_chapters(['Unit 2 Waves', 'unit 2 waves']), ['Unit 2 Waves'])
        for value in ({'chapter': '1'}, [1], 'x' * 121, [str(i) for i in range(9)]):
            with self.assertRaises(ValueError):
                normalize_chapters(value)

    def test_legacy_metadata_is_tolerant_but_prefers_multi_chapter(self):
        self.assertEqual(labels_from_metadata({'chapters': ['第4章', '第5章'], 'chapter': '第1章'}), ['第4章', '第5章'])
        self.assertEqual(labels_from_metadata({'chapters': {}, 'chapter': '第4章'}), ['第4章'])
        self.assertEqual(labels_from_metadata({'chapters': [None], 'chapter': {}}), [])
        self.assertEqual(labels_from_metadata(None), [])

    def test_fields_have_stable_contract_and_display_safe_evidence(self):
        result = chapter_fields(['第4章', '第5章'], 'materials', [{'source_name': '/private/path/教材.pdf', 'page': 2, 'text': '第四章\n光学'}])
        self.assertEqual(result['chapter'], '第4章；第5章')
        self.assertEqual(result['chapter_info'], {'source': 'materials', 'version': 'chapter-v1',
            'evidence': [{'source_name': '教材.pdf', 'page': 2, 'text': '第四章 光学'}]})
        self.assertEqual(chapter_fields([], 'materials')['chapter_info']['source'], 'unknown')
        self.assertEqual(chapter_fields([], 'manual')['chapter_info']['source'], 'manual')

    def test_chinese_title_slide_and_split_heading(self):
        package = {'sources': [source('slides.pdf', [(1, '第四章\n半导体激光器的性能'), (2, '增益与线宽')])]}
        result = infer_chapters(package)
        self.assertEqual(result['chapters'], ['第4章 半导体激光器的性能'])
        self.assertEqual(result['chapter_info']['evidence'][0]['page'], 1)

    def test_english_chapter_and_unit_are_distinct(self):
        result = infer_chapters({'sources': [source('a.pdf', [(1, 'CHAPTER 04: Optical Amplifiers')], 'a'),
                                            source('b.pdf', [(1, 'Unit 2 — Waves')], 'b')]})
        self.assertEqual(result['chapters'], ['Chapter 4 Optical Amplifiers', 'Unit 2 Waves'])

    def test_section_number_author_and_page_number_are_not_fake_chapter_titles(self):
        package = {'sources': [source('光电子器件-第02章-光纤结构与导光原理-课件.pdf',
            [(1, 'Chapter 2.1\nExample Teacher\nEmail: teacher@example.invalid')])]}
        self.assertEqual(infer_chapters(package)['chapters'], ['第2章 光纤结构与导光原理'])
        package['sources'][0]['pages'][0]['text'] += '\n[[AI_TRANSCRIPT_PAGE:1]]\n[TEXT]\nChapter 2.1\n[TEXT]\nExample Teacher'
        self.assertEqual(infer_chapters(package)['chapters'], ['第2章 光纤结构与导光原理'])
        self.assertEqual(infer_chapters({'sources': [source('a.pdf', [(1, 'Chapter 4\n12')])]})['chapters'], ['Chapter 4'])

    def test_book_toc_is_ignored_and_relevant_pages_select_one_chapter(self):
        package = {'sources': [source('物理光学教材.pdf', [(1, '目录\n第一章 光的电磁理论\n第二章 干涉\n第三章 衍射'),
            (6, '第一章 光的电磁理论'), (50, '第二章 干涉'), (51, '双缝干涉'), (100, '第三章 衍射')])],
            'relevant_pages': [{'source_id': 'slides', 'page': 51, 'score': 10},
                               {'source_id': 'slides', 'page': 1, 'score': .2}]}
        result = infer_chapters(package)
        self.assertEqual(result['chapters'], ['第2章 干涉'])
        self.assertEqual(result['chapter_info']['evidence'][0]['page'], 50)

    def test_cross_chapter_support_retains_both_but_weak_adjacent_page_does_not(self):
        package = {'sources': [source('教材.pdf', [(1, '第4章 信道'), (20, '第5章 模拟通信系统')])],
            'relevant_pages': [{'source_id': 'slides', 'page': 19, 'score': 10},
                               {'source_id': 'slides', 'page': 20, 'score': 2.5}]}
        self.assertEqual(infer_chapters(package)['chapters'], ['第4章 信道'])
        package['relevant_pages'][1]['score'] = 7
        self.assertEqual(infer_chapters(package)['chapters'], ['第4章 信道', '第5章 模拟通信系统'])

    def test_multi_chapter_book_without_page_evidence_stays_unknown(self):
        package = {'sources': [source('完整教材.pdf', [(1, '目录\n第一章 绪论\n第二章 声波'),
            (20, '第一章 绪论'), (80, '第二章 声波')])]}
        result = infer_chapters(package)
        self.assertEqual(result['chapters'], [])
        self.assertEqual(result['chapter_info']['source'], 'unknown')

    def test_selected_filename_fallback_and_stale_lesson_title(self):
        package = {'sources': [source('光电子学-第04章-半导体激光器的性能-课件.pdf', [])],
                   'material_selection': {'lesson_title': '第二章 发光二极管 2'}}
        self.assertEqual(infer_chapters(package)['chapters'], ['第4章 半导体激光器的性能'])

    def test_unselected_files_and_no_signal_all_do_not_create_false_cross_chapter(self):
        package = {'sources': [source('第1章.pdf', [(1, '第一章 绪论')], 'one'),
                               source('第2章.pdf', [(1, '第二章 声波')], 'two')],
                   'material_selection': {'files': [{'source_id': 'one', 'chosen': False},
                       {'source_id': 'two', 'chosen': True, 'mode': 'manual'}]}}
        self.assertEqual(infer_chapters(package)['chapters'], ['第2章 声波'])
        package['material_selection']['files'][1]['mode'] = 'no_signal_all'
        self.assertEqual(infer_chapters(package)['chapters'], [])

    def test_lesson_title_fallback_never_invents_chapter_for_topic_only(self):
        result = infer_chapters({}, {'lesson_title': '第十二章 光波导'})
        self.assertEqual(result['chapters'], ['第12章 光波导'])
        self.assertEqual(result['chapter_info']['source'], 'lesson_title')
        self.assertEqual(infer_chapters({}, {'title': '欧姆定律概念公式与例题计算'})['chapters'], [])
        self.assertEqual(infer_chapters({}, {'title': '9月5日授课'})['chapters'], [])

    def test_prose_reference_is_not_a_chapter_heading(self):
        package = {'sources': [source('slides.pdf', [(1, '回顾\n见第四章 半导体激光器。\n第五章介绍了光放大器。')])]}
        self.assertEqual(infer_chapters(package)['chapters'], [])
        self.assertEqual(infer_chapters({'sources': [source('assembly.pdf', [(1, 'CH CL\n寄存器与总线')])]})['chapters'], [])

    def test_weak_recap_material_does_not_add_a_second_chapter(self):
        package = {'sources': [source('第1章 复习.pdf', [(1, '第1章 绪论')], 'old'),
                               source('第6章 数字基带.pdf', [(1, '第6章 数字基带')], 'current')],
                   'relevant_pages': [{'source_id': 'old', 'page': 1, 'score': 1},
                                      {'source_id': 'current', 'page': 1, 'score': 10}]}
        self.assertEqual(infer_chapters(package)['chapters'], ['第6章 数字基带'])

    def test_cache_lookup_is_readonly_and_confined_to_current_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_dir = root / 'record'; cache = record_dir / '.note-review/current'
            cache.mkdir(parents=True)
            path = cache / 'materials.json'
            path.write_text(json.dumps({'sources': [source('chapter.pdf', [(1, 'Chapter 3: Interference')])]}))
            record = SimpleNamespace(directory=record_dir, metadata={'ai_note_summary': {'review_package': '.note-review/current'}})
            before = {str(p): p.stat().st_mtime_ns for p in record_dir.rglob('*')}
            self.assertEqual(suggestion_for_record(record)['chapters'], ['Chapter 3 Interference'])
            self.assertEqual(before, {str(p): p.stat().st_mtime_ns for p in record_dir.rglob('*')})
            outside = root / 'outside'; outside.mkdir(); (outside / 'materials.json').write_text(path.read_text())
            record.metadata['ai_note_summary']['review_package'] = '../outside'
            self.assertEqual(suggestion_for_record(record)['chapters'], [])
            record.metadata['ai_note_summary']['review_package'] = str(outside)
            self.assertEqual(suggestion_for_record(record)['chapters'], [])

    def test_cached_file_selection_fallback_ignores_excluded_names(self):
        record = SimpleNamespace(directory=None, metadata={'material_selection': {'files': [
            {'source_id': 'a', 'name': 'Chapter 4 Amplifiers.pdf', 'chosen': True},
            {'source_id': 'b', 'name': 'Chapter 5 Filters.pdf', 'chosen': False}]}})
        self.assertEqual(suggestion_for_record(record)['chapters'], ['Chapter 4 Amplifiers'])

    def test_weak_old_laser_materials_do_not_label_an_amplifier_lesson(self):
        package = {'sources': [source('第3章 半导体激光器的工作原理及类型.pdf', [(1, '第3章 半导体激光器的工作原理及类型')], 'a'),
                               source('第4章 半导体激光器的性能.pdf', [(1, '第4章 半导体激光器的性能')], 'b')],
                   'material_selection': {'files': [{'source_id': 'a', 'chosen': True, 'coverage': .36, 'mode': 'coverage'},
                                                     {'source_id': 'b', 'chosen': True, 'coverage': .25, 'mode': 'coverage'}]}}
        result = infer_chapters(package, {'title': '光电子学：半导体光放大器原理与分类', 'category': '光电子学'})
        self.assertEqual(result['chapters'], [])

    def test_native_fallback_reads_only_current_course_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            course = Path(directory) / '光电子学'
            record_dir = course / '上课录音' / 'record'; record_dir.mkdir(parents=True)
            original = course / '第5章 半导体光放大器.pdf'; original.write_bytes(b'%PDF test')
            derived = course / 'AI友好型'; derived.mkdir(); (derived / 'ignored.pdf').write_bytes(b'%PDF derived')
            other = course / 'old-record'; other.mkdir(); (other / 'session.json').write_text('{}'); (other / 'ignored.pdf').write_bytes(b'%PDF old lesson')
            record = SimpleNamespace(directory=record_dir, metadata={'category': '光电子学', 'title': '半导体光放大器原理与分类'})
            before = {str(p): p.stat().st_mtime_ns for p in course.rglob('*')}
            with patch('qwen_asr_app.chapters._native_source_pages', return_value=[{'page': 1, 'text': '第五章 半导体光放大器'}]) as read:
                result = suggestion_for_record(record)
            self.assertEqual(result['chapters'], ['第5章 半导体光放大器'])
            self.assertEqual(read.call_count, 1)
            self.assertEqual(read.call_args.args[0], str(original.resolve()))
            self.assertEqual(result['chapter_info']['evidence'][0]['source_name'], original.name)
            self.assertEqual(before, {str(p): p.stat().st_mtime_ns for p in course.rglob('*')})


if __name__ == '__main__':
    unittest.main()
