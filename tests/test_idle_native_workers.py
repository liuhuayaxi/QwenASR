from tests import isolation
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class IdleNativeWorkerTests(unittest.TestCase):
    def test_real_media_and_pdf_work_does_not_load_native_runtimes_in_parent(self):
        code = r'''
import os, sys, wave
from pathlib import Path
root = Path(sys.argv[1]);os.chdir(root)
with wave.open('source.wav', 'wb') as output:
    output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000)
    output.writeframes(b'\x01\x00' * 32000)
from qwen_asr_app.audio import decode_audio_file, convert_audio_to_aac, make_playback_audio, split_audio_to_aac
samples = decode_audio_file(Path('source.wav'))
assert samples.shape == (32000,) and abs(float(samples[0]) - 1/32768) < 1e-6
convert_audio_to_aac(Path('source.wav'), Path('audio.aac'))
make_playback_audio(Path('audio.aac'), Path('playback.m4a'))
split_audio_to_aac(Path('source.wav'), [Path('part1.aac'), Path('part2.aac')])
from qwen_asr_app.course_file_conversion import _text_pdf
from qwen_asr_app.native_pdf import pdf_operation
from qwen_asr_app.mineru_timing import pdf_pages
Path('source.txt').write_text('Memory regression test with searchable PDF text.')
_text_pdf(Path('source.txt'), Path('source.pdf'), 'Regression')
assert pdf_pages(Path('source.pdf')) == 1
assert pdf_operation('text_stats', path='source.pdf')['text_chars'] > 20
assert not {'av','pymupdf','fitz'}.intersection(sys.modules), 'Codec or PDF runtime leaked into parent'
for name in ('audio.aac','playback.m4a','part1.aac','part2.aac','source.pdf'):
    assert Path(name).stat().st_size > 10
try:
    decode_audio_file(Path('does-not-exist.wav'))
except ValueError:
    pass
else:
    raise AssertionError('Invalid input was accepted')
print('Real AAC, playback, split, PCM and PDF operations passed without resident codecs')
'''
        with tempfile.TemporaryDirectory() as root:
            import os
            env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parent.parent)}
            result = subprocess.run([sys.executable, '-c', code, root], capture_output=True,
                                    text=True, timeout=60, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
