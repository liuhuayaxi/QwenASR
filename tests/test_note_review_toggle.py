from tests import isolation  # noqa: F401
import hashlib,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from qwen_asr_app.ai_preferences import AIPreferences
from qwen_asr_app.ai_secrets import AISecretStore
from qwen_asr_app.review_notes import upgrade_pdf
from qwen_asr_app.note_pdf import reviewed_pdf


class ReviewSettingsTests(unittest.TestCase):
    def test_default_off_and_only_explicit_saved_boolean_enables_review(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);prefs=AIPreferences(root/'settings.json');secrets=AISecretStore(root/'keys.json')
            settings={'function_providers':{'summary':{'transport':'agy_cli','cli_model':'model'}}}
            prefs.save(settings,secrets)
            self.assertFalse(prefs.task_config({'note_review_enabled':True})['note_review_enabled'])
            prefs.save({'function_providers':{},'note_review_enabled':True},secrets)
            self.assertTrue(AIPreferences(prefs.path).task_config()['note_review_enabled'])
            prefs.save({'function_providers':{},'note_review_enabled':False},secrets)
            self.assertFalse(prefs.task_config()['note_review_enabled'])
            self.assertEqual(prefs.read()['function_providers'],settings['function_providers'])
            with self.assertRaises(ValueError):prefs.save({'function_providers':{},'note_review_enabled':'true'},secrets)


class ReviewToggleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_export_skips_models_preserves_actual_review_and_downloads(self):
        for enabled in (None,False,True):
            with self.subTest(enabled=enabled),tempfile.TemporaryDirectory() as raw:
                root=Path(raw);folder=root/'.review-notes/test';folder.mkdir(parents=True)
                pdf=folder/'candidate.pdf';pdf.write_bytes(b'synthetic pdf');sha=hashlib.sha256(pdf.read_bytes()).hexdigest()
                export={'pdf_sha256':sha,'pages':2,'renderer':'test'}
                (folder/'materials.json').write_text(json.dumps({'relevant_pages':[]}))
                original=json.dumps({'status':'unreviewed','viewed_pages':[1],'issues':['retained evidence']})
                (folder/'review.json').write_text(original)
                metadata={'ai_note_summary':{'summary':'完整正文','review_package':'.review-notes/test'}}
                meta=root/'session.json';meta.write_text(json.dumps(metadata))
                record=SimpleNamespace(directory=root,metadata=metadata,meta_path=meta,
                    save_metadata=lambda:meta.write_text(json.dumps(metadata)))
                config={'ai':{}}
                if enabled is not None:config['note_review_enabled']=enabled
                with patch('qwen_asr_app.review_notes.review_scope.prepare',AsyncMock(return_value={'enabled':False})), \
                     patch('qwen_asr_app.note_latex_author.export_direct',AsyncMock(return_value=((pdf,export),{}))), \
                     patch('qwen_asr_app.review_notes.visual_review',AsyncMock(return_value={'status':'passed','issues':[],'policy':'test'})) as review:
                    result=await upgrade_pdf(record,config)
                self.assertEqual(metadata['ai_note_summary']['summary'],'完整正文')
                if enabled:
                    review.assert_awaited_once();self.assertEqual(result['status'],'passed')
                else:
                    review.assert_not_awaited();self.assertEqual(result['status'],'skipped')
                    self.assertEqual((folder/'review.json').read_text(),original)
                    self.assertFalse(metadata['ai_note_summary']['review_enabled'])
                    with patch('qwen_asr_app.note_pdf.export_pdf',return_value=(pdf,export)):
                        self.assertEqual(reviewed_pdf(record),pdf)
                    with patch('qwen_asr_app.note_pdf.export_pdf',return_value=(pdf,{**export,'pdf_sha256':'changed'})):
                        with self.assertRaisesRegex(ValueError,'已改变'):reviewed_pdf(record)
