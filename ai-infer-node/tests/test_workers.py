import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workers.asr_worker import boundaries, merge_text


class AudioTests(unittest.TestCase):
    def test_language_switch_pause_splits_even_short_recording(self):
        chunks=list(boundaries(19.2552,[.5312,4.6491,6.5748],25,1,[4.6491]))
        self.assertEqual(chunks,[(0.0,4.6491),(4.6491,19.2552)])

    def test_long_recording_keeps_complete_overlap_coverage(self):
        chunks=list(boundaries(7200,[],25,1))
        self.assertEqual(chunks[0][0],0)
        self.assertEqual(chunks[-1][1],7200)
        self.assertTrue(all(0<end-start<=25 for start,end in chunks))
        self.assertTrue(all(b[0]<=a[1] for a,b in zip(chunks,chunks[1:])))

    def test_dedup_does_not_drop_short_coincidental_overlap(self):
        self.assertEqual(merge_text('soda','dark matters'),'soda\ndark matters')
        self.assertEqual(merge_text('物理光学实验内容','光学实验内容与讨论'),'物理光学实验内容\n光学实验内容与讨论')

    def test_whole_utterance_repeated_intentionally_is_preserved(self):
        phrase='The long drizzle had begun. Pedestrians had turned up collars and trousers at the bottom.'
        text=''
        for _ in range(3):text=merge_text(text,phrase)
        self.assertEqual(text.count(phrase),3)


if __name__=='__main__':unittest.main()
