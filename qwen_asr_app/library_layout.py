"""Human-readable layout shared by Finder and the local workbench."""
from datetime import datetime
from pathlib import Path
import json
import re

RECORDINGS = "上课录音"
AI_FILES = "AI友好型"
INDEX = ".qwenasr-files"
NOTE_LIBRARY_MARKER = ".qwenasr-note-library.json"
SHARED_LAYOUT = "shared-courses-v1"


def manifest_time(directory):
    try:
        manifest = json.loads((Path(directory) / '课程来源.json').read_text())
    except (OSError, ValueError):
        return None
    values = sorted(item['start'] for item in manifest.get('sources', [])
                    if isinstance(item, dict) and isinstance(item.get('start'), str))
    if values:
        return values[0]
    match = re.search(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?', str(manifest.get('title') or ''))
    return match.group(0) if match else None


def shared(settings):
    return getattr(settings, "library_layout", "") == SHARED_LAYOUT


def recording_time(metadata):
    sources = (metadata.get("course_stream") or {}).get("sources") or []
    candidates = [item.get("start") for item in sources if isinstance(item, dict)]
    candidates = sorted(value for value in candidates if isinstance(value, str) and len(value) >= 16)
    for value in [*candidates, metadata.get("recorded_at"), metadata.get("started_at")]:
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H-%M-%S")
        except ValueError:
            continue
    raise ValueError("录音缺少有效时间，无法生成时间目录")


def recording_path(root, category, metadata):
    folder = Path(root) / category / RECORDINGS / recording_time(metadata)
    if folder.exists():
        import json
        try:
            if json.loads((folder / "session.json").read_text()).get("id") == metadata.get("id"):
                return folder
        except (ValueError, OSError):
            pass
        folder = folder.with_name(folder.name + "-" + str(metadata["id"]).rsplit("-", 1)[-1])
    return folder
