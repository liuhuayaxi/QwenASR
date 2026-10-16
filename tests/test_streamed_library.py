from tests import isolation  # noqa: F401
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import patch

from qwen_asr_app.library_query import listing
from qwen_asr_app.knowledge.task_adapter import IntelligenceTasks


class LightweightListingTests(unittest.TestCase):
    def records(self):
        alive=weakref.WeakSet();observed={'peak':0}
        class Record:
            def __init__(self,i):
                self.id=str(i);self.duration=10
                self.metadata={'id':self.id,'title':'class '+self.id,'category':'course',
                    'started_at':f'2026-10-{i%28+1:02d}T08:00:00+08:00',
                    'ai_note_summary':{'summary':'saved note'}}
                alive.add(self);observed['peak']=max(observed['peak'],len(alive))
            def list_item(self):return {'id':self.id,'title':self.metadata['title']}
        def visit():
            for i in range(100):yield Record(i)
        return visit,observed

    def test_library_consumes_once_keeps_small_dtos_and_preserves_filters_counts_and_order(self):
        visit,observed=self.records()
        result=listing(visit(),category='course',date='2026-10-28',offset=1,limit=1)
        self.assertEqual(result['total'],3)
        self.assertEqual(result['records'],[{'id':'55','title':'class 55'}])
        self.assertEqual(result['next_offset'],2)
        self.assertEqual(result['course_counts']['course'],{'records':100,'notes':100})
        self.assertEqual(result['calendar']['2026-10-28'],{'count':3,'duration':30})
        self.assertEqual(result['note_count'],3)
        self.assertLessEqual(observed['peak'],2)

    def test_task_adapter_does_not_retain_full_recordings_for_titles_or_note_tasks(self):
        visit,observed=self.records()
        def forbidden():raise AssertionError('full recording list loaded')
        recordings=SimpleNamespace(iter_records=visit,list_records=forbidden)
        adapter=IntelligenceTasks(SimpleNamespace(jobs=lambda:[]),None,recordings)
        with patch('qwen_asr_app.knowledge.task_adapter.read_notes_doc',new=lambda record:{'notes':[]}):
            self.assertEqual(adapter.tasks(),[])
        self.assertLessEqual(observed['peak'],2)
