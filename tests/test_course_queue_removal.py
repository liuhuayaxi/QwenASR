from tests import isolation  # noqa: F401

import copy
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock, Mock

from fastapi import HTTPException
from qwen_asr_app import smartclass_service as service


class CourseQueueRemovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_targeted_start_does_not_retry_other_interrupted_lessons(self):
        state={'paused':False,'items':[{'group':{'id':'old','title':'已有中断'},'state':'interrupted','options':{}},
                                     {'group':{'id':'chosen','title':'本次选择'},'state':'queued','options':{}}]}
        with patch.object(service,'queue_state',state), \
             patch.object(service,'queue_wakeup',asyncio.Event()), \
             patch.object(service,'stop_event') as stop, \
             patch.object(service,'save_queue',Mock()), \
             patch.object(service,'status',{}), \
             patch.object(service,'video_ids',return_value=[]), \
             patch.object(service,'import_group',AsyncMock()) as run:
            stop.is_set.return_value=False
            await service.run_queue(retry_interrupted=False)
        self.assertEqual(run.await_count,1)
        self.assertEqual(run.await_args.args[0]['id'],'chosen')
        self.assertEqual(state['items'][0]['state'],'interrupted')

    async def test_removes_only_selected_failure_and_keeps_files_and_running_job(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / '课堂笔记.md'
            source.write_text('已保存的课件笔记')
            state = {'paused':False, 'items':[
                {'group':{'id':'failed','title':'失败课堂'},'state':'interrupted'},
                {'group':{'id':'busy','title':'正在处理'},'state':'running'},
            ]}
            original_busy = copy.deepcopy(state['items'][1])
            with patch.object(service, 'STATE_DIR', root), patch.object(service, 'queue_state', state):
                result = await service.remove_queue_items(service.Selection(ids=['failed']))
            self.assertEqual(result['removed'], ['failed'])
            self.assertEqual(state['items'], [original_busy])
            self.assertFalse(state['paused'])
            self.assertEqual(json.loads((root/'queue.json').read_text())['items'], [original_busy])
            receipt = json.loads((root/'removed-queue-items.json').read_text())
            self.assertEqual(receipt['items'][0]['entries'][0]['group']['id'], 'failed')
            self.assertEqual(source.read_text(), '已保存的课件笔记')

    async def test_rejects_running_selection_without_partial_removal(self):
        with tempfile.TemporaryDirectory() as raw:
            state={'items':[{'group':{'id':'failed'},'state':'interrupted'},
                            {'group':{'id':'busy'},'state':'running'}]}
            before=copy.deepcopy(state)
            with patch.object(service,'STATE_DIR',Path(raw)), patch.object(service,'queue_state',state):
                with self.assertRaises(HTTPException) as raised:
                    await service.remove_queue_items(service.Selection(ids=['failed','busy']))
            self.assertEqual(raised.exception.status_code,409)
            self.assertEqual(state,before)
            self.assertEqual(list(Path(raw).iterdir()),[])
