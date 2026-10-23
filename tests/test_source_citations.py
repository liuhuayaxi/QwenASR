import unittest

from qwen_asr_app.review_notes import timestamp_references

BLOCKS = [{'start': i * 60.0, 'end': i * 60.0 + 59} for i in range(20)]


class SourceCitationTests(unittest.TestCase):
    def test_writer_block_ids_resolve_to_timed_links(self):
        # The writer sees transcript blocks labelled "S4" and sometimes cites them so.
        out = timestamp_references('a [课堂 S4](#source-S4) b [课堂 S2-S3, S7](#source-S2) c [原文 S5](#source-5)', BLOCKS)
        self.assertNotIn('#source-S', out)
        self.assertIn('[课堂 S4 · 00:03:00–00:03:59](#source-4)', out)
        self.assertIn('[课堂 S2–S3 · 00:01:00–00:02:59](#source-2)、[课堂 S7 · 00:06:00–00:06:59](#source-7)', out)
        self.assertIn('[课堂 S5 · 00:04:00–00:04:59](#source-5)', out)

    def test_resolved_links_are_stable(self):
        once = timestamp_references('[课堂 S12, S16](#source-S12)', BLOCKS)
        self.assertEqual(timestamp_references(once, BLOCKS), once)
        self.assertEqual(timestamp_references('[课堂 S99](#source-S99)', BLOCKS), '（转写来源待核对）')


if __name__ == '__main__':
    unittest.main()
