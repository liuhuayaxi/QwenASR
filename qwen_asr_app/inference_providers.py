"""Optional Windows providers. Default local callbacks retain original behavior."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
import ipaddress

from .durable import atomic_json
from .paths import PRIVATE_ROOT, ROOT

MODES = ("local", "remote_preferred", "remote_only")
DEFAULTS = {"mineru_provider": "local", "asr_provider": "local", "base_url": "http://100.81.62.69:8765",
    "token_file": str(PRIVATE_ROOT / "remote-infer" / "token"), "connect_timeout_seconds": 10,
    "request_timeout_seconds": 1800, "job_timeout_seconds": 14400, "poll_seconds": 2,
    "network_recovery_seconds": 60}


def normalize(raw, *, validate_remote=False):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("推理配置字段无效")
    result = {**DEFAULTS, **raw}
    for key in ("mineru_provider", "asr_provider"):
        if result[key] not in MODES:
            raise ValueError("推理模式无效")
    if result["token_file"] != DEFAULTS["token_file"]:
        raise ValueError("令牌文件位置固定在本机受保护目录")
    if not validate_remote and all(result[key] == 'local' for key in ('mineru_provider', 'asr_provider')):
        return result
    url = urlsplit(str(result["base_url"]))
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment or '\\' in str(result['base_url']) or any(c.isspace() for c in str(result['base_url'])):
        raise ValueError("远程地址无效")
    _ = url.port
    if url.scheme == "http" and url.hostname != "localhost":
        try:
            address = ipaddress.ip_address(url.hostname)
            networks = ('127.0.0.0/8','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','100.64.0.0/10','::1/128','fc00::/7')
            if not any(address in ipaddress.ip_network(value) for value in networks):
                raise ValueError()
        except ValueError:
            raise ValueError("HTTP 地址请使用局域网或 Tailscale 内网 IP；其他地址需使用 HTTPS") from None
    for key, limits in {"connect_timeout_seconds": (1, 60), "request_timeout_seconds": (30, 3600),
                        "job_timeout_seconds": (60, 86400), "poll_seconds": (.1, 30),
                        "network_recovery_seconds": (5, 600)}.items():
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not limits[0] <= value <= limits[1]:
            raise ValueError("超时或轮询间隔无效")
    return result


class InferenceSettings:
    def __init__(self, path=None):
        classroom=os.getenv('QWEN_ASR_CLASSROOM_EDITION')=='1'
        self.path = path or PRIVATE_ROOT / ("classroom-inference-providers.json" if classroom else "inference-providers.json")
        if path is None and classroom and not self.path.exists():
            legacy=PRIVATE_ROOT/'inference-providers.json'
            raw=json.loads(legacy.read_text()) if legacy.is_file() else dict(DEFAULTS)
            raw.update(asr_provider='local',mineru_provider='local')
            self.save(raw)

    def read(self):
        try:
            return normalize(json.loads(self.path.read_text()))
        except FileNotFoundError:
            return dict(DEFAULTS)

    def for_task(self, kind, explicit=None):
        if explicit is not None and explicit.get(kind + '_provider', 'local') == 'local':
            return {'mineru_provider': 'local', 'asr_provider': 'local'}
        if explicit is None:
            try:
                raw = json.loads(self.path.read_text())
            except FileNotFoundError:
                raw = {}
        else:
            raw = explicit
        mode = raw.get(kind + '_provider', 'local')
        if mode == 'local':
            return {'mineru_provider': 'local', 'asr_provider': 'local'}
        return normalize(raw, validate_remote=True)

    def save(self, raw):
        result = normalize(raw)
        atomic_json(self.path, result)
        self.path.chmod(0o600)
        return result


inference_settings = InferenceSettings()
_transport = None


def remote_client(cfg):
    global _transport
    if _transport is None:
        spec = importlib.util.spec_from_file_location("_qwen_infer_transport", ROOT / "ai-infer-node/client/remote.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _transport = module
    return _transport.RemoteClient(cfg)


class LocalMinerUProvider:
    def parse(self, runner):
        value = runner()
        return {"provider": "local", "fallback": False, **(value if isinstance(value, dict) else {})}


class RemoteMinerUProvider:
    def parse(self, source, output, cfg, progress=None, stop=None, receipt_path=None):
        return remote_client(cfg).run("mineru", source, output, progress=progress, stop=stop, receipt_path=receipt_path)


def parse_document(source, output, local_runner, progress=None, stop=None, config=None, receipt_path=None):
    from .conversion_control import check_stopped, ConversionStopped
    cfg = inference_settings.for_task('mineru', config)
    mode = cfg['mineru_provider']
    if mode == "local":
        return LocalMinerUProvider().parse(local_runner)
    try:
        return RemoteMinerUProvider().parse(source, output, cfg, progress, stop, receipt_path)
    except ConversionStopped:
        raise
    except Exception as exc:
        check_stopped(stop)
        if mode == 'remote_only' or not getattr(exc, 'proven_not_accepted', False):
            raise
        if progress:
            progress("远程不可用，正在回退本机 MinerU", None)
        if receipt_path:
            from .remote_operations import mark_local_fallback
            mark_local_fallback(receipt_path)
        value = LocalMinerUProvider().parse(local_runner)
        value.update(fallback=True, fallback_reason=str(exc), configured_mode=mode)
        return value


class LocalASRProvider:
    async def transcribe(self, runner):
        return await runner()


def prepare_audio(folder, audio, language, context, max_tokens, cfg, asr_params=None):
    """Persist normalized PCM and the full request before scheduling its operation."""
    import os
    import soundfile as sf
    from .settings import DEFAULT_ASR_PARAMS
    from .remote_operations import without_secrets
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    request_path = folder/'request.json'
    if request_path.exists():
        return json.loads(request_path.read_text())
    if (folder/'receipt.json').exists():
        raise ValueError('旧远程语音操作缺少参数快照，请修复记录')
    params = dict(asr_params or {})
    unsupported = [name for name, value in params.items()
                   if name not in {'max_tokens', 'hotwords'} and value != DEFAULT_ASR_PARAMS.get(name)]
    if unsupported:
        raise ValueError('Windows 不支持这些自定义 ASR 参数：' + ', '.join(sorted(unsupported)))
    requested = int(params.get('max_tokens', max_tokens))
    if not 128 <= requested <= 8192:
        raise ValueError('Windows max_tokens 必须在 128–8192 之间')
    source = folder/'audio.wav'
    temporary = folder/'audio.wav.part'
    sf.write(temporary, audio, 16000, subtype='PCM_16', format='WAV')
    temporary.chmod(0o600)
    with temporary.open('rb') as inp:
        os.fsync(inp.fileno())
        import hashlib
        sha = hashlib.file_digest(inp, 'sha256').hexdigest()
    os.replace(temporary, source)
    request = {'language': language or '', 'prompt': context or '', 'max_new_tokens': requested,
        'duration': len(audio)/16000, 'source_sha256': sha, 'config': without_secrets(cfg), 'parameters': params}
    atomic_json(request_path, request)
    return request


class RemoteASRProvider:
    async def transcribe(self, audio, language, context, max_tokens, cfg, operation_dir=None, asr_params=None):
        started = time.monotonic()
        value, metadata = await self.transcribe_segments(audio, language, context, max_tokens, cfg, operation_dir=operation_dir, asr_params=asr_params)
        return (value["text"], value.get("language", ""), round(time.monotonic()-started, 2)), metadata

    async def transcribe_segments(self, audio, language, context, max_tokens, cfg, operation_dir=None, operation_key=None, asr_params=None):
        stop = threading.Event()
        started = time.monotonic()
        def operation():
            import soundfile as sf
            if operation_dir is None:
                raise ValueError('远程语音识别需要持久操作目录')
            folder = Path(operation_dir)
            from .remote_operations import accepted_result_guard
            with accepted_result_guard(folder/'receipt.json'):
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = folder / 'audio.wav'
                request = prepare_audio(folder, audio, language, context, max_tokens, cfg, asr_params)
                metadata = remote_client(request['config']).run('asr', source, folder/'result',
                    {k: request[k] for k in ('language', 'prompt', 'max_new_tokens')}, stop=stop,
                    key=operation_key, receipt_path=folder/'receipt.json')
                value = json.loads((folder / "result/transcript.json").read_text())
                duration = request['duration']
                segments = value.get("segments")
                if not isinstance(segments, list) or not segments:
                    raise RuntimeError("远程音频分段缺失")
                # Older nodes may delete an identical whole segment. Rebuild
                # contributions from preserved raw segments, with provenance.
                spec = importlib.util.spec_from_file_location('_qwen_merge_policy', ROOT/'ai-infer-node/workers/text_merge.py')
                policy = importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
                rebuilt = ''
                prior_end = 0.0
                for segment in segments:
                    merged = policy.merge_text(rebuilt, segment['text'],
                                              allow_overlap=float(segment['start']) < prior_end)
                    segment['merged_text'] = merged[len(rebuilt):].lstrip('\n')
                    rebuilt = merged
                    prior_end = float(segment['end'])
                if rebuilt != value.get('text'):
                    metadata['transcript_postprocessing'] = {'provider': 'mac_adapter', 'policy': 'preserve_whole_segment_repetitions',
                        'raw_remote_result_preserved': True}
                    value['text'] = rebuilt
                previous_end = 0.0
                for segment in segments:
                    start, end = float(segment["start"]), float(segment["end"])
                    if not 0 <= start < end <= duration + .001 or start > previous_end + .01 or not isinstance(segment["text"],str):
                        raise RuntimeError("远程音频分段不完整")
                    previous_end = end
                if abs(previous_end-duration) > .01:
                    raise RuntimeError("远程音频未处理到结束")
                return value, metadata
        pending = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            stop.set()
            await asyncio.gather(pending, return_exceptions=True)
            raise


def runtime_fingerprint(config):
    """Remote cache reuse requires the actual receipt, never a health preflight."""
    if config.get('mineru_provider', 'local') != 'local':
        return None
    import hashlib
    from .pdf_ai import find_mineru_binary, runtime_info
    binary = find_mineru_binary()
    if binary is None:
        return None
    info = runtime_info(binary)
    builder = ROOT / 'scripts/build_ai_friendly_pdf.py'
    return hashlib.sha256(json.dumps({'runtime': info,
        'binary': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'builder': hashlib.sha256(builder.read_bytes()).hexdigest()}, sort_keys=True).encode()).hexdigest()
