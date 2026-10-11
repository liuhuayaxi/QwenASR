"""Stateless MinerU 4 SDK parse with official save plus legacy mapping adapter."""
import importlib.metadata
import json
import os
import time
from pathlib import Path

from .common import event, finish, run


def main(cfg):
    started = time.monotonic()
    settings = cfg["config"]["mineru"]
    options = cfg["options"]
    job = Path(cfg["job_dir"])
    for key in tuple(os.environ):
        if key.startswith("MINERU_"):
            os.environ.pop(key)
    os.environ["MINERU_HOME"] = settings["home"]
    os.environ["MINERU_PROCESSING_WINDOW_SIZE"] = str(settings["processing_window_size"])
    os.environ["MINERU_VIRTUAL_VRAM_SIZE"] = "8"
    os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"
    # vLLM's FlashInfer sampler needs the CUDA compiler bundled by [full].
    # Use the isolated toolkit; no NVIDIA Linux display driver is installed.
    toolkit = Path(settings["home"]).parent / "cuda-toolkit"
    if (toolkit / "bin/nvcc").is_file():
        os.environ["CUDA_HOME"] = str(toolkit)
        os.environ["PATH"] = str(toolkit / "bin") + os.pathsep + os.environ["PATH"]
        os.environ["LD_LIBRARY_PATH"] = str(toolkit / "lib64") + os.pathsep + "/usr/lib/wsl/lib" + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")
    runtime_config = job / "mineru-config.json"
    runtime_config.write_text(json.dumps({"model": {"base_dir": str(Path(settings["home"]) / "models"),
        "source": "local", "small_backend": settings["small_backend"], "vlm": {
            "engine": settings["vlm_engine"], "server_url": "", "max_concurrency": settings["vlm_concurrency"]}}}), encoding="utf-8")
    os.environ["MINERU_CONFIG"] = str(runtime_config)
    version = importlib.metadata.version("mineru")
    if version != settings["version"]:
        raise RuntimeError("Unexpected MinerU version")
    event(cfg, "loading", None, "MinerU · 加载解析环境")
    model_load_times = []
    # Supply supported vLLM arguments at the official engine constructor. The
    # 4.0.10 VlmConfig does not expose these memory limits. No model/code changes.
    tier = options["tier"]
    if tier == "advanced" and settings["vlm_engine"] == "vllm":
        from mineru.model.vlm.runtime import ModelSingleton
        original = ModelSingleton._create_model
        def create_model(self, backend, model_path, server_url, **kwargs):
            if backend.startswith("vllm"):
                kwargs.update(gpu_memory_utilization=settings["gpu_memory_utilization"],
                    max_model_len=settings["max_model_len"],
                    max_num_seqs=settings.get("max_num_seqs", settings["vlm_concurrency"]),
                    enforce_eager=settings["enforce_eager"], dtype="bfloat16")
            load_started = time.monotonic()
            result = original(self, backend, model_path, server_url, **kwargs)
            model_load_times.append(time.monotonic() - load_started)
            event(cfg, "running", None, "MinerU · 视觉模型已加载")
            return result
        ModelSingleton._create_model = create_model
    infer_started = time.monotonic()
    from .mineru_observer import install_stage_observer
    install_stage_observer()
    from mineru.parser import parse, ParseResult
    from mineru.parser.writer import FileBasedDataWriter
    from mineru.render.content_list import render_content_list
    print('QWENASR_STAGE parse', flush=True)
    result = parse(cfg["input"], tier=tier, ocr_mode=options.get("ocr_mode", "auto"),
        image_analysis=True, page_range="all" if Path(cfg["input"]).suffix.lower() == ".pdf" else "")
    parse_seconds = time.monotonic() - infer_started
    event(cfg, "saving", None, "MinerU · 识别完成，正在保存结构化结果")
    output = job / "pending-result"
    output.mkdir()
    print("QWENASR_STAGE save", flush=True)
    result.save(FileBasedDataWriter(str(output)))
    print("QWENASR_STAGE content_list", flush=True)
    # Adapt the materialized document, whose relative image paths refer to the
    # exact assets in this result folder, rather than its pre-export in-memory IR.
    materialized = (output / "middle_json.json").read_text(encoding="utf-8")
    exported = ParseResult.from_json(materialized)
    (output / "document_content_list.json").write_text(json.dumps(render_content_list(exported.middle_json),ensure_ascii=False), encoding="utf-8")
    (output / "document.json").write_text(materialized, encoding="utf-8")
    from client.document_quality import annotate_export
    annotate_export(output)
    metrics = {"provider": "remote", "fallback": False, "mineru_version": version, "tier": tier,
        "small_backend": settings["small_backend"] if tier == "advanced" else "native", "vlm_engine": settings["vlm_engine"] if tier == "advanced" else "none",
        "pages": len(result.pages), "page_range": "all", "image_analysis": True,
        "vlm_load_seconds": sum(model_load_times), "parse_seconds_including_load": parse_seconds,
        "total_seconds": time.monotonic() - started,
        "precision": "BF16 VLM, official Torch small-model defaults" if tier == "advanced" else "native document parser",
        'quality_validation': {'scientific_symbols': 'unverified', 'table_cells': 'unverified',
            'chart_numeric_estimates': 'unverified', 'original_images_authoritative': True},
        "vllm_memory_fraction": settings["gpu_memory_utilization"] if tier == "advanced" else None,
        "performance_config": {
            "processing_window_size": settings["processing_window_size"],
            "vlm_concurrency": settings["vlm_concurrency"],
            "max_num_seqs": settings.get("max_num_seqs", settings["vlm_concurrency"]),
            "enforce_eager": settings["enforce_eager"],
            "configured_cuda_graphs": not settings["enforce_eager"],
        } if tier == "advanced" else None}
    finish(cfg, metrics)


if __name__ == "__main__":
    run(main)
