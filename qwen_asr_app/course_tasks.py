"""Read the course service's persisted queue without starting or logging into it."""
import json
import os
import hashlib
from .paths import PRIVATE_ROOT


def course_tasks():
    root = PRIVATE_ROOT / 'smartclass'
    try:
        queue = json.loads((root / 'queue.json').read_text())
        imports = json.loads((root / 'imports.json').read_text()) if (root / 'imports.json').exists() else {}
    except (OSError, ValueError):
        return []
    all_items = queue.get('items', [])
    # Completed replay imports belong in the course library, not in the
    # actionable task feed. Keep the queue file intact for replay recovery,
    # but expose only unfinished course groups to the task center.
    items = [item for item in all_items if item.get('state') != 'completed']
    if not items:
        return []
    # The queue reuses one feed ID. Distinguish later selections/completions so
    # clearing the last completed queue does not hide a future one.
    run_identity = hashlib.sha256(json.dumps({'items': items, 'imports': {
        item['group']['id']: imports.get(item['group']['id'], {}) for item in items
    }}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    remaining = len(items)
    states = {item.get('state') for item in items}
    status = 'completed' if not remaining else 'paused' if queue.get('paused') else 'running' if 'running' in states else 'queued' if 'queued' in states else 'interrupted'
    course_items = []
    for item in items:
        ident = item['group']['id']
        entry = imports.get(ident, {})
        phase_data = []
        from .course_phase_status import lesson_phases
        from .storage import recording_store
        record=recording_store.find(entry.get('record_id') or entry.get('recording_id') or '')
        for key, phase in lesson_phases(entry,record).items():
            if not isinstance(phase, dict):
                continue
            phase_data.append({
                **phase,
                'key': key, 'label': phase.get('label', key),
                'percent': round(float(phase.get('percent', 0) or 0), 1),
                'detail': phase.get('detail') or '',
                'state': phase.get('state') or 'pending',
            })
        course_items.append({
            'id': ident,
            'recording_id': entry.get('record_id') or entry.get('recording_id') or '',
            'title': item['group'].get('title', ident),
            'course': item['group'].get('course_name') or '',
            'start': item['group'].get('start') or '',
            'stop': item['group'].get('stop') or '',
            'state': item.get('state', 'queued'),
            'progress': round(float(entry.get('progress', 0) or 0), 1),
            'message': entry.get('message') or '',
            'updated_at': entry.get('updated_at') or '',
            'phases': phase_data,
        })
    details = [item['title'] + '：' + (item['message'] or item['state']) for item in course_items]
    overall = round(sum(item['progress'] for item in course_items) / max(1, len(course_items)), 1)
    return [{'id': 'course_queue', 'type': 'course_queue', 'family': 'asr', 'label': '课程回放队列',
             'title': f'课程回放 · {len(items)} 堂课', 'category': '', 'source': '课程回放', 'status': status,
             'run_identity': run_identity,
             'completed': len(all_items)-len(items), 'total': len(all_items), 'progress': overall, 'stage': '按未完成课次处理',
             'actions': ['pause'] if status in {'running', 'queued'} else ['continue'] if remaining else [],
             'warnings': details, 'course_items': course_items, 'result_url': '/course/'}]


async def course_action(action):
    import httpx
    if action not in {'pause', 'continue'}:
        raise ValueError('课程队列支持暂停或继续，请到课程回放页调整课次')
    origin = 'http://127.0.0.1:' + os.getenv('QWEN_ASR_PORT', '8765')
    base = origin + '/course'
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(base + ('/api/stop' if action == 'pause' else '/api/resume'),
                headers={'Origin': origin, 'X-QwenASR-Client': 'smartclass'})
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise ValueError(exc.response.json().get('detail', '课程服务无法执行操作')) from exc
    except httpx.HTTPError as exc:
        raise ValueError('课程服务未连接，请启动课程回放服务后重试') from exc
