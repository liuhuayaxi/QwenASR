from tests import isolation  # noqa: F401
import copy
from pathlib import Path
import tempfile
import unittest
from qwen_asr_app.course_phase_status import lesson_phases
from qwen_asr_app.task_center import group_tasks
from tests.test_live_recovery import live_record


class LessonStageTests(unittest.TestCase):
    def test_legacy_transcript_verification_does_not_block_note_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            record=live_record(Path(raw))
            record.segments=[{'id':1,'raw':'原话九','corrected':'原话九','start':0,'end':1,
                              'correction_review':{'before':'原话九','after':'原话9','risks':[],'evidence':[]}}]
            record.metadata.update(status='interrupted',course_stream={'input_complete':True,'chunks':[{}],
                'completed_chunks':1,'error':'output limit'},correction_verification={'status':'failed','total':1,'completed':0})
            entry={'phases':{'download':{},'asr':{'state':'error'},'correction':{'state':'error'},'summary':{'state':'pending'}}}
            before=copy.deepcopy(record.metadata)
            phases=lesson_phases(entry,record)
            self.assertEqual(phases['asr']['state'],'completed')
            self.assertEqual(phases['correction']['state'],'completed')
            self.assertNotIn('verification',phases)
            self.assertEqual(record.metadata,before)
            task={'id':'course_queue','type':'course_queue','title':'whole queue','status':'interrupted','total':5,'completed':0,
                  'course_items':[{'id':'one','title':'lesson','recording_id':record.id,'state':'interrupted',
                    'phases':[{'key':k,**v} for k,v in phases.items()]}]}
            groups,_=group_tasks([task]);shown=groups[0]['tasks'][0]
            self.assertEqual(shown['completed'],3)
            self.assertEqual(shown['total'],4)
            self.assertEqual(shown['progress_unit'],'阶段')
            self.assertNotIn('复核',shown['next_action'])
