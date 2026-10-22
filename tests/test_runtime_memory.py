from tests import isolation
import asyncio,sys,unittest
from unittest.mock import Mock,patch
from qwen_asr_app import runtime_memory as memory

class RuntimeMemoryTests(unittest.IsolatedAsyncioTestCase):
    def test_reclaim_clears_only_transient_cache_and_records_live_rss(self):
        clear=Mock()
        with patch.object(memory,'rss_bytes',side_effect=[300,180]),patch.object(memory,'release_pdf_cache',return_value=120),patch.object(memory.gc,'collect'),patch.object(memory.ctypes,'CDLL',side_effect=OSError('no allocator API')):
            result=memory.reclaim_idle_memory(clear)
        clear.assert_called_once();self.assertEqual(result,{'rss_before':300,'rss_after':180,'pdf_cache_released_bytes':120})

    def test_no_pdf_import_when_reclaiming_idle_server(self):
        with patch.dict(sys.modules, {'fitz': None, 'pymupdf': None}):
            with patch('builtins.__import__', wraps=__import__) as importer:
                self.assertEqual(memory.release_pdf_cache(), 0)
            self.assertFalse(any(call.args[0].endswith('review_materials') or call.args[0].endswith('native_pdf') for call in importer.call_args_list))

    async def test_finished_jobs_debounce_and_busy_service_is_not_reclaimed(self):
        with patch.object(memory,'reclaim_idle_memory') as reclaim:
            memory.schedule_idle_reclaim(lambda:False)
            await asyncio.sleep(.55)
            reclaim.assert_not_called()
            clear=Mock()
            memory.schedule_idle_reclaim(lambda:True,clear)
            memory.schedule_idle_reclaim(lambda:True,clear)
            await asyncio.sleep(.55)
            reclaim.assert_called_once_with(clear)
