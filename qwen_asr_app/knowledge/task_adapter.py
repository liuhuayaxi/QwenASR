"""Expose the new durable owners through the existing task-center contract."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from urllib.parse import urlencode

from ..classroom_intelligence.store import ClassroomNoteStore, read_notes_doc
from .ingest import compute_file_stat_hash, compute_recording_stat_hash


def _time(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return 0


class IntelligenceTasks:
    def __init__(self, knowledge, intelligence, recordings):
        self.knowledge=knowledge
        self.intelligence=intelligence
        self.recordings=recordings

    def tasks(self):
        result=[]
        jobs = self.knowledge.jobs()
        records = list(self.recordings.list_records())
        by_record = {record.id: record for record in records}
        for job in jobs:
            status=job['status']
            task = {'id':'knowledge:'+job['job_id'],'type':'knowledge_index','family':'knowledge',
                           'job_id':job['job_id'],'title':job['category']+' · 知识索引','label':'课程知识索引',
                           'category':job['category'],'status':status,'stage':job.get('message') if status=='completed' else job['stage'],'source':'Mac 知识库',
                           'execution_location':'local',
                           'progress':round(job['progress']*100),'progress_percent':round(job['progress']*100),'error':job.get('error') or '',
                           'created_at':job['created_at'],'updated_at':job['updated_at'],
                           'result_url':'/course-files?'+urlencode({'category':job['category']}),
                           'actions':['cancel'] if status in {'running','queued'} else [] if status=='completed' else ['retry']}
            # A successful job elsewhere in the course is not evidence that
            # this failed source recovered. Bind history to the exact source,
            # its current file, active revision, and complete vector set.
            match = re.search(r'Embedding failure for source ([A-Za-z0-9_-]+):', job.get('error') or '')
            if match:
                source_id = match.group(1)
                source = self.knowledge.store.get_source(source_id)
                if source and source.get('category') == job['category']:
                    task['source_id'] = source_id
                    record_id = source.get('lesson_id')
                    if record_id:
                        task['recording_id'] = record_id
                        record = by_record.get(record_id)
                        if record:
                            task['title'] = record.metadata.get('title') or task['title']
                            task['result_url'] = '/library?' + urlencode({'recording': record_id})
                    recovery = self._recovered_index(job, source, jobs)
                    if recovery:
                        task.update(recovered=True, display_status='recovered',
                                    recovery_reason='该来源已在后续任务中完成索引，旧错误保留在历史中',
                                    recovery=recovery)
            result.append(task)
        for record in records:
            try:
                notes=read_notes_doc(record).get('notes',[])
            except (OSError,ValueError):
                continue
            for note in notes:
                job=note.get('job') or {}
                if not job:
                    continue
                status=job.get('status','')
                if status=='pending':status='running'
                elif status=='waiting_retry':status='waiting_retry'
                elif note.get('status')=='enhanced':status='completed'
                elif status not in {'completed','failed','cancelled','interrupted','running'}:continue
                result.append({'id':'classroom-note:'+record.id+':'+note['id'],'type':'classroom_note',
                               'family':'knowledge','recording_id':record.id,'note_id':note['id'],
                               'title':record.metadata.get('title') or '随堂速记','label':'速记增强',
                               'category':record.metadata.get('category') or '未分类','status':status,
                               'stage':'课堂与资料来源核对','source':'Mac 课堂助手','error':note.get('error') or '',
                               'created_at':note.get('created_at'),'updated_at':note.get('updated_at'),
                               'result_url':'/?'+urlencode({'chat':record.id,'panel':'scribbles'}),
                               'actions':['cancel'] if status in {'running','waiting_retry'} else [] if status=='completed' else ['retry']})
        return result

    def _recovered_index(self, job, source, jobs):
        if (job.get('status') not in {'failed', 'interrupted'}
                or source.get('status') != 'active' or source.get('error')
                or source.get('needs_vectors') or not source.get('active_revision')
                or _time(source.get('updated_at')) <= _time(job.get('updated_at'))):
            return None
        recovered_at = _time(source.get('updated_at'))
        successors = [item for item in jobs if item.get('category') == job['category']
                      and item.get('status') == 'completed'
                      and _time(item.get('created_at')) > _time(job.get('updated_at'))
                      and _time(item.get('created_at')) <= recovered_at <= _time(item.get('updated_at'))]
        if not successors:
            return None
        if not source.get('file_path'):
            return None
        path = Path(source['file_path'])
        if not path.exists() or not source.get('stat_hash'):
            return None
        actual_stat = (compute_recording_stat_hash(path if path.is_dir() else path.parent) if source.get('source_type') == 'recording_refined'
                       else compute_file_stat_hash(path))[2]
        if actual_stat != source['stat_hash']:
            return None
        with self.knowledge.store._connect() as db:
            revision = db.execute('SELECT embedding_contract FROM source_revisions WHERE source_id=? AND revision=? AND is_active=1',
                                  (source['source_id'], source['active_revision'])).fetchone()
            if not revision or not revision['embedding_contract']:
                return None
            row = db.execute('''SELECT COUNT(*) AS total, SUM(EXISTS(
                SELECT 1 FROM chunk_vectors v WHERE v.chunk_id=c.chunk_id AND v.embedding_contract=?)) AS ready
                FROM chunks c WHERE c.source_id=? AND c.revision=? AND c.is_active=1''',
                (revision['embedding_contract'], source['source_id'], source['active_revision'])).fetchone()
        if not row or not row['total'] or row['total'] != row['ready']:
            return None
        successor = max(successors, key=lambda item: _time(item.get('updated_at')))
        return {'source_id': source['source_id'], 'revision': source['active_revision'],
                'job_id': successor['job_id'], 'updated_at': source['updated_at'], 'chunks': row['total']}

    async def action(self, task, action):
        if task['type']=='knowledge_index':
            if action=='cancel':
                return {'cancelled':self.knowledge.cancel(task['job_id'])}
            return {'job_id':self.knowledge.schedule(task['category'])}
        record=self.recordings.find(task['recording_id'])
        if action=='cancel':
            await self.intelligence.cancel_task(task['note_id'])
            store=ClassroomNoteStore(record)
            note=await store.get_note(task['note_id'])
            return {'note':await store.apply_action(note['id'],'cancel',note['revision'])}
        await self.intelligence.enqueue_enhancement(record,task['note_id'],force=True)
        return {'queued':True}
