from tests import isolation  # noqa: F401

import unittest
from copy import deepcopy

from qwen_asr_app import section_pages as sp


def page(n, *lines):
    return '\n'.join([f'[[AI_TRANSCRIPT_PAGE:{n}]]', f'SOURCE_PDF_PAGE: {n}', *lines, f'[[END_AI_TRANSCRIPT_PAGE:{n}]]'])


FILLER = ['[TEXT] 课程概述与学习安排', '[TEXT] 本章内容回顾']


class SectionPageTests(unittest.TestCase):
    def test_headings_and_exclusions(self):
        md = '# 笔记标题\n\n## 本节要点\n- a\n\n## 2. 端口概念\n正文\n### 2.1 端口的定义\n内容\n'
        items = sp.headings(md)
        by_index = {i['index']: i for i in items}
        self.assertEqual([i['text'] for i in items], ['笔记标题', '本节要点', '2. 端口概念', '2.1 端口的定义'])
        self.assertTrue(sp.exclusion(items[0], by_index))
        self.assertTrue(sp.exclusion(items[1], by_index))
        self.assertFalse(sp.exclusion(items[3], by_index))

    def test_page_links_are_on_until_explicitly_disabled(self):
        if sp.SETTINGS.exists():
            sp.SETTINGS.unlink()
        self.assertTrue(sp.enabled())
        sp.set_enabled(False)
        self.assertFalse(sp.enabled())
        sp.set_enabled(True)
        self.assertTrue(sp.enabled())

    def test_viewer_page_uses_marker_map(self):
        page_map = {'method': 'ai-transcript-markers', 'source_to_viewer': {'1': 2, '82': 83}}
        self.assertEqual(sp.to_viewer(page_map, 82), 83)
        self.assertIsNone(sp.to_viewer(page_map, 5))
        self.assertEqual(sp.to_viewer({'method': 'identity'}, 5), 5)

    def test_numbered_divider_line_opens_topic(self):
        # page 3 carries a short "4. DMA传送的过程" divider at its bottom; the
        # detailed content (best text match) is on page 4
        pages = [page(1, *FILLER), page(2, '[TEXT] 中断方式的特点与响应过程', '[TEXT] 中断向量表'),
                 page(3, '[TEXT] 存储器之间的数据块传送说明', '4. DMA传送的过程'),
                 page(4, '[TEXT] HOLD HLDA 总线保持请求 总线响应 DREQ DACK 联络信号',
                      '[TEXT] DMAC 获得总线控制权后发出地址与读写信号，联络信号 DREQ DACK HRQ HLDA'),
                 page(5, '[TEXT] DMA传送与中断方式的比较', '[TEXT] 响应时间 数据传送速度')]
        md = ('# 微机\n\n## 5. 中断方式\n中断方式的特点与响应过程，中断向量表。\n\n'
              '## DMA 传送的核心过程与联络信号\nDMAC 通过 HOLD/HLDA 总线保持请求与总线响应，外设用 DREQ/DACK 联络信号。\n\n'
              '## DMA 传送与中断方式的比较\n响应时间与数据传送速度的比较。\n')
        sources = [{'source_id': 's', 'name': 'x.pdf', 'sha256': 'a' * 64, 'pages': pages}]
        out = {s['heading']: s for s in sp.build_links(md, sources, [], {})}
        target = out['DMA 传送的核心过程与联络信号']['targets'][0]
        self.assertEqual(target['page_start'], 3)
        self.assertEqual(out['DMA 传送与中断方式的比较']['targets'][0]['page_start'], 5)

    def test_public_view_hides_unchecked_and_changed_headings(self):
        md = '# T\n\n## A 节\n正文\n\n## B 节\n正文\n'
        target = {'source_id': 's', 'page_start': 3, 'page_end': 3, 'viewer_page_start': 4, 'viewer_page_end': 4}
        doc = {'sections': [
            {'index': 1, 'heading': 'A 节', 'section_id': 'a', 'status': 'verified', 'targets': [target]},
            {'index': 2, 'heading': 'B 节', 'section_id': 'b', 'status': 'review', 'targets': [target]}]}
        view = sp.public_view(doc, md)
        self.assertEqual([s['heading'] for s in view['sections']], ['A 节'])
        view = sp.public_view(doc, md.replace('A 节', 'A 节（改）'))
        self.assertEqual(view['sections'], [])


class ReviewCacheTests(unittest.TestCase):
    markdown = '# T\n\n## 主题 P3\n原来的讲解\n\n## 其他 P4\n别的内容\n'

    def document(self, markdown=None, source_sha='a' * 64, viewer_sha='b' * 64):
        markdown = self.markdown if markdown is None else markdown
        sources = [{'source_id': 's', 'name': 'deck.pdf', 'sha256': source_sha,
                    'viewer_sha256': viewer_sha, 'page_mapping_method': 'identity', 'pages': ['slide'] * 6}]
        maps = {'s': {'method': 'identity', 'sha256': viewer_sha}}
        return sp.document('rec12345', markdown, sp.build_links(markdown, sources, [], maps), sources)

    def checked(self):
        doc = self.document()
        section = doc['sections'][1]
        section.update(status='verified', review={'by': 'ai-review-v1'})
        section['targets'][0]['viewer_page_start'] = 4
        return doc

    def test_unchanged_section_keeps_its_review_when_another_section_changes(self):
        old = self.checked()
        new = self.document(self.markdown.replace('别的内容', '重新写的其他小节'))
        section = sp.merge_verified(new, old)['sections'][1]
        self.assertEqual(section['status'], 'verified')
        self.assertEqual(section['targets'][0]['viewer_page_start'], 4)

    def test_changed_section_body_invalidates_review_even_with_the_same_heading(self):
        new = self.document(self.markdown.replace('原来的讲解', '完全不同的讲解'))
        section = sp.merge_verified(new, self.checked())['sections'][1]
        self.assertNotEqual(section['status'], 'verified')
        self.assertNotIn('review', section)
        self.assertEqual(section['targets'][0]['viewer_page_start'], 3)

    def test_changed_original_or_viewer_pdf_invalidates_review(self):
        for kwargs in [{'source_sha': 'c' * 64}, {'viewer_sha': 'd' * 64}]:
            with self.subTest(**kwargs):
                section = sp.merge_verified(self.document(**kwargs), self.checked())['sections'][1]
                self.assertNotEqual(section['status'], 'verified')
                self.assertNotIn('review', section)

    def test_legacy_reviews_require_the_whole_note_to_be_unchanged(self):
        old = self.checked()
        for section in old['sections']: section.pop('content_sha')
        self.assertEqual(sp.merge_verified(self.document(), old)['sections'][1]['status'], 'verified')
        changed = self.document(self.markdown.replace('别的内容', '新内容'))
        self.assertNotEqual(sp.merge_verified(changed, old)['sections'][1]['status'], 'verified')

    def test_a_stale_target_hash_is_not_reused_even_if_the_resource_list_matches(self):
        old = self.checked()
        old['sections'][1]['targets'][0]['viewer_sha256'] = 'c' * 64
        self.assertNotEqual(sp.merge_verified(self.document(), old)['sections'][1]['status'], 'verified')

    def test_descendant_edits_invalidate_the_parent_fingerprint(self):
        first = sp.headings('# T\n## Parent\nintro\n### Child\nold\n')
        second = sp.headings('# T\n## Parent\nintro\n### Child\nnew\n')
        self.assertNotEqual(first[1]['content_sha'], second[1]['content_sha'])

    def test_changed_live_body_hides_the_previously_verified_button(self):
        doc = self.checked()
        self.assertEqual(len(sp.public_view(doc, self.markdown)['sections']), 1)
        changed = self.markdown.replace('原来的讲解', '新讲解')
        self.assertEqual(sp.public_view(doc, changed)['sections'], [])
        legacy = deepcopy(doc)
        for section in legacy['sections']: section.pop('content_sha')
        self.assertEqual(sp.public_view(legacy, changed)['sections'], [])


if __name__ == '__main__':
    unittest.main()
