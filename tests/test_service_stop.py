from tests import isolation

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from qwen_asr_app import service_control as control


class ProcessStopTests(unittest.TestCase):
    def test_unresponsive_server_and_detached_child_are_killed_without_http(self):
        with tempfile.TemporaryDirectory() as raw:
            # A real process with the launcher's command, token, and a worker
            # in another process group. Neither process cooperates with TERM.
            Path(raw, 'uvicorn.py').write_text('''
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
worker = subprocess.Popen([sys.executable, '-u', '-c',
    'import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(os.getpid(), flush=True); time.sleep(60)'],
    stdout=subprocess.PIPE, text=True, start_new_session=True)
print(worker.stdout.readline().strip(), flush=True)
time.sleep(60)
''')
            token = 'synthetic-stop-instance'
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app:app', '--host',
                '127.0.0.1', '--port', str(control.PORT)], cwd=raw, env={**os.environ, 'QWEN_ASR_INSTANCE': token},
                stdout=subprocess.PIPE, text=True, start_new_session=True)
            child = int(process.stdout.readline())
            try:
                private = Path(raw) / 'private'
                queue_path = private / 'smartclass' / 'queue.json'
                queue_path.parent.mkdir(parents=True)
                queue_path.write_text(json.dumps({'paused': False, 'items': [
                    {'state': 'running', 'group': {'id': 'A'}, 'options': {'model': 'saved'}},
                    {'state': 'completed', 'group': {'id': 'B'}}]}))
                snapshot = control.process_snapshot()
                saved = {'pid': process.pid, 'token': token, 'started': snapshot[process.pid]['started']}
                start = time.monotonic()
                with patch.object(control, 'request', side_effect=TimeoutError) as request, \
                        patch.object(control, 'PRIVATE_ROOT', private):
                    control.stop_services({'workbench': saved, 'courses': saved})
                self.assertLess(time.monotonic() - start, 4)
                request.assert_called_once_with('workbench', '/api/service/request-stop', 'POST', token, timeout=.5)
                self.assertEqual(process.wait(timeout=1), -signal.SIGKILL)
                current = control.process_snapshot()
                self.assertTrue(child not in current or current[child]['state'].startswith('Z'))
                queue = json.loads(queue_path.read_text())
                self.assertTrue(queue['paused'])
                self.assertEqual(queue['items'][0]['state'], 'paused')
                self.assertEqual(queue['items'][0]['options'], {'model': 'saved'})
                self.assertEqual(queue['items'][1]['state'], 'completed')
            finally:
                for pid in (process.pid, child):
                    try: os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                process.wait(timeout=2)
                process.stdout.close()

    def test_stale_pid_or_wrong_token_is_never_signalled(self):
        pid = os.getpid()
        snapshot = control.process_snapshot()
        with patch.object(control, 'process_snapshot', return_value=snapshot), patch.object(control.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, '路径不匹配'):
                control.stop_services({'workbench': {'pid': pid, 'token': 'wrong'}})
            with self.assertRaisesRegex(RuntimeError, '标识已变化'):
                control.stop_services({'workbench': {'pid': pid, 'token': 'wrong', 'started': 'old'}})
            kill.assert_not_called()

    def test_missing_process_is_already_stopped(self):
        with patch.object(control, 'process_snapshot', return_value={}), patch.object(control.os, 'kill') as kill:
            control.stop_services({'workbench': {'pid': 12345, 'token': 'old'}})
            kill.assert_not_called()


class RequestStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_work_does_not_veto_exit_and_queue_is_persisted(self):
        import app
        service = app.smartclass_service
        queue = {'paused': False, 'items': [{'group': {'id': 'A'}, 'state': 'running'}]}
        with patch.dict(os.environ, {'QWEN_ASR_INSTANCE': 'test-stop'}), \
                patch.object(app.model_manager, 'active_jobs', 1), \
                patch.object(app, 'service_draining', False), \
                patch.object(app.library_watch, 'draining', False), \
                patch.object(service, 'service_draining', False), \
                patch.object(service, 'queue_state', queue), \
                patch.object(service, 'save_queue') as save, patch.object(service, 'save_imports'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url=control.BASE_URL) as client:
                denied = await client.post('/api/service/request-stop')
                self.assertEqual(denied.status_code, 403)
                response = await client.post('/api/service/request-stop', headers={'X-QwenASR-Control': 'test-stop'})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(app.service_draining)
            self.assertTrue(service.service_draining)
            self.assertTrue(queue['paused'])
            self.assertEqual(queue['items'][0]['state'], 'paused')
            save.assert_called_once()
        service.stop_event.clear()
