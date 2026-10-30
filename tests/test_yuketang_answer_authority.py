from tests import isolation  # noqa: F401
import copy
import unittest
from datetime import datetime, timezone

from qwen_asr_app.yuketang import parsing, retrieval, vision
from tests.test_yuketang import activities, fixture, lesson_answers


class AnswerAuthorityTests(unittest.TestCase):
    def test_sync_rechecks_legacy_parser_even_when_activity_fingerprint_is_unchanged(self):
        from qwen_asr_app.yuketang.sync import ClassroomSync
        sync = ClassroomSync(None, None, '1')
        legacy = {'status':'complete', 'fingerprint':'same'}
        self.assertTrue(sync.needs_import(legacy, 'same'))
        current = {**legacy, 'parser_version': parsing.PARSER_VERSION,
                   'checked_at': datetime.now(timezone.utc).isoformat()}
        self.assertFalse(sync.needs_import(current, 'same'))

    def test_student_choice_never_becomes_teacher_answer_when_teacher_did_not_publish(self):
        answers = copy.deepcopy(lesson_answers('lesson_objective'))
        answers['1801123460303932550']['data']['content']['answer'] = []
        questions = parsing.parse_lesson(activities()['35003874'], '26260946',
            fixture('lesson_objective/student_detail.json'), answers, fixture('lesson_objective/lesson_info.json'))
        first = questions[0]
        self.assertEqual(first['student_answer'],['B'])
        self.assertTrue(first['correct'])
        self.assertFalse(first['answer_known'])
        self.assertEqual(first['answer'],[])
        self.assertEqual(first['answer_source'],'unpublished')

    def test_only_labeled_teacher_evidence_is_accepted_from_question_images(self):
        for reading in [
            {'answer_source':'student_selection','answer_text':'B','answer_evidence':'绿色 B'},
            {'answer_source':'teacher','answer_text':'B','answer_evidence':'B 圆点是绿色'},
            {'answer_text':'B'},
        ]:
            self.assertEqual(vision.teacher_answer(reading,'slide'),'')
        self.assertEqual(vision.teacher_answer({'answer_source':'teacher','answer_text':'A',
                         'answer_evidence':'正确答案：A'},'slide'),'A')
        self.assertEqual(vision.teacher_answer({'text':'教师解析图中的公式'},'remark'),'教师解析图中的公式')

    def test_legacy_vision_answers_cannot_reach_note_writer(self):
        q={'body':'下列哪项正确','answer':[],'answer_known':False,'images':[],
           'vision':{'status':'done','text':'题干和全部选项的转写\n绿色选项 B','answer_text':'B'}}
        item=retrieval.compact(q)
        self.assertNotIn('正确答案',item)
        self.assertFalse(item['答案已知'])
        self.assertNotIn('绿色',str(item))
        self.assertEqual(item['图片转写'],'题干和全部选项的转写')
        q['vision']['version']=vision.VERSION
        self.assertTrue(retrieval.compact(q)['答案已知'])


if __name__ == '__main__':
    unittest.main()
