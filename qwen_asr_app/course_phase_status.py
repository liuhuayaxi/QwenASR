"""Project durable lesson stages without changing recordings or queue history."""
from __future__ import annotations
import copy


def lesson_phases(entry, record=None):
    phases=copy.deepcopy(entry.get('phases') or {})
    if record is None:return phases
    from .refinement import saved_progress
    from .transcription_assist.routes import active_candidates
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
    pending=len(active_candidates(record))
    if 'correction' in phases:
        done=asr_done and progress['remaining_segments']==0
        put('correction','首轮文字修正',100 if done else phases['correction'].get('percent',0),
            'completed' if done else phases['correction'].get('state','pending'),
            f"已保存 {progress['saved_segments']}/{progress['total_segments']} 段")
        state=record.metadata.get('correction_verification') or {}
        retrying=entry.get('state') in {'running','queued'} and stream.get('stage')!='interrupted'
        failed=not retrying and (state.get('status')=='failed' or (done and pending and stream.get('error')))
        from .note_content import transcript_blocks
        checked=max(int(state.get('completed') or 0),sum(bool(b.get('ai_verification')) for b in transcript_blocks(record)))
        total=max(checked+pending,int(state.get('total') or 0))
        if not pending and done:
            put('verification','独立文字复核',100,'completed','修改已复核，无法确认的内容保留原文')
        else:
            put('verification','独立文字复核',100*checked/max(1,total),'error' if failed else state.get('status','pending'),
                f'已复核 {checked}/{total} 条修改；剩余 {pending} 条'+('，继续未完成复核' if failed else ''))
    return phases
