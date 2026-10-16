from tests import isolation  # noqa: F401
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from qwen_asr_app.inference_providers import DEFAULTS, InferenceSettings, normalize
from qwen_asr_app.knowledge.settings import embedding_contract, DEFAULTS as KNOWLEDGE
from qwen_asr_app.note_pdf import fingerprint, reviewed_pdf
from qwen_asr_app.review_materials import safe_path, extract_image, bind_markdown_images,lesson_precedes
from qwen_asr_app.review_notes import validate_plan, visual_review, _checkpoint_revision, _resume_revision_checkpoint
from qwen_asr_app.storage import RecordingRecord

spec=importlib.util.spec_from_file_location('classroom_test',Path(__file__).resolve().parents[1]/'ai-infer-node/server/classroom.py')
node=importlib.util.module_from_spec(spec);spec.loader.exec_module(node)


class ReplicaTests(unittest.TestCase):
    def test_course_contract_filters_and_incremental_delete(self):
        with tempfile.TemporaryDirectory() as raw:
            store=node.VectorReplica(Path(raw)/'vectors.db')
            contract={'dimension':2,'revision':'a'}
            def put(course,ident,kind='lecture',lesson='lesson-one',derived=False):
                return store.sync({'course_id':course,'contract':contract,'upsert':[{'id':ident,'version':'v1','vector':[1.,0.],
                    'source_type':kind,'lesson_id':lesson,'derived':derived}]})
            put('physics','p');put('chemistry','c');put('physics','future',lesson='future');put('physics','ai','summary',derived=True)
            result=store.search({'course_id':'physics','contract':contract,'vector':[1.,0.],'lesson_id':'lesson-one'})
            self.assertEqual([m['chunk_id'] for m in result['matches']],['p'])
            self.assertEqual(store.search({'course_id':'physics','contract':{**contract,'revision':'b'},'vector':[1.,0.]})['matches'],[])
            prior=result['index_version']
            store.sync({'course_id':'physics','contract':contract,'delete':['p']})
            self.assertNotEqual(store.version('physics',contract),prior)
            self.assertNotIn('p',store.manifest('physics',contract))
            self.assertIn('c',store.manifest('chemistry',contract))

    def test_rejects_nonfinite_unnormalized_or_wrong_dimension_vectors(self):
        with tempfile.TemporaryDirectory() as raw:
            store=node.VectorReplica(Path(raw)/'db')
            for vector in ([1.], [2.,0.], [float('nan'),0.]):
                with self.subTest(vector=vector),self.assertRaises(ValueError):
                    store.sync({'course_id':'c','contract':{'dimension':2},'upsert':[{'id':'x','version':'1','vector':vector,'source_type':'lecture'}]})
            self.assertEqual(store.manifest('c',{'dimension':2}),{})


class AssetTests(unittest.TestCase):
    def test_history_order_uses_instants_and_never_future_or_unknown_timezone(self):
        current='2026-10-12T09:50:00+08:00'
        self.assertFalse(lesson_precedes('2026-10-12T09:00:00Z',current))
        self.assertFalse(lesson_precedes('2026-10-12T01:50:00Z',current))
        self.assertTrue(lesson_precedes('2026-10-12T01:49:00Z',current))
        self.assertFalse(lesson_precedes('2026-10-11T08:00:00',current))
    def test_unsafe_paths_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as raw,tempfile.TemporaryDirectory() as external:
            root=Path(raw);(root/'link').symlink_to(external,target_is_directory=True)
            for value in ('../a','/etc/passwd','https://x/a','x\\a','link/private.png'):
                with self.subTest(value=value),self.assertRaises(ValueError):safe_path(root,value)

    def test_original_page_crop_provenance_and_hash_guard(self):
        import fitz
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/'sources').mkdir();source=root/'sources/a.pdf'
            doc=fitz.open();page=doc.new_page();page.insert_text((50,50),'Original lesson diagram');doc.save(source);doc.close()
            original_hash=hashlib.sha256(source.read_bytes()).hexdigest()
            package={'course_id':'course-a','sources':[{'source_id':'a','course_id':'course-a','path':'sources/a.pdf','sha256':original_hash,'name':'a.pdf','pages':[{'page':1}]}]}
            image=extract_image(root,package,{'source_id':'a','page':1,'concept':'topic','reason':'labels'})
            self.assertEqual(image['source_sha256'],original_hash);self.assertTrue((root/image['path']).is_file())
            for request in ({'source_id':'other','page':1},{'source_id':'a','page':2},{'source_id':'a','page':1,'bbox':[.9,0,.1,1]}):
                with self.assertRaises(ValueError):extract_image(root,package,request)
            source.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'版本'):extract_image(root,package,{'source_id':'a','page':1})

    def test_fingerprint_tracks_body_assets_material_and_renderer(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/'assets').mkdir();image=root/'assets/x.png';Image.new('RGB',(100,100),'red').save(image)
            md='![diagram](assets/x.png)';before=fingerprint(md,root,{'source':'v1'})
            self.assertNotEqual(before,fingerprint(md+'edited',root,{'source':'v1'}))
            self.assertNotEqual(before,fingerprint(md,root,{'source':'v2'}))
            Image.new('RGB',(100,100),'blue').save(image)
            self.assertNotEqual(before,fingerprint(md,root,{'source':'v1'}))
            image.unlink()
            with self.assertRaisesRegex(ValueError,'不存在'):fingerprint(md,root)

    def test_writer_source_page_locators_bind_only_to_real_inspected_images(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/'assets').mkdir();file=root/'assets/fig-0123456789abcdef.png';file.write_bytes(b'PNG fixture')
            sid='a'*32;image={'source_id':sid,'page':3,'path':'assets/'+file.name,'image_id':file.stem,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
            md=f'![diagram](figures/{sid}_p3.png)\n\n![text slide](figures/{sid}_p4.png)'
            bound=bind_markdown_images(md,root,[image],[{'image':{'source_id':sid,'page':4},'reason':'text-only slide'}])
            self.assertIn('](assets/'+file.name+')',bound);self.assertNotIn('_p4',bound)
            with self.assertRaises(ValueError):bind_markdown_images(f'![made up](figures/{sid}_p99.png)',root,[image])
            with self.assertRaises(ValueError):bind_markdown_images('![escape](../outside.png)',root,[image])

    def test_edit_invalidates_review_and_history_preserves_assets(self):
        with tempfile.TemporaryDirectory() as raw:
            record=RecordingRecord(Path(raw),{'id':'sample-record','ai_note_summary':{'summary':'old','review_status':'passed','pdf_sha256':'oldhash','review_package':'.review-notes/old'}})
            record.save_all();record.update_note_summary('new')
            self.assertEqual(record.metadata['ai_note_summary']['review_status'],'stale')
            self.assertNotIn('pdf_sha256',record.metadata['ai_note_summary'])
            from qwen_asr_app.note_history import versions,read_version
            old=read_version(record,versions(record)[0]['id'])
            self.assertEqual(old['review_package'],'.review-notes/old')

    def test_text_only_history_is_never_silently_downloaded_as_markdown(self):
        with tempfile.TemporaryDirectory() as raw:
            record=RecordingRecord(Path(raw),{'id':'sample-record','ai_note_summary':{'summary':'old'}})
            with self.assertRaisesRegex(ValueError,'独立视觉审查'):reviewed_pdf(record)


class RevisionResumeTests(unittest.TestCase):
    def test_crash_before_task_save_recovers_next_revision_text_and_author(self):
        from qwen_asr_app.durable import atomic_json
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);task={'revision':0,'writer_session_ids':['original-writer']}
            (root/'note.md').write_text('original draft');atomic_json(root/'task.json',task)
            def interrupt_task_save(path,value):
                if path.name=='task.json':raise OSError('simulated process exit before task save')
                return atomic_json(path,value)
            with patch('qwen_asr_app.review_notes.atomic_json',side_effect=interrupt_task_save):
                with self.assertRaises(OSError):_checkpoint_revision(root,task,'original draft','revision one',0,'revision-writer')
            persisted=json.loads((root/'task.json').read_text())
            self.assertEqual(persisted['revision'],0)
            self.assertEqual((root/'note.md').read_text(),'original draft')
            _resume_revision_checkpoint(root,persisted)
            self.assertEqual(persisted['revision'],1)
            self.assertEqual(persisted['writer_session_ids'],['original-writer','revision-writer'])
            self.assertEqual((root/'note.md').read_text(),'revision one')
            self.assertEqual((root/'note-before-revision-0.md').read_text(),'original draft')
            _checkpoint_revision(root,persisted,'revision one','revision two',1,'second-writer')
            again=json.loads((root/'task.json').read_text());_resume_revision_checkpoint(root,again)
            self.assertEqual(again['revision'],2)
            self.assertEqual(again['writer_session_ids'],['original-writer','revision-writer','second-writer'])
            with self.assertRaisesRegex(ValueError,'上限'):_checkpoint_revision(root,again,'revision two','extra revision',2,'extra-writer')
            self.assertEqual((root/'note-before-revision-0.md').read_text(),'original draft')

    def test_crash_before_draft_install_recovers_without_overwriting_changed_evidence(self):
        from qwen_asr_app.durable import atomic_bytes, atomic_json
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);task={'revision':0,'writer_session_ids':['writer']}
            (root/'note.md').write_text('before');atomic_json(root/'task.json',task)
            def interrupt_install(path,value):
                if path.name=='note.md':raise OSError('simulated process exit before draft install')
                return atomic_bytes(path,value)
            with patch('qwen_asr_app.review_notes.atomic_bytes',side_effect=interrupt_install):
                with self.assertRaises(OSError):_checkpoint_revision(root,task,'before','after',0,'reviser')
            persisted=json.loads((root/'task.json').read_text())
            self.assertEqual(persisted['revision'],1)
            _resume_revision_checkpoint(root,persisted)
            self.assertEqual((root/'note.md').read_text(),'after')
            with self.assertRaisesRegex(ValueError,'历史草稿'):_checkpoint_revision(root,persisted,'different before','other after',0)
            self.assertEqual((root/'note-before-revision-0.md').read_text(),'before')

    def test_tampered_revision_checkpoint_is_not_installed(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);task={'revision':0,'writer_session_ids':['writer']}
            (root/'note.md').write_text('before')
            _checkpoint_revision(root,task,'before','after',0,'reviser')
            checkpoint=json.loads((root/'revision-checkpoint.json').read_text());checkpoint['markdown']='changed'
            (root/'revision-checkpoint.json').write_text(json.dumps(checkpoint))
            with self.assertRaisesRegex(ValueError,'校验失败'):_resume_revision_checkpoint(root,task)
            self.assertEqual((root/'note.md').read_text(),'after')


class RoutingTests(unittest.TestCase):
    def test_windows_worker_contract_matches_mac_and_rejects_semantic_drift(self):
        spec=importlib.util.spec_from_file_location('classroom_worker_test',Path(__file__).resolve().parents[1]/'ai-infer-node/workers/classroom_worker.py')
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        contract=embedding_contract(KNOWLEDGE);worker.verify_contract(contract)
        for key,value in [('revision','old'),('dimension',3),('pooling','mean'),('query_instruction','other'),('compute_dtype','float32')]:
            with self.subTest(key=key),self.assertRaises(ValueError):worker.verify_contract({**contract,key:value})
        from qwen_asr_app.knowledge.reranking import CONTRACT
        self.assertEqual(worker.RERANK_CONTRACT,CONTRACT)
    def test_power_saving_enforces_both_remote_only_and_snapshot_is_immutable(self):
        with tempfile.TemporaryDirectory() as raw:
            settings=InferenceSettings(Path(raw)/'routing.json')
            settings.save({**DEFAULTS,'classroom_power_saving':True})
            self.assertEqual(settings.for_task('asr')['asr_provider'],'remote_only')
            self.assertEqual(settings.for_task('knowledge')['knowledge_provider'],'remote_only')
            snapshot=settings.for_task('asr')
            settings.save({**DEFAULTS,'asr_provider':'local'})
            self.assertEqual(settings.for_task('asr',snapshot)['asr_provider'],'remote_only')

    def test_new_upload_cannot_use_stale_browser_local_override_in_power_mode(self):
        from qwen_asr_app.upload_jobs import UploadJobs
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);settings=InferenceSettings(root/'routing.json');settings.save({**DEFAULTS,'classroom_power_saving':True})
            jobs=UploadJobs(None,None,lambda value:value,lambda _:None)
            jobs.root=root/'uploads';jobs.path=jobs.root/'jobs.json';jobs.schedule=lambda *args:None
            source=root/'audio.wav';source.write_bytes(b'preserved audio')
            with patch('qwen_asr_app.inference_providers.inference_settings',settings):
                job=jobs.create(source,'audio.wav',{'inference_settings':{'asr_provider':'local','mineru_provider':'local'}})
            self.assertEqual(job['config']['inference_settings']['asr_provider'],'remote_only')
            self.assertTrue(job['config']['inference_settings']['classroom_power_saving'])

    def test_replay_resume_uses_original_provider_snapshot(self):
        from qwen_asr_app.course_stream import CourseStreamJob
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);record=RecordingRecord(root,{'id':'sample-course','course_stream':{'chunks':[],
                'inference_settings':{**DEFAULTS,'asr_provider':'remote_only'}}})
            job=CourseStreamJob(record,{'inference_settings':{'asr_provider':'local'}},None,set())
            self.assertEqual(job.config['inference_settings']['asr_provider'],'remote_only')

    def test_power_saving_never_constructs_local_embedder(self):
        from qwen_asr_app.knowledge.embeddings import get_embedder,close_embedder
        with patch('qwen_asr_app.inference_providers.inference_settings.for_task',return_value={**DEFAULTS,'knowledge_provider':'remote_only'}),patch('qwen_asr_app.knowledge.embeddings.LocalEmbedder') as local:
            close_embedder();embedder=get_embedder();self.assertTrue(embedder.remote);local.assert_not_called();close_embedder()

    def test_knowledge_plans_cannot_invent_source_or_cross_course_page(self):
        package={'sources':[{'source_id':'a','pages':[{}]}]}
        for plan in ({'knowledge':[{'kind':'first_introduction','sources':['S99']}]},
                     {'knowledge':[],'figures':[{'source_id':'foreign','page':1}]},
                     {'knowledge':[],'figures':[{'source_id':'a','page':2}]}):
            with self.assertRaises(ValueError):validate_plan(plan,{'S1'},package)


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_settings_wait_for_audio_boundary_and_keep_old_clip_snapshot(self):
        from qwen_asr_app.live_session import TranscriptionSession
        from types import SimpleNamespace
        session=TranscriptionSession(SimpleNamespace(),None,None)
        original={**DEFAULTS,'asr_provider':'remote_only'}
        session.config['inference_settings']=original.copy();session.speaking=True;session.chunks=[b'audio']
        saved={**DEFAULTS,'asr_provider':'local'}
        with patch('qwen_asr_app.inference_providers.inference_settings.for_task',return_value=saved):
            self.assertTrue(session.update_config({'inference_settings':original}))
        self.assertEqual(session.config['inference_settings']['asr_provider'],'remote_only')
        session.apply_pending_config()
        self.assertEqual(session.config['inference_settings']['asr_provider'],'local')
        self.assertEqual(original['asr_provider'],'remote_only')

    async def test_course_replay_passes_remote_snapshot_and_persistent_operation_dir(self):
        from qwen_asr_app.course_stream import CourseStreamJob
        from tests.test_course_stream import make_record
        from types import SimpleNamespace
        import numpy as np
        with tempfile.TemporaryDirectory() as raw:
            record=make_record(Path(raw));model=SimpleNamespace(active_jobs=0,cancel_unload=lambda:None,schedule_unload=lambda:None,
                transcribe=AsyncMock(return_value=('Windows replay text','Chinese',.1)))
            routing={**DEFAULTS,'asr_provider':'remote_only'}
            job=CourseStreamJob(record,{'inference_settings':routing},model,set());job.start()
            await job.accept(0,(np.ones(16000,dtype='<i2')*1000).tobytes(),{'source':0,'samples':16000});await job.finish_input(1)
            await asyncio.wait_for(job.task,5)
            arguments=model.transcribe.await_args.kwargs
            self.assertEqual(arguments['inference_config'],routing)
            self.assertTrue(arguments['operation_dir'].is_relative_to(record.directory/'.course-operations'))
            self.assertEqual(record.metadata['status'],'completed')

    async def test_agy_vision_requires_actual_tool_read_and_fresh_cli_invocation(self):
        from qwen_asr_app.agy_materials import request_materials
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve();picture=root/'page.png';picture.write_bytes(b'image-fixture')
            provider={'transport':'agy_cli','context_window':256000,'cli_model':'gemini-3.8-flash-high'}
            def process(events):
                stdout=asyncio.StreamReader();stdout.feed_data(('\n'.join(json.dumps(e) for e in events)+'\n').encode());stdout.feed_eof()
                stderr=asyncio.StreamReader();stderr.feed_eof()
                return SimpleNamespace(stdout=stdout,stderr=stderr,stdin=SimpleNamespace(write=lambda _:None,drain=AsyncMock(),close=lambda:None),wait=AsyncMock(),returncode=0)
            result={'event':'result','result':{'status':'SUCCESS','response':'{"viewed":true}','conversation_id':'fresh-review'}}
            init={'event':'init','conversation_id':'fresh-review'}
            read={'event':'step_update','step_update':{'state':'DONE','step_type':'tool','tool_info':{'name':'view_file','parameters':{'AbsolutePath':str(picture)}}}}
            with patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'),patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process([init,read,result])) as launch:
                text,receipt=await request_materials('Inspect image',root,provider,read_paths=[picture],require_images=[picture])
                args=launch.call_args.args
                self.assertNotIn('--continue',args);self.assertNotIn('--conversation',args);self.assertNotIn('--dangerously-skip-permissions',args)
                self.assertIn('--sandbox',args);self.assertEqual(receipt['viewed_files'],[str(picture)]);self.assertEqual(receipt['sandbox_version'],2)
            structured={'status':'needs_revision','viewed_pages':[1],'issues':[{'page':1,'severity':'blocking','problem':'formula','evidence':'actual image','suggestion':'fix'}]}
            result['result'].update(response='{"status":"passed"}\n{"second":"finish text"}',structured_output=structured)
            with patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'),patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process([init,read,result])) as launch:
                text,receipt=await request_materials('Inspect image',root,provider,read_paths=[picture],require_images=[picture],schema={'type':'object'})
                self.assertEqual(json.loads(text),structured)
                self.assertTrue(receipt['structured_output_used']);self.assertIn('--json-schema',launch.call_args.args)
            with patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'),patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process([init,result])):
                with self.assertRaisesRegex(RuntimeError,'实际读取'):await request_materials('Claim image seen',root,provider,read_paths=[picture],require_images=[picture])
            read['step_update']['tool_info']['output']='Permission denied'
            with patch('qwen_asr_app.agy_materials.executable',return_value='/usr/bin/true'),patch('qwen_asr_app.agy_materials.asyncio.create_subprocess_exec',return_value=process([init,read,result])):
                with self.assertRaisesRegex(RuntimeError,'未能实际读取'):await request_materials('Read failed',root,provider,read_paths=[picture],require_images=[picture])

    async def test_reviewed_download_response_has_pdf_mime_and_filename(self):
        import app
        import fitz
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);pdf=root/'note.pdf';doc=fitz.open();doc.new_page().insert_text((40,40),'PDF');doc.save(pdf);doc.close()
            record=RecordingRecord(root,{'id':'pdf-download-record','status':'completed','title':'复习笔记'})
            with patch.object(app,'recording_store',SimpleNamespace(find=lambda _:record)),patch('qwen_asr_app.note_pdf.reviewed_pdf',return_value=pdf):
                response=await app.download_recording(record.id,'ai-summary')
            self.assertEqual(response.media_type,'application/pdf');self.assertTrue(response.filename.endswith('.pdf'))
            self.assertIn('attachment;',response.headers['content-disposition']);self.assertEqual(response.headers['cache-control'],'no-store')

    async def test_all_pdf_pages_need_real_image_reads_and_fresh_session(self):
        import fitz
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);pdf=root/'candidate.pdf';doc=fitz.open()
            for i in range(5):doc.new_page().insert_text((50,50),f'Page {i+1}')
            doc.save(pdf);doc.close()
            calls=[]
            async def model(prompt,folder,provider,**kwargs):
                self.assertLessEqual(len(kwargs['require_images']),4)
                value=json.loads(prompt.rsplit('\n',1)[-1]);nums=value['pages'];calls.extend(nums)
                required=[int(p.stem.split('-')[-1]) for p in kwargs['require_images']]
                self.assertEqual(required,nums+[p['page'] for p in value['context_pages']])
                self.assertTrue(all(p.is_file() and p.read_bytes().startswith(b'\x89PNG') for p in kwargs['require_images']))
                return json.dumps({'status':'passed','issues':[],'viewed_pages':nums}),{'session_id':'review-'+str(nums[0]),'viewed_files':[str(p) for p in kwargs['require_images']]}
            export={'pages':5,'pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),'renderer':'test'}
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=model):
                report=await visual_review(pdf,export,root,{},[],[],generator_session_ids=['writer'])
            self.assertEqual(calls,[1,2,3,4,5]);self.assertEqual(report['status'],'passed');self.assertEqual(report['pdf_sha256'],export['pdf_sha256'])
            (root/'review.json').unlink()
            with patch('qwen_asr_app.review_notes.request_materials',return_value=(json.dumps({'status':'passed','issues':[],'viewed_pages':[1,2,3,4]}),{'session_id':'writer'})):
                with self.assertRaisesRegex(ValueError,'相同'):await visual_review(pdf,export,root,{},[],[],generator_session_ids=['writer'])

    async def test_reexported_pdf_does_not_reuse_previous_pdf_page_images(self):
        import fitz
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);pdf=root/'candidate.pdf';observed=[]
            async def model(prompt,folder,provider,**kwargs):
                page=kwargs['require_images'][0]
                observed.append((page,hashlib.sha256(page.read_bytes()).hexdigest()))
                return json.dumps({'status':'passed','issues':[],'viewed_pages':[1]}),{'session_id':'review-'+str(len(observed))}
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=model):
                for content in ('first exported document','DIFFERENT regenerated layout and text'):
                    doc=fitz.open();doc.new_page().insert_text((50,50),content)
                    pdf.unlink(missing_ok=True);doc.save(pdf);doc.close()
                    export={'pages':1,'pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),'renderer':'test'}
                    report=await visual_review(pdf,export,root,{},[],[])
                    self.assertEqual(report['pdf_sha256'],export['pdf_sha256'])
                    self.assertEqual(observed[-1][0].parent.name,export['pdf_sha256'])
                # An unchanged, completed review still reuses its actual receipt.
                await visual_review(pdf,export,root,{},[],[])
            self.assertEqual(len(observed),2)
            self.assertNotEqual(observed[0][0],observed[1][0])
            self.assertNotEqual(observed[0][1],observed[1][1])

    async def test_adjacent_figure_context_uses_actual_cached_final_page_with_four_image_limit(self):
        import fitz
        from PIL import Image
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/'assets').mkdir();figures=[]
            for ident,color,page in [('red-figure','red',1),('blue-figure','blue',2)]:
                image=root/'assets'/(ident+'.png');Image.new('RGB',(80,40),color).save(image)
                figures.append({'image_id':ident,'path':'assets/'+image.name,'page':page,'concept':'core diagram','reason':'explanation'})
            (root/'images.json').write_text(json.dumps(figures))
            pdf=root/'candidate.pdf';doc=fitz.open()
            for i in range(4):doc.new_page().insert_text((50,50),f'Page {i+1} core diagram explanation')
            doc[1].insert_image(fitz.Rect(50,100,210,180),filename=str(root/figures[0]['path']))
            doc[3].insert_image(fitz.Rect(50,100,210,180),filename=str(root/figures[1]['path']))
            doc.save(pdf);doc.close()
            export={'pages':4,'pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),'renderer':'test'}
            calls=[];cached_page=None;cached_hash=None
            async def model(prompt,folder,provider,**kwargs):
                nonlocal cached_page,cached_hash
                value=json.loads(prompt.rsplit('\n',1)[-1]);required=kwargs['require_images'];calls.append(value)
                self.assertLessEqual(len(required),4)
                self.assertEqual(value['figure_pdf_locations'],{'red-figure':[2],'blue-figure':[4]})
                self.assertTrue(all(path.is_file() and path.read_bytes().startswith(b'\x89PNG') for path in required))
                if value['pages']==[1,2]:
                    self.assertEqual([p['page'] for p in value['context_pages']],[3])
                    self.assertEqual(value['context_pages'][0]['figure_ids'],[])
                    self.assertEqual(required[2],Path(value['context_pages'][0]['path']))
                    cached_page=required[1];cached_hash=hashlib.sha256(cached_page.read_bytes()).hexdigest()
                    self.assertEqual(len(value['original_figures']),1)
                    self.assertEqual(len(required),4)
                else:
                    self.assertEqual(value['pages'],[3,4])
                    self.assertEqual([p['page'] for p in value['context_pages']],[2])
                    self.assertEqual(value['context_pages'][0]['figure_ids'],['red-figure'])
                    self.assertEqual(required[2],cached_page)
                    self.assertEqual(cached_page.parent.name,export['pdf_sha256'])
                    self.assertEqual(hashlib.sha256(cached_page.read_bytes()).hexdigest(),cached_hash)
                    self.assertEqual(len(value['original_figures']),1)
                    self.assertEqual(len(required),4)
                return json.dumps({'status':'passed','issues':[],'viewed_pages':value['pages']}),{'session_id':'review-'+str(value['pages'][0])}
            with patch('qwen_asr_app.review_notes.request_materials',side_effect=model):
                report=await visual_review(pdf,export,root,{},[],[])
            self.assertEqual(len(calls),2)
            self.assertEqual(report['viewed_pages'],[1,2,3,4])
            self.assertEqual(report['batches'][1]['result']['viewed_pages'],[3,4])
            self.assertEqual(report['batches'][1]['context_pages'],[2])


if __name__=='__main__':unittest.main()
