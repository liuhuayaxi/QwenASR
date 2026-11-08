from tests import isolation  # noqa: F401

import unittest

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


if __name__ == '__main__':
    unittest.main()
