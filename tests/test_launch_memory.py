import unittest
from qwen_asr_app.launch_memory import service_environment


class LaunchMemoryTests(unittest.TestCase):
    def test_macos_policy_is_per_child_and_preserves_input(self):
        source = {'PATH': '/usr/bin', 'QWEN_ASR_INSTANCE': 'test'}
        result = service_environment(source, 'darwin')
        self.assertEqual(source, {'PATH': '/usr/bin', 'QWEN_ASR_INSTANCE': 'test'})
        self.assertEqual(result['MallocAggressiveMadvise'], '1')
        self.assertEqual(result['MallocLargeCache'], '0')
        self.assertEqual(result['QWEN_ASR_INSTANCE'], 'test')
        self.assertEqual(result['PYTHONMALLOC'], 'malloc')

    def test_explicit_allocator_options_are_not_overridden(self):
        source = {'MallocLargeCache': '1', 'MallocAggressiveMadvise': '0', 'PYTHONMALLOC':'pymalloc'}
        self.assertEqual(service_environment(source, 'darwin'), source)

    def test_operator_can_opt_out(self):
        source = {'QWEN_ASR_MALLOC_TUNING': '0'}
        self.assertEqual(service_environment(source, 'darwin'), source)

    def test_other_platforms_are_unchanged(self):
        for platform in ('linux', 'win32'):
            self.assertEqual(service_environment({'PATH': '/bin'}, platform), {'PATH': '/bin'})
