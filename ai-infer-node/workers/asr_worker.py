"""Native Qwen3-ASR 1.7B BF16, one model load per complete recording."""
import json
import re
import subprocess
import time
from pathlib import Path

from .common import event, finish, run
from .text_merge import merge_text


def boundaries(duration, silence, target=25, overlap=1, hard_silence=()):
    start = 0.0
    while start < duration - .001:
        end = min(duration, start + target)
        hard = [x for x in hard_silence if start + max(2, overlap + 1) <= x < min(end, duration - .5)]
        candidates = [x for x in silence if start + target * .6 <= x <= end]
        if hard:
            end = hard[0]
        elif end < duration and candidates:
            end = candidates[-1]
        yield round(start, 4), round(end, 4)
        if end >= duration:
            break
        # A long silence is an acoustic boundary: never carry speech across it.
        start = end if hard else end - overlap


def main(cfg):
    total_started = time.monotonic()
    options = cfg["options"]
    settings = cfg["config"]["asr"]
    event(cfg, "loading", 1)
    job = Path(cfg["job_dir"])
    wav = job / "normalized.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", cfg["input"], "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], check=True, stdout=subprocess.DEVNULL)
    detection = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(wav), "-af", "silencedetect=noise=-35dB:d=0.35", "-f", "null", "-"], capture_output=True, text=True, check=True)
    pauses = [(float(end),float(length)) for end,length in re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", detection.stderr)]
    silence = [end for end,length in pauses]
    hard_silence = [end-length/2 for end,length in pauses if length >= .8]
    import soundfile as sf
    import torch
    import transformers
    from transformers import AutoProcessor, AutoModelForMultimodalLM
    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError("CUDA BF16 support is required")
    if "4060" not in torch.cuda.get_device_name(0):
        raise RuntimeError("Expected RTX 4060")
    torch.cuda.reset_peak_memory_stats()
    started = time.monotonic()
    processor = AutoProcessor.from_pretrained(settings["model_path"], local_files_only=True)
    model = AutoModelForMultimodalLM.from_pretrained(settings["model_path"], dtype=torch.bfloat16,
            device_map={"": "cuda:0"}, local_files_only=True, attn_implementation="sdpa").eval()
    if {p.device.type for p in model.parameters()} != {"cuda"}:
        raise RuntimeError("CPU model offload is forbidden")
    if {p.dtype for p in model.parameters() if p.is_floating_point()} != {torch.bfloat16}:
        raise RuntimeError("Model weights must all be BF16")
    load_seconds = time.monotonic() - started
    event(cfg, "running", 5)
    segments, text = [], ""
    infer_started = time.monotonic()
    with sf.SoundFile(wav) as audio:
        duration = len(audio) / audio.samplerate
        for start, end in boundaries(duration, silence, settings["chunk_seconds"], settings["overlap_seconds"], hard_silence):
            audio.seek(round(start * 16000))
            clip = audio.read(round((end - start) * 16000), dtype="float32")
            # Local audio paths keep native processor sample-rate handling exact.
            clip_path = job / "segment.wav"
            sf.write(clip_path, clip, 16000, subtype="PCM_16")
            inputs = processor.apply_transcription_request(audio=str(clip_path),
                language=options.get("language") or None, prompt=options.get("prompt") or None).to(model.device, model.dtype)
            token_limit = int(options.get("max_new_tokens", settings["max_new_tokens"]))
            with torch.inference_mode():
                output = model.generate(**inputs, max_new_tokens=token_limit, do_sample=False)
            ids = output[:, inputs["input_ids"].shape[1]:]
            eos_ids = model.generation_config.eos_token_id
            eos_ids = eos_ids if isinstance(eos_ids, (list, tuple)) else [eos_ids]
            actual_tokens = int(ids.shape[1])
            ended_with_eos = bool(actual_tokens and int(ids[0, -1]) in eos_ids)
            token_report = {'requested_limit': token_limit, 'effective_limit': token_limit,
                'actual_generated_tokens': actual_tokens, 'eos': ended_with_eos,
                'truncated': actual_tokens >= token_limit and not ended_with_eos}
            if token_report['truncated']:
                (job/'token-limit.json').write_text(json.dumps({'code': 'ASR_TOKEN_LIMIT',
                    'start': start, 'end': end, **token_report}))
                raise RuntimeError('ASR_TOKEN_LIMIT')
            parsed = processor.decode(ids, return_format="parsed")[0]
            segment_text = parsed["transcription"].strip()
            merged = merge_text(text, segment_text, allow_overlap=bool(segments and start < segments[-1]['end']))
            contribution = merged[len(text):].lstrip("\n")
            segments.append({"start": start, "end": end, "text": segment_text,
                             "merged_text": contribution, "tokens": token_report, "language": parsed.get("language", "")})
            text = merged
            event(cfg, "running", 5 + 90 * end / max(duration, .001))
    torch.cuda.synchronize()
    infer_seconds = time.monotonic() - infer_started
    metrics = {"provider": "remote", "fallback": False, "model": settings["model_id"],
        "transformers_version": transformers.__version__, "torch_version": torch.__version__,
        "dtype": "bfloat16", "device": torch.cuda.get_device_name(0), "all_weights_cuda": True,
        "load_seconds": load_seconds, "inference_seconds": infer_seconds, "audio_seconds": duration,
        "rtf": infer_seconds/max(duration,.001), "peak_allocated_mib": torch.cuda.max_memory_allocated()/2**20,
        "peak_reserved_mib": torch.cuda.max_memory_reserved()/2**20, "total_seconds": time.monotonic()-total_started,
        'merge_policy': 'preserve_all_raw_segments_without_word_alignment',
        "timestamp_kind": "segment", "forced_aligner": False}
    output = job / "pending-result"
    output.mkdir()
    (output / "transcript.txt").write_text(text, encoding="utf-8")
    (output / "transcript.json").write_text(json.dumps({"text": text, "language": segments[0]["language"] if segments else "",
        "segments": segments, "metadata": metrics}, ensure_ascii=False), encoding="utf-8")
    finish(cfg, metrics)


if __name__ == "__main__":
    run(main)
