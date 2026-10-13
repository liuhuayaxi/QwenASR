from tests import isolation
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from qwen_asr_app import model_setup
from qwen_asr_app.inference_providers import inference_settings


class ModelLaunchPolicyTests(unittest.TestCase):
    def test_saved_remote_only_skips_download_and_resolves_custom_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            settings=root/'inference.json';settings.write_text(json.dumps({'asr_provider':'remote_only'}))
            with patch.object(inference_settings,'path',settings),patch.dict(os.environ,{'QWEN_ASR_MODEL_PATH':str(root/'custom')}):
                download=Mock(side_effect=AssertionError('must not download'))
                plan=model_setup.prepare_model(download)
                self.assertEqual(plan['path'],root/'custom')
                self.assertFalse(plan['local_required']);download.assert_not_called()
                self.assertEqual(model_setup.requirements_path().name,'requirements-core.lock')
                settings.write_text('{broken')
                with self.assertRaises(ValueError):model_setup.launch_plan()
                download.assert_not_called()

    def test_local_prepare_only_explicit_and_download_failure_does_not_poison_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            with patch.object(inference_settings,'path',root/'missing.json'),patch.dict(os.environ,{'QWEN_ASR_MODEL_PATH':'custom-model'}),patch.object(model_setup,'ROOT',root):
                self.assertEqual(model_setup.model_path(),root/'custom-model')
                self.assertFalse(model_setup.launch_plan()['ready'])
                download=Mock(side_effect=OSError('synthetic download unavailable'))
                with self.assertRaises(OSError):model_setup.prepare_model(download)
                self.assertFalse(model_setup.launch_plan()['ready'])
                self.assertEqual(download.call_args.kwargs['local_dir'],str(root/'custom-model'))
                folder=root/'custom-model';folder.mkdir();(folder/'model.safetensors.index.json').write_text('{}')
                self.assertTrue(model_setup.prepare_model(Mock(side_effect=AssertionError('already ready')))['ready'])

    def test_actual_shell_launcher_with_fake_dependencies_never_downloads(self):
        source=Path(__file__).resolve().parents[1]/'start_qwen_asr.command'
        for fresh in (False,True):
            with self.subTest(fresh=fresh),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);shutil.copyfile(source,root/source.name)
                binary=root/'.venv/bin';binary.mkdir(parents=True)
                log=root/'calls.log'
                fake=binary/'python'
                fake.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$LAUNCH_LOG"
if [[ "$*" == *check_environment.py* ]]; then exit "${DEPENDENCIES_MISSING:-0}"; fi
if [[ "$*" == *"model_setup requirements"* ]]; then printf '%s\\n' "$PWD/requirements-core.lock"; fi
''');fake.chmod(0o755)
                # The launcher may use a host uv path, so exercise dependency
                # selection in a source copy with that discovery branch disabled.
                launcher=root/source.name
                text=launcher.read_text().replace('if [ -x /opt/homebrew/bin/uv ]; then','if false; then')
                launcher.write_text(text)
                uv=root/'uv';uv.write_text('#!/bin/bash\nprintf "uv %s\\n" "$*" >> "$LAUNCH_LOG"\n');uv.chmod(0o755)
                hf=binary/'hf';hf.write_text('#!/bin/bash\nexit 99\n');hf.chmod(0o755)
                env={**os.environ,'PATH':str(root)+os.pathsep+os.environ['PATH'],'LAUNCH_LOG':str(log),
                     'DEPENDENCIES_MISSING':str(int(fresh)),'WTP_NO_OPEN':'1','QWEN_ASR_DATA_DIR':str(root/'data')}
                result=subprocess.run(['bash',str(launcher)],env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                calls=log.read_text()
                self.assertIn('model_setup check',calls);self.assertIn('service_control start',calls)
                self.assertNotIn('download',calls);self.assertNotIn('model_setup prepare',calls)
                if fresh:self.assertIn('requirements-core.lock',calls)
