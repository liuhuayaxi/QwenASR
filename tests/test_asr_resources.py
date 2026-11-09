from tests import isolation

import asyncio
import importlib.util
import json
from pathlib import Path
import plistlib
import psutil
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from fastapi import FastAPI
import httpx

from qwen_asr_app import asr_resources as resources
from qwen_asr_app.course_phase_status import lesson_phases


def recording(provider='remote_preferred', segments=None, **stream):
    return SimpleNamespace(metadata={'course_stream': {'inference_settings': {
        'asr_provider': provider, 'base_url': 'http://100.64.0.2:8765'}, **stream}}, segments=segments or [])


class PlacementTests(unittest.TestCase):
    def test_actual_segment_wins_over_preference_and_fallback_is_explicit(self):
        record = recording(segments=[{'metrics': {'provider': 'local', 'fallback': True}}])
        value, config = resources.execution(record)
        self.assertEqual(value['label'], 'Mac 转写（已回退）')
        self.assertEqual(config['asr_provider'], 'remote_preferred')
        record.metadata['course_stream']['asr_execution'] = {'provider': 'remote', 'state': 'requested', 'updated_at': 123}
        value, _ = resources.execution(record)
        self.assertEqual(value['label'], 'Windows 转写')
        self.assertEqual(value['updated_at'], 123)

    def test_unobserved_preferred_mode_is_not_claimed_as_windows(self):
        self.assertIsNone(resources.execution(recording())[0]['provider'])
        self.assertEqual(resources.execution(recording('local'))[0]['label'], 'Mac 转写')
        self.assertEqual(resources.execution(recording('remote_only'))[0]['basis'], 'configured')

    def test_phase_label_uses_task_device_and_does_not_mutate_source(self):
        entry = {'phases': {'asr': {'label': '本地语音转写', 'state': 'running'}}}
        phase = lesson_phases(entry, recording('remote_only'))['asr']
        self.assertEqual(phase['label'], 'Windows 转写')
        self.assertEqual(phase['execution']['provider'], 'remote')
        self.assertEqual(entry['phases']['asr']['label'], '本地语音转写')

    def test_mac_uses_gpu_activity_not_memory_and_preserves_zero(self):
        payload = [{'PerformanceStatistics': {'Device Utilization %': 0, 'Alloc system memory': 123456}}]
        with patch.object(resources.sys, 'platform', 'darwin'), \
                patch('psutil.cpu_percent', return_value=23.6), \
                patch.object(resources.subprocess, 'check_output', return_value=plistlib.dumps(payload)):
            value = resources.mac_usage()
        self.assertEqual((value['cpu_percent'], value['gpu_percent']), (23.6, 0))
        for invalid in (None, True, '40', -1, 101, float('nan')):
            self.assertIsNone(resources.percent(invalid))


class SamplingTests(unittest.IsolatedAsyncioTestCase):
    async def test_closing_one_client_does_not_duplicate_the_same_host_probe(self):
        sampler = resources.ResourceSampler()
        started, release = threading.Event(), threading.Event()
        def sample():
            started.set()
            release.wait(2)
            return {'device': 'Mac', 'scope': 'host', 'cpu_percent': 20,
                    'gpu_percent': 50, 'sampled_at': time.time()}
        with patch.object(resources, 'mac_usage', side_effect=sample) as observe:
            first = asyncio.create_task(sampler.sample('local', {}))
            try:
                async with asyncio.timeout(1):
                    while not started.is_set(): await asyncio.sleep(.005)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError): await first
                second = asyncio.create_task(sampler.sample('local', {}))
                await asyncio.sleep(.01)
                release.set()
                self.assertEqual((await second)['gpu_percent'], 50)
                observe.assert_called_once()
            finally:
                release.set()
                await asyncio.gather(*sampler.pending.values(), return_exceptions=True)

    async def test_concurrent_same_host_probes_are_shared_and_failures_never_become_zero(self):
        sampler = resources.ResourceSampler()
        sample = {'device': 'Windows', 'scope': 'host', 'sampled_at': time.time(),
                  'cpu_percent': None, 'gpu_percent': 0}
        with patch.object(resources, 'windows_usage', return_value=sample) as observe:
            values = await asyncio.gather(*(sampler.sample('remote', {'base_url': 'http://one'}) for _ in range(5)))
        observe.assert_called_once()
        self.assertIsNone(values[0]['cpu_percent'])
        self.assertEqual(values[0]['gpu_percent'], 0)

    async def test_stale_remote_readings_are_not_presented_as_current(self):
        with patch.object(resources, 'windows_usage', return_value={
            'sampled_at': time.time()-60, 'cpu_percent': 90, 'gpu_percent': 80}):
            sample = await resources.ResourceSampler().sample('remote', {})
        self.assertIsNone(sample['cpu_percent'])
        self.assertIsNone(sample['gpu_percent'])

    async def test_paused_or_asr_complete_jobs_do_not_poll_resources(self):
        store = SimpleNamespace(find=lambda ident: recording('remote_only', input_complete=True,
            chunks=[{}], completed_chunks=1))
        entries = [{'id': 'one', 'record_id': 'record', 'state': 'running', 'phases': {'asr': {'state': 'running'}}}]
        with patch.object(resources.sampler, 'sample', AsyncMock()) as sample:
            data = await resources.course_resources(entries, store)
            sample.assert_not_awaited()
        self.assertIsNone(data['one']['usage'])


spec = importlib.util.spec_from_file_location('host_usage_test', Path(__file__).resolve().parents[1]/'ai-infer-node/server/host_usage.py')
host_usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host_usage)


class WindowsProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resource_endpoint_shares_samples_without_loading_models(self):
        app = FastAPI()
        host_usage.install(app)
        with patch.object(host_usage, 'observe', AsyncMock(return_value={
                'device': 'Windows', 'scope': 'host', 'cpu_percent': 28, 'gpu_percent': 74,
                'sampled_at': time.time()})) as observe:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                responses = await asyncio.gather(*(client.get('/v1/resources/usage') for _ in range(4)))
        observe.assert_awaited_once()
        self.assertEqual(responses[0].json()['data']['cpu_percent'], 28)
    async def test_cpu_is_windows_host_not_wsl_and_gpu_zero_is_valid(self):
        async def output(*args):
            if args[0] == 'powershell.exe':
                self.assertIn('Win32_PerfFormattedData_PerfOS_Processor', args[-1])
                self.assertIn("Name='_Total'", args[-1])
                return '28'
            self.assertIn('--query-gpu=utilization.gpu', args)
            return '0'
        with patch.object(host_usage.shutil, 'which', side_effect=lambda name: name), \
                patch.object(host_usage, 'output', output):
            value = await host_usage.observe()
        self.assertEqual((value['cpu_percent'], value['gpu_percent']), (28, 0))
        self.assertEqual(value['scope'], 'host')

    async def test_unavailable_probes_remain_unknown(self):
        with patch.object(host_usage.shutil, 'which', return_value=None), \
                patch.object(host_usage.Path, 'is_file', return_value=False), \
                patch.object(host_usage, 'output', AsyncMock(return_value='N/A')):
            value = await host_usage.observe()
        self.assertIsNone(value['cpu_percent'])
        self.assertIsNone(value['gpu_percent'])
