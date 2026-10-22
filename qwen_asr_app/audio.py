from __future__ import annotations

from pathlib import Path
from .lazy import lazy_module

from .settings import SAMPLE_RATE

np = lazy_module("numpy")


def decode_audio_file(path: Path) -> np.ndarray:
    """Decode in a disposable process into an anonymous, disk-backed PCM mapping."""
    import tempfile
    from .native_audio import audio_operation
    with tempfile.TemporaryFile(prefix='qwenasr-pcm-') as scratch:
        samples = audio_operation('decode', source=str(path), output_fd=scratch.fileno())
        return np.memmap(scratch, dtype=np.float32, mode='r', shape=(samples,))


def vad_split_ranges(
    audio: np.ndarray,
    start_sample: int = 0,
    target_seconds: float = 90.0,
    min_seconds: float = 55.0,
    max_seconds: float = 120.0,
) -> list[tuple[int, int]]:
    """Split long audio near low-energy frames while keeping bounded chunks."""
    total = len(audio)
    start_sample = max(0, min(int(start_sample), total))
    if total - start_sample <= int(max_seconds * SAMPLE_RATE):
        return [(start_sample, total)] if total > start_sample else []

    frame_samples = int(0.10 * SAMPLE_RATE)
    usable = total - (total % frame_samples)
    if usable:
        framed = np.asarray(audio[:usable], dtype=np.float32).reshape(-1, frame_samples)
        energies = np.concatenate([np.mean(np.square(framed[i:i + 1000]), axis=1, dtype=np.float64) for i in range(0, len(framed), 1000)])
    else:
        energies = np.empty(0, dtype=np.float64)

    ranges: list[tuple[int, int]] = []
    cursor = start_sample
    min_samples = int(min_seconds * SAMPLE_RATE)
    target_samples = int(target_seconds * SAMPLE_RATE)
    max_samples = int(max_seconds * SAMPLE_RATE)
    search_radius = int(25 * SAMPLE_RATE)
    while total - cursor > max_samples:
        desired = cursor + target_samples
        low = max(cursor + min_samples, desired - search_radius)
        high = min(cursor + max_samples, desired + search_radius, total)
        low_frame = max(0, low // frame_samples)
        high_frame = min(len(energies), max(low_frame + 1, high // frame_samples))
        if high_frame > low_frame:
            boundary_frame = low_frame + int(np.argmin(energies[low_frame:high_frame]))
            boundary = min(total, (boundary_frame + 1) * frame_samples)
        else:
            boundary = min(total, desired)
        if boundary <= cursor:
            boundary = min(total, cursor + target_samples)
        ranges.append((cursor, boundary))
        cursor = boundary
    if cursor < total:
        if ranges and total - cursor < int(20 * SAMPLE_RATE):
            previous_start, _ = ranges.pop()
            ranges.append((previous_start, total))
        else:
            ranges.append((cursor, total))
    return ranges


def convert_audio_to_aac(source_path: Path, destination_path: Path, *, bit_rate: int = 64_000) -> None:
    from .native_audio import audio_operation
    audio_operation('aac', source=str(source_path), destination=str(destination_path), bit_rate=bit_rate)


def split_audio_to_aac(source_path: Path, destination_paths: list[Path], *, bit_rate: int = 32_000) -> None:
    from .native_audio import audio_operation
    audio_operation('split', source=str(source_path), destinations=[str(p) for p in destination_paths], bit_rate=bit_rate)


def split_audio_to_aac_lossless(source_path: Path, destination_paths: list[Path]) -> None:
    from .native_audio import audio_operation
    audio_operation('split_lossless', source=str(source_path), destinations=[str(p) for p in destination_paths])


def make_playback_audio(source_path, output_path):
    from .native_audio import audio_operation
    audio_operation('playback', source=str(source_path), destination=str(output_path))
