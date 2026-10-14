"""Expose the new durable owners through the existing task-center contract."""
from __future__ import annotations

from urllib.parse import urlencode

from ..classroom_intelligence.store import ClassroomNoteStore, read_notes_doc


class IntelligenceTasks:
    def __init__(self, knowledge, intelligence, recordings):
        self.knowledge=knowledge
        self.intelligence=intelligence
        self.recordings=recordings

    def tasks(self):
        result=[]
        for job in self.knowledge.jobs():
            status=job['status']
            result.append({'id':'knowledge:'+job['job_id'],'type':'knowledge_index','family':'knowledge',
                           'job_id':job['job_id'],'title':job['category']+' · 知识索引','label':'课程知识索引',
                           'category':job['category'],'status':status,'stage':job['stage'],'source':'Mac 知识库',
                           'progress_percent':round(job['progress']*100),'error':job.get('error') or '',
                           'created_at':job['created_at'],'updated_at':job['updated_at'],
                           'result_url':'/course-files?'+urlencode({'category':job['category']}),
                           'actions':['cancel'] if status in {'running','queued'} else [] if status=='completed' else ['retry']})
        for record in self.recordings.list_records():
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
