"""Read-only timing snapshots distinguish measured phases from missing data."""
from tests import isolation  # noqa: F401

import copy
from pathlib import Path
from types import SimpleNamespace
import unittest

from qwen_asr_app.course_stream import CourseStreamJob


class CourseTimingSnapshotTests(unittest.TestCase):
    def snapshot(self,segments):
        job=CourseStreamJob.__new__(CourseStreamJob)
        job.record=SimpleNamespace(id='synthetic-timing',directory=Path('/synthetic'),duration=120,
            metadata={'status':'processing'},segments=segments)
        job.state={'chunks':[{'samples':16000*60},{'samples':16000*60}],
            'completed_chunks':1,'expected_seconds':120,'input_complete':True,
            'first_text_seconds':12.5,'download_wall_seconds':6.2,'archive_seconds':None}
        job.config={'ai_correction':False,'translation':'off'}
        return job.snapshot()

    def test_mixed_local_and_remote_metrics_are_each_counted_once(self):
        segments=[{'id':1,'raw':'local synthetic','latency':6.5,
            'metrics':{'provider':'local','queue_seconds':1.5,'load_seconds':2,'inference_seconds':3}},
            {'id':2,'raw':'remote synthetic','latency':20,
             'metrics':{'provider':'remote','queue_seconds':999,'inference_seconds':999,
                'metrics':{'queue_seconds':4,'load_seconds':5,'inference_seconds':6,
                    'total_seconds':17,'gateway_job_seconds':19}}}]
        original=copy.deepcopy(segments);value=self.snapshot(segments);metrics=value['metrics']
        self.assertEqual(metrics['asr_queue_seconds'],5.5)
        self.assertEqual(metrics['asr_inference_seconds'],9)
        self.assertEqual(metrics['asr_load_seconds'],7)
        self.assertEqual(metrics['asr_worker_seconds'],17)
        self.assertEqual(metrics['asr_gateway_seconds'],19)
        self.assertEqual(metrics['asr_client_seconds'],26.5)
        self.assertEqual(metrics['asr_timing_observations']['asr_queue_seconds'],2)
        self.assertEqual(metrics['asr_timing_observations']['asr_worker_seconds'],1)
        self.assertEqual(metrics['asr_timing_basis'],'completed_transcript_segments')
        self.assertEqual(segments,original)
        self.assertEqual(value['asr_seconds'],60);self.assertEqual(value['metrics']['download_wall_seconds'],6.2)

    def test_resident_worker_combined_seconds_are_not_fabricated_as_inference_or_queue(self):
        metrics=self.snapshot([{'id':1,'raw':'synthetic','latency':15,
            'metrics':{'provider':'remote','seconds':9,'cold_start':True}}])['metrics']
        self.assertEqual(metrics['asr_worker_seconds'],9)
        self.assertIsNone(metrics['asr_inference_seconds']);self.assertIsNone(metrics['asr_queue_seconds'])
        self.assertIsNone(metrics['asr_load_seconds']);self.assertIsNone(metrics['asr_gateway_seconds'])
        self.assertEqual(metrics['asr_client_seconds'],15)
        self.assertEqual(metrics['asr_timing_observations']['asr_inference_seconds'],0)

    def test_absent_invalid_or_negative_measurements_do_not_claim_zero_or_poison_totals(self):
        rows=[{'id':1,'raw':'synthetic','latency':float('nan'),'metrics':None},
            {'id':2,'raw':'synthetic','latency':True,'metrics':{'inference_seconds':float('inf'),'queue_seconds':-1}},
            {'id':3,'raw':'synthetic','metrics':{'metrics':{'inference_seconds':'8','queue_seconds':False}}}]
        metrics=self.snapshot(rows)['metrics']
        for name in metrics['asr_timing_observations']:
            self.assertIsNone(metrics[name]);self.assertEqual(metrics['asr_timing_observations'][name],0)
        metrics=self.snapshot([{'id':1,'raw':'synthetic','latency':0,
            'metrics':{'queue_seconds':0,'inference_seconds':0,'load_seconds':0}}])['metrics']
        self.assertEqual(metrics['asr_queue_seconds'],0)
        self.assertEqual(metrics['asr_timing_observations']['asr_queue_seconds'],1)

    def test_partial_new_measurements_remain_comparable_without_inventing_a_task_total(self):
        metrics=self.snapshot([{'id':1,'raw':'old receipt','metrics':{'metrics':{'inference_seconds':4}}},
            {'id':2,'raw':'new receipt','metrics':{'queue_seconds':1,'metrics':{'inference_seconds':5,'load_seconds':0,'total_seconds':6,'seconds':20}}}])['metrics']
        self.assertEqual(metrics['asr_inference_seconds'],9)
        self.assertEqual(metrics['asr_worker_seconds'],6)
        self.assertEqual(metrics['asr_queue_seconds'],1)
        self.assertEqual(metrics['asr_timing_observations']['asr_queue_seconds'],1)
        self.assertEqual(metrics['asr_timing_observations']['asr_inference_seconds'],2)


if __name__=='__main__':unittest.main()
