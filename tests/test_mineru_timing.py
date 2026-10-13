from tests import isolation

import json
from pathlib import Path
import tempfile
import unittest

from qwen_asr_app.mineru_timing import MineruTiming, TimingHistory


def window(index, total=3, pages=12):
    return f'Windows · MinerU · 版面分析 · 阶段 100% · 第 {(index-1)*4+1}–{min(index*4,pages)} 页/{pages} · 窗口 {index}/{total}'


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.history = TimingHistory(Path(self.temp.name) / 'timing.json')
        self.now = 0

    def tracker(self, **kwargs):
        return MineruTiming(history=self.history, clock=lambda: self.now, pages=12,
            config={'mineru_provider': 'remote_only', 'base_url': 'http://private-node', 'token': 'private-token'}, **kwargs)

    def observe(self, tracker, seconds, stage):
        self.now = seconds
        return tracker.observe(stage)

    def full_run(self, tracker, start=0):
        rows = [(0, 'Windows · 加载模型'), (10, window(1)), (70, window(2)),
                (90, window(3)), (110, 'Windows · 保存结果'),
                (113, 'Windows · 下载识别结果'), (115, '正在写入隐藏文字层'), (120, '正在安全写入')]
        results = [self.observe(tracker, start+t, stage) for t, stage in rows]
        self.now = start + 121
        return results, tracker.finish()

    def test_queue_excluded_and_time_weighted_whole_document_not_phase_percent(self):
        run = self.tracker()
        self.observe(run, 0, 'Windows · 排队')
        queued = self.observe(run, 1000, 'Windows · 排队')
        self.assertEqual(queued['percent'], 0)
        self.assertIsNone(queued['remaining_seconds'])
        results, completed = self.full_run(run, start=1001)
        self.assertEqual(completed['elapsed_seconds'], 121)
        self.assertEqual(completed['percent'], 100)
        self.assertTrue(all(0 <= r['percent'] <= 99 for r in results))
        self.assertEqual([r['percent'] for r in results], sorted(r['percent'] for r in results))
        samples = self.history.samples(run.profile_key())
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]['seconds']['startup'], 10)
        self.assertEqual(samples[0]['seconds']['first_window'], 60)
        self.assertEqual(samples[0]['seconds']['pages'], 40)
        self.assertEqual(samples[0]['seconds']['package'], 5)
        self.assertNotIn('private-node', self.history.path.read_text())
        self.assertNotIn('private-token', self.history.path.read_text())

    def test_averages_learn_and_current_speed_adjusts_without_regression(self):
        first = self.tracker(run_id='one'); self.full_run(first)
        second = self.tracker(run_id='two'); self.full_run(second, start=200)
        current = self.tracker(run_id='three')
        self.observe(current, 400, 'Windows · 加载模型')
        result = self.observe(current, 410, window(1))
        self.assertEqual(result['sample_count'], 2)
        self.assertEqual(current.budgets()['first_window'], 60)
        # First-window startup costs are NOT mixed into seconds/page.
        self.assertEqual(current.budgets()['pages'], 40)
        p1 = self.observe(current, 470, window(2))['percent']
        p2 = self.observe(current, 590, window(3))['percent']
        self.assertGreaterEqual(p2, p1)
        self.assertGreater(current.budgets()['pages'], 40)
        self.assertEqual(current.snapshot()['observed_windows'], 1)

    def test_partial_resume_failed_and_duplicate_runs_do_not_pollute_averages(self):
        run = self.tracker(); self.observe(run, 0, window(2)); self.observe(run, 20, 'Windows · 保存结果'); run.finish()
        self.assertEqual(self.history.samples(run.profile_key()), [])
        run = self.tracker(); self.observe(run, 50, window(1)); self.observe(run, 90, window(2))
        self.assertFalse(self.history.path.exists())  # failure/cancellation never calls finish
        run = self.tracker(run_id='same'); self.full_run(run, start=100)
        duplicate = self.tracker(run_id='same'); self.full_run(duplicate, start=300)
        run.finish()
        self.assertEqual(len(self.history.samples(run.profile_key())), 1)

    def test_overrun_keeps_task_below_completion_and_does_not_claim_zero_eta(self):
        run = self.tracker()
        self.observe(run, 0, window(1))
        result = self.observe(run, 500, window(1))
        self.assertLess(result['percent'], 100)
        self.assertTrue(result['overrun'])
        self.assertIsNone(result['remaining_seconds'])

    def test_existing_remote_operation_is_not_a_complete_new_timing_sample(self):
        run = self.tracker(resumed=True)
        self.full_run(run)
        self.assertEqual(self.history.samples(run.profile_key()), [])

    def test_mac_fallback_separates_profiles_and_discards_remote_elapsed(self):
        run = self.tracker()
        self.observe(run, 0, 'Windows · 排队')
        self.observe(run, 100, '远程不可用，正在回退本机 MinerU')
        self.observe(run, 101, 'Mac · 准备页面与模型')
        self.observe(run, 111, window(1).removeprefix('Windows · '))
        self.assertEqual(run.provider, 'local')
        self.assertEqual(run.snapshot()['elapsed_seconds'], 10)
        self.assertNotEqual(run.profile_key(), self.tracker().profile_key())

    def test_corrupt_history_never_breaks_conversion_or_overwrites_evidence(self):
        for invalid in ('{broken', '[]', 'null', '{"schema":1,"profiles":{"broken": []}}'):
            self.history.path.write_text(invalid)
            run = self.tracker(); _, completed = self.full_run(run)
            self.assertEqual(completed['percent'], 100)
            if invalid != '{"schema":1,"profiles":{"broken": []}}':
                self.assertEqual(self.history.path.read_text(), invalid)


if __name__ == '__main__':
    unittest.main()
