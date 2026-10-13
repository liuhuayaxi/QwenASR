"""Compare list payloads using synthetic notes only; never open the user's library."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import isolation  # allocate disposable data root before app modules
from qwen_asr_app.storage import RecordingRecord
from qwen_asr_app.library_query import listing


def measure():
    with tempfile.TemporaryDirectory(prefix='qwen-library-benchmark-') as tmp:
        records = [RecordingRecord(Path(tmp) / str(index), {
            'id': f'synthetic-{index:03d}', 'title': '合成课堂', 'category': '合成课程',
            'status': 'completed', 'started_at': '2026-10-13T08:00:00+08:00',
            'ai_note_summary': {'summary': '中' * 20000, 'abstract': '合成短摘要'},
        }) for index in range(100)]
        size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())
        legacy = size({'records': [record.public(include_segments=False) for record in records]})
        compact = size(listing(records))
        page = size(listing(records, limit=20))
        return {'source': '100 synthetic records; 20000 Chinese characters per note',
                'legacy_bytes': legacy, 'summary_list_bytes': compact, 'page_20_bytes': page,
                'summary_reduction_percent': round((1 - compact / legacy) * 100, 2)}


if __name__ == '__main__':
    print(json.dumps(measure(), ensure_ascii=False, indent=2))
