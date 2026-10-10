"""Only standard library configuration; no inference imports."""
import os
import tomllib
import ipaddress
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def configuration():
    path = Path(os.environ.get("AI_INFER_CONFIG", ROOT / "config.toml"))
    with path.open("rb") as source:
        cfg = tomllib.load(source)
    for section, fields in {
        "server": ("data_dir", "token_file"),
        "mineru": ("python", "home"),
        "asr": ("python", "model_path"),
    }.items():
        for field in fields:
            path = Path(cfg[section][field]).expanduser()
            # Resolving a venv's python symlink selects the system interpreter
            # and silently loses its isolated site-packages.
            cfg[section][field] = str(path.absolute() if field == "python" else path.resolve())
    host = ipaddress.ip_address(cfg['server']['host'])
    if not (host.is_unspecified or host.is_loopback or host in ipaddress.ip_network('100.64.0.0/10') or host.is_private):
        raise ValueError('Gateway must listen on a LAN/Tailnet interface')
    for value in cfg['server'].get('allowed_client_networks', ['127.0.0.0/8','100.64.0.0/10']):
        ipaddress.ip_network(value)
    if cfg["server"]["max_concurrent_gpu_jobs"] != 1:
        raise ValueError("This 8 GB node requires a single GPU queue")
    if cfg["server"]["model_idle_timeout_seconds"] != 0:
        raise ValueError("This disposable-worker implementation supports immediate unloading only")
    if cfg["mineru"]["tier"] != "advanced":
        raise ValueError("PDF/image quality must remain advanced")
    if cfg["asr"]["dtype"] != "bfloat16":
        raise ValueError("ASR must use unquantized BF16")
    validate_contract(cfg)
    return cfg


def validate_contract(cfg):
    if cfg['mineru'].get('vlm_engine') != 'vllm' or cfg['mineru'].get('tier') != 'advanced':
        raise ValueError('PDF/image contract requires official Advanced and vLLM')
    if cfg['asr'].get('model_id') not in {'Qwen/Qwen3-ASR-1.7B', 'Qwen/Qwen3-ASR-1.7B-hf'}:
        raise ValueError('Only official Qwen3-ASR 1.7B is allowed')
    if cfg['asr'].get('dtype') != 'bfloat16':
        raise ValueError('ASR contract requires BF16')
    for section in ('asr', 'mineru'):
        for key in ('quantization', 'load_in_4bit', 'load_in_8bit', 'cpu_offload_gb', 'cpu_offload'):
            if cfg[section].get(key):
                raise ValueError('Quantization and CPU offload are forbidden')


def verify_model_manifests(cfg):
    """Verify every model byte against a pinned, operator-provisioned manifest."""
    import hashlib
    import json
    result = {}
    for section in ('asr', 'mineru'):
        values = cfg[section]
        manifest = Path(values.get('manifest_file', '/nonexistent')).expanduser()
        pinned = values.get('manifest_sha256')
        raw = manifest.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if not pinned or actual != pinned:
            raise ValueError('MODEL_MANIFEST_PIN_MISMATCH_' + section.upper())
        data = json.loads(raw)
        if section == 'asr' and data.get('model') != values['model_id']:
            raise ValueError('ASR_MANIFEST_MODEL_MISMATCH')
        if data.get('source') not in {'huggingface', 'modelscope', 'official-mineru'}:
            raise ValueError('MODEL_MANIFEST_SOURCE_NOT_OFFICIAL')
        root = Path(values['model_path'] if section == 'asr' else values['home'] + '/models').resolve()
        files = data.get('files')
        if not isinstance(files, dict) or not files:
            raise ValueError('EMPTY_MODEL_MANIFEST')
        observed_weights = set()
        for name, expected in files.items():
            path = (root/name).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('MODEL_MANIFEST_PATH_INVALID')
            with path.open('rb') as inp:
                if hashlib.file_digest(inp, 'sha256').hexdigest() != expected['sha256']:
                    raise ValueError('MODEL_CONTENT_MISMATCH')
            if path.suffix in {'.safetensors', '.bin', '.pt', '.pth', '.onnx', '.gguf', '.pdiparams'}:
                observed_weights.add(path)
        actual_weights = {p.resolve() for p in root.rglob('*') if p.is_file() and
                          p.suffix in {'.safetensors', '.bin', '.pt', '.pth', '.onnx', '.gguf', '.pdiparams'}}
        if not actual_weights or observed_weights != actual_weights:
            raise ValueError('MODEL_MANIFEST_INCOMPLETE')
        result[section] = actual
    return result
