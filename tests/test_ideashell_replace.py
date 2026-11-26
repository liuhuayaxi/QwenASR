from tests import isolation
import tempfile
import unittest
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import app
from qwen_asr_app.storage import RecordingRecord


class ReplaceExistingTests(unittest.IsolatedAsyncioTestCase):
    """note_update appends a memo, so re-pushing a synced note must publish a
    fresh note and retire the old one to 「待删除」 instead of updating it."""

    async def push(self, old_visible=True, payload=None, sync_status='synced'):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = RecordingRecord(root, {'id': 'synthetic-replace', 'title': '合成笔记', 'category': '合成课程',
                                            'status': 'completed', 'started_at': '2026-10-13T08:00:00+08:00',
                                            'ai_note_summary': {'summary': '合成完整笔记'},
                                            'ideashell_sync': {'note_id': 'a' * 32, 'note_ids': ['a' * 32], 'status': sync_status,
                                                               'server_url': 'https://synthetic.invalid'}})
            record.segments = [{'id': 1, 'start': 0, 'end': 1, 'raw': '合成逐字稿'}]
            record.save_all()
            calls = []

            async def remote(name, args):
                calls.append((name, args))
                if name == 'note_create':
                    return {'note_id': 'b' * 32}
                if name == 'folder_notes':
                    return {'notes': [{'note_id': 'a' * 32}] if old_visible else []}
                return {}

            async def folder(_connection, name):
                return 't' * 32 if name == app.IDEASHELL_TRASH_FOLDER else 'c' * 32
            connection = SimpleNamespace(server_url='https://synthetic.invalid', call_tool=remote,
                                         list_tools=AsyncMock(return_value={'note_create': {}, 'note_update': {}, 'folder_notes': {}}))
            with patch.object(app.recording_store, 'find', side_effect=lambda _: record), \
                 patch.object(app, 'ideashell_recording_audio_parts', AsyncMock(return_value=[])), \
                 patch.object(app.ideashell_manager, 'connection', return_value=connection), \
                 patch.object(app, 'ensure_ideashell_course_folder', side_effect=folder):
                result = await app.push_recording_summary_to_ideashell(record.id, payload or {})
            return result, calls, record.metadata['ideashell_sync']

    async def test_repush_creates_fresh_note_and_retires_old(self):
        result, calls, sync = await self.push()
        names = [name for name, _ in calls]
        self.assertIn('note_create', names)
        self.assertNotIn('note_update', names)
        self.assertEqual(sync['note_id'], 'b' * 32)
        self.assertEqual(result['retired_note_ids'], ['a' * 32])
        self.assertIn(('note_move', {'note_ids': ['a' * 32], 'folder_id': 't' * 32}), calls)

    async def test_old_note_already_gone_is_not_pulled_back(self):
        result, calls, _ = await self.push(old_visible=False)
        self.assertEqual(result['retired_note_ids'], [])
        self.assertNotIn(('note_move', {'note_ids': ['a' * 32], 'folder_id': 't' * 32}), calls)

    async def test_local_changes_republish_one_memo_and_retire_previous_version(self):
        result, calls, sync = await self.push(sync_status='local_changes')
        self.assertIn('note_create', [name for name, _ in calls])
        self.assertNotIn('note_update', [name for name, _ in calls])
        self.assertEqual(result['retired_note_ids'], ['a' * 32])
        self.assertEqual(sync['note_id'], 'b' * 32)

    async def test_explicit_opt_out_updates_in_place(self):
        _, calls, sync = await self.push(payload={'replace_existing': False})
        self.assertNotIn('note_create', [name for name, _ in calls])
        self.assertEqual(sync['note_id'], 'a' * 32)

    async def test_readback_digest_binds_the_primary_body_only(self):
        ident='b'*32
        record=SimpleNamespace(id='synthetic-verify-digest',metadata={'category':'合成课程',
            'ideashell_sync':{'note_id':ident,'server_url':'https://synthetic.invalid'}})
        body='# 新版正文\n\n保留公式与原文。'
        detail={'text':'## Memos\n**Memo 2: 当前版本**\nsummary: 摘要\n'+body+
                        '\n**Memo 1: 历史版本**\n旧正文\n## Images\n配图.png'}
        async def remote(name,args):
            return detail if name=='note_detail' else {'notes':[{'note_id':ident}]}
        connection=SimpleNamespace(call_tool=remote)
        with patch.object(app.recording_store,'find',return_value=record), \
             patch.object(app.ideashell_manager,'connection',return_value=connection), \
             patch.object(app,'ensure_ideashell_course_folder',AsyncMock(return_value='c'*32)):
            result=await app.verify_ideashell_note(record.id,{'retire':False})
        self.assertEqual(result['content_sha256'],hashlib.sha256(body.encode()).hexdigest())


if __name__ == '__main__':
    unittest.main()
