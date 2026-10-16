"""Real adoption routes preserve source files; only the review gate is mocked."""
from tests import isolation  # noqa: F401

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from qwen_asr_app.note_history import read_version,version_id


class ProposalPreservationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_review_capture import ProductionCaptureTests
        self.harness=ProductionCaptureTests('test_first_install_clear_corrupt_and_disabled_save')
        self.harness.setUp();self.addCleanup(self.harness.doCleanups)
        self.store=self.harness.store;self.client=self.harness.client
        record,_=self.store.start_or_resume('合成采用笔记','未分类')
        record.metadata['status']='completed';self.store.active=None
        self.before={'summary':'# 原有人工复习笔记\n\n保留这段原文。','abstract':'原有摘要','source':'human'}
        record.metadata['ai_note_summary']=self.before
        record.segments=[{'id':1,'start':0,'end':12,'raw':'合成原始识别','corrected':'合成校对文字','language':'Chinese'}]
        record.save_all()
        record.audio_path.write_bytes(b'synthetic immutable archived audio')
        # Preserve an existing transcript verbatim, including prior summary and
        # a manual edit; regenerating from structured segments would lose it.
        record.transcript_path.write_text('合成课堂转写\n手动保留文字\n# 原有人工复习笔记\n保留这段原文。\n')
        record.notes_path.write_text('合成课堂手记，不属于新AI复习笔记。\n')
        self.task_id='a'*24;self.candidate={'summary':'# 已审候选复习笔记\n\n新的完整正文。','abstract':'候选摘要','review_package':'.review-notes/'+self.task_id}
        package=record.directory/'.review-notes'/self.task_id;package.mkdir(parents=True)
        (package/'proposed-note.json').write_text(json.dumps(self.candidate,ensure_ascii=False))
        record.metadata['ai_note_proposal']={'task_id':self.task_id};record.save_metadata()
        self.record=record

    def snapshot(self):
        return {str(path.relative_to(self.record.directory)):path.read_bytes()
            for path in self.record.directory.rglob('*') if path.is_file()}

    def test_adoption_preserves_every_source_file_and_captures_old_note_history(self):
        baseline=self.snapshot();route=f'/api/recordings/{self.record.id}/note-proposal/adopt'
        with patch('qwen_asr_app.note_pdf.reviewed_pdf',return_value=Path('review-gate-only-not-a-real-pdf')) as gate:
            response=self.client.post(route,json={'task_id':self.task_id,'expected_summary':self.before['summary']})
        self.assertEqual(response.status_code,200,response.text);gate.assert_called_once()
        self.assertEqual(gate.call_args.args[0].metadata['ai_note_summary'],self.candidate)
        after=self.snapshot();allowed={self.record.meta_path.name,self.record.ai_summary_path.name}
        for relative,original in baseline.items():
            if relative not in allowed:self.assertEqual(after[relative],original,relative)
        saved=self.store.find(self.record.id)
        self.assertEqual(saved.metadata['ai_note_summary'],self.candidate)
        self.assertNotIn('ai_note_proposal',saved.metadata)
        self.assertEqual(saved.ai_summary_path.read_text(),self.candidate['summary'].rstrip()+'\n')
        historical=read_version(saved,version_id(self.before))
        self.assertEqual(historical,self.before)
        self.assertEqual(response.json()['summary'],self.candidate)

    def test_stale_preview_or_different_candidate_stays_409_without_touching_files(self):
        baseline=self.snapshot();route=f'/api/recordings/{self.record.id}/note-proposal/adopt'
        with patch('qwen_asr_app.note_pdf.reviewed_pdf') as gate:
            for payload in ({'task_id':self.task_id,'expected_summary':'stale body'},
                            {'task_id':'b'*24,'expected_summary':self.before['summary']}):
                response=self.client.post(route,json=payload)
                self.assertEqual(response.status_code,409,response.text)
                self.assertEqual(self.snapshot(),baseline)
            gate.assert_not_called()

    def test_rejected_review_gate_retains_proposal_old_body_and_all_source_bytes(self):
        baseline=self.snapshot();route=f'/api/recordings/{self.record.id}/note-proposal/adopt'
        with patch('qwen_asr_app.note_pdf.reviewed_pdf',side_effect=ValueError('synthetic not reviewed')):
            response=self.client.post(route,json={'task_id':self.task_id,'expected_summary':self.before['summary']})
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.snapshot(),baseline)
        self.assertEqual(self.store.find(self.record.id).metadata['ai_note_summary'],self.before)


if __name__=='__main__':unittest.main()
