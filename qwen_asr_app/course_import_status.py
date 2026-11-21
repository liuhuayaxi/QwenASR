"""Match lesson import states by stable replay IDs, never by editable titles."""
import hashlib


LABELS = {
    'completed': '已入库', 'partial': '部分入库', 'running': '入库中',
    'queued': '已排队', 'paused': '已暂停', 'interrupted': '待继续',
    'removed': '已移除', 'new': '',
}


def video_ids(group):
    return [str(video['NewID']) for video in group.get('videos', []) if video.get('NewID')]


def recording_inventory(records):
    from .course_identity import record_sources
    return [{'id': record.id, 'status': record.metadata.get('status'),
             'source_filename': record.metadata.get('source_filename'),
             'sources': record_sources(record),
             'course_stream': {'keys': record.metadata.get('course_stream', {}).get('keys', []),
                               'paused': record.metadata.get('course_stream', {}).get('paused', False)}}
            for record in records]


def lesson_import_statuses(groups, records, imports, queue_items):
    """Use live archives for completion and the durable queue/history for attempts."""
    records_by_id = {record['id']: record for record in records}
    queued_groups = {item['group']['id']: item['group'] for item in queue_items}
    history = [{**entry, 'id': ident,
                'video_ids': entry.get('video_ids') or video_ids(queued_groups.get(ident, {}))}
               for ident, entry in imports.items()]
    tasks = {entry['id']: entry for entry in history}
    # The queue is authoritative for a pending retry or an explicitly paused job.
    for item in queue_items:
        ident = item['group']['id']
        if item['state'] != 'completed':
            tasks[ident] = {**tasks.get(ident, {}), 'id': ident, 'state': item['state'],
                            'video_ids': video_ids(item['group'])}

    def record_keys(record):
        keys = set(record.get('course_stream', {}).get('keys', []))
        filename = record.get('source_filename') or ''
        if filename.startswith('smartclass_') and filename.endswith('.flac'):
            keys.add(filename[len('smartclass_'):-len('.flac')])
        return keys

    indexed_records = []
    for record in records:
        sources = (record.get('sources') or record.get('course_stream', {}).get('sources')
                   or record.get('course_replay_sources') or [])
        indexed_records.append((record, {str(s['video_id']) for s in sources if s.get('video_id')},
                                record_keys(record)))

    def pending_state(states):
        return next((state for state in ('running', 'queued', 'paused', 'interrupted', 'removed')
                     if state in states), 'new')

    result = {}
    for group in groups:
        periods = []
        for video in group.get('videos', []):
            ident = str(video['NewID'])
            single_key = hashlib.sha256(ident.encode()).hexdigest()[:20]
            keys = {group['id'], single_key}

            def matches(entry):
                ids = entry.get('video_ids') or []
                return ident in ids if ids else entry['id'] in keys

            matching_history = [entry for entry in history if matches(entry)]
            linked_ids = {entry.get('record_id') for entry in matching_history}
            matching_records = [record for record, ids, aliases in indexed_records
                                if (ident in ids if ids else bool(keys & aliases) or record['id'] in linked_ids)]
            archived = any(record.get('status') == 'completed' for record in matching_records)
            # A completed single period stays archived while its recording is
            # being supplemented with a newly available adjacent period.
            archived |= any(entry.get('state') == 'completed' and entry.get('record_id') in records_by_id
                            for entry in matching_history)
            states = []
            if not archived:
                for entry in tasks.values():
                    if matches(entry):
                        state = entry.get('state')
                        states.append('interrupted' if state == 'error' else 'removed' if state == 'completed' else state)
                for record in matching_records:
                    states.append('paused' if record.get('course_stream', {}).get('paused') else
                                  'running' if record.get('status') in {'processing', 'active'} else 'interrupted')
            state = 'completed' if archived else pending_state(states)
            periods.append({'video_id': ident, 'period': str(video.get('NodeName') or ''),
                            'state': state, 'label': LABELS[state]})
        completed = sum(period['state'] == 'completed' for period in periods)
        if periods and completed == len(periods):
            state = 'completed'
        else:
            state = pending_state({period['state'] for period in periods})
            if completed and state in {'new', 'removed'}:
                state = 'partial'
        detail = '；'.join(f"第 {period['period']} 节：{period['label'] or '未入库'}" for period in periods)
        result[group['id']] = {'state': state, 'label': LABELS[state], 'detail': detail,
                               'completed': completed, 'total': len(periods), 'periods': periods}
        ids = set(video_ids(group))
        warnings = list(dict.fromkeys(warning for entry in history
            if entry['id'] == group['id'] or ids.intersection(entry.get('video_ids') or [])
            for warning in entry.get('warnings', [])))
        if warnings:
            result[group['id']]['warnings'] = warnings
            result[group['id']]['detail'] += '；' + '；'.join(warnings)
    return result
