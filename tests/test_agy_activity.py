from tests import isolation  # noqa: F401
import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from qwen_asr_app.agy_activity import SessionActivity
from qwen_asr_app.ai_activity import track_ai_activity,track_ai_request,bind_ai_session,task_ai_activity

SID='12345678-1234-1234-1234-123456789abc'

class SessionActivityTests(unittest.TestCase):
    def test_native_read_file_is_reported_as_actual_reading(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);path=root/SID/'.system_generated/logs/transcript_full.jsonl';path.parent.mkdir(parents=True)
            events=[{'step_index':1,'type':'PLANNER_RESPONSE','tool_calls':[{'name':'read_file','args':{'Path':'/private/input.json'}}]},
                    {'step_index':2,'type':'GENERIC','status':'DONE'}]
            path.write_text('\n'.join(json.dumps(e) for e in events)+'\n')
            state=SessionActivity(SID,root).snapshot()
            self.assertEqual(state['read_files'],1)
            self.assertEqual(state['image_reads'],0)
    def test_counts_actual_activity_without_exposing_reasoning_or_content(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);path=root/SID/'.system_generated/logs/transcript_full.jsonl';path.parent.mkdir(parents=True)
            image='/private/course/pages/hash/page-009.png'
            events=[{'step_index':0,'type':'USER_INPUT','content':'private task'},
                {'step_index':1,'type':'PLANNER_RESPONSE','thinking':'private reasoning','tool_calls':[
                    {'name':'view_file','args':{'AbsolutePath':image}}]},
                {'step_index':2,'type':'GENERIC','status':'DONE','media':[{'uri':'private image'}]},
                {'step_index':3,'type':'PLANNER_RESPONSE','content':'private result'}]
            path.write_text('\n'.join(json.dumps(e) for e in events)+'\n')
            state=SessionActivity(SID,root).snapshot()
            self.assertEqual(state['opened_pages'],[9]);self.assertEqual(state['image_reads'],1)
            self.assertEqual(state['model_turns'],2);self.assertEqual(state['thinking_chars'],17)
            for private in ['private reasoning','private result','private task',image]:self.assertNotIn(private,json.dumps(state))

    def test_partial_append_and_repeated_reads_never_double_count(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);path=root/SID/'.system_generated/logs/transcript_full.jsonl';path.parent.mkdir(parents=True)
            first=json.dumps({'step_index':1,'type':'PLANNER_RESPONSE','thinking':'abc'})
            path.write_text(first[:20]);activity=SessionActivity(SID,root)
            self.assertEqual(activity.snapshot()['thinking_chars'],0)
            with path.open('a') as out:out.write(first[20:]+'\n')
            activity.checked=0
            self.assertEqual(activity.snapshot()['thinking_chars'],3)
            activity.checked=0
            self.assertEqual(activity.snapshot()['thinking_chars'],3)

    def test_request_exposes_bound_page_activity_and_cancelled_request_disappears(self):
        with track_ai_activity('test-live'):
            with track_ai_request(label='review',pages=[9,10]):
                with patch('qwen_asr_app.agy_activity.SessionActivity.snapshot',return_value={
                    'phase':'thinking','last_event_at':time.time(),'thinking_chars':10,'opened_pages':[9]}):
                    bind_ai_session(SID)
                    state=task_ai_activity('test-live')
                self.assertEqual(state['request_activity'][0]['pages'],[9,10])
                self.assertEqual(state['request_activity'][0]['opened_pages'],[9])
                self.assertEqual(state['phase'],'thinking')
            self.assertEqual(task_ai_activity('test-live')['request_activity'],[])

    def test_session_id_is_not_a_file_path(self):
        with self.assertRaises(ValueError):SessionActivity('../../credentials')
