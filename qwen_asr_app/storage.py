from __future__ import annotations

import json
import copy
import contextlib
import fcntl
import os
import re
import shutil
import struct
import tempfile
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .lazy import lazy_module

from .paths import data_path, PRIVATE_ROOT
from .durable import atomic_json
from .settings_recovery import read_settings, save_settings
from .library_layout import AI_NOTES, RECORDINGS, shared, recording_path, manifest_time
from .audio import convert_audio_to_aac
from .note_content import transcript_markdown, transcript_blocks
from .course_replay import replay_sources
from .course_dates import apply_course_dates
from .settings import (
    DEFAULT_AI_SYSTEM_PROMPT,
    DEFAULT_ASR_PARAMS,
    DEFAULT_LIVE_PARAMS,
    SAMPLE_RATE,
    normalize_asr_params,
    normalize_live_params,
)
from .terminology import normalize_terminology

np = lazy_module("numpy")

ROOT = Path(__file__).resolve().parent.parent
LEGACY_RECORDINGS_DIR = Path(os.getenv("QWEN_ASR_LEGACY_RECORDINGS_DIR", data_path("recordings", ROOT / "recordings")))
LEGACY_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = Path(os.getenv("QWEN_ASR_SETTINGS_PATH", data_path("app-settings.json", ROOT / "app-settings.json")))
DEFAULT_STORAGE_ROOT = Path(os.getenv("QWEN_ASR_STORAGE_ROOT", data_path("library", Path.home() / "Documents" / "QwenASR资料库")))
TRASH_ROOT = data_path("trash", Path.home() / ".Trash" / "QwenASR-课堂录音")


def local_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def format_clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def safe_component(value: str, fallback: str) -> str:
    """Return a readable, filesystem-safe folder component without allowing traversal."""
    cleaned = re.sub(r"[\x00-\x1f/:]", "-", str(value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = fallback
    return cleaned[:80].rstrip(" .") or fallback


def default_course_profile(category: str = "") -> dict[str, Any]:
    return {
        "ai_system_prompt": DEFAULT_AI_SYSTEM_PROMPT,
        "asr_prompt": "",
        "terminology": normalize_terminology(None, category=category, seed_if_missing=True),
        "asr_params": json.loads(json.dumps(DEFAULT_ASR_PARAMS)),
        "live_params": dict(DEFAULT_LIVE_PARAMS),
    }


class AppSettings:
    def __init__(self) -> None:
        self.storage_root = DEFAULT_STORAGE_ROOT
        self.categories: list[str] = ["未分类"]
        self.known_roots: list[str] = []
        self.category_profiles: dict[str, dict[str, Any]] = {}
        self.library_layout = "legacy"
        self.terminology_seed_version = 0
        self.load()

    def load(self) -> None:
        try:
            payload = read_settings(SETTINGS_PATH)
        except FileNotFoundError:
            payload = {}
        self.library_layout = str(payload.get("library_layout") or "legacy")
        try:
            self.terminology_seed_version = int(payload.get("terminology_seed_version") or 0)
        except (TypeError, ValueError):
            self.terminology_seed_version = 0
        seed_empty_profiles = self.terminology_seed_version < 1
        raw_root = str(payload.get("storage_root") or DEFAULT_STORAGE_ROOT)
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            root = DEFAULT_STORAGE_ROOT
        self.storage_root = root.resolve()
        raw_categories = payload.get("categories")
        if isinstance(raw_categories, list):
            self.categories = []
            for item in raw_categories:
                category = safe_component(str(item), "未分类")
                if category not in self.categories:
                    self.categories.append(category)
        if "未分类" not in self.categories:
            self.categories.insert(0, "未分类")
        raw_profiles = payload.get("category_profiles")
        if isinstance(raw_profiles, dict):
            for raw_name, raw_profile in raw_profiles.items():
                if not isinstance(raw_profile, dict):
                    continue
                name = safe_component(str(raw_name), "未分类")
                profile = default_course_profile(name)
                profile["ai_system_prompt"] = str(
                    raw_profile.get("ai_system_prompt") or DEFAULT_AI_SYSTEM_PROMPT
                )[:20_000]
                profile["asr_prompt"] = str(raw_profile.get("asr_prompt") or "")[:20_000]
                profile["terminology"] = normalize_terminology(
                    None if seed_empty_profiles and not raw_profile.get("terminology") else raw_profile.get("terminology"),
                    category=name,
                    seed_if_missing="terminology" not in raw_profile or (seed_empty_profiles and not raw_profile.get("terminology")),
                )
                try:
                    profile["asr_params"] = normalize_asr_params(
                        raw_profile.get("asr_params"), DEFAULT_ASR_PARAMS["max_tokens"]
                    )
                except (ValueError, json.JSONDecodeError):
                    profile["asr_params"] = default_course_profile(name)["asr_params"]
                try:
                    profile["live_params"] = normalize_live_params(raw_profile.get("live_params"))
                except ValueError:
                    profile["live_params"] = dict(DEFAULT_LIVE_PARAMS)
                self.category_profiles[name] = profile
        for category in self.categories:
            self.category_profiles.setdefault(category, default_course_profile(category))
            profile_path = self.storage_root / category / ".qwenasr-course.json"
            if profile_path.exists():
                try:
                    folder_profile = json.loads(profile_path.read_text(encoding="utf-8"))
                    if isinstance(folder_profile, dict):
                        self.category_profiles[category] = {
                            "ai_system_prompt": str(
                                folder_profile.get("ai_system_prompt") or DEFAULT_AI_SYSTEM_PROMPT
                            )[:20_000],
                            "asr_prompt": str(folder_profile.get("asr_prompt") or "")[:20_000],
                            "terminology": normalize_terminology(
                                folder_profile.get("terminology"),
                                category=category,
                                seed_if_missing="terminology" not in folder_profile,
                            ),
                            "asr_params": normalize_asr_params(
                                folder_profile.get("asr_params"), DEFAULT_ASR_PARAMS["max_tokens"]
                            ),
                            "live_params": normalize_live_params(folder_profile.get("live_params")),
                        }
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    print(f"忽略无法读取的课程配置 {profile_path}: {exc}", flush=True)
        raw_roots = payload.get("known_roots")
        self.known_roots = [str(Path(item).expanduser().resolve()) for item in raw_roots or [] if item]
        self.add_known_root(self.storage_root)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.terminology_seed_version = 1
        self.save()

    def add_known_root(self, root: Path) -> None:
        value = str(root.resolve())
        if value not in self.known_roots:
            self.known_roots.append(value)

    def add_category(self, name: str) -> str:
        category = safe_component(name, "未分类")
        changed = False
        if category not in self.categories:
            self.categories.append(category)
            self.categories.sort(key=lambda item: (item != "未分类", item.casefold()))
            changed = True
        if category not in self.category_profiles:
            self.category_profiles[category] = default_course_profile(category)
            changed = True
        if changed:
            self.save()
        if shared(self):
            directory = self.storage_root / category
            (directory / RECORDINGS).mkdir(parents=True, exist_ok=True)
            marker = directory / ".qwenasr-course.json"
            if not marker.exists():
                atomic_json(marker, {"category": category, **self.category_profiles[category]})
        return category

    def save_category_profile(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        category = self.add_category(name)
        profile = {
            "ai_system_prompt": str(
                payload.get("ai_system_prompt") or DEFAULT_AI_SYSTEM_PROMPT
            ).strip()[:20_000],
            "asr_prompt": str(payload.get("asr_prompt") or "").strip()[:20_000],
            "terminology": normalize_terminology(payload.get("terminology")),
            "asr_params": normalize_asr_params(
                payload.get("asr_params"), DEFAULT_ASR_PARAMS["max_tokens"]
            ),
            "live_params": normalize_live_params(payload.get("live_params")),
        }
        self.category_profiles[category] = profile
        category_directory = self.storage_root / category
        category_directory.mkdir(parents=True, exist_ok=True)
        profile_path = category_directory / ".qwenasr-course.json"
        atomic_json(profile_path, {"category": category, **profile})
        self.save()
        return profile

    def set_storage_root(self, value: str) -> Path:
        root = Path(str(value or "").strip()).expanduser()
        if not root.is_absolute():
            raise ValueError("保存位置必须填写绝对路径")
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not os.access(root, os.W_OK):
            raise ValueError("保存位置不可写")
        self.storage_root = root
        self.add_known_root(root)
        self.save()
        return root

    def migrate_storage_root(self, value: str) -> dict[str, Any]:
        """Move the current library into a new root without overwriting files."""
        root = Path(str(value or "").strip()).expanduser()
        if not root.is_absolute():
            raise ValueError("保存位置必须填写绝对路径")
        root = root.resolve()
        source = self.storage_root.resolve()
        if root == source:
            return {
                "source_root": str(source),
                "storage_root": str(root),
                "migrated_records": 0,
                "migrated_files": 0,
                "skipped": [],
            }
        if shared(self):
            if root.is_relative_to(source) or source.is_relative_to(root):
                raise ValueError('同步文件夹不能迁移到自身的上级或子目录')
            planned = [(source / name, root / name) for name in self.categories if (source / name).is_dir()]
            if any(target.exists() for _, target in planned):
                raise ValueError('目标文件夹已有同名课程，请选择空文件夹')
            root.mkdir(parents=True, exist_ok=True)
            moved = []
            previous_roots = list(self.known_roots)
            try:
                for old, target in planned:
                    shutil.move(str(old), str(target))
                    moved.append((old, target))
                self.storage_root = root
                self.add_known_root(root)
                self.save()
            except BaseException:
                for old, target in reversed(moved):
                    shutil.move(str(target), str(old))
                self.storage_root, self.known_roots = source, previous_roots
                self.save()
                raise
            return {'source_root': str(source), 'storage_root': str(root),
                    'migrated_records': sum(1 for _, target in moved for _ in target.glob(f'{RECORDINGS}/*/session.json')),
                    'migrated_files': sum(1 for _, target in moved for path in target.rglob('*') if path.is_file()), 'skipped': []}
        if root.is_relative_to(source):
            relative_root = root.relative_to(source)
            if relative_root.parts and relative_root.parts[0] in set(self.categories):
                raise ValueError("新位置不能放在课程或录音目录里面")

        root.mkdir(parents=True, exist_ok=True)
        if not os.access(root, os.W_OK):
            raise ValueError("保存位置不可写")
        if not source.exists():
            self.storage_root = root
            self.add_known_root(root)
            self.save()
            return {
                "source_root": str(source),
                "storage_root": str(root),
                "migrated_records": 0,
                "migrated_files": 0,
                "skipped": [],
            }

        managed_names = set(self.categories)
        entries = sorted(
            (item for item in source.iterdir() if item.resolve() != root),
            key=lambda item: item.name.casefold(),
        )
        skipped: list[str] = []
        record_moves: list[tuple[Path, Path]] = []
        file_moves: list[tuple[Path, Path]] = []

        # Preflight every destination before moving anything so a collision
        # cannot leave half of the library in the old location.
        for entry in entries:
            destination = root / entry.name
            if not entry.is_dir() or (
                entry.name not in managed_names
                and not any(
                    child.is_dir()
                    and (child / "session.json").exists()
                    or child.name == ".qwenasr-course.json"
                    for child in entry.iterdir()
                )
            ):
                # The selected source may be Documents or another shared
                # folder. Only move QwenASR-owned category data.
                continue
            if entry.is_dir():
                for child in sorted(entry.iterdir(), key=lambda item: item.name.casefold()):
                    target = destination / child.name
                    if not (
                        child.name == ".qwenasr-course.json"
                        or (child.name == AI_NOTES and child.is_dir())
                        or (child.name == "课程资料" and child.is_dir() and (child / ".qwenasr-materials.json").is_file())
                        or (child.is_dir() and (child / "session.json").exists())
                    ):
                        continue
                    if target.exists():
                        raise FileExistsError(f"目标位置已有同名文件：{target}")
                    if child.is_dir():
                        record_moves.append((child, target))
                    else:
                        file_moves.append((child, target))

        completed_moves: list[tuple[Path, Path]] = []
        previous_known_roots = list(self.known_roots)
        try:
            for source_path, target_path in [*record_moves, *file_moves]:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source_path), str(target_path))
                completed_moves.append((source_path, target_path))
            self.storage_root = root
            self.add_known_root(source)
            self.add_known_root(root)
            self.save()
        except OSError:
            self.storage_root = source
            self.known_roots = previous_known_roots
            for source_path, target_path in reversed(completed_moves):
                if target_path.exists() and not source_path.exists():
                    source_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target_path), str(source_path))
            raise
        for entry in entries:
            if entry.is_dir():
                try:
                    entry.rmdir()
                except OSError:
                    pass

        return {
            "source_root": str(source),
            "storage_root": str(root),
            "migrated_records": sum((target / "session.json").is_file() for _, target in record_moves),
            "migrated_files": len(file_moves) + sum(
                sum(path.is_file() for path in target.rglob("*"))
                for _, target in record_moves if (target / ".qwenasr-materials.json").is_file()
            ),
            "skipped": skipped,
        }

    def save(self) -> None:
        payload = {
            "schema_version": 1,
            "storage_root": str(self.storage_root),
            "categories": self.categories,
            "known_roots": self.known_roots,
            "library_layout": self.library_layout,
            "category_profiles": self.category_profiles,
            "terminology_seed_version": self.terminology_seed_version,
        }
        try:
            save_settings(SETTINGS_PATH, payload)
        except (OSError, ValueError):
            # A failed settings mutation must not switch the running process
            # to a path that was never durably accepted.
            for key, value in getattr(self, '_saved_state', {}).items():
                setattr(self, key, copy.deepcopy(value))
            raise
        self._saved_state = {key: copy.deepcopy(getattr(self, key)) for key in payload if key != 'schema_version'}

    def public(self) -> dict[str, Any]:
        return {
            "storage_root": str(self.storage_root),
            "categories": self.categories,
            "category_profiles": self.category_profiles,
            "library_layout": self.library_layout,
        }


app_settings = AppSettings()


class RecordingRecord:
    def __init__(self, directory: Path, metadata: dict[str, Any]) -> None:
        self.directory = directory
        self.meta_path = directory / "session.json"
        self.audio_path = directory / str(metadata.get("audio_filename", "recording.wav"))
        self.working_audio_path = directory / str(
            metadata.get("working_audio_filename", metadata.get("audio_filename", "recording.wav"))
        )
        self.transcript_path = directory / "transcript.txt"
        self.raw_transcript_path = directory / "原始转写.txt"
        self.corrected_transcript_path = directory / "AI修正后文本.txt"
        self.transcript_json_path = directory / "transcript.json"
        self.notes_path = directory / "课堂笔记.md"
        self.ai_summary_path = directory / "AI总结.md"
        self.bundle_path = directory / "recording-and-transcript.zip"
        self.metadata = metadata
        self.segments: list[dict[str, Any]] = list(metadata.pop("segments", []))
        self._last_meta_write = 0.0
        self._persisted_note = copy.deepcopy(metadata.get("ai_note_summary"))

    def complete_material_only_note(self) -> bool:
        """Finish explicit empty-transcript work without retrying silent audio.

        A saved note alone never completes a failed recording. Both the user's
        material-only decision and a completed material-derived note are needed.
        """
        choice=self.metadata.get('material_only_note') or {}
        note=self.metadata.get('ai_note_summary')
        if (choice.get('transcript_policy')!='empty' or self.segments
                or not isinstance(note, dict)
                or note.get('source')!='course_material_only' or not note.get('summary')
                or self.metadata.get('status') not in {'interrupted','failed'}):return False
        stream=self.metadata.get('course_stream') or {}
        if stream.get('error'):choice.setdefault('previous_audio_error',stream['error'])
        choice.update(status='completed',asr_status='skipped_by_user',completed_at=local_now())
        self.metadata.update(status='completed',ended_at=self.metadata.get('ended_at') or
            (self.metadata.get('course_time') or {}).get('end') or local_now())
        if stream:
            stream.update(stage='completed_material_only',error='',paused=False)
        self.save_metadata()
        return True

    @classmethod
    def create_live(cls, title: str, category: str, storage_root: Path, recorded_at: str | None = None) -> "RecordingRecord":
        started = datetime.now().astimezone()
        session_id = f"{started:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("开始录音前，请先填写录音名称")
        clean_category = safe_component(category, "未分类")
        directory = storage_root / clean_category / f"{safe_component(clean_title, '课堂录音')}__{session_id}"
        if shared(app_settings):
            directory = recording_path(storage_root, clean_category, {"id": session_id,
                "started_at": started.isoformat(), "recorded_at": recorded_at})
        directory.mkdir(parents=True, exist_ok=False)
        metadata: dict[str, Any] = {
            "id": session_id,
            "title": clean_title,
            "category": clean_category,
            "kind": "microphone",
            "status": "active",
            "started_at": started.isoformat(timespec="seconds"),
            "recorded_at": recorded_at or started.isoformat(timespec="seconds"),
            "updated_at": started.isoformat(timespec="seconds"),
            "ended_at": None,
            "sample_rate": SAMPLE_RATE,
            "sample_count": 0,
            "next_segment_id": 1,
            "audio_filename": "recording.aac",
            "working_audio_filename": ".recording-recovery.wav",
        }
        record = cls(directory, metadata)
        record._create_empty_wav()
        record.save_all()
        return record

    @classmethod
    def create_upload(
        cls,
        source: Path,
        original_filename: str,
        duration: float,
        title: str,
        category: str,
        storage_root: Path,
        session_id: str | None = None,
        course_time: dict | None = None,
    ) -> "RecordingRecord":
        started = datetime.now().astimezone()
        session_id = session_id or f"upload-{started:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        clean_title = str(title or "").strip() or Path(original_filename).stem or "上传录音"
        clean_category = safe_component(category, "未分类")
        directory = storage_root / clean_category / f"{safe_component(clean_title, '上传录音')}__{session_id}"
        if shared(app_settings):
            directory = recording_path(storage_root, clean_category, {"id": session_id, "started_at": started.isoformat(),
                "recorded_at": (course_time or {}).get('start')})
        if (directory / "session.json").exists():
            existing = cls.load(directory)
            if existing.id != session_id:
                raise ValueError("上传归档目录冲突")
            return existing
        directory.mkdir(parents=True, exist_ok=True)
        audio_filename = "recording.aac"
        convert_audio_to_aac(source, directory / audio_filename)
        metadata: dict[str, Any] = {
            "id": session_id,
            "title": clean_title,
            "category": clean_category,
            "kind": "upload",
            "status": "processing",
            "source_filename": original_filename,
            "started_at": started.isoformat(timespec="seconds"),
            "updated_at": started.isoformat(timespec="seconds"),
            "ended_at": None,
            "sample_rate": SAMPLE_RATE,
            "sample_count": int(duration * SAMPLE_RATE),
            "next_segment_id": 2,
            "audio_filename": audio_filename,
        }
        record = cls(directory, metadata)
        if course_time:
            record.metadata['course_time'] = course_time
        record.save_all()
        return record

    @classmethod
    def load(cls, directory: Path) -> "RecordingRecord":
        payload = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        if not payload.get('recorded_at'):
            recorded_at = manifest_time(directory)
            if recorded_at:
                payload['recorded_at'] = recorded_at
        segments = []
        transcript_json = directory / "transcript.json"
        if transcript_json.exists():
            saved = json.loads(transcript_json.read_text(encoding="utf-8"))
            if isinstance(saved, list):
                segments = saved
        payload["segments"] = segments
        record = cls(directory, payload)
        apply_course_dates(record.metadata, directory)
        if shared(app_settings) and directory.is_relative_to(app_settings.storage_root):
            record.metadata['category'] = directory.relative_to(app_settings.storage_root).parts[0]
        return record

    @property
    def id(self) -> str:
        return str(self.metadata["id"])

    @property
    def duration(self) -> float:
        return int(self.metadata.get("sample_count", 0)) / SAMPLE_RATE

    @property
    def next_segment_id(self) -> int:
        return int(self.metadata.get("next_segment_id", 1))

    def library_summary(self, max_chars: int = 360) -> tuple[str, str]:
        """Return only a saved AI summary; never synthesize one from transcript excerpts."""
        limit = max(80, min(int(max_chars), 8_000))
        item = self.metadata.get("ai_note_summary")
        if isinstance(item, dict) and not item.get("ai_error") and not item.get("abstract_error"):
            abstract = re.sub(r"\s+", " ", str(item.get("abstract") or "")).strip()
            if abstract and len(abstract) <= 100:
                return abstract, "ai"
        return "", "missing"

    def library_summary_error(self) -> str:
        item = self.metadata.get("ai_note_summary")
        if not isinstance(item, dict):
            return ""
        return str(item.get("abstract_error") or "").strip()

    def update_note_summary(self, markdown: str, *, source: str = "edited") -> dict[str, Any]:
        text = str(markdown or "").strip()
        if not text:
            raise ValueError("AI 总结不能为空")
        if len(text) > 1_000_000:
            raise ValueError("AI 总结超过 100 万字符，请分开保存；原内容未修改")
        now = local_now()
        previous = self.metadata.get("ai_note_summary")
        item = {
            **(previous if isinstance(previous, dict) else {}),
            "summary": text,
            "updated_at": now,
            "created_at": (
                str(previous.get("created_at") or now) if isinstance(previous, dict) else now
            ),
            "source": source,
        }
        self.metadata["ai_note_summary"] = item
        if not isinstance(previous, dict) or previous.get('summary') != text:
            item['review_status'] = 'stale'
            item.pop('pdf_sha256', None)
            item['review_warning'] = '正文已修改，PDF 与独立审查需要重新生成'
        sync = self.metadata.get("ideashell_sync")
        if isinstance(sync, dict) and sync.get("note_id") and source != "ideashell_pull":
            sync["status"] = "local_changes"
        self.metadata["updated_at"] = now
        self.save_all()
        return item

    @staticmethod
    def _compact_summary(text: str, limit: int) -> str:
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(compact) <= limit:
            return compact
        candidate = compact[:limit].rstrip("，,；;：:、 ")
        punctuation = max(candidate.rfind(mark) for mark in "。！？!?")
        if punctuation >= max(24, int(limit * 0.55)):
            candidate = candidate[: punctuation + 1]
        return candidate.rstrip() + "…"

    def _create_empty_wav(self) -> None:
        header = (
            b"RIFF"
            + struct.pack("<I", 36)
            + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
            + b"data"
            + struct.pack("<I", 0)
        )
        self.working_audio_path.write_bytes(header)

    def append_audio(self, audio: np.ndarray) -> None:
        if self.metadata.get("status") != "active" or not len(audio):
            return
        pcm = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
        pcm16 = (pcm * 32767.0).astype("<i2", copy=False).tobytes()
        with self.working_audio_path.open("r+b") as output:
            output.seek(0, os.SEEK_END)
            output.write(pcm16)
            data_size = output.tell() - 44
            output.seek(4)
            output.write(struct.pack("<I", 36 + data_size))
            output.seek(40)
            output.write(struct.pack("<I", data_size))
        self.metadata["sample_count"] = int(self.metadata.get("sample_count", 0)) + len(audio)
        self.metadata["updated_at"] = local_now()
        now = time.monotonic()
        if now - self._last_meta_write >= 1.0:
            self.save_metadata()
            self._last_meta_write = now

    def reserve_segment_id(self) -> int:
        segment_id = self.next_segment_id
        self.metadata["next_segment_id"] = segment_id + 1
        self.save_metadata()
        return segment_id

    def upsert_segment(self, segment: dict[str, Any]) -> None:
        for index, existing in enumerate(self.segments):
            if existing.get("id") == segment.get("id"):
                self.segments[index] = {**existing, **segment}
                break
        else:
            self.segments.append(segment)
        self.segments.sort(key=lambda item: int(item.get("id", 0)))
        self.metadata["updated_at"] = local_now()
        self.save_all()

    def save_metadata(self) -> None:
        self.refresh_location()
        apply_course_dates(self.metadata, self.directory)
        from .note_history import capture
        capture(self, self._persisted_note)
        atomic_json(self.meta_path, self.metadata)
        from .background_wakeup import library_changes
        library_changes.notify([str(self.meta_path)])
        self._persisted_note = copy.deepcopy(self.metadata.get("ai_note_summary"))

    def refresh_location(self) -> None:
        if not shared(app_settings):
            return
        if not self.directory.exists():
            location = None
            for candidate in app_settings.storage_root.glob(f'*/{RECORDINGS}/*/session.json'):
                try:
                    if json.loads(candidate.read_text()).get('id') == self.id:
                        location = candidate.parent
                        break
                except (OSError, ValueError):
                    continue
            if location is None:
                raise FileNotFoundError('录音目录已从文件夹移除，停止写入以免重新创建已删除的文件')
            old = self.directory
            for key in ('meta_path', 'audio_path', 'working_audio_path', 'transcript_path',
                        'raw_transcript_path', 'corrected_transcript_path', 'transcript_json_path',
                        'notes_path', 'ai_summary_path', 'bundle_path'):
                setattr(self, key, location / getattr(self, key).relative_to(old))
            self.directory = location
        try:
            category = self.directory.relative_to(app_settings.storage_root).parts[0]
        except ValueError:
            return
        self.metadata['category'] = category

    def save_transcripts(self) -> None:
        atomic_json(self.transcript_json_path, self.segments)

        blocks: list[str] = []
        for segment in self.segments:
            start = format_clock(float(segment.get("start", 0)))
            end = format_clock(float(segment.get("end", 0)))
            language = str(segment.get("language") or "自动识别")
            blocks.append(f"[{start} - {end}] {language}\n{segment.get('raw', '')}")
            if segment.get("corrected"):
                blocks.append(f"修正：{segment['corrected']}")
            if segment.get("translation"):
                blocks.append(f"译文：{segment['translation']}")
            blocks.append("")
        ai_reviews = self.metadata.get("ai_reviews")
        if isinstance(ai_reviews, list) and ai_reviews:
            blocks.extend(["===== AI 每五分钟整体整理 =====", ""])
            for review in ai_reviews:
                if not isinstance(review, dict):
                    continue
                start = format_clock(float(review.get("start", 0)))
                end = format_clock(float(review.get("end", 0)))
                blocks.extend(
                    [
                        f"[{start} - {end}]",
                        str(review.get("corrected") or review.get("raw") or ""),
                    ]
                )
                if review.get("translation"):
                    blocks.append(f"译文：{review['translation']}")
                if review.get("ai_error"):
                    blocks.append(f"AI 整理未完成：{review['ai_error']}")
                blocks.append("")
        ai_groups = self.metadata.get("ai_groups")
        if isinstance(ai_groups, list) and ai_groups:
            blocks.extend(["===== AI 整理稿 =====", ""])
            for group in ai_groups:
                if not isinstance(group, dict):
                    continue
                start = format_clock(float(group.get("start", 0)))
                end = format_clock(float(group.get("end", 0)))
                blocks.append(f"[{start} - {end}]")
                blocks.append(str(group.get("corrected") or group.get("raw") or ""))
                if group.get("translation"):
                    blocks.append(f"译文：{group['translation']}")
                if group.get("ai_error"):
                    blocks.append(f"AI 处理未完成：{group['ai_error']}")
                blocks.append("")
        global_rewrite = self.metadata.get("ai_global_rewrite")
        if isinstance(global_rewrite, dict) and global_rewrite.get("text"):
            blocks.extend(
                [
                    "===== AI 全局改写 =====",
                    "",
                    str(global_rewrite.get("text") or ""),
                    "",
                ]
            )
            if global_rewrite.get("translation"):
                blocks.extend([f"译文：{global_rewrite['translation']}", ""])
        ai_summaries = self.metadata.get("ai_summaries")
        if isinstance(ai_summaries, list) and ai_summaries:
            blocks.extend(["===== AI 实时总结 =====", ""])
            for summary in ai_summaries:
                if not isinstance(summary, dict) or not summary.get("summary"):
                    continue
                end = format_clock(float(summary.get("end", 0)))
                blocks.extend([f"[截至 {end}]", str(summary["summary"])])
                points = summary.get("key_points")
                if isinstance(points, list) and points:
                    blocks.extend([f"- {point}" for point in points])
                blocks.append("")
        ai_note_summary = self.metadata.get("ai_note_summary")
        if (isinstance(ai_note_summary, dict) and ai_note_summary.get("summary")
                and not (self.metadata.get('material_only_note') and not self.segments)):
            blocks.extend(
                ["===== 笔记完成后 AI 总结 =====", "", str(ai_note_summary["summary"]), ""]
            )
        text_temp = self.transcript_path.with_suffix(".txt.tmp")
        text_temp.write_text("\n".join(blocks).rstrip() + ("\n" if blocks else ""), encoding="utf-8")
        text_temp.replace(self.transcript_path)

        raw_blocks = []
        for segment in self.segments:
            start = format_clock(float(segment.get("start", 0)))
            end = format_clock(float(segment.get("end", 0)))
            raw_blocks.append(f"[{start} - {end}]\n{segment.get('raw', '')}")
        raw_temp = self.raw_transcript_path.with_suffix(".txt.tmp")
        raw_temp.write_text(
            "\n\n".join(raw_blocks).rstrip() + ("\n" if raw_blocks else ""),
            encoding="utf-8",
        )
        raw_temp.replace(self.raw_transcript_path)

        corrected_blocks: list[tuple[float, str]] = []
        if (
            isinstance(global_rewrite, dict)
            and global_rewrite.get("complete") is not False
            and global_rewrite.get("text")
        ):
            corrected_blocks.append((0.0, str(global_rewrite["text"])))
        else:
            covered_ids: set[int] = set()
            if isinstance(ai_reviews, list):
                for review in ai_reviews:
                    if not isinstance(review, dict) or not review.get("corrected"):
                        continue
                    corrected_blocks.append(
                        (float(review.get("start", 0)), str(review["corrected"]))
                    )
                    covered_ids.update(int(value) for value in review.get("segment_ids", []))
            for segment in self.segments:
                if int(segment.get("id", 0)) in covered_ids:
                    continue
                text = str(segment.get("corrected") or segment.get("raw") or "").strip()
                if text:
                    corrected_blocks.append((float(segment.get("start", 0)), text))
        corrected_blocks.sort(key=lambda item: item[0])
        corrected_text, _ = transcript_markdown(self)
        corrected_temp = self.corrected_transcript_path.with_suffix(".txt.tmp")
        corrected_temp.write_text(
            corrected_text + ("\n" if corrected_text else ""), encoding="utf-8"
        )
        corrected_temp.replace(self.corrected_transcript_path)

    def save_notes(self) -> None:
        title = str(self.metadata.get("title") or "课堂录音")
        category = str(self.metadata.get("category") or "未分类")
        status_name = {
            "active": "录音中",
            "processing": "处理中",
            "interrupted": "已暂停，可继续处理",
            "completed": "已完成",
            "failed": "处理未完成",
        }.get(str(self.metadata.get("status")), str(self.metadata.get("status") or "未知"))
        lines = [
            f"# {title}",
            "",
            f"- 分类：{category}",
            f"- 开始时间：{self.metadata.get('started_at') or ''}",
            f"- 状态：{status_name}",
            f"- 时长：{format_clock(self.duration)}",
            "",
            "## 课堂转写",
            "",
        ]
        if not self.segments:
            lines.append("_尚无转写内容。_")
        for segment in self.segments:
            start = format_clock(float(segment.get("start", 0)))
            end = format_clock(float(segment.get("end", 0)))
            raw = str(segment.get("raw") or "").strip()
            corrected = str(segment.get("corrected") or "").strip()
            translation = str(segment.get("translation") or "").strip()
            lines.extend([f"### {start} – {end}", "", corrected or raw or "_未识别到文字。_", ""])
            if corrected and corrected != raw and raw:
                lines.extend([f"> 原始识别：{raw}", ""])
            if translation:
                lines.extend([f"**翻译：** {translation}", ""])
            if segment.get("ai_error"):
                lines.extend([f"> AI 处理未完成：{segment['ai_error']}", ""])
        ai_reviews = self.metadata.get("ai_reviews")
        if isinstance(ai_reviews, list) and ai_reviews:
            lines.extend(["", "## AI 每五分钟整体整理", ""])
            for review in ai_reviews:
                if not isinstance(review, dict):
                    continue
                start = format_clock(float(review.get("start", 0)))
                end = format_clock(float(review.get("end", 0)))
                lines.extend(
                    [
                        f"### {start} – {end}",
                        "",
                        str(review.get("corrected") or review.get("raw") or "").strip(),
                        "",
                    ]
                )
                if review.get("translation"):
                    lines.extend([f"**翻译：** {review['translation']}", ""])
                if review.get("ai_error"):
                    lines.extend([f"> AI 整理未完成：{review['ai_error']}", ""])
        ai_groups = self.metadata.get("ai_groups")
        if isinstance(ai_groups, list) and ai_groups:
            lines.extend(["", "## AI 整理稿", ""])
            for group in ai_groups:
                if not isinstance(group, dict):
                    continue
                start = format_clock(float(group.get("start", 0)))
                end = format_clock(float(group.get("end", 0)))
                lines.extend(
                    [
                        f"### {start} – {end}",
                        "",
                        str(group.get("corrected") or group.get("raw") or "").strip(),
                        "",
                    ]
                )
                if group.get("translation"):
                    lines.extend([f"**翻译：** {group['translation']}", ""])
                if group.get("ai_error"):
                    lines.extend([f"> AI 处理未完成：{group['ai_error']}", ""])
        global_rewrite = self.metadata.get("ai_global_rewrite")
        if isinstance(global_rewrite, dict) and global_rewrite.get("text"):
            lines.extend(
                ["", "## AI 全局改写", "", str(global_rewrite.get("text") or ""), ""]
            )
            if global_rewrite.get("translation"):
                lines.extend([f"**翻译：** {global_rewrite['translation']}", ""])
            if global_rewrite.get("context_split"):
                lines.extend(["> 内容超过模型上下文窗口，已采用保持时间顺序的分块全局整理。", ""])
        ai_summaries = self.metadata.get("ai_summaries")
        if isinstance(ai_summaries, list) and ai_summaries:
            lines.extend(["", "## AI 实时总结", ""])
            for summary in ai_summaries:
                if not isinstance(summary, dict) or not summary.get("summary"):
                    continue
                end = format_clock(float(summary.get("end", 0)))
                lines.extend([f"### 截至 {end}", "", str(summary["summary"]), ""])
                points = summary.get("key_points")
                if isinstance(points, list) and points:
                    lines.extend(["**要点：**", "", *[f"- {point}" for point in points], ""])
        ai_note_summary = self.metadata.get("ai_note_summary")
        if isinstance(ai_note_summary, dict) and ai_note_summary.get("summary"):
            lines.extend(
                ["", "## 笔记完成后 AI 总结", "", str(ai_note_summary["summary"]), ""]
            )
        temp = self.notes_path.with_suffix(".md.tmp")
        temp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        temp.replace(self.notes_path)

    def save_summary_markdown(self) -> None:
        summary = self.metadata.get("ai_note_summary")
        text = str(summary.get("summary") or "").strip() if isinstance(summary, dict) else ""
        if text:
            temp = self.ai_summary_path.with_suffix(".md.tmp")
            temp.write_text(text.rstrip() + "\n", encoding="utf-8")
            temp.replace(self.ai_summary_path)
        else:
            self.ai_summary_path.unlink(missing_ok=True)

    def save_all(self) -> None:
        self.save_metadata()
        self.save_transcripts()
        self.save_summary_markdown()

    def complete(self) -> None:
        self.ensure_aac()
        self.metadata["status"] = "completed"
        self.metadata["ended_at"] = local_now()
        self.metadata["updated_at"] = self.metadata["ended_at"]
        self.save_all()
        if self.working_audio_path != self.audio_path and not self.metadata.get('pending_segments'):
            self.working_audio_path.unlink(missing_ok=True)

    def ensure_aac(self) -> Path:
        if self.audio_path.exists() and (
            self.metadata.get("status") == "completed" or self.working_audio_path == self.audio_path
        ):
            return self.audio_path
        source = self.working_audio_path
        if not source.exists():
            if self.audio_path.exists():
                return self.audio_path
            raise FileNotFoundError("录音底稿不存在")
        convert_audio_to_aac(source, self.audio_path)
        return self.audio_path

    def make_bundle(self) -> Path:
        """Build a unique temporary download bundle outside the recording folder."""
        self.ensure_aac()
        # Rebuild text exports immediately before the download so the archive
        # always contains the latest edits. Old persistent bundles are generated
        # artifacts and are removed once this recording is downloaded again.
        self.save_metadata()
        self.save_transcripts()
        self.save_summary_markdown()
        self.bundle_path.unlink(missing_ok=True)
        descriptor, filename = tempfile.mkstemp(
            prefix=f"qwenasr-export-{safe_component(self.id, 'recording')}-",
            suffix=".zip",
        )
        os.close(descriptor)
        bundle = Path(filename)
        try:
            with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in (
                    self.audio_path,
                    self.raw_transcript_path,
                    self.corrected_transcript_path,
                    self.ai_summary_path,
                    self.transcript_path,
                    self.transcript_json_path,
                    self.meta_path,
                    self.directory / "课程来源.json",
                ):
                    if path.exists():
                        archive.write(
                            path,
                            arcname=path.name,
                            compress_type=(
                                zipfile.ZIP_STORED
                                if path == self.audio_path
                                else zipfile.ZIP_DEFLATED
                            ),
                        )
            return bundle
        except BaseException:
            bundle.unlink(missing_ok=True)
            raise

    def list_item(self) -> dict[str, Any]:
        """Allowlist: new metadata fields cannot accidentally bloat list APIs."""
        keys = ('id', 'title', 'category', 'status', 'kind', 'started_at', 'recorded_at',
                'ended_at', 'updated_at', 'sample_count', 'source_filename')
        result = {key: self.metadata[key] for key in keys if key in self.metadata}
        from .chapters import labels_from_metadata
        chapters = labels_from_metadata(self.metadata)
        result.update(chapter='；'.join(chapters), chapters=chapters)
        note = self.metadata.get('ai_note_summary')
        has_note = isinstance(note, dict) and bool(note.get('summary'))
        summary, source = self.library_summary()
        result.update(duration=round(self.duration, 1), has_note=has_note,
                      has_ai_summary=has_note, has_transcript=bool(self.segments),
                      library_summary=summary[:240], summary_source=source,
                      library_summary_error=self.library_summary_error()[:500],
                      course_replay_sources=replay_sources(self),
                      course_stream=bool(self.metadata.get('course_stream')),
                      downloads={name.replace('-', '_'): f'/api/recordings/{self.id}/download/{name}'
                                 for name in ('bundle', 'audio', 'transcript', 'raw-transcript', 'corrected-transcript', 'ai-summary', 'json')})
        for name, fields in {
            'ai_note_summary': ('abstract', 'abstract_error', 'title_error', 'review_status','review_warning','image_count'),
            'summary_job': ('status', 'error', 'resume_stage', 'updated_at'),
            'finalization': ('status', 'error', 'updated_at'),
            'batch_progress': ('stage', 'message', 'progress', 'percent'),
            'ideashell_sync': ('status', 'note_id', 'note_ids', 'server_url', 'last_pushed_at'),
        }.items():
            value = self.metadata.get(name)
            if isinstance(value, dict):
                result[name] = {key: (str(value[key])[:500] if isinstance(value[key], str) else value[key])
                                for key in fields if key in value}
        return result

    def public(self, include_segments: bool = True) -> dict[str, Any]:
        from .lesson_pdfs import lesson_pdfs
        from .chapters import labels_from_metadata
        library_summary, summary_source = self.library_summary()
        payload = {
            **self.metadata,
            "chapter": '；'.join(labels_from_metadata(self.metadata)),
            "chapters": labels_from_metadata(self.metadata),
            "duration": round(self.duration, 1),
            "directory": str(self.directory),
            "note_pdfs": lesson_pdfs(self),
            "library_summary": library_summary,
            "summary_source": summary_source,
            "library_summary_error": self.library_summary_error(),
            "course_replay_sources": replay_sources(self),
            "has_transcript": any(
                str(item.get("corrected") or item.get("raw") or "").strip()
                for item in self.segments
            ),
            "downloads": {
                "bundle": f"/api/recordings/{self.id}/download/bundle",
                "audio": f"/api/recordings/{self.id}/download/audio",
                "transcript": f"/api/recordings/{self.id}/download/transcript",
                "raw_transcript": f"/api/recordings/{self.id}/download/raw-transcript",
                "corrected_transcript": f"/api/recordings/{self.id}/download/corrected-transcript",
                "ai_summary": f"/api/recordings/{self.id}/download/ai-summary",
                "json": f"/api/recordings/{self.id}/download/json",
            },
        }
        if include_segments:
            payload["segments"] = self.segments
            payload["refined_transcript"] = transcript_blocks(self)
        else:
            # The library view is polled frequently. Large per-window AI logs
            # belong to the recording detail and compact task endpoint.
            for key in ("ai_reviews", "ai_summaries", "ai_groups", "ai_global_rewrite", "ai_jobs"):
                payload.pop(key, None)
        payload.pop("ai_note_summary_draft", None)
        return payload

    def relocate(self, directory: Path, title: str, category: str) -> None:
        old_directory = self.directory
        self.metadata["title"] = title
        self.metadata["category"] = category
        sync = self.metadata.get("ideashell_sync")
        if isinstance(sync, dict) and sync.get("note_id"):
            sync["status"] = "local_changes"
        self.metadata["updated_at"] = local_now()
        self.save_all()
        directory.parent.mkdir(parents=True, exist_ok=True)
        if directory != old_directory:
            if directory.exists():
                raise FileExistsError(f"目标录音目录已存在：{directory}")
            shutil.move(str(old_directory), str(directory))
            self.directory = directory
            self.meta_path = directory / "session.json"
            self.audio_path = directory / str(self.metadata.get("audio_filename", "recording.aac"))
            self.working_audio_path = directory / str(
                self.metadata.get("working_audio_filename", self.metadata.get("audio_filename", "recording.aac"))
            )
            self.transcript_path = directory / "transcript.txt"
            self.raw_transcript_path = directory / "原始转写.txt"
            self.corrected_transcript_path = directory / "AI修正后文本.txt"
            self.transcript_json_path = directory / "transcript.json"
            self.notes_path = directory / "课堂笔记.md"
            self.ai_summary_path = directory / "AI总结.md"
            self.bundle_path = directory / "recording-and-transcript.zip"
        self.save_all()


class RecordingStore:
    def __init__(self) -> None:
        self.active: RecordingRecord | None = None
        self.paths: dict[str, Path] = {}
        self._read_cache = {}
        # CLI readers import this module too. Only the actual serving process
        # may reconcile abandoned work, after it owns the service lifetime lock.
        self.refresh()

    @contextlib.contextmanager
    def service_startup(self):
        folder = PRIVATE_ROOT / 'services'
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / 'recording-owner.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('已有 QwenASR 服务持有录音任务，拒绝重复启动和更改任务状态') from None
            self.refresh(mark_interrupted=True)
            yield

    def _metadata_paths(self) -> list[Path]:
        found: dict[str, Path] = {}
        roots = [LEGACY_RECORDINGS_DIR, *(Path(value) for value in app_settings.known_roots)]
        for root in roots:
            if not root.exists():
                continue
            patterns = ("*/session.json",) if root == LEGACY_RECORDINGS_DIR else (
                "*/*/session.json", f"*/{RECORDINGS}/*/session.json",
            )
            for pattern in patterns:
                for meta_path in root.glob(pattern):
                    found[str(meta_path.resolve())] = meta_path
        return list(found.values())

    def refresh(self, mark_interrupted: bool = False) -> None:
        read_cache = getattr(self, "_read_cache", {}).copy()
        paths = {}
        active_records: list[RecordingRecord] = []
        def trim_cache():
            nonlocal read_cache
            retained={};size=0
            for path,cached in sorted(read_cache.items(),key=lambda pair:str(pair[1][1].metadata.get('updated_at') or ''),reverse=True):
                cost=sum(stamp[1] for stamp in cached[0] if stamp is not None)
                if cached[1] is self.active or (len(retained)<4 and size+cost<=8*1024*1024):
                    retained[path]=cached;size+=cost
            read_cache=retained
        for meta_path in self._metadata_paths():
            try:
                stamp = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in (meta_path, meta_path.parent / "transcript.json"))
                cached = read_cache.get(meta_path)
                record = cached[1] if cached and cached[0] == stamp else RecordingRecord.load(meta_path.parent)
                record.complete_material_only_note()
                read_cache[meta_path] = (stamp, record)
                # Bound the peak as well as the final cache. Keeping every
                # decoded transcript until the sweep ends inflated idle RSS.
                trim_cache()
                if shared(app_settings) and record.directory.is_relative_to(app_settings.storage_root):
                    category = record.directory.relative_to(app_settings.storage_root).parts[0]
                    if record.metadata.get('category') != category:
                        record.metadata['category'] = category
                        if mark_interrupted:
                            record.save_metadata()
                if (
                    mark_interrupted
                    and record.metadata.get("status") in {"processing", "failed"}
                    and record.metadata.get("kind") == "upload"
                ):
                    record.metadata["status"] = "interrupted"
                    record.metadata["batch_progress"] = {
                        **(
                            record.metadata.get("batch_progress")
                            if isinstance(record.metadata.get("batch_progress"), dict)
                            else {}
                        ),
                        "stage": "interrupted",
                        "message": "服务曾关闭；可以从最后成功位置继续处理",
                    }
                    record.metadata["updated_at"] = local_now()
                    record.save_all()
                paths[record.id] = meta_path.parent
                if record.metadata.get("status") == "active" and record.metadata.get("kind") == "microphone":
                    active_records.append(record)
                if mark_interrupted:
                    changed = False
                    # Disk receipts are authoritative even if the process
                    # ended between writing a batch and refreshing the UI.
                    from .note_checkpoints import checkpoint_draft
                    summary_job = record.metadata.get('summary_job') or {}
                    if summary_job.get('status') not in {'completed'}:
                        draft = checkpoint_draft(record)
                        if draft and draft != record.metadata.get('ai_note_summary_draft'):
                            record.metadata['ai_note_summary_draft'] = draft
                            changed = True
                    for key in ("summary_job", "finalization"):
                        job = record.metadata.get(key)
                        if isinstance(job, dict) and job.get("status") in {"queued", "running", "waiting_retry"}:
                            job.update(status="interrupted", error="本地服务曾关闭；已完成检查点保留，可从任务中心继续",
                                       error_kind='service_restart', retry_in_seconds=0)
                            changed = True
                    ai_jobs = record.metadata.get("ai_jobs")
                    if isinstance(ai_jobs, dict):
                        for job in ai_jobs.values():
                            if isinstance(job, dict) and job.get("status") in {"queued", "running", "waiting_retry"}:
                                job.update(
                                    status="interrupted",
                                    error="本地服务曾关闭；已完成内容保留，可从任务中心继续",
                                    error_kind="service_restart",
                                    updated_at=local_now(),
                                )
                                changed = True
                    if changed:
                        record.save_metadata()
            except Exception as exc:
                print(f"忽略无法读取的录音会话 {meta_path}: {exc}", flush=True)
        live_paths = set(paths.values())
        read_cache = {path: cached for path, cached in read_cache.items() if path.parent in live_paths}
        # Retain only a small recent read window. Complete transcripts include
        # retrieval evidence and must not grow into an unbounded service cache.
        trim_cache()
        self.paths = paths
        self._read_cache = read_cache
        if active_records:
            active_records.sort(key=lambda item: str(item.metadata.get("started_at", "")))
            latest = active_records[-1]
            if self.active is None or self.active.id != latest.id:
                self.active = latest
            elif shared(app_settings):
                self.active.refresh_location()
        elif self.active is not None and self.active.metadata.get("status") != "active":
            self.active = None

    def start_or_resume(self, title: str, category: str) -> tuple[RecordingRecord, bool]:
        if self.active is not None and self.active.metadata.get("status") == "active":
            return self.active, False
        category = app_settings.add_category(category)
        self.active = RecordingRecord.create_live(title, category, app_settings.storage_root)
        self.paths[self.active.id] = self.active.directory
        return self.active, True

    def create_upload(
        self, source: Path, original_filename: str, duration: float, title: str, category: str, session_id: str | None = None,
        course_time: dict | None = None,
    ) -> RecordingRecord:
        category = app_settings.add_category(category)
        record = RecordingRecord.create_upload(
            source, original_filename, duration, title, category, app_settings.storage_root, session_id=session_id,
            course_time=course_time,
        )
        self.paths[record.id] = record.directory
        return record


    def find(self, session_id: str) -> RecordingRecord | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", session_id):
            return None
        if self.active is not None and self.active.id == session_id:
            return self.active
        directory = self.paths.get(session_id)
        if directory is None or not (directory / 'session.json').is_file():
            self.refresh()
            directory = self.paths.get(session_id)
        if directory is None:
            return None
        if not (directory / "session.json").exists():
            return None
        try:
            return RecordingRecord.load(directory)
        except Exception:
            return None

    def remote_backlog_ids(self) -> list[str]:
        """Scan pending headers without retaining complete recordings."""
        def eligible(metadata):
            if (metadata.get('remote_backlog') or {}).get('status') in {'paused','cancelled'}:return False
            return any((item.get('config_snapshot') or {}).get('inference_settings',{}).get('asr_provider') in {'remote_only', 'remote_preferred'}
                and (item.get('config_snapshot') or {}).get('_remote_operation_dir')
                for item in (metadata.get('pending_segments') or {}).values())
        cache=getattr(self,'_remote_backlog_cache',{});retained={};identities=[]
        for path in self._metadata_paths():
            try:
                stat=path.stat();stamp=(stat.st_mtime_ns,stat.st_size);saved=cache.get(path)
                if saved and saved[0]==stamp:entry=saved
                else:
                    metadata=json.loads(path.read_text());entry=(stamp,str(metadata['id']),bool(eligible(metadata)))
                retained[path]=entry
                if entry[2]:
                    self.paths[entry[1]]=path.parent;identities.append(entry[1])
            except (OSError,ValueError,KeyError,TypeError,AttributeError):continue
        self._remote_backlog_cache=retained
        if self.active and eligible(self.active.metadata) and self.active.id not in identities:identities.append(self.active.id)
        return identities

    def _catalog(self):
        if not hasattr(self, '_list_catalog'):
            from .recording_catalog import RecordingCatalog
            self._list_catalog = RecordingCatalog(self)
        return self._list_catalog

    def iter_list_items(self):
        """Read public list snapshots without retaining full notes/transcripts."""
        return self._catalog().items()

    def catalog_signature(self):
        """Invalidate derived task summaries on any recording file replacement."""
        return self._catalog().signature()

    def recording_directories(self):
        """Discover persisted records without reading their transcripts."""
        return [path.parent for path in self._metadata_paths()]

    def list_records(self) -> list[RecordingRecord]:
        self.refresh()
        records: list[RecordingRecord] = []
        # Catalog refreshes and note renames can replace paths from another
        # thread. Keep identities paired with their directory for this read.
        active = self.active
        for session_id, directory in tuple(self.paths.items()):
            cached = self._read_cache.get(directory / "session.json")
            record = active if active is not None and active.id == session_id else cached[1] if cached else self.find(session_id)
            if record is not None:
                records.append(record)
        records.sort(key=lambda item: str(item.metadata.get("started_at") or ""), reverse=True)
        return records

    def iter_records(self):
        """Visit records without retaining all decoded transcripts at once."""
        self.refresh()
        for session_id in tuple(self.paths):
            record=self.find(session_id)
            if record is not None:yield record

    def update(self, session_id: str, title: str, category: str) -> RecordingRecord:
        record = self.find(session_id)
        if record is None:
            raise FileNotFoundError("没有找到这条录音")
        if record.metadata.get("status") in {"active", "processing"}:
            raise ValueError("录音进行中不能移动或改名，请先暂停、结束或等待处理完成")
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("录音名称不能为空")
        clean_category = app_settings.add_category(category)
        target = (
            app_settings.storage_root
            / clean_category
            / f"{safe_component(clean_title, '课堂录音')}__{record.id}"
        )
        if shared(app_settings):
            target = recording_path(app_settings.storage_root, clean_category, record.metadata)
        record.relocate(target, clean_title, clean_category)
        self.paths[record.id] = record.directory
        if self.active is not None and self.active.id == record.id:
            self.active = record
        return record

    def delete(self, session_id: str) -> Path:
        record = self.find(session_id)
        if record is None:
            raise FileNotFoundError("没有找到这条录音")
        if record.metadata.get("status") in {"active", "processing"} or (
            self.active is not None and self.active.id == session_id
        ):
            raise ValueError("正在进行中的录音不能删除，请先点“结束并保存”")
        TRASH_ROOT.mkdir(parents=True, exist_ok=True)
        target = TRASH_ROOT / record.directory.name
        if target.exists():
            target = TRASH_ROOT / f"{record.directory.name}__deleted-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        shutil.move(str(record.directory), str(target))
        self.paths.pop(session_id, None)
        return target


recording_store = RecordingStore()
