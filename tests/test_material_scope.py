from tests import isolation  # noqa: F401

import unittest

from qwen_asr_app import material_scope as ms

TEXT = [{"page": 1, "text": "x" * 600}]


def src(sid, name, pages=TEXT):
    return {"source_id": sid, "name": name, "pages": pages}


COURSE = [src("c0", "光电子学-绪论-课件.pdf"), src("c1", "光电子学-第01章-半导体中光子与电子的互作用-课件.pdf"),
          src("c2", "光电子学-第02章-半导体发光二极管-课件.pdf"), src("c3", "光电子学-第03章-半导体激光器的工作原理及类型-课件.pdf"),
          src("c4", "光电子学-第04章-半导体激光器的性能-课件.pdf")]


def chosen(ids, rows):
    return [r["source_id"] for r in rows if r["chosen"]]


class ChapterParsingTests(unittest.TestCase):
    def test_chinese_and_arabic_chapters(self):
        self.assertEqual(ms.chapters("第四章 半导体激光器的性能 1"), {4})
        self.assertEqual(ms.chapters("第十二章"), {12})
        self.assertEqual(ms.chapters("光电子学-第03章-课件.pdf"), {3})
        self.assertEqual(ms.chapters("9月5日授课"), set())


class ChooseTests(unittest.TestCase):
    def test_chapter_match_beats_shared_vocabulary_and_skips_future_chapters(self):
        ids, rows = ms.choose(COURSE, {"c3": 1.0, "c4": 0.7, "c2": 0.1}, "第三章 半导体激光器的 工作原理及类型 4")
        self.assertEqual(ids, ["c3"])  # 第04章 shares "半导体激光器" but is a later chapter
        reasons = {r["source_id"]: r["reasons"] for r in rows}
        self.assertTrue(any("章节号与雨课堂课次一致" in x for x in reasons["c3"]))
        self.assertTrue(any("不同" in x for x in reasons["c4"]))

    def test_strong_recap_of_previous_chapter_is_kept(self):
        ids, _ = ms.choose(COURSE, {"c1": 0.9, "c2": 1.0}, "第二章 半导体发光二极管 2")
        self.assertEqual(ids, ["c1", "c2"])

    def test_fallback_top_k_without_lesson_match(self):
        ids, rows = ms.choose(COURSE, {"c1": 0.2, "c2": 1.0, "c3": 0.5, "c4": 0.4}, "9月5日授课")
        self.assertEqual(ids, ["c2", "c3"])
        self.assertEqual(rows[0]["mode"], "retrieval_top_k")

    def test_no_signal_keeps_everything(self):
        ids, rows = ms.choose(COURSE, {}, "")
        self.assertEqual(len(ids), 5)
        self.assertEqual(rows[0]["mode"], "no_signal_all")

    def test_user_selection_is_respected(self):
        ids, rows = ms.choose(COURSE[:2], {"c0": 1}, "第四章", selected=["c0", "c1"])
        self.assertEqual(ids, ["c0", "c1"])
        self.assertIn("用户手动补选", rows[0]["reasons"][0])

    def test_intro_maps_to_chapter_one_and_stale_title_is_dropped(self):
        course = [src("a", "通信原理 C-第01章-课件.pdf"), src("b", "通信原理 C-第05章-模拟通信系统-课件.pdf"),
                  src("s", "通信原理 C-第04章-信道-课件.pdf", pages=[{"page": 1, "text": ""}])]
        self.assertEqual(ms.choose(course, {"a": 1.0, "b": 0.1}, "通信原理——绪论（2）")[0], ["a"])
        ids, rows = ms.choose(course, {"a": 0.05, "b": 1.0}, "通信原理——绪论")
        self.assertEqual(ids, ["b"])
        self.assertTrue(any("标题可能沿用旧课次" in x for x in rows[0]["reasons"]))

    def test_explicit_chapter_keeps_scanned_files_without_text(self):
        course = [src("a", "第01章-课件.pdf"), src("b", "第04章-信道-课件.pdf", pages=[{"page": 1, "text": ""}])]
        self.assertEqual(ms.choose(course, {"a": 1.0}, "第四章 信道（2）")[0], ["a", "b"])

    def test_cap(self):
        many = [src(f"x{i}", f"第02章-资料{i}.pdf") for i in range(6)]
        ids, rows = ms.choose(many, {f"x{i}": 1 for i in range(6)}, "第二章")
        self.assertEqual(len(ids), ms.MAX_SOURCES)
        self.assertTrue(any("上限" in x for r in rows if not r["chosen"] for x in r["reasons"]))

    def test_summary_lists_chosen_files(self):
        _, rows = ms.choose(COURSE, {"c4": 1.0}, "第四章 半导体激光器的性能 1")
        self.assertEqual(ms.summary(rows, "第四章 半导体激光器的性能 1"),
                         "按雨课堂课次「第四章 半导体激光器的性能 1」选用 1/5 份课件：光电子学-第04章-半导体激光器的性能-课件.pdf")


if __name__ == "__main__":
    unittest.main()


class AIFriendlyTextTests(unittest.TestCase):
    def run_collect(self, conversion):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from qwen_asr_app import review_materials as rm
        tmp = Path(tempfile.mkdtemp())
        original = tmp / 'orig.pdf'; original.write_bytes(b'%PDF original')
        derived = tmp / 'ai.pdf'; derived.write_bytes(b'%PDF ai')
        digest = rm.digest_file(original)
        item = {'id': 'f1', 'name': '第01章-课件.pdf', 'sha256': digest, 'conversion': {**conversion, 'source_sha256': digest}}
        files = SimpleNamespace(list=lambda c: [item], file=lambda c, i, v: (None, original if v == 'original' else derived))
        settings = SimpleNamespace(categories=['课'], storage_root=tmp)
        record = SimpleNamespace(id='r', metadata={'category': '课', 'started_at': ''}, segments=[], audio_path=tmp / 'none.m4a')
        blocks = [{'start': 0, 'end': 1, 'text': '麦克斯韦方程组'}]
        with patch.object(rm, 'CourseFiles', return_value=files), \
             patch.object(rm, 'native_page_cache', return_value=[{'page': 1, 'text': '原件文字' * 20, 'visual_source': True, 'image_primary': False}]), \
             patch('qwen_asr_app.native_pdf.pdf_operation', return_value=[{'text': '[[AI_TRANSCRIPT_PAGE:1]] # 麦克斯韦方程组'}]), \
             patch('qwen_asr_app.knowledge.ingest.ensure_course_id', return_value='cid'), \
             patch('qwen_asr_app.knowledge.store.KnowledgeStore'), \
             patch.object(rm, 'transcript_blocks', return_value=blocks), \
             patch('qwen_asr_app.classroom_intelligence.integrations.saved_notes_markdown', return_value=''), \
             patch('qwen_asr_app.material_scope.lesson_title_for', return_value=''):
            return rm.collect(record, tmp / 'out', settings=settings)

    def test_up_to_date_ai_friendly_text_is_used_for_text_pages(self):
        package = self.run_collect({'status': 'completed'})
        self.assertEqual(package['sources'][0]['text_source'], 'ai_friendly')
        self.assertIn('麦克斯韦方程组', package['sources'][0]['pages'][0]['text'])
        self.assertFalse(any('AI友好型' in w for w in package['warnings']))

    def test_missing_ai_friendly_version_falls_back_with_visible_warning(self):
        package = self.run_collect({'status': 'failed'})
        self.assertEqual(package['sources'][0]['text_source'], 'original')
        self.assertTrue(any('AI友好型版本缺少' in w for w in package['warnings']))
