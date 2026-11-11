from tests import isolation  # noqa: F401

import asyncio
import unittest
from unittest import mock

from qwen_asr_app import section_page_review as rv
from qwen_asr_app import section_pages as sp


def section(index, heading, page, status='auto'):
    return {'section_id': f'h{index}', 'index': index, 'level': 2, 'heading': heading, 'path': [heading], 'status': status,
            'targets': [{'source_id': 's', 'viewer_sha256': None, 'page_start': page, 'page_end': page,
                         'viewer_page_start': page, 'viewer_page_end': page}]}


class ReviewTests(unittest.TestCase):
    def test_agy_long_context_reviews_the_entire_note_and_confirms_moves_concurrently(self):
        md='# t\n'+'\n'.join(f'## Topic {n}\nbody {n}' for n in range(1,26))
        doc={'sections':[section(n,f'Topic {n}',3) for n in range(1,26)]}
        sources=[{'source_id':'s','pages':[f'page {n}' for n in range(1,8)]}]
        config={'ai':{'summary_provider':{'transport':'agy_cli','cli_model':'gemini-3.8-flash-high','context_window':1048576,'concurrency':3}}}
        batches=[];active=peak=0
        async def fake(*,payload,**kwargs):
            nonlocal active,peak
            if 'items' in payload:
                batches.append(len(payload['items']))
                return {'items':[{'id':item['id'],'page':4,'sure':True} for item in payload['items']]}
            active+=1;peak=max(peak,active)
            await asyncio.sleep(.001)
            active-=1
            return {'ok':True,'why':'原件主题起始页'}
        with mock.patch('qwen_asr_app.ai.request_ai_json',fake):
            stats=asyncio.run(rv.review(doc,md,sources,config))
        self.assertEqual(batches,[25]);self.assertEqual(peak,3)
        self.assertEqual(stats['verified'],25)
        self.assertTrue(all(s['targets'][0]['page_start']==4 for s in doc['sections']))

    def run_review(self, answers, confirm=True):
        md = '# t\n## A\nalpha\n## B\nbeta\n## C\ngamma\n## D\ndelta\n'
        doc = {'sections': [section(1, 'A', 3), section(2, 'B', 5), section(3, 'C', 7), section(4, 'D', 9, 'verified')]}
        doc['sections'][3]['review'] = {'by': 'agent-pilot'}
        sources = [{'source_id': 's', 'pages': [f'page {n}' for n in range(1, 13)]}]

        async def fake(*, instructions, payload, **kw):
            if 'items' in payload: return {'items': answers}
            return {'ok': confirm, 'why': 'x'}
        with mock.patch('qwen_asr_app.ai.request_ai_json', fake):
            stats = asyncio.run(rv.review(doc, md, sources, {}))
        return doc, stats

    def test_only_sure_pages_in_window_are_verified(self):
        doc, stats = self.run_review([{'id': 'h1', 'page': 3, 'sure': True}, {'id': 'h2', 'page': 5, 'sure': False},
                                      {'id': 'h3', 'page': 1, 'sure': True}])
        a, b, c, d = doc['sections']
        self.assertEqual(a['status'], 'verified')
        self.assertEqual(b['status'], 'review')          # not sure → hidden
        self.assertEqual(c['status'], 'review')          # page outside the pages shown → hidden
        self.assertEqual(d['review'], {'by': 'agent-pilot'})  # earlier verdicts are not re-sent
        self.assertEqual(stats['verified'], 1)

    def test_moved_page_needs_confirmation(self):
        doc, _ = self.run_review([{'id': 'h1', 'page': 4, 'sure': True}], confirm=False)
        self.assertEqual(doc['sections'][0]['status'], 'review')
        doc, _ = self.run_review([{'id': 'h1', 'page': 4, 'sure': True}], confirm=True)
        self.assertEqual(doc['sections'][0]['status'], 'verified')
        self.assertEqual(doc['sections'][0]['targets'][0]['page_start'], 4)

    def test_unreviewed_auto_links_are_not_public(self):
        md = '# t\n## A\nalpha\n'
        doc = {'sections': [section(1, 'A', 3)]}
        self.assertEqual(sp.public_view(doc, md)['sections'], [])
        doc['sections'][0]['status'] = 'verified'
        self.assertEqual(len(sp.public_view(doc, md)['sections']), 1)


if __name__ == '__main__':
    unittest.main()
