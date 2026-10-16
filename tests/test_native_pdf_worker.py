"""Real native PDF work must not import its native runtime into the caller."""
from tests import isolation  # noqa: F401
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qwen_asr_app.native_pdf import pdf_operation


class NativePdfWorkerTests(unittest.TestCase):
    def test_knowledge_transactions_close_native_connections_and_rollback_on_error(self):
        import sqlite3
        from qwen_asr_app.knowledge.store import KnowledgeStore
        with tempfile.TemporaryDirectory() as raw:
            store=KnowledgeStore(Path(raw)/'index.sqlite')
            with store._connect() as connection:connection.execute('CREATE TABLE synthetic(value INTEGER)')
            with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
            with self.assertRaisesRegex(RuntimeError,'rollback'):
                with store._connect() as failed:
                    failed.execute('INSERT INTO synthetic VALUES (1)');raise RuntimeError('rollback')
            with store._connect() as check:self.assertEqual(check.execute('SELECT count(*) FROM synthetic').fetchone()[0],0)

    def test_index_vector_roundtrip_keeps_float32_bytes_without_loading_numpy(self):
        with tempfile.TemporaryDirectory() as raw:
            code='''import struct,sys
from qwen_asr_app.knowledge.store import KnowledgeStore
store=KnowledgeStore(sys.argv[1]);store.upsert_course('course','synthetic','/synthetic')
source={'source_id':'source','course_id':'course','category':'synthetic','title':'test','kind':'material','origin':'original','source_type':'file','open_url':'/synthetic'}
store.commit_source_revision(source_meta=source,revision='revision',content_hash='hash',parser_version='parser',chunker_version='chunker',embedding_contract={'dimension':3},chunks_data=[{'chunk_id':'chunk','content_hash':'chunk-hash','text':'synthetic'}],vectors=[[0.1,-2.0,3.5]])
assert store.cached_vectors(['chunk-hash'],{'dimension':3})['chunk-hash']==list(struct.unpack('=3f',struct.pack('=3f',0.1,-2.0,3.5)))
from array import array
compact=store.cached_vectors(['chunk-hash'],{'dimension':3},compact=True)['chunk-hash']
assert isinstance(compact,array) and compact.itemsize==4
assert compact.tolist()==store.cached_vectors(['chunk-hash'],{'dimension':3})['chunk-hash']
hits=store.search_vectors('synthetic',[0.1,-2.0,3.5],{'dimension':3})
assert len(hits)==1 and hits[0]['chunk_id']=='chunk' and abs(hits[0]['vector_score']-1)<1e-6
assert 'numpy' not in sys.modules
'''
            subprocess.run([sys.executable,'-c',code,str(Path(raw)/'index.sqlite')],capture_output=True,text=True,timeout=10,check=True)

    def test_vector_ranking_threshold_dimensions_and_top_k_match_exact_cosine(self):
        from qwen_asr_app.knowledge.store import KnowledgeStore
        with tempfile.TemporaryDirectory() as raw:
            store=KnowledgeStore(Path(raw)/'index.sqlite');store.upsert_course('course','synthetic','/synthetic')
            vectors=[[1,0],[.8,.6],[0,1],[-1,0],[0,0]]
            source={'source_id':'source','course_id':'course','category':'synthetic','title':'test','kind':'material',
                    'origin':'original','source_type':'file','open_url':'/synthetic'}
            store.commit_source_revision(source_meta=source,revision='r',content_hash='h',parser_version='p',chunker_version='c',
                embedding_contract={'dimension':2},chunks_data=[{'chunk_id':str(i),'content_hash':str(i),'text':'vector '+str(i)} for i in range(5)],vectors=vectors)
            hits=store.search_vectors('synthetic',[2,0],{'dimension':2},limit=1)
            self.assertEqual([h['chunk_id'] for h in hits],['0'])
            self.assertAlmostEqual(hits[0]['vector_score'],1,places=6)
            self.assertEqual([h['chunk_id'] for h in store.search_vectors('synthetic',[2,0],{'dimension':2})],['0','1'])
            self.assertFalse(store.search_vectors('synthetic',[0,0],{'dimension':2}))
            self.assertFalse(store.search_vectors('synthetic',[1,0,0],{'dimension':2}))
            self.assertFalse(store.search_vectors('other',[1,0],{'dimension':2}))
            self.assertFalse(store.search_vectors('synthetic',[float('nan'),0],{'dimension':2}))

    def fixture(self,folder):
        import pymupdf
        path=folder/'synthetic.pdf'
        with pymupdf.open() as doc:
            for text in ('First original page','Second original page'):
                doc.new_page().insert_text((60,60),text)
            doc.embfile_add('mineru-transcript.md',b'Original embedded parser output')
            doc.save(path)
        return path

    def test_real_extract_render_and_inspect_leave_parent_without_native_modules(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);pdf=self.fixture(folder)
            code='''import json,sys
from qwen_asr_app.native_pdf import pdf_operation
pages=pdf_operation('text',path=sys.argv[1],sort=True)
assert pages[0]['text'].strip()=='First original page'
render=pdf_operation('render',path=sys.argv[1],pages=[{'page':2,'output':sys.argv[2],'scale':1.25}])
assert render[0]['text'].strip()=='Second original page'
assert pdf_operation('inspect',path=sys.argv[1])=={'pages':2,'blank_pages':[]}
assert pdf_operation('embedded_markdown',path=sys.argv[1])=='Original embedded parser output'
assert not any(name in sys.modules for name in ['numpy','pymupdf','fitz','torch','av'])
print(json.dumps({'native_modules_in_parent':False,'pages':len(pages)}))
'''
            result=subprocess.run([sys.executable,'-c',code,str(pdf),str(folder/'page-2.png')],
                capture_output=True,text=True,env=os.environ.copy(),timeout=20,check=True)
            self.assertFalse(json.loads(result.stdout)['native_modules_in_parent'])
            self.assertTrue((folder/'page-2.png').read_bytes().startswith(b'\x89PNG'))

    def test_invalid_page_crop_or_render_batch_fails_without_poisoning_next_operation(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);pdf=self.fixture(folder)
            for pages in ([{'page':0,'output':str(folder/'bad.png')}],
                [{'page':1,'output':str(folder/'bad.png'),'bbox':[0,0,.01,.01]}],
                [{'page':1,'output':str(folder/'bad.png')}] * 5):
                with self.subTest(pages=pages),self.assertRaises(ValueError):pdf_operation('render',path=str(pdf),pages=pages)
            self.assertFalse((folder/'bad.png').exists())
            self.assertEqual(pdf_operation('inspect',path=str(pdf))['pages'],2)

    def test_image_location_keeps_exact_embedded_pixel_matching(self):
        import pymupdf
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);image=folder/'image.png'
            pix=pymupdf.Pixmap(pymupdf.csRGB,pymupdf.IRect(0,0,100,100),False);pix.clear_with(180);pix.save(image)
            pdf=folder/'image.pdf'
            with pymupdf.open() as doc:
                doc.new_page();page=doc.new_page();page.insert_image(pymupdf.Rect(10,10,110,110),filename=image);doc.save(pdf)
            self.assertEqual(pdf_operation('image_locations',path=str(pdf),figures=[{'image_id':'real-pixels','path':str(image)}]),{'real-pixels':[2]})
