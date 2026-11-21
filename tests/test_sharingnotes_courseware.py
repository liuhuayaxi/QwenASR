from tests import isolation  # Must precede application imports.

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import pymupdf

from qwen_asr_app import courseware_publish as cp
from qwen_asr_app.sharingnotes_courseware import CoursewareSource, material_id
from qwen_asr_app.sharingnotes_export import export_note
from qwen_asr_app.sharingnotes_mock_api import MockPublishAPI
from qwen_asr_app.sharingnotes_publish import AutoPublisher, Publisher, PublishError, PublishState, build_upsert
from tests.test_sharingnotes_publish import CONTRACT, COURSE, FIGURE, png, session

ASSET = 'c' * 32


def make_pdf(path, pages, label):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for n in range(pages):
        doc.new_page(width=320, height=240).insert_text((40, 120), f'{label} {n + 1}')
    doc.save(path)


class FakeCourseFiles:
    def __init__(self, root, items):
        self.root, self.items = root, items

    def folder(self, course, create=False):
        return self.root

    def list(self, course):
        return copy.deepcopy(self.items)


class CoursewareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.image = png()
        self.pdf = self.tmp / 'files' / ASSET / 'PDF' / '第一章.pdf'
        make_pdf(self.pdf, 2, 'v1')
        self.item = {'id': ASSET, 'name': '第一章.pptx', 'kind': '课件',
                     'conversion': {'status': 'completed', 'name': '第一章.pdf', 'pages': 2,
                                    'source_pages': 2, 'updated_at': '2026-03-01T00:00:00+08:00'}}
        self.files = FakeCourseFiles(self.tmp / 'files', [self.item])
        self.links = self.tmp / 'links'
        self.links.mkdir()
        self.renders = []

        def render(pdf, folder, sha):
            self.renders.append(sha)
            return cp.render_deck(pdf, folder, sha)
        self.source = CoursewareSource(self.files, links_dir=self.links, cache_dir=self.tmp / 'cache',
                                       enabled=lambda: True, render=render, local_pages=self.tmp / 'none')
        self.api = MockPublishAPI()
        self.state = PublishState(self.tmp / 'state.json')
        self.publisher = Publisher(self.api, [COURSE], self.state, courseware=self.source)

    def read(self, name):
        return self.image

    def note_session(self, ident='rec-1', title='第一课'):
        value = session(id=ident, title=title)
        value['ai_note_summary']['material_version'] = {'course_files': {ASSET: 'x' * 64}}
        return value

    def link(self, value):
        note = export_note(value, self.read)['note']
        sha = self.source.digest(self.pdf)
        (self.links / f"{value['id']}.json").write_text(json.dumps({
            'mapping_version': 'm' * 16, 'note_body_sha': note['sourceBodySha256'],
            'sections': [{'index': 0, 'heading': '傅里叶', 'status': 'verified', 'targets': [
                {'viewer_sha256': sha, 'page_start': 1, 'page_end': 1,
                 'viewer_page_start': 1, 'viewer_page_end': 2}]}]}), encoding='utf-8')
        return value

    def puts(self):
        return [path.split('/')[-2] for method, path in self.api.calls if method == 'PUT']

    def publish(self, value):
        return self.publisher.publish_session(value, self.read)

    def test_first_publish_uploads_pdf_manifest_and_pages_once(self):
        result = self.publish(self.link(self.note_session()))
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(sorted(self.puts()), ['image', 'manifest', 'page', 'page', 'page', 'page', 'pdf'])
        stored = self.api.notes[result['id']]
        material = stored['materials'][0]
        self.assertEqual(material['id'], material_id(COURSE, ASSET))
        self.assertEqual(material['sha256'], hashlib.sha256(self.pdf.read_bytes()).hexdigest())
        self.assertEqual(material['url'], f"/media/pdf-{material['sha256']}.pdf")
        page_set = stored['pageSets'][0]
        self.assertEqual(page_set['manifest']['pdf_sha256'], material['sha256'])
        self.assertEqual([f['page'] for f in page_set['files']], [1, 2])
        self.assertEqual(stored['pageLinks']['sections'][0]['targets'][0]['pdf'], material['id'])
        self.assertEqual(set(stored['references']['pages']),
                         {h for f in page_set['files'] for h in (f['sha256'], f['thumbnailSha256'])})

        self.api.calls.clear()
        other = self.publish(self.link(self.note_session('rec-2', '第二课')))
        self.assertEqual(other['status'], 'accepted')
        self.assertEqual(self.puts(), [])  # shared deck and image: nothing re-sent
        self.assertEqual(self.api.calls, [('POST', '/api/publish/v1/notes')])
        self.assertEqual(len(self.renders), 1)

    def test_upsert_matches_contract_schema(self):
        import jsonschema
        from referencing import Registry, Resource
        schemas = {p.name: json.loads(p.read_text()) for p in CONTRACT.glob('*.schema.json')}
        registry = Registry().with_resources((s['$id'], Resource.from_contents(s)) for s in schemas.values())
        value = self.link(self.note_session())
        exported = export_note(value, self.read)
        extra = self.source(exported, value)
        extra.pop('blobs')
        message = build_upsert(exported, None, **extra)
        jsonschema.Draft202012Validator(schemas['upsert-note.schema.json'], registry=registry).validate(message)
        jsonschema.Draft202012Validator(schemas['pages-manifest.schema.json'], registry=registry).validate(
            message['pageSets'][0]['manifest'])

    def test_note_edit_does_not_resend_courseware(self):
        value = self.link(self.note_session())
        self.publish(value)
        self.api.calls.clear()
        self.assertEqual(self.publish(value)['status'], 'unchanged')
        self.assertEqual(self.api.calls, [])
        value['title'] = '第一课（修订）'
        self.assertEqual(self.publish(value)['status'], 'accepted')
        self.assertEqual(self.puts(), [])

    def test_changed_pdf_uploads_only_new_hashes_and_republishes_sharing_notes(self):
        first, second = self.link(self.note_session()), self.link(self.note_session('rec-2', '第二课'))
        results = self.publisher.sync([('rec-1', lambda: first), ('rec-2', lambda: second)], self.read)
        old = {r['session']: r for r in results}
        old_pdf = self.api.notes[old['rec-2']['id']]['materials'][0]['sha256']
        make_pdf(self.pdf, 2, 'v2-' + 'x' * 10)  # new PDF bytes, one page changed text
        self.link(first)  # first note's links reviewed again against the new PDF
        self.api.calls.clear()
        loaders = {'rec-1': first, 'rec-2': second}
        results = self.publisher.sync([('rec-1', lambda: first)], self.read, loader=lambda i: (lambda: loaders[i]))
        self.assertEqual([(r['session'], r['status']) for r in results], [('rec-1', 'accepted'), ('rec-2', 'accepted')])
        self.assertEqual(results[0]['requeued'], ['rec-2'])
        self.assertEqual(results[1]['replaced'], old['rec-2']['version'])  # expectedVersion = current version
        self.assertIn('pdf', self.puts())
        self.assertNotIn('image', self.puts())
        stored = self.api.notes[old['rec-2']['id']]
        self.assertNotEqual(stored['materials'][0]['sha256'], old_pdf)
        # rec-2's links still name the old PDF, so they are withheld instead of pointing at stale pages.
        self.assertEqual(stored['pageLinks']['sections'], [])
        self.assertEqual(self.state.pending, [])

    def test_blob_the_server_lost_is_uploaded_on_replay(self):
        self.publish(self.link(self.note_session()))
        self.api.stored.clear()
        self.api.calls.clear()
        self.publish(self.link(self.note_session('rec-2', '第二课')))
        self.assertEqual(sorted(self.puts()), ['image', 'manifest', 'page', 'page', 'page', 'page', 'pdf'])

    def test_server_reset_registers_skipped_blobs_again(self):
        self.publish(self.link(self.note_session()))
        self.api.stored.clear()
        self.api.descriptors.clear()
        result = self.publish(self.link(self.note_session('rec-2', '第二课')))
        self.assertEqual(result['status'], 'accepted')

    def test_stale_or_unreviewed_links_are_withheld(self):
        value = self.link(self.note_session())
        doc = json.loads((self.links / 'rec-1.json').read_text())
        doc['sections'][0]['status'] = 'candidate'
        (self.links / 'rec-1.json').write_text(json.dumps(doc))
        result = self.publish(value)
        stored = self.api.notes[result['id']]
        self.assertEqual(stored['pageLinks']['sections'], [])
        self.assertEqual(stored['pageSets'], [])
        self.assertEqual(len(stored['materials']), 1)  # the note's PDF is still published

    def test_pdf_outside_the_course_folder_is_ignored(self):
        outside = self.tmp / 'secret.pdf'
        make_pdf(outside, 1, 'secret')
        self.item['conversion']['relative_path'] = '../secret.pdf'
        result = self.publish(self.note_session())
        self.assertEqual(self.api.notes[result['id']]['materials'], [])
        self.assertNotIn('pdf', self.puts())

    def test_unconverted_material_is_not_published(self):
        self.item['conversion'] = {'status': 'running'}
        result = self.publish(self.note_session())
        self.assertEqual(self.api.notes[result['id']]['materials'], [])


    def test_read_failure_retries_instead_of_dropping_courseware(self):
        value = self.note_session()
        first = self.publish(value)
        self.assertEqual(len(self.api.notes[first['id']]['materials']), 1)

        def broken(course):
            raise OSError('disk unavailable')
        self.files.list = broken
        with self.assertRaises(PublishError):
            self.publish(self.note_session(title='改标题'))
        self.assertEqual(len(self.api.notes[first['id']]['materials']), 1)

    def test_invalid_unrelated_pdf_does_not_block_the_note(self):
        other = 'd' * 32
        bad = self.tmp / 'files' / other / 'PDF' / '坏.pdf'
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b'not a pdf')
        self.files.items.append({'id': other, 'name': '坏.pptx', 'kind': '课件',
                                 'conversion': {'status': 'completed', 'name': '坏.pdf'}})
        result = self.publish(self.note_session())
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual([m['id'] for m in self.api.notes[result['id']]['materials']],
                         [material_id(COURSE, ASSET)])
        value = self.note_session('rec-2')
        value['ai_note_summary']['material_version']['course_files'] = {other: 'x' * 64}
        self.assertEqual(self.publish(value)['status'], 'invalid')

    def test_link_review_and_conversion_wake_the_auto_publisher(self):
        from qwen_asr_app import section_pages as sp
        auto = AutoPublisher(None)
        auto.publisher = Publisher(self.api, [COURSE], self.state)
        auto.mark([str(sp.LINKS / 'rec-9.json')])
        self.assertEqual(self.state.pending, ['rec-9'])
        self.assertFalse(auto.full)
        auto.mark([str(self.tmp / 'files' / ASSET / 'asset.json')])
        self.assertTrue(auto.full)


if __name__ == '__main__':
    unittest.main()
