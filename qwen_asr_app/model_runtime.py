from __future__ import annotations

import asyncio
import multiprocessing as mp
import queue
import time
from collections import deque
from pathlib import Path
from typing import Any

from .lazy import lazy_module
from .async_tasks import await_owned_task

from .scheduler import PriorityGate
from .settings import SAMPLE_RATE, normalize_asr_params

np = lazy_module("numpy")


def model_worker_main(model_path: str, requests: Any, results: Any) -> None:
    """Own MLX and all model memory in a disposable child process."""
    try:
        from mlx_audio.stt import load

        print(f"Qwen 子进程正在加载 {model_path}…", flush=True)
        from .local_compute import metal_lease
        with metal_lease(realtime=True, timeout=180):
            model = load(model_path, lazy=False)
        results.put({"type": "ready"})
    except Exception as exc:
        results.put({"type": "load_error", "message": str(exc)})
        return

    while True:
        request = requests.get()
        if request.get("type") == "stop":
            return
        request_id = request.get("id")
        try:
            asr_params = normalize_asr_params(
                request.get("asr_params"), int(request.get("max_tokens", 384))
            )
            generated = model.generate(
                np.asarray(request["audio"], dtype=np.float32),
                language=request.get("language"),
                system_prompt=request.get("context") or None,
                **asr_params,
            )
            detected = generated.language
            if isinstance(detected, list):
                detected = detected[0] if detected else ""
            results.put(
                {
                    "type": "result",
                    "id": request_id,
                    "text": generated.text.strip(),
                    "language": str(detected or ""),
                }
            )
        except Exception as exc:
            results.put({"type": "infer_error", "id": request_id, "message": str(exc)})


class ModelManager:
    def __init__(self, model_path: Path) -> None:
        self.model_path = model_path
        self.worker: Any | None = None
        self.request_queue: Any | None = None
        self.result_queue: Any | None = None
        self.next_request_id = 1
        self.status = "idle"
        self.detail = "Qwen3-ASR 已待命 · 点击转写时按需加载"
        self.device = "Apple Silicon GPU (MLX · BF16)"
        self.load_seconds: float | None = None
        self.infer_lock = asyncio.Lock()
        self.scheduler = PriorityGate()
        self.measurements = deque(maxlen=500)
        self.last_realtime = 0.0
        self.load_lock = asyncio.Lock()
        self.connected_clients = 0
        self.active_jobs = 0
        self.unload_task: asyncio.Task[Any] | None = None
        self._unloading = False
        self._closing = False
        self._retained_unready_worker = False

    async def _worker_thread(self, operation: Any) -> None:
        """Keep the caller's locks until an uncancellable worker thread exits."""
        pending = asyncio.create_task(asyncio.to_thread(operation))
        await await_owned_task(pending)

    async def load(self, force_local: bool = False, inference_config=None) -> None:
        from .inference_providers import inference_settings
        cfg = inference_settings.for_task('asr', inference_config)
        if cfg['asr_provider'] == 'remote_only' or (cfg['asr_provider'] != 'local' and not force_local):
            self.detail = 'Windows 语音识别 · 本机模型未加载'
            return
        self.cancel_unload()
        async with self.load_lock:
            if self.worker is not None and self.worker.is_alive():
                if getattr(self, '_retained_unready_worker', False):
                    raise RuntimeError("模型尚未加载成功且原子进程未退出，请先释放该进程后重试")
                self.schedule_unload()
                return
            self.status = "loading"
            self.detail = "正在按需加载 Qwen3-ASR-1.7B…"
            started = time.monotonic()
            load_completed = False
            def start_worker():
                nonlocal load_completed
                self._start_worker_sync()
                load_completed = True
                self._retained_unready_worker = False
            try:
                if not self.model_path.exists():
                    raise FileNotFoundError(f"模型目录不存在：{self.model_path}；请运行 .venv/bin/python -m qwen_asr_app.model_setup prepare，或在设置中选择仅远程 ASR")
                await self._worker_thread(start_worker)
                self.load_seconds = round(time.monotonic() - started, 1)
                self.status = "ready"
                self.detail = f"Qwen3-ASR-1.7B BF16 已就绪 · {self.device}"
                self.schedule_unload()
            except asyncio.CancelledError:
                # Loading may have completed after its requester disconnected.
                if load_completed and self.worker is not None and self.worker.is_alive():
                    self.status = "ready"
                    self.detail = f"Qwen3-ASR-1.7B BF16 已就绪 · {self.device}"
                    self.load_seconds = round(time.monotonic() - started, 1)
                elif self.worker is not None and self.worker.is_alive():
                    self._retained_unready_worker = True
                    self.status = "error"
                    self.detail = "模型加载未完成，子进程尚未退出；暂不启动新模型"
                else:
                    self.status = "idle"
                    self.detail = "模型加载已取消 · 下次转写时自动加载"
                self.schedule_unload()
                raise
            except Exception as exc:  # pragma: no cover - exposed by health endpoint
                self._retained_unready_worker = bool(not load_completed and self.worker is not None and self.worker.is_alive())
                self.status = "error"
                self.detail = f"模型加载失败：{exc}"
                print(self.detail, flush=True)
                self.schedule_unload()

    def _start_worker_sync(self) -> None:
        self._stop_worker_sync()
        context = mp.get_context("spawn")
        self.request_queue = context.Queue(maxsize=2)
        self.result_queue = context.Queue(maxsize=2)
        self.worker = context.Process(
            target=model_worker_main,
            args=(str(self.model_path), self.request_queue, self.result_queue),
            daemon=True,
            name="qwen-asr-mlx-worker",
        )
        self.worker.start()
        try:
            message = self.result_queue.get(timeout=180)
        except queue.Empty as exc:
            self._stop_worker_sync()
            raise RuntimeError("模型子进程加载超时") from exc
        if message.get("type") != "ready":
            self._stop_worker_sync()
            raise RuntimeError(str(message.get("message") or "模型子进程加载失败"))

    def _stop_worker_sync(self) -> None:
        worker = self.worker
        request_queue = self.request_queue
        result_queue = self.result_queue
        if worker is not None and worker.is_alive():
            try:
                if request_queue is not None:
                    request_queue.put({"type": "stop"}, timeout=0.5)
            except Exception:
                pass
            worker.join(timeout=3)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=3)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=3)
            if worker.is_alive():
                # Retain ownership: starting a new worker could duplicate GPU
                # allocations while the old process still uses these queues.
                raise RuntimeError("模型子进程未能退出，保留进程引用以防重复加载")
        for channel in (request_queue, result_queue):
            if channel is not None:
                try:
                    channel.close()
                    channel.cancel_join_thread()
                except Exception:
                    pass
        self.worker = None
        self._retained_unready_worker = False
        self.request_queue = None
        self.result_queue = None

    def _infer_with_lease(
        self,
        audio: np.ndarray,
        language: str | None,
        context: str,
        max_tokens: int,
        asr_params: Any,
        timeout_seconds: float,
    ) -> tuple[str, str]:
        if (
            self.worker is None
            or not self.worker.is_alive()
            or self.request_queue is None
            or self.result_queue is None
        ):
            raise RuntimeError("模型子进程未运行")
        request_id = self.next_request_id
        self.next_request_id += 1
        self.request_queue.put(
            {
                "type": "transcribe",
                "id": request_id,
                "audio": np.asarray(audio, dtype=np.float32),
                "language": language,
                "context": context,
                "max_tokens": max_tokens,
                "asr_params": asr_params,
            }
        )
        try:
            message = self.result_queue.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            self._stop_worker_sync()
            self.status = "idle"
            self.detail = "上一次转写超时，模型已释放；下次转写会自动重新加载"
            raise RuntimeError("模型子进程转写超时") from exc
        if message.get("id") != request_id:
            raise RuntimeError("模型子进程返回了错误的任务编号")
        if message.get("type") != "result":
            raise RuntimeError(str(message.get("message") or "模型转写失败"))
        return str(message.get("text") or ""), str(message.get("language") or "")

    def _infer_sync(self, audio, language, context, max_tokens, asr_params, timeout_seconds):
        from .local_compute import metal_lease
        with metal_lease(realtime=True, timeout=timeout_seconds):
            return self._infer_with_lease(audio, language, context, max_tokens, asr_params, timeout_seconds)

    def cancel_unload(self) -> None:
        # Only the idle timer/lock wait can be cancelled. Once shutdown starts,
        # a new request must wait for it, then load a fresh worker under the lock.
        if self._unloading:
            return
        if self.unload_task is not None and not self.unload_task.done():
            self.unload_task.cancel()
        self.unload_task = None

    def client_connected(self) -> None:
        self.connected_clients += 1
        self.schedule_unload()

    def client_disconnected(self) -> None:
        self.connected_clients = max(0, self.connected_clients - 1)
        self.schedule_unload()

    @property
    def idle_release_seconds(self) -> float:
        # A paused page is not a model lease. Keep a short warm interval for
        # live work, then let process exit return both RAM and Metal allocations.
        return 30.0 if self.connected_clients else 5.0

    def schedule_unload(self, delay: float | None = None) -> None:
        if self._closing or self._unloading or self.active_jobs or self.worker is None:
            return
        self.cancel_unload()
        self.unload_task = asyncio.create_task(self._unload_after(
            self.idle_release_seconds if delay is None else delay))

    async def _unload_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self.active_jobs:
                return
            async with self.infer_lock:
                async with self.load_lock:
                    if self.active_jobs or self.worker is None:
                        return
                    await self._unload_worker()
                    print("Qwen3-ASR 子进程已退出，模型内存已归还系统。", flush=True)
        except asyncio.CancelledError:
            pass
        finally:
            if self.unload_task is asyncio.current_task():
                self.unload_task = None

    async def _unload_worker(self) -> None:
        # Both worker locks must be held by the caller, including during cancel.
        self._unloading = True
        try:
            await self._worker_thread(self._stop_worker_sync)
        finally:
            self._unloading = False
            if self.worker is None:
                self.status = "idle"
                self.detail = "Qwen3-ASR 已卸载释放内存 · 下次转写时自动加载"
            else:
                self.status = "error"
                self.detail = "Qwen3-ASR 子进程尚未退出，暂不启动新模型"

    async def release_if_idle(self):
        async with self.infer_lock:
            async with self.load_lock:
                if not self.active_jobs and self.worker is not None:
                    await self._unload_worker()

    async def close(self) -> None:
        self._closing = True
        timer = self.unload_task
        self.cancel_unload()
        if timer is not None:
            await asyncio.gather(timer, return_exceptions=True)
        async with self.infer_lock:
            async with self.load_lock:
                await self._unload_worker()

    @property
    def realtime_pending(self):
        return time.monotonic() - self.last_realtime < 20 or bool(self.scheduler.snapshot()["realtime_queued"])

    def performance(self):
        result = {"scheduler": self.scheduler.snapshot(), "samples": len(self.measurements)}
        for kind in ("realtime", "background"):
            values = [m for m in self.measurements if m["kind"] == kind]
            result[kind] = {"count": len(values)}
            for field in ("queue_seconds", "load_seconds", "inference_seconds"):
                numbers = sorted(v[field] for v in values)
                result[kind][field] = {
                    "p50": round(float(np.percentile(numbers, 50)), 3) if numbers else None,
                    "p95": round(float(np.percentile(numbers, 95)), 3) if numbers else None,
                }
        return result

    async def transcribe(self, audio, language, context, max_tokens=384, asr_params=None,
                         timeout_seconds=180.0, priority=10, metrics=None, is_current=None, inference_config=None, operation_dir=None,
                         on_provider=None):
        from .inference_providers import inference_settings, LocalASRProvider, RemoteASRProvider, normalize
        cfg = inference_settings.for_task('asr', inference_config)
        mode = cfg["asr_provider"]
        def report(provider, *, fallback=False):
            if on_provider is not None:
                on_provider({'provider': provider, 'state': 'requested' if provider == 'remote' else 'running',
                             'fallback': fallback})
        async def local():
            return await self._transcribe_local(audio, language, context, max_tokens, asr_params,
                                                timeout_seconds, priority, metrics, is_current)
        if mode == "local":
            report('local')
            result = await LocalASRProvider().transcribe(local)
            if metrics is not None:
                metrics.update(provider="local", fallback=False, config_snapshot=cfg,
                    model='Qwen3-ASR-1.7B-MLX-bf16', dtype='bfloat16', device='Apple Silicon MLX', gpu_verification='unknown')
            return result
        normalize_asr_params(asr_params, max_tokens)
        if is_current is not None and not is_current():
            return "", "", 0.0
        self.active_jobs += 1
        try:
            report('remote')
            remote = RemoteASRProvider()
            if operation_dir is None:
                result, metadata = await remote.transcribe(audio, language, context, max_tokens, cfg)
            else:
                result, metadata = await remote.transcribe(audio, language, context, max_tokens, cfg, operation_dir=operation_dir, asr_params=asr_params)
            if metrics is not None:
                metrics.update(metadata)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if mode == "remote_only" or not getattr(exc, 'proven_not_accepted', False):
                raise
            from .remote_operations import mark_local_fallback
            mark_local_fallback(Path(operation_dir)/'receipt.json')
            self.detail = '远程确认未接受，正在回退本机 Qwen3-ASR'
            report('local', fallback=True)
            result = await LocalASRProvider().transcribe(local)
            if metrics is not None:
                metrics.update(provider="local", fallback=True, fallback_reason=type(exc).__name__,
                    config_snapshot=cfg, model='Qwen3-ASR-1.7B-MLX-bf16', dtype='bfloat16',
                    device='Apple Silicon MLX', gpu_verification='unknown')
            return result
        finally:
            self.active_jobs = max(0, self.active_jobs - 1)
            self.schedule_unload()

    async def _transcribe_local(
        self,
        audio: np.ndarray,
        language: str | None,
        context: str,
        max_tokens: int = 384,
        asr_params: Any = None,
        timeout_seconds: float = 180.0,
        priority: int = 10,
        metrics: dict | None = None,
        is_current: Any = None,
    ) -> tuple[str, str, float]:
        self.active_jobs += 1
        self.cancel_unload()
        try:
            validated_asr_params = normalize_asr_params(asr_params, max_tokens)
            duration_seconds = len(audio) / SAMPLE_RATE
            # A course-level max_tokens value is a safety ceiling. Scaling the
            # effective limit to clip length prevents noisy/silent audio from
            # decoding thousands of hallucinated tokens before it can stop.
            dynamic_token_limit = min(1_536, max(192, int(duration_seconds * 10) + 128))
            validated_asr_params["max_tokens"] = min(
                int(validated_asr_params["max_tokens"]), dynamic_token_limit
            )
            started = time.monotonic()
            audio_np = np.asarray(audio, dtype=np.float32)
            if priority < 10:
                self.last_realtime = time.monotonic()
            async with self.scheduler.slot(priority):
                acquired = time.monotonic()
                if is_current is not None and not is_current():
                    return "", "", 0.0
                async def infer():
                    async with self.infer_lock:
                        # Existing local tests and integrations call load() with
                        # no keyword arguments. Bypass only for actual fallback.
                        from .inference_providers import inference_settings
                        if inference_settings.for_task('asr')['asr_provider'] != 'local':
                            await self.load(force_local=True, inference_config={'asr_provider':'local'})
                        else:
                            await self.load()
                        loaded = time.monotonic()
                        if self.status != "ready" or self.worker is None or not self.worker.is_alive():
                            raise RuntimeError(self.detail)
                        result = await asyncio.to_thread(
                            self._infer_sync, audio_np, language, context, max_tokens,
                            validated_asr_params, max(30.0, min(float(timeout_seconds), 600.0)),
                        )
                        measured = {"kind": "realtime" if priority < 10 else "background",
                                    "audio_seconds": round(duration_seconds, 3),
                                    "queue_seconds": round(acquired - started, 3),
                                    "load_seconds": round(loaded - acquired, 3),
                                    "inference_seconds": round(time.monotonic() - loaded, 3)}
                        self.measurements.append(measured)
                        if metrics is not None:
                            metrics.update(measured)
                        return result
                # Cancellation must not release the GPU while its thread still owns it.
                inference = asyncio.create_task(infer())
                text, detected = await await_owned_task(inference)
            return text, detected, round(time.monotonic() - started, 2)
        finally:
            self.active_jobs = max(0, self.active_jobs - 1)
            self.schedule_unload()
