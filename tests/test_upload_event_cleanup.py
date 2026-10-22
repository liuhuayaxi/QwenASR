from tests import isolation
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from qwen_asr_app.upload_jobs import UploadJobs


class UploadEventCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, failed=False, live=False):
        record = SimpleNamespace(public=lambda: {'id':'synthetic-record','segments':[{'raw':'saved text'}]})
        jobs = UploadJobs(None, lambda ident: record, lambda c:c, lambda c:None)
        jobs.save = Mock()
        job = {'id':'cleanup-test','config':{},'status':'queued','recording_id':'synthetic-record',
               'progress':0,'stage':'queued','error':''}
        jobs.jobs = {job['id']:job}
        gate = asyncio.Event()
        async def run(job, config):
            job['status']='running'
            jobs.emit(job, {'type':'progress','stage':'working','payload':'x' * 1_000_000})
            await gate.wait()
            jobs.emit(job, {'type':'error','message':'test failure'} if failed else
                      {'type':'complete','session':record.public(),'progress':100})
        jobs.run = run
        jobs.schedule(job)
        task = jobs.tasks[job['id']]
        await asyncio.sleep(0)
        self.assertIn(job['id'], jobs.events)
        received=[]
        async def subscribe():
            async for event in jobs.stream(job['id']):
                received.append(json.loads(event))
        subscriber=asyncio.create_task(subscribe()) if live else None
        await asyncio.sleep(0)
        gate.set();await task;await asyncio.sleep(0)
        for table in (jobs.tasks,jobs.events,jobs.sequence,jobs.notifications,jobs.intent):
            self.assertNotIn(job['id'],table)
        self.assertIn(job['id'],jobs.jobs)
        if subscriber:await asyncio.wait_for(subscriber,2)
        else:await subscribe()
        self.assertEqual(received[-1]['type'],'error' if failed else 'complete')
        if not failed:self.assertEqual(received[-1]['session']['segments'][0]['raw'],'saved text')

    async def test_completed_job_releases_events_and_reconnect_reads_record(self):
        await self.exercise()

    async def test_live_subscriber_survives_event_cleanup(self):
        await self.exercise(live=True)

    async def test_failed_job_releases_events_and_preserves_error(self):
        await self.exercise(failed=True)
