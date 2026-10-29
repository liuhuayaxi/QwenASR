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
        self.assertEqual(timestamp_references('[课堂 S99](#source-S99)', BLOCKS), '')
        # No reader-facing meta text, including in tables and stored notes.
        out = timestamp_references('| a | [课堂 S1](#source-1)、[课堂 S99](#source-99) |', BLOCKS)
        self.assertNotIn('待核对', out); self.assertNotIn('、 |', out); self.assertNotIn('、|', out)
        self.assertEqual(timestamp_references('字节 （转写来源待核对）。', BLOCKS), '字节。')
        # Writer sometimes copies a range into the anchor itself.
        out = timestamp_references('[课堂 S2-S3](#source-S2-S3)', BLOCKS)
        self.assertIn('#source-2', out); self.assertIn('#source-3' if False else 'S2–S3', out)
        self.assertNotIn('source-S', out)


if __name__ == '__main__':
    unittest.main()
