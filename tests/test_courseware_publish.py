from tests import isolation  # noqa: F401

import hashlib
import json
import subprocess
import unittest
from pathlib import Path

from qwen_asr_app import courseware_publish as pub
from qwen_asr_app import section_pages as sp
from qwen_asr_app.batch_ai import _section_page_links


SHA = 'a' * 64
OTHER = 'b' * 64
OLD = 'c' * 64


def git(repo, *args, env):
    subprocess.run(['git', *args], cwd=repo, check=True, env=env, stdout=subprocess.DEVNULL)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.id().split('.')[-1])
        # isolation already points private data at a temp dir; use another folder beside the links.
        self.base = sp.ROOT.parent / 'publish-fixture' / self.root.name
        if self.base.exists():
            import shutil
            shutil.rmtree(self.base)
        self.sharing = self.base / 'sharingnotes'
        self.data = self.sharing / 'docs' / 'data'
        self.notes = self.data / 'notes'
        self.notes.mkdir(parents=True)
        self.pdf = self.base / 'deck.pdf'
        self.pdf.write_bytes(b'%PDF-1.4 fixture')
        self.assets = self.base / 'prepared-assets.json'
        self.assets.write_text(json.dumps([{'sha256': SHA, 'path': str(self.pdf), 'bytes': self.pdf.stat().st_size}]), encoding='utf-8')
        self.ident = hashlib.sha256(b'course:' + b'rec12345').hexdigest()[:20]
        (self.notes / 'note.json').write_text(json.dumps({'markdown': '# 标题\n\n## 端口\n正文\n'}), encoding='utf-8')
        (self.data / 'catalog.json').write_text(json.dumps({'notes': [{
            'id': self.ident, 'file': 'notes/note.json', 'version': 'v1',
        }]}), encoding='utf-8')
        (self.data / 'resources.json').write_text(json.dumps({'version': 2, 'pdfs': [{
            'id': 'pdf1', 'sha256': SHA, 'title': '课件', 'url': 'https://github.com/liuhuayaxi/sharingnotes/releases/download/course-media/pdf-' + SHA + '.pdf',
        }]}), encoding='utf-8')
        link = {
            'recording_id': 'rec12345', 'category': 'course', 'mapping_version': 'section-pages-v1',
            'sections': [
                {'index': 1, 'heading': '端口', 'status': 'verified', 'targets': [{
                    'viewer_sha256': SHA, 'page_start': 2, 'page_end': 2, 'viewer_page_start': 3, 'viewer_page_end': 3,
                }]},
                {'index': 1, 'heading': '端口', 'status': 'review', 'targets': [{
                    'viewer_sha256': SHA, 'page_start': 9, 'page_end': 9, 'viewer_page_start': 9, 'viewer_page_end': 9,
                }]},
                {'index': 1, 'heading': '别的标题', 'status': 'verified', 'targets': [{
                    'viewer_sha256': SHA, 'page_start': 4, 'page_end': 4, 'viewer_page_start': 4, 'viewer_page_end': 4,
                }]},
            ],
        }
        sp.LINKS.mkdir(parents=True, exist_ok=True)
        (sp.LINKS / 'rec12345.json').write_text(json.dumps(link), encoding='utf-8')
        orphan = self.data / 'pages' / OLD
        orphan.mkdir(parents=True)
        (orphan / 'manifest.json').write_text('{}', encoding='utf-8')
        self.calls = []

    def render(self, pdf, folder, sha):
        before = pdf.read_bytes(), pdf.stat().st_mtime_ns
        self.calls.append(sha)
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text(json.dumps({
            'version': pub.VERSION, 'pdf_sha256': sha, 'pages': 1, 'format': 'webp', 'sizes': [[8, 8]],
        }), encoding='utf-8')
        (folder / '1.webp').write_bytes(b'full')
        (folder / 't1.webp').write_bytes(b'thumb')
        self.assertEqual((pdf.read_bytes(), pdf.stat().st_mtime_ns), before)

    def test_export_keeps_only_verified_matching_headings(self):
        result, report = pub.export_page_links(sp.LINKS, self.data, True)
        sections = result['notes'][self.ident]['sections']
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]['heading'], '端口')
        self.assertEqual(sections[0]['targets'][0]['pdf'], 'pdf1')
        self.assertEqual(sections[0]['targets'][0]['viewer_page_start'], 3)
        self.assertTrue(any('1 links' in row[1] for row in report))
        resources = (self.data / 'resources.json').read_text(encoding='utf-8')
        self.assertIn('releases/download/course-media/pdf-' + SHA, resources)

    def test_sync_renders_only_missing_decks_and_drops_orphans(self):
        pages = self.data / 'pages'
        ready = pages / OTHER
        ready.mkdir()
        (ready / 'manifest.json').write_text(json.dumps({'version': pub.VERSION, 'pdf_sha256': OTHER, 'pages': 1}), encoding='utf-8')
        (ready / '1.webp').write_bytes(b'f')
        (ready / 't1.webp').write_bytes(b't')
        first = pub.sync_viewer_pages(pages, pages, {SHA: self.pdf, OTHER: self.pdf}, render=self.render)
        self.assertEqual(first['rendered'], [SHA])
        self.assertEqual(first['skipped'], [OTHER])
        self.assertEqual(first['removed'], [OLD])
        self.assertFalse((pages / OLD).exists())
        second = pub.sync_viewer_pages(pages, pages, {SHA: self.pdf, OTHER: self.pdf}, render=self.render)
        self.assertEqual(second['rendered'], [])
        self.assertEqual(self.calls, [SHA])

    def test_publish_commits_links_and_images_without_touching_the_pdf_url(self):
        env = {**dict(**{k: v for k, v in __import__('os').environ.items()}),
               'GIT_AUTHOR_NAME': 'Publish Test', 'GIT_AUTHOR_EMAIL': 'publish@example.com',
               'GIT_COMMITTER_NAME': 'Publish Test', 'GIT_COMMITTER_EMAIL': 'publish@example.com'}
        git(self.sharing, 'init', '-b', 'main', env=env)
        git(self.sharing, 'add', 'docs', env=env)
        git(self.sharing, 'commit', '-m', 'base', env=env)
        url_before = (self.data / 'resources.json').read_bytes()
        result = pub.publish_courseware_view(
            self.sharing, assets_path=self.assets, links_dir=sp.LINKS, push=False, env=env, render=self.render,
        )
        self.assertEqual(result['commit'], 'committed')
        self.assertEqual((self.data / 'resources.json').read_bytes(), url_before)
        self.assertTrue((self.data / 'pages' / SHA / 't1.webp').is_file())
        self.assertFalse((self.data / 'pages' / OLD).exists())
        message = subprocess.run(['git', 'log', '-1', '--format=%s'], cwd=self.sharing, check=True, text=True, stdout=subprocess.PIPE, env=env).stdout.strip()
        self.assertEqual(message, pub.COMMIT_MESSAGE)
        names = subprocess.run(['git', 'show', '--name-only', '--format=', 'HEAD'], cwd=self.sharing, check=True, text=True, stdout=subprocess.PIPE, env=env).stdout
        self.assertNotIn(str(self.pdf), names)
        self.assertTrue(all(line.startswith('docs/data/') for line in names.splitlines() if line))
        commits = subprocess.run(['git', 'rev-list', '--count', 'HEAD'], cwd=self.sharing, check=True, text=True, stdout=subprocess.PIPE, env=env).stdout.strip()
        again = pub.publish_courseware_view(
            self.sharing, assets_path=self.assets, links_dir=sp.LINKS, push=False, env=env, render=self.render,
        )
        self.assertEqual(again['commit'], 'unchanged')
        self.assertEqual(again['pages']['rendered'], [])
        self.assertFalse(again['links_changed'])
        again_commits = subprocess.run(['git', 'rev-list', '--count', 'HEAD'], cwd=self.sharing, check=True, text=True, stdout=subprocess.PIPE, env=env).stdout.strip()
        self.assertEqual(again_commits, commits)

    def test_real_render_reads_the_page_marker_and_leaves_the_pdf_bytes(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest('pymupdf is not installed')
        pdf = self.base / 'real.pdf'
        document = pymupdf.open()
        page = document.new_page(width=640, height=360)
        page.insert_text((36, 36), '[[AI_TRANSCRIPT_PAGE:7]]')
        document.save(pdf)
        document.close()
        before = pdf.read_bytes()
        manifest = pub.render_deck(pdf, self.base / 'rendered' / SHA, SHA)
        self.assertEqual(pdf.read_bytes(), before)
        self.assertEqual(manifest['source_pages'], [7])
        self.assertEqual(manifest['sizes'], [[1280, 720]])
        self.assertTrue(pub.pages_current(self.base / 'rendered' / SHA, SHA))

    def test_page_links_stay_byte_for_byte_when_sections_are_reordered(self):
        pub.write_page_links(sp.LINKS, self.data, True)
        first = (self.data / 'page-links.json').read_bytes()
        path = sp.LINKS / 'rec12345.json'
        doc = json.loads(path.read_text(encoding='utf-8'))
        doc['sections'] = list(reversed(doc['sections']))
        for section in doc['sections']:
            section['targets'] = list(reversed(section.get('targets') or []))
        path.write_text(json.dumps(doc), encoding='utf-8')
        _result, _report, changed = pub.write_page_links(sp.LINKS, self.data, True)
        second = (self.data / 'page-links.json').read_bytes()
        self.assertEqual(first, second)
        self.assertFalse(changed)
        self.assertNotIn(b'publishedAt', first)
        self.assertNotIn(b'timestamp', first)

    def test_sync_site_flags_write_the_stage_and_do_not_commit(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest('pymupdf is not installed')
        document = pymupdf.open()
        page = document.new_page(width=320, height=180)
        page.insert_text((20, 20), '[[AI_TRANSCRIPT_PAGE:1]]')
        document.save(self.pdf)
        document.close()
        env = {**__import__('os').environ,
               'GIT_AUTHOR_NAME': 'Publish Test', 'GIT_AUTHOR_EMAIL': 'publish@example.com',
               'GIT_COMMITTER_NAME': 'Publish Test', 'GIT_COMMITTER_EMAIL': 'publish@example.com'}
        git(self.sharing, 'init', '-b', 'main', env=env)
        git(self.sharing, 'add', 'docs', env=env)
        git(self.sharing, 'commit', '-m', 'base', env=env)
        stage = self.base / 'stage'
        import shutil
        shutil.copytree(self.data, stage)
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'publish_courseware_view.py'
        args = [__import__('sys').executable, str(script), '--sharingnotes', str(self.sharing),
                '--stage', str(stage), '--no-commit', '--assets', str(self.assets)]
        subprocess.run(args, check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first = (stage / 'page-links.json').read_bytes()
        commits = subprocess.run(['git', 'rev-list', '--count', 'HEAD'], cwd=self.sharing, check=True, text=True, stdout=subprocess.PIPE, env=env).stdout.strip()
        subprocess.run(args, check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual((stage / 'page-links.json').read_bytes(), first)
        self.assertEqual(subprocess.run(['git', 'rev-list', '--count', 'HEAD'], cwd=self.sharing, check=True, text=True, stdout=subprocess.PIPE, env=env).stdout.strip(), commits)
        self.assertTrue((stage / 'pages' / SHA / 't1.webp').is_file())
        self.assertFalse((self.data / 'pages' / OLD).exists())

    def test_hook_detection_matches_the_sync_site_flags(self):
        repo = self.base / 'checkout'
        script = repo / 'scripts' / 'sync_site.py'
        script.parent.mkdir(parents=True)
        self.assertFalse(pub.sync_site_publishes_courseware(repo))
        script.write_text("subprocess.run([sys.executable, str(hook), '--sharingnotes', str(ROOT), '--stage', str(stage), '--no-commit'])\n")
        self.assertFalse(pub.sync_site_publishes_courseware(repo))
        script.write_text(script.read_text(encoding='utf-8') + 'publish_courseware_view.py\n')
        self.assertTrue(pub.sync_site_publishes_courseware(repo))

    def test_a_missing_pdf_does_not_delete_existing_images(self):
        self.assets.write_text('[]', encoding='utf-8')
        with self.assertRaises(ValueError):
            pub.publish_courseware_view(self.sharing, assets_path=self.assets, links_dir=sp.LINKS, commit=False, render=self.render)
        self.assertTrue((self.data / 'pages' / OLD).is_dir())


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_note_is_linked_without_a_prior_enable_step(self):
        if sp.SETTINGS.exists():
            sp.SETTINGS.unlink()
        from unittest.mock import AsyncMock, patch
        record = type('Record', (), {})()
        record.id = 'rec12345'
        record.directory = sp.ROOT / 'course' / '上课录音' / 'rec12345'
        record.directory.mkdir(parents=True)
        record.metadata = {'ai_note_summary': {'review_package': '.review-notes/pkg'}}
        try:
            with patch('qwen_asr_app.section_page_review.refresh', new=AsyncMock(return_value={'verified': 1})) as refresh:
                await _section_page_links(record, {'ai': {}})
            refresh.assert_awaited()
            sp.set_enabled(False)
            with patch('qwen_asr_app.section_page_review.refresh', new=AsyncMock()) as skipped:
                await _section_page_links(record, {'ai': {}})
            skipped.assert_not_awaited()
        finally:
            if sp.SETTINGS.exists():
                sp.SETTINGS.unlink()


if __name__ == '__main__':
    unittest.main()
