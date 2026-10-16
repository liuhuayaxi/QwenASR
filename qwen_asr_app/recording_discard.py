"""Recoverable recording discard, with capture fenced before owners drain.

An HTTP disconnect never owns this operation. Accepted ASR and AAC writers are
allowed to finish, and the complete directory moves only after their owners
have released it. A manifest travels with the recording into the normal Trash.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import uuid

from .durable import atomic_json
from .storage import local_now
from .paths import PRIVATE_ROOT


async def finish_owned(task):
    """Wait for a writer even when this waiter is cancelled repeatedly."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        # The owner is now finished; cancellation cannot leave its thread behind.
        raise asyncio.CancelledError
    return task.result()


class RecordingDiscard:
    def __init__(self, store, sessions, quiesce, state_path=None):
        self.store, self.sessions, self.quiesce = store, sessions, quiesce
        self.tasks = {}
        self.state_path=Path(state_path) if state_path is not None else PRIVATE_ROOT/'recording-discards.json'
        self.results = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if not isinstance(self.results,dict) or any(not isinstance(v,dict) for v in self.results.values()):
            raise ValueError('丢弃任务记录无效，原文件已保留')

    def persist(self):
        atomic_json(self.state_path,self.results)

    async def notify(self,session,message):
        if session is not None:
            try:await session.send(message)
            except Exception:pass  # A closed browser never cancels an accepted discard.

    def busy(self, ident):
        task = self.tasks.get(ident)
        return task is not None and not task.done()

    def reconcile_restored(self, ident):
        """Release a completed discard only after its exact directory returns.

        A cached receipt, a copied directory, or a still-running move cannot
        revoke the live-session fence. The manifest and vanished Trash target
        identify a restoration of this particular discard operation.
        """
        result = self.results.get(ident, {})
        if self.busy(ident) or result.get('status') != 'discarded':
            return False
        record = self.store.find(ident)
        if record is None or record.metadata.get('kind') != 'microphone':
            return False
        try:
            trashed = Path(result['trashed_path'])
            if not trashed.is_absolute() or trashed.exists() or trashed.is_symlink():
                return False
            raw = (record.directory / '.recording-discard.json').read_bytes()
            manifest = json.loads(raw)
            original = Path(manifest['original_path'])
            if (not original.is_absolute() or record.directory.resolve() != original.resolve()
                    or manifest.get('session_id') != ident
                    or record.metadata.get('status') not in {'completed', 'interrupted'}):
                return False
            saved = record.metadata.get('discard_job') or {}
            if saved.get('status') not in {'discarded', 'restored'}:
                return False
            if result.get('original_path') and str(original) != result['original_path']:
                return False
            operation = result.get('operation_id')
            if operation and (manifest.get('operation_id') != operation or saved.get('operation_id') != operation):
                return False
            digest = result.get('manifest_sha256')
            if digest and hashlib.sha256(raw).hexdigest() != digest:
                return False
        except (KeyError, OSError, TypeError, ValueError):
            return False

        restored_at = local_now()
        previous = copy.deepcopy({key: value for key, value in result.items() if key != 'history'})
        previous['restored_at'] = restored_at
        history = copy.deepcopy(result.get('history', []))
        history.append(previous)
        # Keep the old live object fenced. Restoring archived files must never
        # revive an old socket or reattach it to a newer workbench session.
        record.metadata['discard_job'] = {
            **saved, 'status': 'restored', 'restored_at': restored_at,
            'message': '录音已从废纸篓恢复，资料可以继续处理',
        }
        record.save_metadata()
        self.results[ident] = {
            'session_id': ident, 'status': 'restored', 'recoverable': True,
            'restored_at': restored_at, 'original_path': str(original),
            'message': '录音已从废纸篓恢复，资料仍保留', 'error': '', 'history': history,
        }
        self.persist()
        return True

    def snapshot(self, ident):
        self.reconcile_restored(ident)
        if ident in self.results:
            result=copy.deepcopy(self.results[ident])
            if result.get('status')=='discarding' and not self.busy(ident):
                result.update(status='failed',error='服务曾关闭；录音已保留，请重新确认丢弃')
            return result
        record = self.store.find(ident)
        saved = record.metadata.get('discard_job') if record else None
        if saved:
            result = {'session_id': ident, 'recoverable': True, **copy.deepcopy(saved)}
            if result.get('status') == 'discarding' and not self.busy(ident):
                result.update(status='failed', error='服务曾关闭；录音已保留，请重新确认丢弃',
                              message='丢弃尚未完成，原录音仍保留')
            return result
        raise FileNotFoundError('没有找到这条录音的丢弃任务')

    def start(self, ident):
        self.reconcile_restored(ident)
        if self.busy(ident) or self.results.get(ident, {}).get('status') == 'discarded':
            return self.snapshot(ident)
        record = self.store.find(ident)
        if record is None:
            raise FileNotFoundError('没有找到这条录音')
        session = self.sessions.get(ident)
        if session is not None and getattr(session, 'recording', None) is not None:
            record = session.recording
        if record.metadata.get('kind') != 'microphone':
            raise ValueError('这里只能丢弃实时录音；上传文件请在课堂记录中操作')
        # No await until the fence and durable intent are both established.
        if session is not None:
            session.accepting_audio = False
            session.writer = None
            session.discarding = True
        state = {'status': 'discarding', 'started_at': local_now(), 'error': '',
                 'operation_id': uuid.uuid4().hex, 'original_path': str(record.directory.resolve()),
                 'message': '已停止收音，正在等待这条录音的转写和保存安全结束，再移到废纸篓'}
        record.metadata['discard_job'] = state
        record.metadata.setdefault('summary_job', {}).update(cancel_requested=True)
        record.metadata['remote_backlog'] = {'status': 'cancelled', 'message': '用户已确认丢弃，停止自动补转'}
        record.save_metadata()
        result = {'session_id': ident, 'recoverable': True, **state,
                  'history': copy.deepcopy(self.results.get(ident, {}).get('history', []))}
        self.results[ident] = result
        self.persist()
        task = asyncio.create_task(self._run(record, session))
        self.tasks[ident] = task
        task.add_done_callback(lambda done: self.tasks.pop(ident, None)
                               if self.tasks.get(ident) is done else None)
        return copy.deepcopy(result)

    async def _run(self, record, session):
        ident = record.id
        try:
            await self.notify(session,{'type': 'recording_discarding', 'session_id': ident,
                                    'message': self.results[ident]['message']})
            await self.quiesce(record, session)
            original = str(record.directory.resolve())

            def move():
                audio = []
                for path in (record.audio_path, record.working_audio_path):
                    if path.is_file() and not any(row['filename'] == path.name for row in audio):
                        with path.open('rb') as stream:
                            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                        audio.append({'filename': path.name, 'size': path.stat().st_size, 'sha256': digest})
                # This file is an explicit restoration lineage, not a deletion log
                # stored somewhere else that could be separated from its audio.
                previous_status=record.metadata.get('status')
                atomic_json(record.directory / '.recording-discard.json', {
                    'schema': 1, 'session_id': ident, 'discarded_at': local_now(),
                    'operation_id': self.results[ident]['operation_id'],
                    'original_path': original, 'title': record.metadata.get('title', ''),
                    'category': record.metadata.get('category', ''), 'audio': audio,
                    'previous_status':previous_status,
                    'restore': '将本目录移回 original_path 后刷新资料库；全部音频和转写均在本目录内',
                })
                # A restored interrupted recording can continue processing;
                # a completed archive remains playable.
                record.metadata['status'] = 'completed' if previous_status=='completed' else 'interrupted'
                record.metadata['discard_job'].update(status='discarded', updated_at=local_now())
                record.save_all()
                # Detach only this exact active record; a newer recording can
                # have acquired the workbench while AAC was finishing.
                if self.store.active is not None and self.store.active.id==ident:
                    self.store.active = None
                digest = hashlib.sha256((record.directory / '.recording-discard.json').read_bytes()).hexdigest()
                return self.store.delete(ident), digest

            # Keep ownership of the filesystem thread through cancellation.
            target, manifest_digest = await finish_owned(asyncio.create_task(asyncio.to_thread(move)))
            intent = self.results[ident]
            result = {'session_id': ident, 'status': 'discarded', 'recoverable': True,
                      'message': '录音已移到废纸篓，可以恢复', 'trashed_path': str(target), 'error': '',
                      'original_path': original, 'operation_id': intent['operation_id'],
                      'manifest_sha256': manifest_digest, 'history': copy.deepcopy(intent.get('history', []))}
            self.results[ident] = result
            self.persist()
            if session is not None:
                if session.recording is record:
                    session.recording = None
                if self.sessions.get(ident) is session:
                    self.sessions.pop(ident, None)
            await self.notify(session,{'type': 'recording_discarded', **result})
        except asyncio.CancelledError:
            # Graceful service shutdown drains this manager instead of cancelling
            # it. Forced interruption leaves a durable, retryable intention.
            self.results[ident].update(status='failed', error='丢弃已中断，录音保留，请重新确认后继续')
            self.persist()
            raise
        except Exception as exc:
            self.results[ident].update(status='failed', error=str(exc),
                                      message='丢弃未完成，录音已保留，可重新确认后重试')
            self.persist()
            if record.directory.is_dir():
                record.metadata['status'] = 'interrupted'
                record.metadata['discard_job'].update(self.results[ident], updated_at=local_now())
                record.save_metadata()

    async def close(self):
        if self.tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in list(self.tasks.values())),
                                 return_exceptions=True)
