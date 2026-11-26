from tests import isolation  # noqa: F401

import plistlib
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import patch

from qwen_asr_app import launch_agent


class LaunchAgentTests(unittest.TestCase):
    def test_service_uses_login_session_and_private_nonpersistent_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(launch_agent, '_inspect', side_effect=[(False, None), (True, 1234)]), \
                    patch.object(launch_agent.subprocess, 'run', return_value=CompletedProcess([], 0)), \
                    patch.object(launch_agent.os, 'getuid', return_value=501):
                process = launch_agent.start(['/python', '-m', 'qwen_asr_app.server'], root=root,
                    run=root/'private', env={'HOME': '/user', 'QWEN_ASR_INSTANCE': 'test',
                        'OPENAI_API_KEY': 'do-not-persist', 'CODEX_APP_TOOLS_PIPE_PATH': '/editor'}, port=8765)
                self.assertEqual(process.pid, 1234)
                launch_agent.subprocess.run.assert_called_once_with(
                    ['/bin/launchctl', 'bootstrap', 'gui/501', str(root/'private/workbench.plist')],
                    capture_output=True, text=True, timeout=10)
            path = root/'private/workbench.plist'
            data = plistlib.loads(path.read_bytes())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(data['EnvironmentVariables'], {'HOME': '/user', 'QWEN_ASR_INSTANCE': 'test'})
            self.assertFalse(data['KeepAlive'])
            self.assertEqual(data['LimitLoadToSessionType'], 'Aqua')
            self.assertEqual(data['ProgramArguments'], ['/python', '-m', 'qwen_asr_app.server'])

    def test_live_job_cannot_be_replaced_or_stopped_by_start(self):
        with patch.object(launch_agent, '_inspect', return_value=(True, 1234)), \
                patch.object(launch_agent.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, '仍在运行'):
                launch_agent.start(['/python'], root='/tmp/project', run='/tmp/run', env={}, port=8765)
            run.assert_not_called()

    def test_stopped_job_is_unloaded_before_restart(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(launch_agent, '_inspect', side_effect=[(True, None), (True, 4321)]), \
                patch.object(launch_agent.subprocess, 'run', return_value=CompletedProcess([], 0)) as run:
            process=launch_agent.start(['/python'], root=directory, run=directory, env={}, port=8765)
            self.assertEqual(process.pid,4321)
            self.assertEqual(run.call_args_list[0].args[0][1],'bootout')
            self.assertEqual(run.call_args_list[1].args[0][1],'bootstrap')

    def test_bootstrap_failure_does_not_fall_back_to_inherited_gui_context(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(launch_agent, '_inspect', return_value=(False, None)), \
                patch.object(launch_agent.subprocess, 'run', return_value=CompletedProcess([], 5)) as run:
            with self.assertRaisesRegex(RuntimeError, '退出码 5'):
                launch_agent.start(['/python'], root=directory, run=directory, env={}, port=8765)
            self.assertEqual(run.call_count, 1)
