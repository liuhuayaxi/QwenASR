from tests import isolation  # noqa: F401

import gzip
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qwen_asr_app import standby_state as standby


class StandbyStateTests(unittest.TestCase):
    def test_picker_and_oauth_requests_must_always_reach_application(self):
        for path in ['/api/library/choose-root','/api/pdf-ai/choose-files',
                     '/api/ideashell/oauth/callback','/api/recordings/a/download/audio',
                     '/api/recordings/a/playback','/api/library/events']:
            self.assertFalse(standby.cacheable(path),path)
        self.assertFalse(standby.refreshable('/api/ideashell/status'))
        self.assertFalse(standby.refreshable('/api/ai/classroom-chat/models'))
        self.assertTrue(standby.refreshable('/api/recordings/a/summary-status'))

    def test_compressed_body_and_sensitive_headers_are_safe_for_idle_reads(self):
        from qwen_asr_app.paths import PRIVATE_ROOT
        url='/api/library?synthetic=compressed'
        body=json.dumps({'title':'隔离测试'}).encode()
        self.assertTrue(standby.save_response(url,200,[('Content-Encoding','gzip'),
                        ('Set-Cookie','synthetic-secret'),('Content-Type','application/json')],gzip.compress(body)))
        key=hashlib.sha256(url.encode()).hexdigest()
        folder=PRIVATE_ROOT/'services/http-cache'
        self.assertEqual((folder/(key+'.body')).read_bytes(),body)
        entry=json.loads((folder/(key+'.json')).read_text())
        self.assertNotIn('synthetic-secret',json.dumps(entry))
        self.assertEqual(folder.stat().st_mode&0o777,0o700)
        self.assertFalse(standby.save_response(url,200,[('Content-Encoding','gzip')],
                                              gzip.compress(b'x'*(2*1024*1024+1))))

    def test_wake_deadlines_survive_exec_instead_of_resetting_backup_timer(self):
        drive=SimpleNamespace(next_run=0)
        with patch.dict(os.environ,{'QWEN_ASR_WAKE_DEADLINES':json.dumps({'drive':time.time()+17})}):
            standby.restore_deadlines(drive)
            self.assertGreater(drive.next_run-time.monotonic(),16)
            self.assertLess(drive.next_run-time.monotonic(),18)
            self.assertNotIn('QWEN_ASR_WAKE_DEADLINES',os.environ)

    def test_old_publisher_ack_cannot_suspend_new_changes(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);mode=root/'mode.json';status=root/'companion.json'
            mode.write_text(json.dumps({'pid':42,'mode':'active','at':2}))
            self.assertFalse(standby.companion_ready(status,mode,42))
            value={'qwenPid':42,'qwenGeneration':1,'status':'waiting_for_change'}
            status.write_text(json.dumps(value))
            self.assertFalse(standby.companion_ready(status,mode,42))
            value['qwenGeneration']=2
            for state,ready in [('syncing',False),('retry_scheduled',False),
                                ('waiting_for_change',True),('retry_on_next_change',True)]:
                value['status']=state;status.write_text(json.dumps(value))
                self.assertEqual(standby.companion_ready(status,mode,42),ready)


class ConditionalReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_304_from_prior_runtime_is_warmed_before_sleep(self):
        async def application(scope,receive,send):
            await send({'type':'http.response.start','status':304,'headers':[(b'etag',b'prior-version')]})
            await send({'type':'http.response.body','body':b''})
        activity=standby.Activity(application)
        messages=[]
        async def receive():return {'type':'http.request','body':b''}
        async def send(message):messages.append(message)
        await activity({'type':'http','method':'GET','path':'/api/library',
                        'raw_path':b'/api/library','query_string':b'ids=synthetic&include_settings=false',
                        'headers':[(b'if-none-match',b'prior-version')]},receive,send)
        self.assertIn('/api/library?ids=synthetic&include_settings=false',activity.urls)
        self.assertEqual(activity.active,0)
        self.assertEqual(messages[0]['status'],304)
