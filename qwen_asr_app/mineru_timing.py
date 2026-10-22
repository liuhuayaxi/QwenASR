"""Time-weighted estimates, learned only from successful full observations.

Stage labels are observations, not a claim of precise engine instrumentation.
Queue time is excluded. First-window model warmup is separated from later pages.
No document text, file names, URLs or credentials are stored in the history.
"""
from collections import defaultdict
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import threading
import time

from .durable import atomic_json
from .paths import PRIVATE_ROOT

WINDOW = re.compile(r'第\s*(\d+)[–-](\d+)\s*页/(\d+)\s*·\s*窗口\s*(\d+)/(\d+)')
PHASES = ('startup', 'first_window', 'pages', 'export', 'download', 'package', 'finalize')
LABELS = dict(zip(PHASES, ('准备与加载', '首个页段', '后续页段识别', '保存识别结果', '传回结果', '生成可检索 PDF', '校验与保存')))
_LOCK = threading.RLock()


def pdf_pages(source):
    if Path(source).suffix.lower() != '.pdf':
        return 0
    try:
        from .native_pdf import pdf_operation
        return pdf_operation('page_count', path=str(source))
    except Exception:
        return 0


class TimingHistory:
    def __init__(self, path=None):
        self.path = Path(path) if path else PRIVATE_ROOT / 'mineru-timing.json'

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('profiles'), dict):
                raise ValueError('Invalid MinerU timing history')
            return data
        except FileNotFoundError:
            return {'schema': 1, 'profiles': {}}

    def samples(self, key):
        try:
            with _LOCK:
                values = self.read()['profiles'].get(key, [])
                if not isinstance(values, list):
                    return []
                return [s for s in values[-20:] if isinstance(s, dict)
                    and all(isinstance(s.get(field), dict) and all(
                        isinstance(s[field].get(p, 0), (int, float))
                        and math.isfinite(s[field].get(p, 0)) and s[field].get(p, 0) >= 0
                        for p in PHASES) for field in ('seconds', 'units'))]
        except (OSError, ValueError, TypeError):
            return []

    def add(self, key, sample):
        # Timing persistence must never turn a successful conversion into failure.
        try:
            with _LOCK:
                data = self.read()
                values = data['profiles'].setdefault(key, [])
                if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
                    raise ValueError('Invalid timing samples; preserve existing evidence')
                if any(item.get('id') == sample['id'] for item in values):
                    return
                values.append(sample)
                data['profiles'][key] = values[-20:]
                atomic_json(self.path, data)
        except (OSError, ValueError, TypeError):
            logging.warning('MinerU timing history unavailable; conversion result preserved')

    def known_window(self, key):
        """Use the last observed node window while its first new window loads."""
        try:
            wanted = json.loads(key); wanted.pop('window', None)
            candidates = []
            with _LOCK:
                for profile, samples in self.read()['profiles'].items():
                    identity = json.loads(profile)
                    if not isinstance(identity, dict) or not isinstance(samples, list):
                        continue
                    window = identity.pop('window', 0)
                    if identity == wanted and samples and window > 0:
                        candidates.append((samples[-1]['recorded_at'], window))
            return max(candidates)[1] if candidates else 0
        except (OSError, ValueError, TypeError, KeyError):
            return 0


class MineruTiming:
    def __init__(self, *, settings=None, config=None, pages=0, history=None, clock=time.monotonic, run_id='', resumed=False):
        self.settings, self.config = settings or {}, config or {}
        self.provider = 'local' if self.config.get('mineru_provider', 'local') == 'local' else 'remote'
        self.page_count = pages
        self.window_size = int(self.settings.get('processing_window_size') or 0) if self.provider == 'local' else 0
        self.history, self.clock = history or TimingHistory(), clock
        self.run_id = run_id or str(time.time_ns())
        self.last_time = None
        self.phase = 'startup'
        self.seconds = defaultdict(float)
        self.details = defaultdict(float)
        self.detail = ''
        self.window = None
        self.window_started = None
        self.rates = []
        self.high_water = 0.0
        self.complete_observation = not resumed
        self.finished = False
        self.cached_key, self.cached_samples = None, []
        if not self.window_size:
            self.window_size = self.history.known_window(self.profile_key())

    def profile_key(self):
        # Remote PDFs always use the node's Advanced contract, not local sliders.
        cfg = self.settings
        identity = {'provider': self.provider, 'format': 'pdf', 'engine_major': 4,
            'tier': cfg.get('tier', 'advanced') if self.provider == 'local' else 'advanced',
            'engine': cfg.get('vlm_engine', 'llama-cpp') if self.provider == 'local' else 'vllm',
            'ocr': cfg.get('ocr_mode', 'auto') if self.provider == 'local' else 'auto', 'window': self.window_size,
            'images': cfg.get('image_analysis', True) if self.provider == 'local' else True}
        if self.provider == 'remote':
            identity['node'] = hashlib.sha256(str(self.config.get('base_url', '')).encode()).hexdigest()[:12]
        else:
            identity.update(small_backend=cfg.get('small_backend', 'auto'),
                            concurrency=cfg.get('vlm_concurrency', 4), memory_gb=cfg.get('virtual_vram_gb', 0))
        return json.dumps(identity, sort_keys=True, separators=(',', ':'))

    def samples(self):
        key = self.profile_key()
        if key != self.cached_key:
            self.cached_key, self.cached_samples = key, self.history.samples(key)
        return self.cached_samples

    def units(self):
        first = min(self.page_count, self.window_size or 4)
        return {'startup': 1, 'first_window': 1 if first else 0,
                'pages': max(0, self.page_count - first), 'export': 1,
                'download': 1 if self.provider == 'remote' else 0,
                'package': max(1, self.page_count), 'finalize': 1}

    def budgets(self):
        # Explicit cold-start priors. They are never persisted as measurements.
        defaults = {'startup': 12., 'first_window': 65. if self.provider == 'remote' else 45.,
                    'pages': 5. if self.provider == 'remote' else 12.,
                    'export': 3., 'download': 3., 'package': .12, 'finalize': 1.}
        units, samples = self.units(), self.samples()
        result = {}
        for phase in PHASES:
            rates = [s['seconds'].get(phase, 0) / s['units'][phase]
                     for s in samples if s.get('units', {}).get(phase, 0) > 0]
            rate = sum(rates) / len(rates) if rates else defaults[phase]
            if phase == 'pages' and self.rates:
                # The current document dominates after three completed later windows.
                observed = sum(self.rates) / len(self.rates)
                weight = min(.85, len(self.rates) / (len(self.rates) + 2))
                rate = rate * (1 - weight) + observed * weight
            result[phase] = max(.1, rate * units[phase]) if units[phase] else 0.
        return result

    def _advance(self, now):
        if self.last_time is not None and self.phase != 'queued':
            elapsed = max(0, now - self.last_time)
            self.seconds[self.phase] += elapsed
            if self.detail:
                self.details[self.detail] += elapsed
        self.last_time = now

    def observe(self, stage, percent=None):
        if self.finished:
            return self.snapshot()
        now = self.clock()
        actual = 'remote' if stage.startswith('Windows ·') else 'local' if stage.startswith('Mac ·') or '回退本机' in stage else self.provider
        if actual != self.provider:
            # Remote queue/connection failure must not train the local machine.
            self.provider = actual
            self.seconds.clear(); self.details.clear(); self.rates.clear()
            self.window = self.window_started = self.last_time = None
            self.phase = 'startup'
            self.window_size = int(self.settings.get('processing_window_size') or 0)
        self._advance(now)
        if '排队' in stage or '等待 MinerU' in stage or '回退本机' in stage:
            self.phase, self.detail = 'queued', ''
            return self.snapshot()
        match = WINDOW.search(stage)
        if match:
            start, end, pages, index, total = map(int, match.groups())
            self.page_count = pages
            if self.window is None:
                self.complete_observation &= index == 1
                # Except the final short window, its span is the real node setting.
                if index < total or index == 1:
                    self.window_size = end - start + 1
            if self.window is None or index != self.window[3]:
                if self.window and self.window[3] >= 2 and index == self.window[3] + 1:
                    duration = now - self.window_started
                    self.rates.append(duration / (self.window[1] - self.window[0] + 1))
                self.window, self.window_started = (start, end, pages, index, total), now
            self.phase = 'first_window' if index == 1 else 'pages'
        elif any(word in stage for word in ('写入隐藏文字层', '逐页转写与 LaTeX', '生成可检索')):
            self.phase = 'package'
        elif any(word in stage for word in ('安全写入', '转换完成', '校验与保存')):
            self.phase = 'finalize'
        elif '下载识别结果' in stage:
            self.phase = 'download'
        elif any(word in stage for word in ('保存结构化', '生成逐页内容映射', '结构化结果已保存', '保存结果', '释放资源', '识别完成')):
            self.phase = 'export'
        elif self.phase == 'queued':
            self.phase = 'startup'
        # Retain only stage vocabulary, never arbitrary names from file paths/errors.
        self.detail = next((label for label in ('版面分析', '公式识别', '文字区域检测', '文字识别', '视觉内容识别', '加载模型', '加载视觉模型', '加载模型权重', '表格识别') if label in stage), LABELS.get(self.phase, '准备与加载'))
        return self.snapshot()

    def snapshot(self):
        if self.finished:
            return {'estimated': False, 'percent': 100, 'elapsed_seconds': round(sum(self.seconds.values()), 1), 'remaining_seconds': 0}
        if self.phase == 'queued':
            return {'estimated': True, 'percent': 0, 'phase': 'queued', 'phase_label': '排队等待', 'remaining_seconds': None, 'sample_count': len(self.samples())}
        if not self.page_count and self.phase == 'startup':
            return {'estimated': True, 'percent': 0, 'phase': 'startup', 'phase_label': LABELS['startup'],
                    'elapsed_seconds': round(sum(self.seconds.values()), 1), 'remaining_seconds': None,
                    'sample_count': 0, 'basis': 'cold_start'}
        budgets = self.budgets()
        index = PHASES.index(self.phase)
        spent = self.seconds[self.phase]
        budget = budgets[self.phase]
        if self.phase == 'pages' and self.window:
            first = self.window_size
            done_pages = max(0, self.window[0] - 1 - first)
            per_page = budgets['pages'] / max(1, self.units()['pages'])
            window_budget = per_page * (self.window[1] - self.window[0] + 1)
            window_elapsed = max(0, self.clock() - self.window_started)
            current_credit = done_pages * per_page + min(window_elapsed, window_budget * .95)
            overrun = window_elapsed > window_budget * 1.25
        else:
            current_credit = min(spent, budget * .95)
            overrun = spent > max(5, budget * 1.25)
        before = sum(budgets[p] for p in PHASES[:index])
        total = sum(budgets.values())
        estimate = 100 * (before + current_credit) / max(1, total)
        self.high_water = max(self.high_water, min(99., estimate))
        remaining = max(1, total - before - current_credit)
        return {'estimated': True, 'percent': round(self.high_water, 1),
                'phase': self.phase, 'phase_label': LABELS[self.phase],
                'elapsed_seconds': round(sum(self.seconds.values()), 1),
                'expected_seconds': round(total, 1),
                'remaining_seconds': None if overrun else round(remaining),
                'sample_count': len(self.samples()), 'observed_windows': len(self.rates),
                'basis': 'history_and_current' if self.samples() else 'current' if self.rates else 'cold_start',
                'overrun': overrun}

    def finish(self, *, learn=True):
        if self.finished:
            return self.snapshot()
        self._advance(self.clock())
        if learn and self.complete_observation and self.window and self.page_count and self.phase in ('export', 'download', 'package', 'finalize'):
            sample = {'id': hashlib.sha256(self.run_id.encode()).hexdigest()[:24], 'recorded_at': time.time(),
                      'pages': self.page_count, 'units': self.units(),
                      'seconds': {p: round(self.seconds[p], 3) for p in PHASES},
                      'observed_stage_seconds': {k: round(v, 3) for k, v in self.details.items()},
                      'measurement': 'progress_callback_intervals'}
            self.history.add(self.profile_key(), sample)
        self.finished = True
        return self.snapshot()
