from tests import isolation  # noqa: F401
import copy,hashlib,json,tempfile,unittest
from pathlib import Path
from qwen_asr_app.storage import RecordingRecord


class MaterialOnlyCompletionTests(unittest.TestCase):
    def test_explicit_material_only_note_completes_without_touching_sources(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            metadata={'id':'material-note','status':'interrupted','course_time':{'end':'2026-10-19T09:35:00+08:00'},
                'material_only_note':{'transcript_policy':'empty'},
                'course_stream':{'stage':'interrupted','error':'audio contains no speech','chunks':[{'sha256':'original'}]},
                'ai_note_summary':{'source':'course_material_only','summary':'完整课件笔记'}}
            record=RecordingRecord(root,metadata);record.save_metadata()
            preserved={}
            for name,content in [('transcript.json',b'[]'),('transcript.txt',b''),('原始转写.txt',b''),('AI修正后文本.txt',b''),('notes.pdf',b'original PDF')]:
                (root/name).write_bytes(content);preserved[name]=hashlib.sha256(content).hexdigest()
            self.assertTrue(record.complete_material_only_note())
            saved=json.loads((root/'session.json').read_text())
            self.assertEqual(saved['status'],'completed');self.assertEqual(saved['ended_at'],metadata['course_time']['end'])
            self.assertEqual(saved['material_only_note']['previous_audio_error'],'audio contains no speech')
            self.assertEqual(saved['course_stream']['chunks'],[{'sha256':'original'}])
            self.assertEqual(saved['ai_note_summary']['summary'],'完整课件笔记')
            self.assertEqual({name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in preserved},preserved)
            self.assertFalse(record.complete_material_only_note())

    def test_normal_failed_or_unfinished_material_note_stays_resumable(self):
        base={'status':'interrupted','material_only_note':{'transcript_policy':'empty'},
            'ai_note_summary':{'source':'course_material_only','summary':'笔记'}}
        for change in [{'material_only_note':{}},{'ai_note_summary':{}},{'ai_note_summary':{'source':'post_recording','summary':'课堂笔记'}},
                       {'status':'processing'},{'segments':[{'id':1,'raw':'有真实转写'}]}]:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as raw:
                record=RecordingRecord(Path(raw),copy.deepcopy({**base,**change}))
                before=copy.deepcopy(record.metadata)
                self.assertFalse(record.complete_material_only_note());self.assertEqual(record.metadata,before)
