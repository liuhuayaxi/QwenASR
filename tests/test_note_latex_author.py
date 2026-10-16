from tests import isolation
import asyncio,json,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from qwen_asr_app import note_latex_author as author

class DirectLatexAuthorTests(unittest.IsolatedAsyncioTestCase):
    def source_key(self,markdown='ref',provenance=None):
        import hashlib
        binding=author._binding(markdown,provenance or {})
        return hashlib.sha256(json.dumps([author.VERSION,binding,None],sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]

    async def test_source_cache_binds_content_and_provenance(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);(folder/'transcript.json').write_text('{"blocks":[]}')
            body='% knowledge:k1\n正文 $x=1$。\n% /knowledge:k1'
            config={'ai':{}};ledger={'knowledge':[{'id':'k1'}]};package={}
            with patch.object(author,'request_ai_text',AsyncMock(return_value=body)) as request,patch('qwen_asr_app.note_latex.prepare_document',return_value={'template_sha256':'template'}),patch.object(author,'context_window_for',return_value=256000):
                one=await author.ensure_source('reference',folder,{'v':1},config,ledger,package,[])
                two=await author.ensure_source('reference',folder,{'v':1},config,ledger,package,[])
                self.assertEqual(one,two);self.assertEqual(request.await_count,1)
                await author.ensure_source('edited',folder,{'v':1},config,ledger,package,[])
                await author.ensure_source('edited',folder,{'v':2},config,ledger,package,[])
                self.assertEqual(request.await_count,3)

    async def test_cached_equation_name_normalizes_without_another_ai_request(self):
        import hashlib
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);destination=folder/'latex';destination.mkdir()
            (folder/'transcript.json').write_text('{"blocks":[]}')
            body='\\CourseTitle{课程}{测试}\n% knowledge:k\nSchr\\"odinger 方程\n% /knowledge:k'
            original=hashlib.sha256(body.encode()).hexdigest()
            saved={'binding':author._binding('ref',{}),'body_sha256':original,'writer_session_id':'original-writer'}
            (destination/'body.tex').write_text(body);(destination/'source.json').write_text(json.dumps(saved))
            with patch.object(author,'request_ai_text',AsyncMock()) as request:
                manifest=await author.ensure_source('ref',folder,{}, {},{'knowledge':[{'id':'k'}]}, {},[])
            request.assert_not_awaited()
            self.assertIn('Schrodinger',(destination/'body.tex').read_text())
            self.assertEqual((destination/('body-before-plain-names-'+original+'.tex')).read_text(),body)
            self.assertEqual(manifest['previous_body_sha256'],original)
            self.assertEqual(manifest['writer_session_id'],'original-writer')

    async def test_single_batch_exhausted_legacy_source_is_revalidated_without_new_requests(self):
        import hashlib
        for course in ('光纤传输','汇编指令'):
            with self.subTest(course=course),tempfile.TemporaryDirectory() as raw:
                folder=Path(raw);destination=folder/'latex';destination.mkdir()
                (folder/'transcript.json').write_text('{"blocks":[]}')
                key=self.source_key();body='% knowledge:k\n'+course+r'\begin{table}[htbp]\caption{比较}\begin{tabular}{ll}A & B\\\end{tabular}\end{table}'+'\n% /knowledge:k'
                originals={}
                for number in (1,2,3):
                    path=destination/f'{key}-attempt-{number}.tex';path.write_text(body);originals[path]=path.read_bytes()
                    path.with_suffix('.receipt.json').write_text(json.dumps({'body_sha256':hashlib.sha256(body.encode()).hexdigest(),'writer_session_id':f'writer-{number}'}))
                (destination/f'attempts-{key}.json').write_text(json.dumps({'attempts':3,'errors':['table']}))
                with patch.object(author,'request_ai_text',AsyncMock()) as request:
                    await author.ensure_source('ref',folder,{}, {},{'knowledge':[{'id':'k'}]}, {},[])
                request.assert_not_awaited()
                self.assertIn(r'\captionof{table}',(destination/'body.tex').read_text())
                self.assertTrue(all(path.read_bytes()==data for path,data in originals.items()))

    async def test_invalid_knowledge_proposals_are_bounded_and_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);(folder/'transcript.json').write_text('{"blocks":[]}')
            ledger={'knowledge':[{'id':'required'}]}
            with patch.object(author,'request_ai_text',AsyncMock(return_value='缺少来源知识块')) as request,patch.object(author,'context_window_for',return_value=256000):
                with self.assertRaisesRegex(ValueError,'有限修订'):
                    await author.ensure_source('ref',folder,{}, {},ledger,{},[])
                self.assertEqual(request.await_count,3)
                self.assertEqual(len(list((folder/'latex').glob('*-attempt-*.tex'))),3)
                with self.assertRaisesRegex(ValueError,'有限修订'):
                    await author.ensure_source('ref',folder,{}, {},ledger,{},[])
                self.assertEqual(request.await_count,3)

    async def test_legacy_interrupted_requests_do_not_spend_returned_proposal_budget(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);destination=folder/'latex';destination.mkdir()
            (folder/'transcript.json').write_text('{"blocks":[]}')
            key=self.source_key();bad='% knowledge:first\n第一项。\n% /knowledge:first'
            old_source=destination/(key+'-attempt-2.tex');old_source.write_text(bad)
            old_receipt=destination/(key+'-attempt-2.receipt.json');old_receipt.write_text('{"writer_session_id":"old-writer"}')
            state_path=destination/('attempts-'+key+'.json')
            state_path.write_text(json.dumps({'attempts':3,'errors':['旧版覆盖校验失败']}))
            ledger={'knowledge':[{'id':'first'},{'id':'last'}]}
            good=bad+'\n% knowledge:last\n最后一项。\n% /knowledge:last'
            with patch.object(author,'request_ai_text',AsyncMock(return_value=good)) as request,patch('qwen_asr_app.note_latex.prepare_document',return_value={'template_sha256':'template'}),patch.object(author,'context_window_for',return_value=256000):
                await author.ensure_source('ref',folder,{}, {},ledger,{},[])
            self.assertEqual(request.await_count,1)
            payload=request.await_args.kwargs['payload']
            self.assertEqual(payload['previous_latex'],bad)
            self.assertEqual(payload['required_knowledge_ids'],['first','last'])
            self.assertTrue(any('缺少开始ID: last' in error for error in payload['validation_errors']))
            state=json.loads(state_path.read_text())
            self.assertEqual(state['legacy_request_attempts'],3)
            self.assertEqual(state['completed_proposals'],2)
            self.assertEqual(state['attempts'],4)
            self.assertEqual(old_source.read_text(),bad)
            self.assertEqual(json.loads(old_receipt.read_text())['writer_session_id'],'old-writer')
            self.assertTrue((destination/(key+'-attempt-4.tex')).exists())

    async def test_transport_and_cancellation_are_recorded_without_spending_proposals(self):
        for error in (TimeoutError('provider timed out'),asyncio.CancelledError('paused')):
            with self.subTest(error=type(error).__name__),tempfile.TemporaryDirectory() as raw:
                folder=Path(raw);(folder/'transcript.json').write_text('{"blocks":[]}')
                body='% knowledge:k\n正文。\n% /knowledge:k'
                ledger={'knowledge':[{'id':'k'}]}
                with patch.object(author,'request_ai_text',AsyncMock(side_effect=[error,body])) as request,patch('qwen_asr_app.note_latex.prepare_document',return_value={'template_sha256':'template'}),patch.object(author,'context_window_for',return_value=256000):
                    with self.assertRaises(type(error)):
                        await author.ensure_source('ref',folder,{}, {},ledger,{},[])
                    state_path=next((folder/'latex').glob('attempts-*.json'))
                    state=json.loads(state_path.read_text())
                    self.assertEqual(state['completed_proposals'],0)
                    self.assertEqual(state['request_errors'][0]['type'],type(error).__name__)
                    await author.ensure_source('ref',folder,{}, {},ledger,{},[])
                    self.assertEqual(request.await_count,2)
                    state=json.loads(state_path.read_text())
                    self.assertEqual(state['completed_proposals'],1)
                    self.assertEqual(state['attempts'],2)

    async def test_orphan_source_counts_and_three_invalid_legacy_proposals_stay_exhausted(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);destination=folder/'latex';destination.mkdir()
            (folder/'transcript.json').write_text('{"blocks":[]}')
            key=self.source_key();state_path=destination/('attempts-'+key+'.json')
            state_path.write_text(json.dumps({'attempts':7,'errors':['真实失败']}))
            originals={}
            for number in (1,3,6):
                path=destination/(key+'-attempt-'+str(number)+'.tex')
                path.write_text('缺少知识注释 '+str(number));originals[path.name]=path.read_bytes()
            # The response files deliberately have no receipt. A crash while
            # saving a receipt must not reopen the finite validation budget.
            with patch.object(author,'request_ai_text',AsyncMock()) as request,patch.object(author,'context_window_for',return_value=256000):
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError,'有限修订'):
                        await author.ensure_source('ref',folder,{}, {},{'knowledge':[{'id':'required'}]}, {},[])
                self.assertEqual(request.await_count,0)
            self.assertEqual(json.loads(state_path.read_text())['completed_proposals'],3)
            self.assertEqual({path.name:path.read_bytes() for path in destination.glob('*-attempt-*.tex')},originals)

    def test_coverage_diagnostics_identify_missing_duplicate_and_unknown_ids(self):
        body='% knowledge:a\nA\n% /knowledge:a\n% knowledge:a\nA\n% /knowledge:a\n% knowledge:fake\nF\n% /knowledge:fake'
        with self.assertRaises(ValueError) as raised:
            author.validate_coverage(body,{'knowledge':[{'id':'a'},{'id':'b'}]},{})
        text=str(raised.exception)
        self.assertIn('缺少开始ID: b',text)
        self.assertIn('缺少结束ID: b',text)
        self.assertIn('重复开始ID: a',text)
        self.assertIn('未知开始ID: fake',text)

    async def test_dependency_error_does_not_rewrite_note_or_retry_ai(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(author,'ensure_source',AsyncMock(return_value={'body_sha256':'a'})) as source,patch('qwen_asr_app.note_pdf.export_pdf',side_effect=RuntimeError('缺少编译组件')):
                with self.assertRaisesRegex(RuntimeError,'缺少'):
                    await author.export_direct('ref',Path(raw),{}, {},{}, {},[])
                self.assertEqual(source.await_count,1)

    def test_scope_and_knowledge_check_use_tex_body(self):
        ledger={'knowledge':[{'id':'f','scope_level':'familiarity'}]}
        author.validate_coverage('% knowledge:f\n仅简述 $\\Gamma$。\n% /knowledge:f',ledger,{'enabled':True})
        with self.assertRaisesRegex(ValueError,'独立公式'):
            author.validate_coverage('% knowledge:f\n\\[x=1\\]\n% /knowledge:f',ledger,{'enabled':True})

    def test_layout_rewrite_cannot_drop_canonical_source_times(self):
        source='% knowledge:k\n\\TimeRange{invented time}\n说明。\n% /knowledge:k'
        ledger={'knowledge':[{'id':'k','sources':['S3','S4','S8']}]}
        result=author.bind_knowledge_sources(source,ledger)
        self.assertNotIn('invented time',result)
        self.assertIn(r'\SourceRef{3}',result);self.assertIn(r'\SourceRef{8}',result)
        self.assertEqual(result,author.bind_knowledge_sources(result,ledger))

    def test_material_supplement_does_not_acquire_a_false_lecture_citation(self):
        source='% knowledge:s\n课件第25页补充。\\SourceRef{92}\n% /knowledge:s'
        ledger={'knowledge':[{'id':'s','sources':['S92'],'kind':'material_supplement'}]}
        result=author.bind_knowledge_sources(source,ledger)
        self.assertNotIn(r'\SourceRef',result)
        self.assertIn('课件第25页补充',result)

    async def test_single_source_normalizes_box_title_before_validation_and_saving(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);(folder/'transcript.json').write_text('{"blocks":[]}')
            source='% knowledge:k\n\\begin{FormulaBox}\n\\[x=1\\]\n\\end{FormulaBox}\n% /knowledge:k'
            with patch.object(author,'request_ai_text',AsyncMock(return_value=source)) as request,patch.object(author,'context_window_for',return_value=256000):
                await author.ensure_source('ref',folder,{}, {},{'knowledge':[{'id':'k'}]}, {},[])
            self.assertEqual(request.await_count,1)
            self.assertIn('\\begin{FormulaBox}{关键关系}',(folder/'latex'/'body.tex').read_text())
            self.assertEqual(next((folder/'latex').glob('*-attempt-*.tex')).read_text(),source)


class BatchedLatexAuthorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session_token=author.last_session_id.set('test-batch-writer')

    async def asyncTearDown(self):
        author.last_session_id.reset(self.session_token)

    def fixture(self,raw,count=23):
        folder=Path(raw);(folder/'assets').mkdir()
        ledger={'knowledge':[{'id':'k'+str(index),'topic':'概念'+str(index),
            'sources':['S'+str(index+1)],'explanation_needed':'保留完整讲解'+str(index)}
            for index in range(count)],'warnings':['全部知识都要保留']}
        transcript={'blocks':[{'start':index*5,'end':index*5+4,'text':'完整原文'+str(index)} for index in range(count)]}
        (folder/'transcript.json').write_text(json.dumps(transcript))
        images=[]
        for index in (0,12,count-1):
            if index>=count:continue
            path='assets/fig-'+format(index,'016x')+'.png';(folder/path).write_bytes(b'approved image '+str(index).encode())
            images.append({'path':path,'knowledge_id':'k'+str(index),'source_id':'original-ppt','page':index+1})
        pages=[{'page':index+1,'text':'原课件页面证据'+str(index+1)} for index in range(count)]
        package={'sources':[{'source_id':'original-ppt','name':'原课件','sha256':'approved-source','pages':pages}],
            'relevant_pages':[dict(pages[6],source_id='original-ppt')],
            'associations':[{'source':'S'+str(index+1),'pages':[{'source_id':'original-ppt','page':index+1}]} for index in range(count)],
            'history_scope':'真实历史范围'}
        return folder,ledger,images,package

    def proposal(self,payload):
        parts=[]
        for item in payload['coverage']['knowledge']:
            ident=str(item['id']);parts.append('% knowledge:'+ident+'\n\\subsection{'+item['topic']+'}\n'+item['explanation_needed']+'。')
            for image in payload['images']:
                if image['assigned_knowledge_id']==ident:
                    parts.append('\\CourseFigure{'+image['path']+'}{原课件第'+str(image['page'])+'页}{原图读图说明}')
            parts.append('% /knowledge:'+ident)
        return '\n'.join(parts)

    async def test_batches_keep_full_evidence_order_sources_and_each_approved_figure_once(self):
        with tempfile.TemporaryDirectory() as raw:
            folder,ledger,images,package=self.fixture(raw)
            reference='# 完整参考稿\n这是所有批次共同保留的整节讲解。'
            payloads=[]
            async def respond(**kwargs):
                payloads.append(kwargs['payload']);return self.proposal(kwargs['payload'])
            with patch.object(author,'request_ai_text',AsyncMock(side_effect=respond)) as request,patch.object(author,'context_window_for',return_value=256000):
                manifest=await author.ensure_source(reference,folder,{'source':'original'}, {},ledger,package,images)
                repeated=await author.ensure_source(reference,folder,{'source':'original'}, {},ledger,package,images)
            self.assertEqual(manifest,repeated);self.assertEqual(request.await_count,2)
            self.assertEqual(manifest['version'],author.BATCH_VERSION)
            self.assertEqual([len(payload['required_knowledge_ids']) for payload in payloads],[21,2])
            for payload in payloads:
                self.assertEqual(payload['content_reference'],reference)
                self.assertEqual([item['id'] for item in payload['complete_coverage_context']],[item['id'] for item in ledger['knowledge']])
                self.assertTrue(all('explanation_needed' not in item for item in payload['complete_coverage_context']))
                self.assertTrue(payload['source_pages'])
                self.assertEqual(payload['history_scope'],package['history_scope'])
            body=(folder/'latex'/'body.tex').read_text()
            self.assertEqual(body.count('\\CourseTitle{'),1)
            self.assertEqual(__import__('re').findall(r'^% knowledge:(\S+)$',body,__import__('re').M),[item['id'] for item in ledger['knowledge']])
            for image in images:self.assertEqual(body.count('\\CourseFigure{'+image['path']+'}'),1)
            for index in range(23):self.assertIn('\\SourceRef{'+str(index+1)+'}',body)
            from qwen_asr_app.note_latex import prepare_document
            self.assertEqual(len(prepare_document(body,folder,images=images)['assets']),3)
            progress=json.loads((folder/'latex'/'batch-progress.json').read_text())
            self.assertEqual(progress['total_batches'],2);self.assertEqual(progress['completed_batches'],2)
            self.assertEqual(progress['state'],'completed')

    async def test_interrupted_batch_resumes_without_rewriting_accepted_batches(self):
        with tempfile.TemporaryDirectory() as raw:
            folder,ledger,images,package=self.fixture(raw);calls=0
            async def respond(**kwargs):
                nonlocal calls
                calls+=1
                if calls==2:raise asyncio.CancelledError('transport interrupted')
                return self.proposal(kwargs['payload'])
            with patch.object(author,'request_ai_text',AsyncMock(side_effect=respond)) as request,patch.object(author,'context_window_for',return_value=256000):
                with self.assertRaises(asyncio.CancelledError):
                    await author.ensure_source('ref',folder,{}, {},ledger,package,images)
                batch_root=next((folder/'latex'/'batches').iterdir())
                accepted=batch_root/'batch-001'/'body.tex';before=accepted.read_bytes()
                progress=json.loads((folder/'latex'/'batch-progress.json').read_text())
                self.assertEqual(progress['completed_batches'],1);self.assertEqual(progress['current_batch'],2)
                manifest=await author.ensure_source('ref',folder,{}, {},ledger,package,images)
                self.assertEqual(request.await_count,3)
                self.assertEqual(accepted.read_bytes(),before)
                self.assertEqual(manifest['batch_count'],2)
                state=json.loads((batch_root/'batch-002'/'attempts-source.json').read_text())
                self.assertEqual(state['attempts'],2);self.assertEqual(state['completed_proposals'],1)
                self.assertEqual(state['request_errors'][0]['type'],'CancelledError')

    async def test_batch_three_complete_failures_stay_exhausted_and_legacy_attempts_immutable(self):
        with tempfile.TemporaryDirectory() as raw:
            folder,ledger,images,package=self.fixture(raw);destination=folder/'latex';destination.mkdir()
            legacy=destination/'attempts-old-full-document.json';legacy.write_text('{"attempts":3,"errors":["old"]}')
            old_source=destination/'old-attempt-3.tex';old_source.write_text('历史失败源稿')
            before={path.name:path.read_bytes() for path in destination.iterdir()}
            with patch.object(author,'request_ai_text',AsyncMock(return_value='不完整的整份输出')) as request,patch.object(author,'context_window_for',return_value=256000):
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError,'三份完整源稿'):
                        await author.ensure_source('ref',folder,{}, {},ledger,package,images)
                self.assertEqual(request.await_count,3)
            self.assertEqual({name:(destination/name).read_bytes() for name in before},before)
            batch_root=next((destination/'batches').iterdir())
            self.assertEqual(len(list((batch_root/'batch-001').glob('source-attempt-*.tex'))),3)
            self.assertFalse((batch_root/'batch-002').exists())

    async def test_exhausted_table_proposal_recovers_without_new_ai_or_overwriting_sources(self):
        with tempfile.TemporaryDirectory() as raw:
            folder,ledger,images,package=self.fixture(raw)
            async def respond(**kwargs):
                body=self.proposal(kwargs['payload']);ident=kwargs['payload']['required_knowledge_ids'][0]
                table=r'\begin{table}[htbp]\caption{原表题}\begin{tabular}{ll}A & B\\\end{tabular}\end{table}'
                return body.replace('% /knowledge:'+ident,table+'\n% /knowledge:'+ident,1)
            with patch.object(author,'request_ai_text',AsyncMock(side_effect=respond)) as request,patch.object(author,'context_window_for',return_value=256000):
                with patch('qwen_asr_app.note_latex.normalize_table_floats',side_effect=lambda body:body):
                    with self.assertRaisesRegex(ValueError,'三份完整源稿'):
                        await author.ensure_source('ref',folder,{}, {},ledger,package,images)
                sources={path:path.read_bytes() for path in (folder/'latex').rglob('source-attempt-*.tex')}
                self.assertEqual(request.await_count,3)
                await author.ensure_source('ref',folder,{}, {},ledger,package,images)
                self.assertEqual(request.await_count,4)  # Only the previously unstarted second batch.
                self.assertTrue(all(path.read_bytes()==value for path,value in sources.items()))
                self.assertIn(r'\captionof{table}{原表题}',(folder/'latex/body.tex').read_text())

    def test_figure_placement_uses_evidence_and_respects_depth(self):
        ledger=[{'id':'early','topic':'光纤衰减','sources':['S1']},
            {'id':'later','topic':'四波混频 FWM','sources':['S9']},
            {'id':'brief','topic':'四波混频 FWM','sources':['S9'],'scope_level':'familiarity'}]
        figures=[{'path':'assets/fig-0000000000000000.png','concept':'FWM频率图','reason':'课堂S9讲解'}]
        batches,owners=author._figure_batches(ledger,figures)
        self.assertEqual(owners[figures[0]['path']],'later')
        self.assertEqual(batches[0][0]['assigned_knowledge_id'],'later')
        with self.assertRaisesRegex(ValueError,'不符合学习深度'):
            author._figure_batches(ledger,[dict(figures[0],knowledge_id='brief')])

    def test_batch_structure_rejects_nested_reordered_and_outside_content(self):
        ledger={'knowledge':[{'id':'a'},{'id':'b'}]}
        for body in ('% knowledge:a\nA\n% knowledge:b\nB\n% /knowledge:b\n% /knowledge:a',
            '% knowledge:b\nB\n% /knowledge:b\n% knowledge:a\nA\n% /knowledge:a',
            '\\section{外部标题}\n% knowledge:a\nA\n% /knowledge:a\n% knowledge:b\nB\n% /knowledge:b'):
            with self.subTest(body=body),self.assertRaises(ValueError):author._ordered_blocks(body,ledger)

    async def test_literal_code_is_normalized_and_persisted_before_fragment_validation(self):
        with tempfile.TemporaryDirectory() as raw:
            folder,ledger,images,package=self.fixture(raw)
            async def respond(**kwargs):
                body=self.proposal(kwargs['payload'])
                ident=kwargs['payload']['required_knowledge_ids'][0]
                return body.replace('% /knowledge:'+ident,'\\begin{verbatim}\nMOV AX, 200H ; #literal\\path\n\\end{verbatim}\n% /knowledge:'+ident,1)
            with patch.object(author,'request_ai_text',AsyncMock(side_effect=respond)),patch.object(author,'context_window_for',return_value=256000):
                await author.ensure_source('ref',folder,{}, {},ledger,package,images)
            body=(folder/'latex'/'body.tex').read_text()
            self.assertNotIn('\\begin{verbatim}',body)
            self.assertIn('\\texttt{MOV',body)
            self.assertIn('\\#literal',body)

    def test_batch_evidence_selects_associated_and_figure_pages_plus_adjacent_context(self):
        with tempfile.TemporaryDirectory() as raw:
            folder,ledger,images,package=self.fixture(raw)
            transcript=json.loads((folder/'transcript.json').read_text())
            readings=[{'reading':{'images':[{'image_id':'accepted-image','reading':'已核对原图教学解释'},
                {'image_id':'other-image','reading':'其他批次的读图解释'}]},'receipt':{'session_id':'visual-reader'}}]
            owned=[dict(images[1],image_id='accepted-image')]
            evidence=author._batch_evidence([ledger['knowledge'][2]],owned,package,transcript,readings)
            self.assertEqual([row['page'] for row in evidence['source_pages']],[2,3,4,12,13,14])
            self.assertEqual([row['source'] for row in evidence['transcript_sources']],['S2','S3','S4'])
            self.assertEqual(evidence['figure_readings'][0]['images'],readings[0]['reading']['images'][:1])
            manifest=evidence['evidence_manifest']
            self.assertEqual(len(manifest['full_source_inventory_hash']),64)
            included={(row['source_id'],row['page']) for row in manifest['included_pages']}
            excluded={(row['source_id'],page) for row in manifest['excluded_pages'] for page in row['pages']}
            self.assertFalse(included&excluded)
            self.assertEqual(included|excluded,{('original-ppt',page) for page in range(1,24)})
            self.assertTrue(all(row['reason']=='other_batches' for row in manifest['excluded_pages']))
            with self.assertRaisesRegex(ValueError,'缺少已有来源缓存'):
                author._batch_evidence([ledger['knowledge'][2]],[dict(owned[0],page=99)],package,transcript,readings)

    async def test_valid_orphan_source_counts_but_needs_attested_replacement(self):
        with tempfile.TemporaryDirectory() as raw:
            folder,ledger,images,package=self.fixture(raw);items=ledger['knowledge'][:10]
            owned,_=author._figure_batches(ledger['knowledge'],images)
            payload={'coverage':{'knowledge':items},'required_knowledge_ids':[item['id'] for item in items],'images':owned[0]}
            body=self.proposal(payload);destination=folder/'orphan-batch';destination.mkdir()
            orphan=destination/'source-attempt-1.tex';orphan.write_text(body)
            before=orphan.read_bytes();transcript=json.loads((folder/'transcript.json').read_text())
            with patch.object(author,'request_ai_text',AsyncMock(return_value=body)) as request,patch.object(author,'context_window_for',return_value=256000):
                result,receipt=await author._ensure_batch(payload,folder,destination,{},payload['coverage'],{},owned[0],transcript)
            self.assertEqual(request.await_count,1)
            self.assertEqual(receipt['proposal'],2)
            self.assertEqual(receipt['writer_session_id'],'test-batch-writer')
            self.assertEqual(orphan.read_bytes(),before)
            state=json.loads((destination/'attempts-source.json').read_text())
            self.assertEqual(state['completed_proposals'],2)

    async def test_three_valid_but_unattested_sources_remain_exhausted(self):
        with tempfile.TemporaryDirectory() as raw:
            folder,ledger,images,package=self.fixture(raw);items=ledger['knowledge'][:10]
            owned,_=author._figure_batches(ledger['knowledge'],images)
            payload={'coverage':{'knowledge':items},'required_knowledge_ids':[item['id'] for item in items],'images':owned[0]}
            destination=folder/'orphan-batch';destination.mkdir();body=self.proposal(payload)
            for number in range(1,4):(destination/('source-attempt-'+str(number)+'.tex')).write_text(body)
            transcript=json.loads((folder/'transcript.json').read_text())
            with patch.object(author,'request_ai_text',AsyncMock()) as request,patch.object(author,'context_window_for',return_value=256000):
                with self.assertRaisesRegex(ValueError,'三份完整源稿'):
                    await author._ensure_batch(payload,folder,destination,{},payload['coverage'],{},owned[0],transcript)
                self.assertEqual(request.await_count,0)
            self.assertFalse((destination/'accepted.json').exists())

    def test_transcript_projection_retains_scientific_correction_without_search_payload(self):
        transcript={'blocks':[{'start':0,'end':3,'text':'原始物理陈述','segment_ids':[1],
            'correction_review':{'before':'纠错前','after':'纠错后','risks':['物理条件'],'status':'待核对',
                'evidence':[{'citation_id':'proof','source_id':'source','content_hash':'hash','text':'无关检索正文'*5000,'embedding_contract':{'large':'metadata'}}]}}]}
        evidence=author._batch_evidence([{'id':'k','sources':['S1']}],[],{},transcript,[])
        correction=evidence['transcript_sources'][0]['correction_review']
        self.assertEqual(correction['before'],'纠错前');self.assertEqual(correction['after'],'纠错后')
        self.assertEqual(correction['risks'],['物理条件']);self.assertEqual(correction['status'],'待核对')
        self.assertEqual(correction['evidence_references'],[{'citation_id':'proof','source_id':'source','content_hash':'hash'}])
        self.assertNotIn('无关检索正文',json.dumps(evidence,ensure_ascii=False))
