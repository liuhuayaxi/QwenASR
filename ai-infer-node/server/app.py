"""Authenticated, streaming, persistent gateway. No model imports at startup."""
import asyncio
import contextlib
import fcntl
import hashlib
import ipaddress
import hmac
import json
import math
import os
import re
import shutil
import signal
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from .config import ROOT, configuration, validate_contract, verify_model_manifests
from .task_scope import TaskScope, observe_saved
from .cache import reclaim_model_cache, bounded_reclaim
from .resources import memory_observation, group_processes, host_observation, runtime_identity, source_identity, descendants, tracked_processes, terminate_tracked
from .artifacts import finalize
from .store import ACTIVE, TERMINAL, Store, public_job
from client.mineru_progress import mineru_progress

DOCUMENTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
NATIVE = {".docx", ".pptx", ".xlsx", ".odt", ".ods", ".odp", ".rtf", ".epub", ".html", ".mhtml", ".mht", ".csv", ".tsv", ".ofd"}
AUDIO = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4", ".webm", ".aiff"}
IDENT = re.compile(r"^[a-f0-9]{32}$")


def ticks(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        try:
            return 'ctime:' + str(psutil.Process(pid).create_time())
        except psutil.Error:
            return ''


def kill_group(pid, sig):
    with contextlib.suppress(ProcessLookupError):
        try:
            os.killpg(pid, sig)
        except PermissionError:
            # macOS can reject signaling a group containing only reaped/zombie
            # processes. Suppress only after independently observing no live member.
            if group_processes(pid):
                raise


async def gpu_usage():
    binary = shutil.which("nvidia-smi") or "/usr/lib/wsl/lib/nvidia-smi"
    try:
        proc = await asyncio.create_subprocess_exec(binary, "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 3)
        values = [float(x.strip()) for x in out.decode().splitlines()[0].split(",")]
        if len(values) != 3 or not all(math.isfinite(v) for v in values) or not (0 <= values[0] <= values[1] and 0 <= values[2] <= 100):
            raise ValueError('Invalid GPU measurements')
        return dict(zip(("used_mib", "total_mib", "utilization_percent"), values))
    except (OSError, ValueError, IndexError, asyncio.TimeoutError):
        if "proc" in locals() and proc.returncode is None:
            proc.kill()
            await proc.wait()
        return {"available": False}


class Gateway:
    def __init__(self, cfg):
        self.cfg = cfg
        self.data = Path(cfg["server"]["data_dir"])
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        configured_results = cfg["server"].get("results_dir")
        self.results = Path(configured_results).expanduser().resolve() if configured_results else None
        if self.results is not None:
            if self.results == self.data or self.results.is_relative_to(self.data):
                raise RuntimeError("RESULTS_DIRECTORY_MUST_BE_SEPARATE")
            self.results.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.results, 0o700)
        self.instance_lock = (self.data / "gateway.lock").open("a")
        fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.store = Store(self.data / "jobs.sqlite3")
        self.coordination = Path(cfg['server'].get('gpu_coordination_dir') or os.environ.get(
            'AI_INFER_GPU_COORDINATION_DIR', '~/.local/state/ai-infer-node/gpu0')).expanduser().resolve()
        self.coordination.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.coordination.stat().st_uid != os.getuid() or self.coordination.stat().st_mode & 0o077:
            raise RuntimeError('GPU_COORDINATION_DIRECTORY_MUST_BE_PRIVATE')
        self.scope_path = None
        self.process = None
        self.current = None
        self.stopping = False
        self.work_available = asyncio.Event()
        self.observation_lock = asyncio.Lock()
        self.status_observations = {}
        self.loop = None
        self.isolation = None
        self.scheduler_error = None
        self.owned_pgid = None
        self.recovery_samples = 0
        self.release_baseline = None
        self.startup_checks = {'state': 'not_checked'}
        self.loaded_source = source_identity()
        try:
            self.isolation = json.loads((self.coordination / 'resource-isolation.json').read_text())
            self.release_baseline = self.isolation.get('baseline')
            self.owned_pgid = self.isolation.get('pgid')
            self.scope_path = self.isolation.get('scope_path')
        except FileNotFoundError:
            pass
        self.token = os.environ.get("AI_INFER_TOKEN", "").strip()
        if not self.token:
            token_path = Path(cfg["server"]["token_file"])
            if token_path.stat().st_mode & 0o077:
                raise RuntimeError("Token file must be private (chmod 600)")
            self.token = token_path.read_text().strip()
        if len(self.token) < 32:
            raise RuntimeError("Token must have at least 32 characters")

    def folder(self, ident):
        if not IDENT.fullmatch(ident):
            raise HTTPException(404, "Unknown job")
        return self.data / ident

    def result_folder(self, ident):
        if not IDENT.fullmatch(ident):
            raise HTTPException(404, "Unknown job")
        return self.results / ident if self.results is not None else self.data / ident / "result"

    def isolate(self, reason, baseline=None, pgid=None):
        self.isolation = {'reason': reason, 'baseline': baseline, 'pgid': pgid, 'scope_path': self.scope_path, 'data_dir': str(self.data), 'at': time.time()}
        self.release_baseline, self.owned_pgid = baseline, pgid
        self.recovery_samples = 0
        from .artifacts import atomic_json
        atomic_json(self.coordination / 'resource-isolation.json', self.isolation)

    def update(self, ident, **values):
        try:
            self.store.update(ident, **values)
            return True
        except Exception as exc:
            self.scheduler_error = 'STORE_' + type(exc).__name__
            return False

    async def start(self):
        try:
            manifests = await asyncio.to_thread(verify_model_manifests, self.cfg)
            probe = TaskScope(self.cfg['server'].get('worker_cgroup_root'), uuid.uuid4().hex)
            observed = probe.observation()
            if observed['state'] != 'observed' or observed['populated']:
                raise RuntimeError('WORKER_CGROUP_PROBE_UNVERIFIED')
            probe.close()
            gpu = await gpu_usage()
            if 'used_mib' not in gpu:
                raise RuntimeError('GPU_STARTUP_OBSERVATION_UNAVAILABLE')
            self.startup_checks = {'state': 'verified', 'model_manifests': manifests,
                                   'worker_cgroup': observed, 'gpu_observation': gpu}
        except Exception as exc:
            self.startup_checks = {'state': 'unverified', 'error': type(exc).__name__}
            self.isolate('STARTUP_CONTRACT_UNVERIFIED')
        # Terminal receipts survive a failed final SQLite write.
        for receipt in self.data.glob('*/terminal.json'):
            value = json.loads(receipt.read_text())
            self.store.update(receipt.parent.name, **value)
        for row in self.store.unfinished():
            owned = self.data/row['id']/'owned-processes.json'
            if owned.is_file():
                with contextlib.suppress(OSError, psutil.Error):
                    terminate_tracked(json.loads(owned.read_text()), signal.SIGKILL)
            pid = row['pid']
            if pid and row['start_ticks'] and ticks(pid) == row['start_ticks']:
                try:
                    kill_group(pid, signal.SIGKILL)
                except OSError:
                    pass
            self.isolate('GATEWAY_RESTARTED_REQUIRES_RESOURCE_CHECK', json.loads(row['metrics']).get('gpu_baseline', {}).get('used_mib'), pid)
            self.store.update(row['id'], status='canceled' if row['canceled'] else 'failed', error='GATEWAY_RESTARTED', pid=None, start_ticks=None)
        for part in self.data.glob('upload-*.part'):
            part.unlink(missing_ok=True)
        self.loop = asyncio.create_task(self.schedule())

    async def stop(self):
        self.stopping = True
        self.work_available.set()
        classroom=getattr(self,'classroom',None)
        if classroom:
            if classroom.timer:classroom.timer.cancel()
            await classroom.close()
        if self.loop:
            await self.loop
        self.store.db.close()
        self.instance_lock.close()

    def remaining_tracked(self):
        result = []
        for path in self.data.glob('*/owned-processes.json'):
            result.extend(tracked_processes(json.loads(path.read_text())))
        return result

    async def calibrate_idle(self):
        """Authenticated operator action for a missing original GPU baseline."""
        if self.current or self.process:
            return False
        readings = []
        for _ in range(3):
            observed = await gpu_usage()
            if ('used_mib' not in observed or observed['used_mib'] > self.cfg['server'].get('idle_gpu_ceiling_mib', 128)
                    or group_processes(self.owned_pgid) or self.remaining_tracked()):
                return False
            readings.append(observed['used_mib'])
            await asyncio.sleep(1)
        if max(readings)-min(readings) > 16:
            return False
        self.release_baseline = max(readings)
        # Calibration does not bypass reconciliation: require three more
        # samples under this baseline before the supervisor resumes dispatch.
        if self.isolation:
            self.isolate(self.isolation['reason'], self.release_baseline, self.owned_pgid)
        return True

    async def reconcile(self):
        # Unknown baseline cannot be auto-approved. Operator must establish an
        # observed baseline; restarting the gateway does not erase quarantine.
        if self.startup_checks['state'] != 'verified':
            return
        if self.isolation and self.isolation.get('data_dir', str(self.data)) != str(self.data):
            return  # Only the owning node can reconcile its tracked identities/database.
        if self.release_baseline is None or self.current or self.process:
            return
        if self.store.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            return
        if self.scope_path and observe_saved(self.scope_path)['populated']:
            return
        if not self.scope_path and self.owned_pgid:
            return  # Legacy groups cannot prove absence of setsid descendants.
        after = await gpu_usage()
        clear = ('used_mib' in after and after['used_mib'] <= self.release_baseline + 64
                 and not group_processes(self.owned_pgid) and not self.remaining_tracked())
        self.recovery_samples = self.recovery_samples + 1 if clear else 0
        if self.recovery_samples >= 3:
            (self.coordination / 'resource-isolation.json').unlink(missing_ok=True)
            self.isolation, self.owned_pgid, self.scheduler_error = None, None, None

    async def wait_for_work(self, timeout=5):
        try:
            await asyncio.wait_for(self.work_available.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def status_observation(self, key, observe, max_age):
        # Cache dashboard probes only. Startup, job ownership and GPU-release
        # verification always call their observers directly for fresh evidence.
        async with self.observation_lock:
            saved = self.status_observations.get(key)
            if saved is None or time.monotonic() - saved[0] >= max_age:
                value = await observe()
                saved = (time.monotonic(), time.time(), value)
                self.status_observations[key] = saved
            return {**saved[2], 'sampled_at': saved[1],
                    'sample_age_seconds': round(time.monotonic() - saved[0], 3),
                    'sample_interval_seconds': max_age}

    async def schedule(self):
        cleaned = 0
        while not self.stopping:
            try:
                self.work_available.clear()
                classroom = getattr(self, 'classroom', None)
                if classroom and (classroom.reserved() or classroom.guard.locked()):
                    await self.wait_for_work(1)
                    continue
                shared_isolation = self.coordination/'resource-isolation.json'
                if shared_isolation.exists():
                    self.isolation = json.loads(shared_isolation.read_text())
                    self.release_baseline = self.isolation.get('baseline')
                    self.scope_path = self.isolation.get('scope_path')
                    self.owned_pgid = self.isolation.get('pgid')
                if self.isolation or self.scheduler_error:
                    await self.reconcile()
                    await asyncio.sleep(1)
                    continue
                if time.monotonic() - cleaned > 3600:
                    for ident in self.store.expired(time.time() - self.cfg['server']['history_days'] * 86400):
                        if self.results is not None:
                            shutil.rmtree(self.result_folder(ident), ignore_errors=True)
                        shutil.rmtree(self.folder(ident), ignore_errors=True)
                        self.store.remove(ident)
                    cleaned = time.monotonic()
                ident = self.store.queued()
                if ident:
                    await self.execute(ident)
                else:
                    await self.wait_for_work()
            except Exception as exc:
                self.scheduler_error = 'SCHEDULER_' + type(exc).__name__
                with contextlib.suppress(Exception):
                    self.isolate(self.scheduler_error, self.release_baseline, self.owned_pgid)
                await asyncio.sleep(1)

    async def execute(self, ident):
        if self.isolation or self.scheduler_error:
            return
        row = self.store.get(ident)
        folder = self.folder(ident)
        lock = (self.coordination / "gpu.lock").open("a")
        external = None
        external_path = self.cfg['server'].get('external_gpu_lock_file')
        if external_path:
            try:
                # Coordinate with the unchanged older gateway using its
                # existing lock inode. Never create or write its lock file.
                external = Path(external_path).open('rb')
            except OSError:
                lock.close()
                self.isolate('EXTERNAL_GPU_LOCK_UNAVAILABLE')
                return
        acquired = False
        while not self.stopping:
            if self.store.get(ident)["canceled"]:
                self.store.update(ident, status="canceled")
                lock.close()
                if external:
                    external.close()
                return
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if external:
                    fcntl.flock(external, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                await asyncio.sleep(.25)
        if not acquired:
            lock.close()
            if external:
                external.close()
            return
        if (self.coordination/'resource-isolation.json').exists():
            lock.close()
            if external:
                external.close()
            return
        self.current = ident
        metrics, error, status = {}, "", "failed"
        baseline = {}
        pgid = None
        scope = None
        tracked = {}
        started = time.monotonic()
        log = None
        try:
            snapshot = json.loads(row.get('execution_snapshot') or '{}')
            execution_cfg = {**self.cfg, **snapshot.get('models', {})}
            validate_contract(execution_cfg)
            manifests = await asyncio.to_thread(verify_model_manifests, execution_cfg)
            runtime = await asyncio.to_thread(runtime_identity, execution_cfg)
            if snapshot.get('runtime_fingerprint') and runtime != snapshot['runtime_fingerprint']:
                raise RuntimeError('ACCEPTED_RUNTIME_CHANGED_REQUIRES_OPERATOR_REVIEW')
            metrics['model_manifests'] = manifests
            scope = TaskScope(self.cfg['server'].get('worker_cgroup_root'), ident)
            self.scope_path = str(scope.path)
            from .artifacts import atomic_json
            atomic_json(folder/'scope.json', {'path': self.scope_path, 'data_dir': str(self.data)})
            self.store.update(ident, status="loading", progress=1)
            baseline = await gpu_usage()
            if 'used_mib' not in baseline:
                raise RuntimeError('GPU_BASELINE_UNKNOWN')
            peak = baseline['used_mib']
            metrics['memory_before'] = memory_observation()
            metrics['gpu_baseline'] = baseline
            metrics['runtime_fingerprint'] = runtime
            # Save the observed baseline before spawning: crash recovery cannot
            # invent it from the next idle reading.
            self.store.update(ident, metrics=json.dumps(metrics))
            request = {"input": str(folder / ("input" + row["extension"])), "options": json.loads(row["options"]),
                       "job_dir": str(folder), "config": execution_cfg, "parent_pid": os.getpid()}
            from .artifacts import atomic_json
            atomic_json(folder/'request.json', request)
            log = (folder / "worker.log").open("wb")
            self.process = await asyncio.create_subprocess_exec(execution_cfg[row["kind"]]["python"], "-m", "workers.scoped", str(scope.path), "workers." + row["kind"] + "_worker",
                "--request", str(folder / "request.json"), cwd=ROOT, start_new_session=True,
                stdout=log, stderr=log, env={**os.environ, "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4"})
            pgid = self.process.pid
            self.owned_pgid = pgid
            self.store.update(ident, pid=self.process.pid, start_ticks=ticks(self.process.pid))
            waiter = asyncio.create_task(self.process.wait())
            log_cursor, progress_text = 0, ''
            while self.process.returncode is None:
                for identity in descendants(self.process.pid):
                    tracked[identity['pid']] = identity
                from .artifacts import atomic_json
                atomic_json(folder/'owned-processes.json', list(tracked.values()))
                current = self.store.get(ident)
                if current["canceled"] or self.stopping:
                    error = "CANCELED" if current["canceled"] else "GATEWAY_STOPPED"
                    break
                if time.monotonic() - started > self.cfg["server"]["job_timeout_seconds"]:
                    error = "JOB_TIMEOUT"
                    break
                event = folder / "event.json"
                try:
                    value = json.loads(event.read_text())
                    if row['kind'] == 'mineru':
                        # Read the actual stdout/stderr file, including native
                        # subprocess output that Python stream redirection misses.
                        with (folder / 'worker.log').open('rb') as stream:
                            stream.seek(log_cursor)
                            chunk = stream.read()
                            log_cursor = stream.tell()
                        progress_text += chunk.decode('utf-8', errors='replace')
                        observed = mineru_progress(progress_text)
                        value['progress'] = None
                        if observed and value['state'] in {'loading', 'running'}:
                            value['detail'] = observed[0]
                    if value["state"] in ACTIVE and (current["status"] != value["state"] or current["progress"] != value["progress"] or current["detail"] != value.get("detail", "")):
                        self.store.update(ident, status=value["state"], progress=value["progress"], detail=value.get("detail", ""))
                except (OSError, ValueError, KeyError):
                    pass
                peak = max(peak, (await gpu_usage()).get("used_mib", 0))
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), .75)
                except asyncio.TimeoutError:
                    pass
            if error:
                status = "canceled" if error == "CANCELED" else "failed"
            elif self.process.returncode == 0 and (folder / "result" / "result.zip").is_file():
                metrics.update(json.loads((folder / "result" / "metadata.json").read_text()))
                status = "completed"
            else:
                try:
                    error = json.loads((folder / "failure.json").read_text())["code"]
                except (OSError, ValueError, KeyError):
                    error = "WORKER_FAILED"
            metrics.update(gpu_baseline=baseline, peak_gpu_used_mib=peak)
        except Exception as exc:
            error = type(exc).__name__
        finally:
            cleanup_errors = []
            if scope is not None:
                try:
                    scope.stop()
                except OSError as exc:
                    cleanup_errors.append(type(exc).__name__)
            self.update(ident, status='unloading')
            if self.process:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        if scope is None:
                            kill_group(pgid, sig)
                        terminate_tracked(tracked.values(), sig)
                        await asyncio.wait_for(self.process.wait(), 5)
                    except (OSError, asyncio.TimeoutError) as exc:
                        cleanup_errors.append(type(exc).__name__)
            if log:
                with contextlib.suppress(OSError):
                    log.close()
            release_started = time.monotonic()
            after = {}
            release_state = 'unknown'
            try:
                after = await gpu_usage()
                while ('used_mib' in baseline and 'used_mib' in after
                       and after['used_mib'] > baseline['used_mib'] + 64
                       and time.monotonic()-release_started < self.cfg['server'].get('gpu_release_timeout_seconds', 20)):
                    await asyncio.sleep(.5)
                    after = await gpu_usage()
                if 'used_mib' in baseline and 'used_mib' in after:
                    release_state = 'verified' if after['used_mib'] <= baseline['used_mib'] + 64 else 'failed'
                scope_observation = scope.observation() if scope else {'state': 'unknown', 'populated': True}
                metrics['worker_scope'] = scope_observation
                if scope_observation['state'] != 'observed' or scope_observation['populated']:
                    release_state = 'unknown'
                processes = tracked_processes(tracked.values())
                process_deadline = time.monotonic() + 2
                while processes and not any(p.get('state') == 'unknown' for p in processes) and time.monotonic() < process_deadline:
                    await asyncio.sleep(.05)
                    processes = tracked_processes(tracked.values())
                if scope is None:
                    processes += group_processes(pgid)
                if processes or cleanup_errors:
                    release_state = 'failed'
                metrics['remaining_node_processes'] = processes
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
                # A failed process observation cannot retain the GPU-only
                # verdict computed above. Quarantine until ownership is known.
                release_state = 'unknown'
            if release_state != 'verified' or self.scheduler_error:
                with contextlib.suppress(Exception):
                    self.isolate('RESOURCE_' + release_state.upper(), baseline.get('used_mib'), pgid)
                metrics['resource_warning'] = 'GPU_RELEASE_' + release_state.upper()
            metrics.update(gpu_baseline=baseline, gpu_after=after, gpu_release_state=release_state,
                           gpu_release_seconds=time.monotonic()-release_started,
                           gpu_release_verified=release_state == 'verified', cleanup_errors=cleanup_errors,
                           gateway_job_seconds=time.monotonic()-started, memory_after=memory_observation())
            scope_closed = False
            try:
                if release_state == 'verified':
                    if scope is not None:
                        scope.close()
                        scope_closed = True
                    metrics['model_file_cache_reclaim'] = await bounded_reclaim(self.cfg)
            except Exception as exc:
                metrics['model_file_cache_reclaim'] = {'state': 'unknown', 'error': type(exc).__name__}
            try:
                if self.store.get(ident)['canceled']:
                    status, error = 'canceled', 'CANCELED'
                if status == 'completed':
                    try:
                        await asyncio.to_thread(finalize, folder/'result', metrics)
                        if self.results is not None:
                            published = self.results / ident
                            published.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                            os.replace(folder / 'result', published)
                            fd = os.open(self.results, os.O_RDONLY)
                            try:
                                os.fsync(fd)
                            finally:
                                os.close(fd)
                    except Exception as exc:
                        status, error = 'failed', 'RESULT_FINALIZATION_' + type(exc).__name__
                if self.store.get(ident)['canceled']:
                    status, error = 'canceled', 'CANCELED'
                terminal = dict(status=status, error=error, progress=100 if status == 'completed' else 0,
                                metrics=json.dumps(metrics), pid=None, start_ticks=None)
                from .artifacts import atomic_json
                atomic_json(folder/'terminal.json', terminal)
                self.update(ident, **terminal)
            except Exception as exc:
                self.scheduler_error = 'TERMINAL_' + type(exc).__name__
                with contextlib.suppress(Exception):
                    self.isolate(self.scheduler_error, baseline.get('used_mib'), pgid)
            finally:
                for name in ('normalized.wav', 'segment.wav'):
                    with contextlib.suppress(OSError):
                        (folder/name).unlink(missing_ok=True)
                self.process = None
                self.current = None
                if scope_closed and release_state == 'verified' and not self.isolation and not self.scheduler_error:
                    self.owned_pgid = None
                    self.scope_path = None
                lock.close()
                if external:
                    external.close()


def validated_options(kind, suffix, raw):
    if kind == "mineru":
        if suffix not in DOCUMENTS | NATIVE:
            raise HTTPException(400, "Unsupported document format")
        if set(raw) - {"ocr_mode", "tier"}:
            raise HTTPException(400, "Unknown document options")
        tier = "advanced" if suffix in DOCUMENTS else "flash"
        if "tier" in raw and raw["tier"] != tier:
            raise HTTPException(400, "PDF/images require advanced; native documents require flash")
        if raw.get("ocr_mode", "auto") not in ("auto", "txt", "ocr"):
            raise HTTPException(400, "Invalid OCR mode")
        return {"tier": tier, "ocr_mode": raw.get("ocr_mode", "auto")}
    if kind != "asr" or suffix not in AUDIO:
        raise HTTPException(400, "Unsupported job/audio format")
    if set(raw) - {"language", "prompt", "max_new_tokens"}:
        raise HTTPException(400, "Unknown ASR options")
    for key, limit in (("language", 64), ("prompt", 4000)):
        if key in raw and (not isinstance(raw[key], str) or len(raw[key]) > limit):
            raise HTTPException(400, "Invalid ASR text option")
    tokens = raw.get("max_new_tokens", 512)
    if isinstance(tokens, bool) or not isinstance(tokens, int) or not 128 <= tokens <= 8192:
        raise HTTPException(400, "Invalid ASR token limit")
    return {**raw, "max_new_tokens": tokens}


@asynccontextmanager
async def lifespan(app):
    os.umask(0o077)
    app.state.gateway = Gateway(configuration())
    await app.state.gateway.start()
    try:
        yield
    finally:
        await app.state.gateway.stop()


app = FastAPI(title="AI inference node", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
from .classroom import install as install_classroom
install_classroom(app)


@app.middleware("http")
async def authentication(request, call_next):
    try:
        address = ipaddress.ip_address(request.client.host if request.client else '')
        networks = app.state.gateway.cfg['server'].get('allowed_client_networks', ['127.0.0.0/8','100.64.0.0/10','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','::1/128'])
        allowed = any(address in ipaddress.ip_network(value) for value in networks)
    except ValueError:
        allowed = False
    if not allowed:
        return JSONResponse({'ok': False, 'error': 'NETWORK_NOT_ALLOWED'}, status_code=403)
    supplied = request.headers.get("authorization", "")
    expected = "Bearer " + app.state.gateway.token
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        return JSONResponse({"ok": False, "error": "UNAUTHORIZED"}, status_code=401)
    return await call_next(request)


@app.exception_handler(HTTPException)
async def error_response(request, exc):
    return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse({"ok": False, "error": "INVALID_REQUEST"}, status_code=422)


@app.get("/v1/health")
async def health():
    node = app.state.gateway
    alive = node.loop is not None and not node.loop.done()
    source_matches = node.loaded_source == source_identity()
    healthy = alive and source_matches and node.startup_checks['state'] == 'verified' and not node.isolation and not node.scheduler_error and not node.stopping and not (node.coordination/'resource-isolation.json').exists()
    return JSONResponse({'ok': healthy, 'data': {'status': 'healthy' if healthy else 'degraded',
        'scheduler_alive': alive, 'resource_isolation': node.isolation, 'scheduler_error': node.scheduler_error,
        'models_preloaded': False, 'service': 'ai-infer-node', 'protocol_version': 2, 'idempotency_retention': 'permanent', 'result_ttl_separate': True,
        'startup_checks': node.startup_checks, 'loaded_source_sha256': node.loaded_source,
        'source_matches_loaded': source_matches,
        'runtime_fingerprint': await asyncio.to_thread(runtime_identity, node.cfg)}}, status_code=200 if healthy else 503)


@app.post('/v1/resources/recheck')
async def recheck_resources():
    node = app.state.gateway
    if not node.isolation:
        return {'ok': True, 'data': {'state': 'not_isolated'}}
    if node.release_baseline is None and not await node.calibrate_idle():
        raise HTTPException(409, 'Idle GPU and node process ownership could not be verified')
    await node.reconcile()
    return {'ok': True, 'data': {'resource_isolation': node.isolation, 'recovery_samples': node.recovery_samples}}


@app.get("/v1/status")
async def status():
    node = app.state.gateway
    counts = node.store.counts()
    return {"ok": True, "data": {"state": node.store.get(node.current)["status"].upper() if node.current else "QUEUED" if counts.get("queued") else "IDLE",
        "resource_isolation": node.isolation, "scheduler_alive": node.loop is not None and not node.loop.done(),
        "resources": {**memory_observation(), "windows_host": await node.status_observation('host', host_observation, 15), "node_processes": group_processes(node.owned_pgid)},
        "current_job": node.current, "worker_pid": node.process.pid if node.process else None,
        "data_dir": str(node.data), "results_dir": str(node.results) if node.results else str(node.data / "<job-id>" / "result"),
        "startup_checks": node.startup_checks, "models_preloaded": False,
        "queue": counts, "gpu": await node.status_observation('gpu', gpu_usage, 5), "gateway_rss_mib": psutil.Process().memory_info().rss / 2**20,
        "memory": psutil.virtual_memory()._asdict(), "model_idle_timeout_seconds": 0, "max_concurrent_gpu_jobs": 1}}


@app.post("/v1/jobs", status_code=202)
async def submit(request: Request, kind: str, filename: str, options: str = "{}"):
    node = app.state.gateway
    if len(filename) > 255 or any(x in filename for x in ("/", "\\", "\x00")) or filename in (".", ".."):
        raise HTTPException(400, "Invalid filename")
    if len(options) > 8192:
        raise HTTPException(400, "Options too large")
    try:
        raw = json.loads(options)
        if not isinstance(raw, dict):
            raise ValueError()
    except ValueError:
        raise HTTPException(400, "Invalid options JSON")
    suffix = Path(filename).suffix.lower()
    normalized = validated_options(kind, suffix, raw)
    key = request.headers.get("idempotency-key", "")
    if not re.fullmatch(r"[a-zA-Z0-9._:-]{8,128}", key):
        raise HTTPException(400, "An 8-128 character Idempotency-Key is required")
    if node.store.counts().get("queued", 0) >= node.cfg["server"]["max_queued_jobs"] and not node.store.by_key(key):
        raise HTTPException(429, "Queue full")
    maximum = node.cfg["server"]["max_upload_bytes"]
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HTTPException(400, "Invalid Content-Length")
    if length > maximum:
        raise HTTPException(413, "Upload too large")
    models = json.loads(json.dumps({part: node.cfg[part] for part in ('asr', 'mineru')}))
    runtime = await asyncio.to_thread(runtime_identity, {**node.cfg, **models})
    execution_snapshot = {'models': models, 'runtime_fingerprint': runtime}
    ident = uuid.uuid4().hex
    temporary = node.data / ("upload-" + ident + ".part")
    digest, size = hashlib.sha256(), 0
    try:
        async with asyncio.timeout(node.cfg["server"]["upload_timeout_seconds"]):
            with temporary.open("xb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > maximum:
                        raise HTTPException(413, "Upload too large")
                    digest.update(chunk)
                    await asyncio.to_thread(out.write, chunk)
                out.flush()
                os.fsync(out.fileno())
        if not size:
            raise HTTPException(400, "Empty input")
        fingerprint = hashlib.sha256((kind + suffix + json.dumps(normalized, sort_keys=True) + digest.hexdigest()).encode()).hexdigest()
        # No awaits between key lookup and transaction: concurrent retries cannot
        # create a duplicate in the single-process gateway.
        existing = node.store.by_key(key)
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise HTTPException(409, "Idempotency key already used for a different request")
            return {"ok": True, "data": public_job(existing)}
        if (node.startup_checks['state'] != 'verified' or node.loaded_source != source_identity()
                or node.stopping or node.isolation or node.scheduler_error or (node.coordination/'resource-isolation.json').exists()):
            raise HTTPException(503, 'Node cannot accept new operations; old results remain available')
        if node.loop is not None and node.loop.done():
            raise HTTPException(503, 'Scheduler is not running')
        if node.store.counts().get("queued", 0) >= node.cfg["server"]["max_queued_jobs"]:
            raise HTTPException(429, "Queue full")
        folder = node.folder(ident)
        folder.mkdir(mode=0o700)
        os.replace(temporary, folder / ("input" + suffix))
        for directory in (folder, node.data):
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        execution_snapshot['input_sha256'] = digest.hexdigest()
        node.store.add(ident, key, kind, fingerprint, normalized, suffix, execution_snapshot)
        node.work_available.set()
        return {"ok": True, "data": public_job(node.store.get(ident))}
    except TimeoutError:
        raise HTTPException(408, "Upload timeout")
    finally:
        temporary.unlink(missing_ok=True)


def job(ident):
    node = app.state.gateway
    node.folder(ident)
    value = node.store.get(ident)
    if not value:
        raise HTTPException(404, "Unknown job")
    return value


@app.get('/v1/operations/{key}')
async def operation(key: str):
    if not re.fullmatch(r'[a-zA-Z0-9._:-]{8,128}', key):
        raise HTTPException(400, 'Invalid operation key')
    value = app.state.gateway.store.by_key(key)
    if not value:
        raise HTTPException(404, 'Unknown operation')
    return {'ok': True, 'data': public_job(value)}


@app.get("/v1/jobs/{ident}")
async def get_job(ident: str):
    return {"ok": True, "data": public_job(job(ident))}


@app.post("/v1/jobs/{ident}/cancel")
async def cancel(ident: str):
    value = job(ident)
    if value["status"] not in TERMINAL:
        app.state.gateway.store.update(ident, canceled=1, **({"status": "canceled"} if value["status"] == "queued" else {}))
        app.state.gateway.work_available.set()
    return {"ok": True, "data": public_job(job(ident))}


@app.get("/v1/jobs/{ident}/result")
async def result(ident: str, format: str = "zip"):
    value = job(ident)
    if value.get('result_expired'):
        raise HTTPException(410, 'Result expired; operation remains accepted and cannot be resubmitted')
    if value["status"] != "completed":
        raise HTTPException(409, "Job is not completed")
    folder = app.state.gateway.result_folder(ident)
    mapping = {"zip": "result.zip", "json": "transcript.json" if value["kind"] == "asr" else "document.json", "txt": "transcript.txt", "metadata": "metadata.json"}
    if format == "md" and value["kind"] == "mineru":
        paths = list(folder.glob("*.md"))
        if len(paths) != 1:
            raise HTTPException(404, "Markdown unavailable")
        path = paths[0]
    elif format in mapping:
        path = folder / mapping[format]
    else:
        raise HTTPException(400, "Unsupported result format")
    if not path.is_file():
        raise HTTPException(404, "Artifact unavailable")
    return FileResponse(path, filename=path.name)
