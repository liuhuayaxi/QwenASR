from tests import isolation  # Must precede application imports.

import asyncio
import copy
import importlib.util
import json
import os
import runpy
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from qwen_asr_app import sharingnotes_publish
from qwen_asr_app.sharingnotes_export import export_note
from qwen_asr_app.sharingnotes_mock_api import MockPublishAPI
from qwen_asr_app.sharingnotes_publish import (
    AutoPublisher, PublishConfig, PublishState, Publisher, build_upsert, session_ids_for_paths)

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / 'tests' / 'fixtures' / 'sharingnotes-contract'
COURSE = '信号与系统'
FIGURE = 'fig-0123456789abcdef.png'


def png(width=2, height=1, colour=b'\x00\xff\x00'):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    rows = b''.join(b'\x00' + colour * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def session(**changes):
    base = {
        'id': 'rec-1', 'status': 'completed', 'category': COURSE, 'title': '第一课',
        'recorded_at': '2026-03-02T08:00:00+08:00', 'chapter': '第一章',
        'segments': [{'id': 1, 'raw': '傅里叶变换', 'corrected': '傅里叶变换。'}],
        'ai_note_summary': {
            'summary': f'## 傅里叶\n正文。[原文 S1](#source-1)\n![频谱](assets/{FIGURE})',
            'abstract': '傅里叶变换入门', 'review_package': '.review-notes/' + 'a' * 24,
            'source_map': {'1': {'segment_ids': [1], 'start': 0, 'end': 5}}},
    }
    base.update(changes)
    return base


def edited(text):
    value = session()
    value['ai_note_summary']['summary'] += '\n' + text
    return value


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.image = png()
        self.api = MockPublishAPI()
        self.state = PublishState(self.tmp / 'state.json')
        self.publisher = Publisher(self.api, [COURSE], self.state)

    def read(self, name):
        self.assertEqual(name, FIGURE)
        return self.image

    def publish(self, value=None, **kwargs):
        return self.publisher.publish_session(value or session(), self.read, **kwargs)


class IncrementalPublishTests(Harness):
    def test_first_publish_uploads_image_then_switches_the_note(self):
        result = self.publish()
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['uploadedBlobs'], 1)
        self.assertEqual([c[0] for c in self.api.calls], ['POST', 'PUT', 'POST'])
        note = self.api.visible(result['id'])
        self.assertEqual(note['version'], result['version'])
        self.assertEqual(self.state.notes[result['id']]['version'], result['version'])

    def test_unchanged_note_sends_no_request(self):
        self.publish()
        calls = len(self.api.calls)
        self.assertEqual(self.publish()['status'], 'unchanged')
        self.assertEqual(len(self.api.calls), calls)

    def test_edit_replaces_the_previous_version_without_reuploading_images(self):
        first = self.publish()
        self.api.calls.clear()
        second = self.publish(edited('补充一句。'))
        self.assertEqual(second['status'], 'accepted')
        self.assertNotEqual(second['version'], first['version'])
        self.assertEqual(second['replaced'], first['version'])
        self.assertEqual(second['uploadedBlobs'], 0)
        self.assertNotIn('PUT', [c[0] for c in self.api.calls])

    def test_title_change_republishes_the_same_semantic_version(self):
        first = self.publish()
        second = self.publish(session(title='第一课（修订标题）'))
        self.assertEqual(second['status'], 'accepted')
        self.assertEqual(second['version'], first['version'])
        self.assertEqual(self.api.notes[first['id']]['catalogEntry']['title'], '第一课（修订标题）')

    def test_lost_response_stays_pending_and_replay_succeeds(self):
        self.state.mark(['rec-1'])
        loads = lambda: session()  # noqa: E731
        self.api.drop_next_response = True
        self.assertEqual(self.publisher.sync([('rec-1', loads)], self.read)[0]['status'], 'retry')
        self.assertEqual(self.state.pending, ['rec-1'])
        # The server finished the registration; the retried run completes the note.
        self.assertEqual(self.publisher.sync([('rec-1', loads)], self.read)[0]['status'], 'accepted')
        self.assertEqual(self.state.pending, [])

    def test_lost_upsert_response_is_an_idempotent_replay(self):
        exported = export_note(session(), self.read)
        self.publish(edited('先发一版'))
        self.state.notes.clear()  # local state lost: client resends create-only
        self.api.notes.clear()
        result = self.publish()
        self.assertEqual(result['status'], 'accepted')
        self.state.notes.clear()
        # Same id+version again with expectedVersion null: replay, not conflict.
        self.assertEqual(self.publish()['status'], 'accepted')
        self.assertEqual(self.api.visible(exported['note']['id'])['version'], exported['note']['version'])

    def test_conflict_is_recorded_and_not_retried(self):
        other = Publisher(self.api, [COURSE], PublishState(self.tmp / 'other.json'))
        other.publish_session(edited('另一台电脑的修改'), self.read)
        result = self.publish()
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(self.state.sessions['rec-1']['status'], 'conflict')
        self.assertNotIn(result['id'], self.state.notes)

    def test_server_missing_blob_is_uploaded_and_upsert_replayed(self):
        self.publish()
        self.api.stored.clear()
        result = self.publish(edited('新内容'))
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['uploadedBlobs'], 1)

    def test_other_courses_and_unfinished_notes_are_not_sent(self):
        self.assertEqual(self.publish(session(category='私人'))['status'], 'skipped_course')
        self.assertEqual(self.publish(session(status='recording'))['status'], 'skipped_unpublishable')
        self.assertEqual(self.api.calls, [])

    def test_wildcard_publishes_all_named_courses_but_not_unfinished_notes(self):
        self.assertEqual(Publisher(self.api, [], self.state).publish_session(session(), self.read)['status'],
                         'skipped_course')
        publisher = Publisher(self.api, ['*'], self.state)
        for category in (COURSE, '另一门课程'):
            self.assertEqual(publisher.publish_session(session(category=category), self.read)['status'], 'accepted')
        self.assertEqual(publisher.publish_session(session(category=''), self.read)['status'], 'skipped_course')
        self.assertEqual(publisher.publish_session(session(status='recording'), self.read)['status'],
                         'skipped_unpublishable')

    def test_dry_run_honors_wildcard_from_config_or_cli(self):
        script = str(ROOT / 'scripts' / 'publish_sharingnotes.py')
        config = self.tmp / 'config.json'
        config.write_text(json.dumps({'courses': ['*']}))
        for override, expected in (([], 'notes'), (['--courses', ''], 'skipped'), (['--courses', '*'], 'notes')):
            with (self.subTest(override=override), patch.dict(os.environ, {}, clear=True),
                  patch.object(sharingnotes_publish, 'STATE_DIR', self.tmp),
                  patch.object(sys, 'argv', [script, '--session', 'one', '--dry-run', *override]),
                  patch.object(sharingnotes_publish, 'store_loader',
                               return_value=lambda: session(ai_note_summary={'summary': '文本'})),
                  patch('builtins.print') as output):
                runpy.run_path(script)['main']()
                self.assertEqual(json.loads(output.call_args.args[0])[expected], 1)

    def test_invalid_image_is_recorded_without_requests(self):
        self.image = b'not a png'
        self.assertEqual(self.publish()['status'], 'invalid')
        self.assertEqual(self.api.calls, [])

    def test_state_survives_restart(self):
        self.publish()
        restarted = Publisher(self.api, [COURSE], PublishState(self.tmp / 'state.json'))
        self.assertEqual(restarted.publish_session(session(), self.read)['status'], 'unchanged')

    def test_create_only_replay_must_match_the_accepted_request(self):
        result = self.publish()
        message = build_upsert(export_note(session() | {'title': '别的标题'}, self.read), None)
        status, response = self.api.request('POST', '/api/publish/v1/notes', json.dumps(message).encode())
        self.assertEqual((status, response['status']), (409, 'conflict'))
        self.assertEqual(self.api.visible(result['id'])['version'], result['version'])

    def test_explicit_delete_with_tombstone_replay(self):
        result = self.publish()
        self.assertEqual(self.publisher.delete_note(result['id'], result['version'])['status'], 'deleted')
        self.assertIsNone(self.api.visible(result['id']))
        self.assertEqual(self.publisher.delete_note(result['id'], result['version'])['status'], 'deleted')
        self.assertTrue(self.state.notes[result['id']]['deleted'])

    def test_cli_exit_status_distinguishes_success_from_conflicts_and_errors(self):
        directory = self.tmp / 'mock'
        publisher = Publisher(MockPublishAPI(directory), [COURSE], self.state)
        published = publisher.publish_session(session(), self.read)
        base = [sys.executable, str(ROOT / 'scripts' / 'publish_sharingnotes.py'),
                '--mock', str(directory), '--courses', COURSE, '--state', str(self.state.path)]

        def cli(*args):
            return subprocess.run([*base, *args], capture_output=True, text=True, check=False, timeout=20)

        for version, status, code in [('0' * 64, 'conflict', 1), ('not-a-hash', 'rejected', 1),
                                      (published['version'], 'deleted', 0), (published['version'], 'deleted', 0)]:
            with self.subTest(status=status, version=version):
                result = cli('--delete', published['id'], '--expected-version', version, '--request-id', '1' * 32)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(json.loads(result.stdout)['status'], status)

        restore = Publisher(MockPublishAPI(directory), [COURSE], PublishState(self.state.path))
        self.assertEqual(restore.publish_session(session(), self.read, force=True)['status'], 'accepted')
        replay = cli('--delete', published['id'], '--expected-version', published['version'], '--request-id', '1' * 32)
        self.assertEqual(replay.returncode, 1, replay.stderr)
        self.assertEqual(json.loads(replay.stdout)['status'], 'conflict')
        new_delete = cli('--delete', published['id'], '--expected-version', published['version'])
        self.assertEqual(new_delete.returncode, 0, new_delete.stderr)
        self.assertEqual(json.loads(new_delete.stdout)['status'], 'deleted')

        missing = cli('--session', 'missing-recording')
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertEqual(json.loads(missing.stdout.splitlines()[-1]), {'missing': 1})

    def test_deleted_note_is_not_republished_by_reconcile(self):
        result = self.publish()
        self.publisher.delete_note(result['id'], result['version'])
        self.assertEqual(self.publish()['status'], 'skipped_deleted')
        self.assertIsNone(self.api.visible(result['id']))
        forced = self.publisher.publish_session(session(), self.read, force=True)
        self.assertEqual(forced['status'], 'accepted')
        self.assertEqual(self.api.visible(result['id'])['version'], result['version'])

    def test_mock_rejects_a_delete_replay_after_same_version_recreation(self):
        directory = self.tmp / 'mock'
        publisher = Publisher(MockPublishAPI(directory), [COURSE], self.state)
        note = publisher.publish_session(session(), self.read)
        self.assertEqual(publisher.delete_note(note['id'], note['version'])['status'], 'deleted')
        self.assertEqual(publisher.publish_session(session(), self.read, force=True)['status'], 'accepted')
        api = MockPublishAPI(directory)
        restarted = Publisher(api, [COURSE], self.state)
        self.assertEqual(restarted.delete_note(note['id'], note['version'])['status'], 'conflict')
        self.assertEqual(api.visible(note['id'])['version'], note['version'])

    def test_save_during_publication_stays_pending(self):
        self.state.mark(['rec-1'])

        def load():
            self.state.mark(['rec-1'])  # the user saves again while this snapshot publishes
            return session()
        self.assertEqual(self.publisher.sync([('rec-1', load)], self.read)[0]['status'], 'accepted')
        self.assertEqual(self.state.pending, ['rec-1'])

    def test_reconcile_failure_is_kept_for_retry(self):
        self.api.drop_next_response = True
        self.assertEqual(self.publisher.sync([('rec-1', session)], self.read)[0]['status'], 'retry')
        self.assertEqual(self.state.pending, ['rec-1'])

    def test_save_notification_does_not_overwrite_another_publishers_checkpoint(self):
        app_state = PublishState(self.tmp / 'state.json')  # the running app's copy, loaded earlier
        result = self.publish()  # a manual run publishes meanwhile
        app_state.mark(['rec-1'])
        reloaded = PublishState(self.tmp / 'state.json')
        self.assertEqual(reloaded.notes[result['id']]['version'], result['version'])
        self.assertEqual(reloaded.pending, ['rec-1'])

    def test_each_server_has_its_own_checkpoints(self):
        from qwen_asr_app.sharingnotes_publish import state_path
        self.assertNotEqual(state_path('https://a.example'), state_path('https://b.example'))
        self.assertEqual(state_path('https://a.example/'), state_path('https://a.example'))

    def test_missing_local_recording_is_never_deleted_remotely(self):
        result = self.publish()
        self.assertEqual(self.publisher.sync([('rec-1', lambda: None)], self.read)[0]['status'], 'missing')
        self.assertIsNotNone(self.api.visible(result['id']))


class MockContractTests(Harness):
    def test_tampered_blob_bytes_are_rejected(self):
        exported = export_note(session(), self.read)
        sha = next(iter(exported['images']))
        descriptor = {'contractVersion': 1, 'sha256': sha, 'kind': 'image', 'contentType': 'image/png',
                      'size': len(self.image)}
        status, _ = self.api.request('POST', '/api/publish/v1/blobs', json.dumps(descriptor).encode())
        self.assertEqual(status, 409)
        status, body = self.api.request('PUT', f'/api/publish/v1/blobs/image/{sha}', png(colour=b'\xff\x00\x00'),
                                        'image/png')
        self.assertEqual((status, body['status']), (400, 'rejected'))

    def test_unregistered_reference_is_rejected(self):
        message = build_upsert(export_note(session(), self.read))
        status, body = self.api.request('POST', '/api/publish/v1/notes', json.dumps(message).encode())
        self.assertEqual((status, body['status']), (400, 'rejected'))

    def test_messages_match_contract_schemas(self):
        import jsonschema
        from referencing import Registry, Resource
        schemas = {p.name: json.loads(p.read_text()) for p in CONTRACT.glob('*.schema.json')}
        registry = Registry().with_resources(
            (s['$id'], Resource.from_contents(s)) for s in schemas.values())
        def check(name, value):
            jsonschema.Draft202012Validator(schemas[name], registry=registry).validate(value)
        check('upsert-note.schema.json', build_upsert(export_note(session(), self.read)))
        check('upsert-note.schema.json', build_upsert(export_note(session(), self.read), 'a' * 64))
        check('note.schema.json', export_note(session(), self.read)['note'])
        check('delete-note.schema.json', {'contractVersion': 1, 'operation': 'delete_note',
                                          'id': 'a' * 20, 'expectedVersion': 'b' * 64})
        check('delete-note.schema.json', {'contractVersion': 1, 'operation': 'delete_note',
                                          'id': 'a' * 20, 'expectedVersion': 'b' * 64, 'requestId': 'c' * 32})
        original = self.api.request
        def recording(method, path, body=b'', content_type='application/json'):
            status, response = original(method, path, body, content_type)
            check('publish-response.schema.json', response)
            if path.endswith('/blobs'):
                check('blob.schema.json', json.loads(body))
            return status, response
        self.api.request = recording
        self.publish()
        self.publish(edited('x'))


def sharingnotes_exporter():
    for candidate in (os.environ.get('SHARINGNOTES_CODE_DIR'), ROOT.parent / 'sharingnotes-code',
                      ROOT.parent / 'sharingnotes'):
        if candidate and (Path(candidate) / 'scripts' / 'export_courses.py').is_file():
            spec = importlib.util.spec_from_file_location(
                'sharingnotes_export_courses', Path(candidate) / 'scripts' / 'export_courses.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


class ParityTests(Harness):
    def test_note_bytes_equal_the_static_exporter(self):
        original = sharingnotes_exporter()
        if original is None:
            self.skipTest('sharingnotes checkout not found (set SHARINGNOTES_CODE_DIR)')
        for value in (session(), edited('$x^2$ 与 <b>标签</b> 和 http://example.com 链接'),
                      session(chapter='', chapters=['第一章', '第二章'],
                              chapter_info={'sections': {'第一章': '傅里叶'}})):
            exported = export_note(value, self.read)
            note = original.public_note(copy.deepcopy(value),
                                        {f'assets/{FIGURE}': exported['note']['images'][0]['src']},
                                        exported['note']['images'])
            note = {**note, 'sources': original.published_sources(note['id'], note['sources'])}
            self.assertEqual(json.dumps(note, ensure_ascii=False, separators=(',', ':')).encode(), exported['body'])


class KeychainConfigTests(Harness):
    def test_official_site_reads_shared_item_when_explicit_token_is_absent(self):
        path = self.tmp / 'config.json'
        path.write_text(json.dumps({'url': 'https://coursenote.dczxyy.uk/', 'courses': [COURSE]}))
        command = ['/usr/bin/security', 'find-generic-password',
                   '-s', 'uk.dczxyy.coursenote.publish', '-a', 'sharingnotes', '-w']
        with (patch.dict(os.environ, {}, clear=True), patch.object(sys, 'platform', 'darwin'),
              patch.object(sharingnotes_publish.subprocess, 'run', return_value=subprocess.CompletedProcess(
                  command, 0, 'synthetic-keychain-token\n', '')) as run):
            config = PublishConfig.load(path)
        self.assertTrue(config.configured)
        self.assertEqual(config.token, 'synthetic-keychain-token')
        run.assert_called_once_with(command, capture_output=True, text=True, check=False, timeout=30)
        transport = config.transport()
        handler = next(h for h in transport._opener.handlers
                       if isinstance(h, sharingnotes_publish.urllib.request.HTTPRedirectHandler))
        request = sharingnotes_publish.urllib.request.Request(config.url, data=b'{}',
                                                             headers={'Authorization': 'Bearer synthetic-keychain-token'})
        self.assertIsNone(handler.redirect_request(request, None, 302, 'Found', {}, 'https://other.example/'))

    def test_explicit_token_precedence_does_not_access_keychain(self):
        path = self.tmp / 'config.json'
        path.write_text(json.dumps({'url': 'https://coursenote.dczxyy.uk', 'token': 'fixture-file', 'courses': [COURSE]}))
        with (patch.dict(os.environ, {}, clear=True), patch.object(sys, 'platform', 'darwin'),
              patch.object(sharingnotes_publish.subprocess, 'run') as run):
            self.assertEqual(PublishConfig.load(path).token, 'fixture-file')
            os.environ['SHARINGNOTES_PUBLISH_TOKEN'] = 'fixture-environment'
            self.assertEqual(PublishConfig.load(path).token, 'fixture-environment')
        run.assert_not_called()

    def test_other_destinations_and_non_mac_do_not_access_keychain(self):
        for platform, url in (
            ('darwin', 'https://coursenote.dczxyy.uk.other.example'),
            ('darwin', 'https://coursenote.dczxyy.uk/unrelated'),
            ('darwin', 'http://127.0.0.1:8799'),
            ('linux', 'https://coursenote.dczxyy.uk'),
        ):
            with (self.subTest(platform=platform, url=url),
                  patch.dict(os.environ, {'SHARINGNOTES_PUBLISH_URL': url, 'SHARINGNOTES_PUBLISH_COURSES': COURSE}, clear=True),
                  patch.object(sys, 'platform', platform), patch.object(sharingnotes_publish.subprocess, 'run') as run):
                self.assertFalse(PublishConfig.load(self.tmp / 'config.json').configured)
                run.assert_not_called()

    def test_missing_command_or_keychain_item_leaves_publishing_unconfigured(self):
        env = {'SHARINGNOTES_PUBLISH_URL': 'https://coursenote.dczxyy.uk', 'SHARINGNOTES_PUBLISH_COURSES': COURSE}
        with (patch.dict(os.environ, env, clear=True), patch.object(sys, 'platform', 'darwin'),
              patch.object(sharingnotes_publish.subprocess, 'run', side_effect=[
                  FileNotFoundError(), subprocess.CompletedProcess([], 44, '', 'missing'),
              ])):
            for _ in range(2):
                self.assertFalse(PublishConfig.load(self.tmp / 'config.json').configured)


class TriggerTests(Harness):
    def test_changed_paths_map_to_recording_ids(self):
        folder = self.tmp / '课程' / '第一课'
        folder.mkdir(parents=True)
        (folder / 'session.json').write_text(json.dumps({'id': 'rec-1'}))
        (folder / 'transcript.txt').write_text('x')
        ids = session_ids_for_paths([folder / 'session.json', folder / 'transcript.txt', folder,
                                     self.tmp / 'missing' / 'session.json'])
        self.assertEqual(ids, ['rec-1'])

    def test_config_requires_https_or_loopback(self):
        with self.assertRaises(ValueError):
            PublishConfig('http://example.com', 't', [COURSE])
        self.assertTrue(PublishConfig('https://notes.example', 't', [COURSE]).configured)
        self.assertFalse(PublishConfig('https://notes.example', 't', []).configured)
        self.assertTrue(PublishConfig('http://127.0.0.1:8787', 't', [COURSE]).configured)

    def test_keychain_prompt_does_not_block_auto_publisher_startup(self):
        async def scenario(token):
            entered, release, publishing = threading.Event(), threading.Event(), asyncio.Event()

            def read_keychain():
                entered.set()
                return token if release.wait(timeout=2) else ''

            async def run():
                publishing.set()

            auto = AutoPublisher(None, transport=self.api, state=self.state)
            env = {'SHARINGNOTES_PUBLISH_URL': 'https://coursenote.dczxyy.uk', 'SHARINGNOTES_PUBLISH_COURSES': COURSE}
            with (patch.dict(os.environ, env, clear=True), patch.object(sys, 'platform', 'darwin'),
                  patch.object(sharingnotes_publish, 'STATE_DIR', self.tmp),
                  patch.object(sharingnotes_publish, '_read_keychain_token', side_effect=read_keychain) as read,
                  patch.object(auto, 'run', side_effect=run)):
                try:
                    self.assertTrue(await auto.start())
                    read.assert_not_called()
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    self.assertIsNone(auto.publisher)
                    self.assertFalse(publishing.is_set())
                    release.set()
                    await asyncio.wait_for(auto.task, 1)
                    self.assertEqual(publishing.is_set(), bool(token))
                    if not token:
                        self.assertIsNone(auto.publisher)
                    read.assert_called_once_with()
                finally:
                    release.set()
                    await auto.close()

        for token in ('synthetic-keychain-token', ''):
            with self.subTest(available=bool(token)):
                asyncio.run(scenario(token))

    def test_auto_publisher_publishes_after_a_save_notification(self):
        from qwen_asr_app.background_wakeup import library_changes
        folder = self.tmp / 'rec'
        folder.mkdir()
        (folder / 'session.json').write_text(json.dumps({'id': 'rec-1'}))
        current = {'value': session()}

        class Record:
            id = 'rec-1'
            def public(self):
                value = copy.deepcopy(current['value'])
                return value

        class Store:
            def iter_records(self):
                return [Record()]
            def find(self, ident):
                return Record() if ident == 'rec-1' else None

        import qwen_asr_app.sharingnotes_export as export_module
        original = export_module.read_note_asset
        export_module.read_note_asset = lambda directory, package, name: self.read(name)
        self.addCleanup(setattr, export_module, 'read_note_asset', original)

        async def scenario():
            auto = AutoPublisher(Store(), PublishConfig('https://notes.example', 't', [COURSE]),
                                 transport=self.api, state=self.state, debounce=0, retry=0)
            self.assertTrue(await auto.start())
            try:
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if self.state.notes:
                        break
                first = dict(self.state.notes)
                self.assertEqual(len(first), 1)
                current['value'] = edited('保存后自动发布')
                library_changes.notify([str(folder / 'session.json')])
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if self.state.notes != first:
                        break
                self.assertNotEqual(self.state.notes, first)
                self.assertEqual(self.state.pending, [])
            finally:
                await auto.close()
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
