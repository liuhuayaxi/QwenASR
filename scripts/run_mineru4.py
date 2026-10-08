#!/usr/bin/env python3
"""Run one local parse with MinerU 4 and export the PDF packager's V1 mapping."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args()
    version = importlib.metadata.version("mineru")
    if not version.startswith("4."):
        raise RuntimeError(f"需要 MinerU 4.x，当前为 {version}")
    settings = json.loads(args.settings.read_text(encoding="utf-8"))
    # Each child has an isolated configuration and environment. Never inherit a
    # remote VLM address or a global config that could upload document content.
    for key in tuple(os.environ):
        if key.startswith("MINERU_"):
            os.environ.pop(key)
    home = Path.home() / ".qwenasr" / "mineru4"
    os.environ["MINERU_HOME"] = str(home)
    config_path = args.output.parent / "mineru-config.json"
    config_path.write_text(json.dumps({"model": {
        "base_dir": str(home / "models"), "source": settings["model_source"],
        "small_backend": settings["small_backend"], "vlm": {
            "engine": settings["vlm_engine"], "server_url": "",
            "max_concurrency": settings["vlm_concurrency"],
        }}}), encoding="utf-8")
    os.environ["MINERU_CONFIG"] = str(config_path)
    for key, field in (("MINERU_PROCESSING_WINDOW_SIZE", "processing_window_size"),
                       ("MINERU_VIRTUAL_VRAM_SIZE", "virtual_vram_gb")):
        if settings[field]:
            os.environ[key] = str(settings[field])
    from mineru.parser import parse
    from mineru.parser.writer import FileBasedDataWriter
    from mineru.render.content_list import render_content_list

    print(f"QwenASR MinerU {version} · tier={settings['tier']} · all pages", flush=True)
    result = parse(args.source, tier=settings["tier"], ocr_mode=settings["ocr_mode"],
                   image_analysis=settings["image_analysis"], page_range="all")
    args.output.mkdir(parents=True, exist_ok=True)
    result.save(FileBasedDataWriter(str(args.output)))
    content = render_content_list(result.middle_json)
    (args.output / "document_content_list.json").write_text(
        json.dumps(content, ensure_ascii=False), encoding="utf-8")
    print(f"QwenASR MinerU completed · {len(result.pages)} pages", flush=True)


if __name__ == "__main__":
    main()
