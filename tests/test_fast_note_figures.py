import unittest

from qwen_asr_app.fast_note import _pages
from qwen_asr_app.yuketang.retrieval import resolve_markers


class FastNoteFigureInputs(unittest.TestCase):
    def test_material_only_lesson_offers_course_pages(self):
        package = {'relevant_pages': [], 'sources': [{'source_id': 'a' * 32, 'pages': [{'page': 1, 'text': '框图'}, {'page': 2, 'text': '曲线'}]}]}
        self.assertEqual([(p['source_id'], p['page']) for p in _pages(package)], [('a' * 32, 1), ('a' * 32, 2)])

    def test_question_markers_become_displayed_numbers(self):
        note = '考点（见课上习题第Y2、Y1题）\n依据：雨课堂第 Y2 题\n\n## 课上习题\n\n### 第1题\n\n<!-- yuketang:Y2 -->\n\n题干\n\n### 第2题\n\n<!-- yuketang:Y1 -->\n'
        out = resolve_markers(note)
        self.assertIn('第1、2题', out)
        self.assertIn('第 1 题', out)
        self.assertIn('<!-- yuketang:Y2 -->', out)


if __name__ == '__main__':
    unittest.main()
