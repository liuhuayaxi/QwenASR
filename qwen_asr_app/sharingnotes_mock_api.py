"""In-process mock of the Sharing Notes v1 publish API, for tests and dry runs.

Implements the semantics in sharingnotes-code ``contract/README.md``: blob
registration and verified upload, missing_blobs before switching a note,
idempotent replay of an accepted identical request before the ``expectedVersion`` check,
conditional delete with tombstones. Optionally persisted to a directory.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .durable import atomic_bytes, atomic_json
from .sharingnotes_publish import BLOBS_PATH, CONTRACT_VERSION, DELETE_PATH, NOTES_PATH, PublishError

KINDS = {'note': 'application/json', 'manifest': 'application/json', 'image': 'image/png',
         'page': 'image/webp', 'pdf': 'application/pdf'}
SIGNATURES = {'image': (b'\x89PNG\r\n\x1a\n',), 'pdf': (b'%PDF-',), 'page': (b'RIFF',)}
HASH = re.compile(r'^[a-f0-9]{64}$')
NOTE_ID = re.compile(r'^[a-f0-9]{20}$')
MIRRORED = ['id', 'course', 'title', 'chapter', 'chapters', 'date', 'abstract', 'version', 'wordCount', 'imageCount']


def _response(status, accepted=None, missing=(), error=None, operation='blob', ident=None):
    body = {'contractVersion': CONTRACT_VERSION, 'operation': operation, 'status': status,
            'acceptedVersion': accepted, 'missingBlobs': list(missing)}
    if ident:
        body['id'] = ident
    if error:
        body['error'] = error
    return body


class MockPublishAPI:
    def __init__(self, directory=None, token=None):
        self.directory = Path(directory) if directory else None
        self.token = token
        self.notes, self.descriptors, self.tombstones = {}, {}, {}
        self.stored = set()
        self.calls = []
        self.drop_next_response = False
        if self.directory and (self.directory / 'state.json').is_file():
            data = json.loads((self.directory / 'state.json').read_text(encoding='utf-8'))
            self.notes, self.descriptors = data['notes'], data['descriptors']
            self.tombstones, self.stored = data['tombstones'], set(data['stored'])

    def _persist(self, kind=None, sha=None, payload=None):
        if not self.directory:
            return
        if payload is not None:
            atomic_bytes(self.directory / 'blobs' / kind / sha, payload)
        atomic_json(self.directory / 'state.json', {'notes': self.notes, 'descriptors': self.descriptors,
                                                    'tombstones': self.tombstones, 'stored': sorted(self.stored)})

    def visible(self, note_id):
        return (self.notes.get(note_id) or {}).get('note')

    def request(self, method, path, body=b'', content_type='application/json'):
        self.calls.append((method, path))
        status, response = self._handle(method, path, body, content_type)
        if self.drop_next_response:
            self.drop_next_response = False
            raise PublishError('模拟：服务端已处理但响应丢失')
        return status, response

    def _handle(self, method, path, body, content_type):
        if method == 'PUT' and path.startswith(BLOBS_PATH + '/'):
            return self._put(path[len(BLOBS_PATH) + 1:], body, content_type)
        if method != 'POST':
            return 405, _response('rejected', error='method not allowed')
        try:
            message = json.loads(body)
        except ValueError:
            return 400, _response('rejected', error='invalid JSON')
        if not isinstance(message, dict) or message.get('contractVersion') != CONTRACT_VERSION:
            return 400, _response('rejected', error='contractVersion must be 1')
        if path == BLOBS_PATH:
            return self._register(message)
        if path == NOTES_PATH:
            return self._upsert(message)
        if path == DELETE_PATH:
            return self._delete(message)
        return 404, _response('rejected', error='unknown route')

    def _register(self, descriptor):
        sha, kind = descriptor.get('sha256'), descriptor.get('kind')
        if not HASH.match(str(sha)) or KINDS.get(kind) != descriptor.get('contentType') \
                or not isinstance(descriptor.get('size'), int) or descriptor['size'] < 0:
            return 400, _response('rejected', error='invalid blob descriptor')
        known = self.descriptors.get(sha)
        if known and known != descriptor:
            return 400, _response('rejected', error='descriptor differs from registered blob')
        self.descriptors[sha] = descriptor
        self._persist()
        if sha in self.stored:
            return 200, _response('accepted')
        return 409, _response('missing_blobs', missing=[descriptor])

    def _put(self, rest, payload, content_type):
        kind, _, sha = rest.partition('/')
        descriptor = self.descriptors.get(sha)
        if not descriptor or descriptor['kind'] != kind:
            return 400, _response('rejected', error='blob not registered')
        if content_type != descriptor['contentType'] or len(payload) != descriptor['size'] \
                or hashlib.sha256(payload).hexdigest() != sha \
                or not payload.startswith(SIGNATURES.get(kind, (b'',))):
            return 400, _response('rejected', error='blob bytes do not match descriptor')
        self.stored.add(sha)
        self._persist(kind, sha, payload)
        return 200, _response('accepted')

    def _check(self, message):
        note, entry, refs = message.get('note'), message.get('catalogEntry'), message.get('references')
        if message.get('operation') != 'upsert_note' or not isinstance(note, dict) or not isinstance(entry, dict) \
                or not isinstance(refs, dict):
            return 'invalid upsert envelope'
        if not NOTE_ID.match(str(note.get('id'))) or not HASH.match(str(note.get('version'))):
            return 'invalid note id/version'
        if any(entry.get(k) != note.get(k) for k in MIRRORED):
            return 'catalogEntry does not mirror note'
        if entry.get('file') != f"notes/{note['id']}-{note['version'][:12]}.json":
            return 'catalogEntry.file mismatch'
        images = note.get('images') or []
        if any(i.get('src') != f"data/images/{i.get('sha256')}.png" for i in images):
            return 'image src/sha mismatch'
        if sorted(refs.get('images') or []) != sorted({i['sha256'] for i in images}):
            return 'references.images differ from note images'
        links = message.get('pageLinks') or {}
        if links.get('version') != note['version']:
            return 'pageLinks.version differs from note.version'
        return None

    def _upsert(self, message):
        note_id = (message.get('note') or {}).get('id') if isinstance(message.get('note'), dict) else None
        op = {'operation': 'upsert_note', 'ident': note_id if NOTE_ID.match(str(note_id)) else None}
        error = self._check(message)
        if error:
            return 400, _response('rejected', error=error, **op)
        refs = [sha for key in ('images', 'pages', 'pdfs') for sha in message['references'].get(key) or []]
        if any(sha not in self.descriptors for sha in refs):
            return 400, _response('rejected', error='unregistered blob reference', **op)
        missing = [self.descriptors[sha] for sha in refs if sha not in self.stored]
        if missing:
            return 409, _response('missing_blobs', missing=missing, **op)
        note = message['note']
        entry = self.notes.get(note['id']) or {}
        current = entry.get('note')
        request = hashlib.sha256(json.dumps(message, ensure_ascii=False, sort_keys=True,
                                            separators=(',', ':')).encode()).hexdigest()
        # Replay of an accepted identical request first, then the condition (as the Worker does).
        replay = current and current['version'] == note['version'] and request in entry.get('requests', ())
        if not replay:
            expected = message.get('expectedVersion')
            if (expected is None and current) or (expected is not None and (not current or current['version'] != expected)):
                return 409, _response('conflict', error='expectedVersion does not match the current version', **op)
        requests = entry.get('requests', []) if current and current['version'] == note['version'] else []
        self.notes[note['id']] = {**{k: message[k] for k in ('note', 'catalogEntry', 'references', 'materials',
                                                             'pageLinks', 'pageSets')},
                                  'requests': list(dict.fromkeys([*requests, request]))}
        self.tombstones.pop(note['id'], None)
        self._persist()
        return 200, _response('accepted', accepted=note['version'], **op)

    def _delete(self, message):
        ident, expected = message.get('id'), message.get('expectedVersion')
        op = {'operation': 'delete_note', 'ident': ident if NOTE_ID.match(str(ident)) else None}
        if message.get('operation') != 'delete_note' or not NOTE_ID.match(str(ident)) or not HASH.match(str(expected)):
            return 400, _response('rejected', error='invalid delete message', **op)
        current = (self.notes.get(ident) or {}).get('note')
        if not current and self.tombstones.get(ident) == expected:
            return 200, _response('deleted', **op)
        if not current or current['version'] != expected:
            return 409, _response('conflict', error='expectedVersion does not match the current version', **op)
        self.notes.pop(ident)
        self.tombstones[ident] = expected
        self._persist()
        return 200, _response('deleted', **op)


def serve(directory, port=8799, token='mock-token'):
    """Serve the mock on ``http://127.0.0.1:<port>`` for manual end-to-end runs."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    api, lock = MockPublishAPI(directory), threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            if self.headers.get('Authorization') != 'Bearer ' + token:
                status, response = 401, _response('rejected', error='unauthorized')
            else:
                with lock:
                    status, response = api._handle(self.command, self.path, body, self.headers.get('Content-Type'))
            payload = json.dumps(response, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            print(self.command, self.path, status, response['status'], flush=True)

        do_POST = do_PUT = _serve

        def log_message(self, *args):
            pass

    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
