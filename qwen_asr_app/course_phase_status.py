"""Project durable lesson stages without changing recordings or queue history."""
from __future__ import annotations
import copy


def lesson_phases(entry, record=None):
    phases=copy.deepcopy(entry.get('phases') or {})
    phases.pop('verification', None)
    if record is None:return phases
    from .refinement import saved_progress
    stream=record.metadata.get('course_stream') or {}
    chunks=stream.get('chunks') or []
    asr_done=record.metadata.get('status')=='completed' or bool(
        stream.get('input_complete') and chunks and stream.get('completed_chunks',0)>=len(chunks))
    def put(key,label,percent,state,detail):
        phases[key]={'label':label,'percent':percent,'state':state,'detail':detail}
    if asr_done:
        put('download','获取回放音频',100,'completed','回放音频已保存')
        put('asr','语音转写',100,'completed',f'已保存 {len(record.segments)} 段转写')
    if 'asr' in phases:
        from .asr_resources import execution
        placement, _ = execution(record)
        phases['asr'].update(label=placement['label'], execution=placement)
    progress=saved_progress(record)
    if 'correction' in phases:
        done=asr_done and progress['remaining_segments']==0
        correction_job = (record.metadata.get('ai_jobs') or {}).get('refinement') or {}
        correction_state = ('completed' if done else correction_job.get('status')
                            if correction_job.get('status') in {'paused', 'cancelled'}
                            else 'error' if stream.get('correction_deferred')
                            else 'running' if stream.get('ai_in_background')
                            else phases['correction'].get('state', 'pending'))
        put('correction','AI 文字校对',100 if done else 100*progress['saved_segments']/max(1,progress['total_segments']),
            correction_state, f"已保存 {progress['saved_segments']}/{progress['total_segments']} 段"
            + ('；'+stream['correction_error'] if not done and stream.get('correction_error') else ''))
    return phases
