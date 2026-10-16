from tests import isolation  # noqa: F401
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qwen_asr_app.knowledge.store import KnowledgeStore
from qwen_asr_app.remote_classroom import RemoteEmbedder


class RemoteVectorReplicaTests(unittest.TestCase):
    def fixture(self,folder):
        store=KnowledgeStore(folder/'index.sqlite');store.upsert_course('course','synthetic','/synthetic')
        source={'source_id':'source','course_id':'course','category':'synthetic','title':'course source','kind':'material',
                'origin':'original','source_type':'file','open_url':'/synthetic'}
        store.commit_source_revision(source_meta=source,revision='r',content_hash='h',parser_version='p',chunker_version='c',
            embedding_contract={'dimension':2},chunks_data=[{'chunk_id':str(i),'content_hash':str(i),'text':'original '+str(i)} for i in range(70)],
            vectors=[[1,0] for _ in range(70)])
        embedder=RemoteEmbedder.__new__(RemoteEmbedder)
        embedder.contract={'dimension':2};embedder.routing={};embedder.config={'batch_size':16}
        return store,embedder

    def request(self,embedder,manifest,batches,mutate=None):
        def reply(cfg,path,payload,**kwargs):
            if path.endswith('/manifest'):return {'versions':dict(manifest)}
            if path.endswith('/sync'):
                batches.append(payload['upsert'])
                for ident in payload['delete']:manifest.pop(ident,None)
                for item in payload['upsert']:manifest[item['id']]=item['version']
                return {'saved':True}
            if path.endswith('/compute'):return {'vectors':[[1,0]],'contract':embedder.contract,'seconds':.1}
            if path.endswith('/search'):
                if mutate:mutate()
                return {'course_id':'course','contract':embedder.contract,'index_version':'v1',
                        'matches':[{'chunk_id':'0','version':'0','score':1},{'chunk_id':'missing','version':'x','score':1}]}
            raise AssertionError(path)
        return reply

    def test_replica_syncs_only_changes_in_bounded_batches_and_returns_original_text(self):
        with tempfile.TemporaryDirectory() as raw:
            store,embedder=self.fixture(Path(raw));manifest={'deleted':'old'};batches=[]
            with patch('qwen_asr_app.remote_classroom.request',side_effect=self.request(embedder,manifest,batches)):
                hits=embedder.search(store,'course','original',limit=6)
                self.assertEqual([len(b) for b in batches],[64,6]);self.assertNotIn('deleted',manifest)
                self.assertEqual(len(hits),1);self.assertEqual(hits[0]['text'],'original 0')
                self.assertNotIn('vector',hits[0]);self.assertEqual(hits[0]['source_type'],'file')
                self.assertEqual(embedder.search(store,'course','again',limit=6)[0]['chunk_id'],'0')
                self.assertEqual(len(batches),2)

    def test_source_changed_while_windows_was_processing_never_becomes_a_valid_hit(self):
        with tempfile.TemporaryDirectory() as raw:
            store,embedder=self.fixture(Path(raw));manifest={};batches=[]
            def change():
                with store._connect() as db:db.execute("UPDATE chunks SET content_hash='changed' WHERE chunk_id='0'")
            with patch('qwen_asr_app.remote_classroom.request',side_effect=self.request(embedder,manifest,batches,change)):
                self.assertFalse(embedder.search(store,'course','original',limit=6))
