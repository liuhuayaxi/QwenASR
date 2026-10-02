"""Read the course service's persisted queue without starting or logging into it."""
import json
import os
from .paths import PRIVATE_ROOT


def course_tasks():
    root = PRIVATE_ROOT / 'smartclass'
    try:
        queue = json.loads((root / 'queue.json').read_text())
        imports = json.loads((root / 'imports.json').read_text()) if (root / 'imports.json').exists() else {}
    except (OSError, ValueError):
        return []
    items = queue.get('items', [])
    if not items:
        return []
    remaining = sum(item.get('state') != 'completed' for item in items)
    states = {item.get('state') for item in items}
    status = 'completed' if not remaining else 'paused' if queue.get('paused') else 'running' if 'running' in states else 'queued' if 'queued' in states else 'interrupted'
    details = []
    for item in items:
        ident = item['group']['id']
        entry = imports.get(ident, {})
        phases = ' · '.join(f"{phase['label']} {phase.get('percent', 0):.0f}%" for phase in entry.get('phases', {}).values())
        details.append(item['group'].get('title', ident) + '：' + (phases or entry.get('message') or item.get('state', '')))
    return [{'id': 'course_queue', 'type': 'course_queue', 'family': 'asr', 'label': '课程回放队列',
             'title': f'课程回放 · {len(items)} 堂课', 'category': '', 'source': '课程回放', 'status': status,
             'completed': len(items) - remaining, 'total': len(items), 'stage': '按已保存的选课队列处理',
             'actions': ['pause'] if status in {'running', 'queued'} else ['continue'] if remaining else [],
             'warnings': details, 'result_url': '/course/'}]


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
