import unittest

from qwen_asr_app.fast_note import _pages, generation_identity
from types import SimpleNamespace
from qwen_asr_app.yuketang.retrieval import resolve_markers


class FastNoteFigureInputs(unittest.TestCase):
    def test_replacing_courseware_changes_identity_before_a_completed_task_can_reuse(self):
        record = SimpleNamespace(id='lesson')
        original = [{'id':'file','name':'题目.pdf','sha256':'old','conversion':{'status':'completed','sha256':'ocr1'}}]
        first = generation_identity(record, [], [], None, original, {})
        self.assertNotEqual(first, generation_identity(record, [], [], None, [{**original[0],'sha256':'new'}], {}))
        self.assertNotEqual(first, generation_identity(record, [], [], None, original, {'_note_summary_custom_prompt':'重新解释'}))
        self.assertNotEqual(first, generation_identity(record, [], [], None,
                            [{**original[0],'conversion':{'status':'completed','sha256':'ocr2'}}], {}))
        self.assertEqual(first, generation_identity(record, [], [], None, original, {}))

    def test_missing_slides_are_excluded_and_personal_controls_are_labeled(self):
        source = 'a'*32
        values = _pages({'sources':[{'source_id':source,'pages':[
            {'page':1,'text':'broken','yuketang':{'missing_slide':True}},
            {'page':2,'text':'题目','yuketang':{'student_selection':True}}]}]})
        self.assertEqual(len(values),1)
        self.assertEqual(values[0]['page_kind'],'exercise')
        self.assertEqual(values[0]['page'],2)

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
