"""Model macOS sandbox availability only at an already mocked CLI boundary."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch


@contextmanager
def available_material_sandbox(module='qwen_asr_app.agy_materials'):
    # Replacing the module's sys/shutil references does not change the host
    # interpreter platform or subprocess behavior for unrelated tests.
    with patch(module + '.sys', SimpleNamespace(platform='darwin')), \
         patch(module + '.shutil', SimpleNamespace(which=lambda name: '/usr/bin/' + name)):
        yield
