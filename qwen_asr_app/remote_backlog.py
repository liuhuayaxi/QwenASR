"""Replay persisted classroom clips after network/process recovery, without recapture."""
import asyncio
import copy
import time
from pathlib import Path
from contextlib import nullcontext


class SilentSocket:
    async def send_json(self, value):pass


async def retry_once(store, manager, live_sessions, unavailable, claim=None, on_completed=None):
    identities=store.remote_backlog_ids() if hasattr(store,'remote_backlog_ids') else [row.id for row in store.list_records()]
    for recording_id in identities:
        if unavailable(recording_id):continue
        live=live_sessions.get(recording_id)
        record=live.recording if live else store.find(recording_id)
        if not record or (record.metadata.get('remote_backlog') or {}).get('status') in {'paused','cancelled'}:continue
        for ident, pending in list(record.metadata.get('pending_segments',{}).items()):
            if pending.get('state')=='processing' and live:continue
            if pending.get('retry_after',0)>time.time():continue
            config=copy.deepcopy(pending.get('config_snapshot') or {})
            if config.get('inference_settings',{}).get('asr_provider') not in {'remote_only','remote_preferred'}:continue
            folder=config.get('_remote_operation_dir')
            if not folder:continue
            source=Path(folder)/'audio.wav'
            if not source.is_file():continue
            from .live_session import TranscriptionSession
            import soundfile as sf
            session=live or TranscriptionSession(SilentSocket(),manager,store)
            session.recording=record
            audio=await asyncio.to_thread(lambda:sf.read(source,dtype='float32')[0])
            # The owner claim prevents another API writer from editing a
            # detached recording while this durable retry is awaiting network.
            with claim(record.id) if claim else nullcontext():
                record.metadata['remote_backlog']={'status':'running','count':len(record.metadata['pending_segments']),
                    'message':f'Windows 补转片段 {ident}，录音已保留'}
                record.save_metadata()
                task=asyncio.create_task(session.run_final(audio,int(ident),config,'network_recovery',record,float(pending['start']),float(pending['end'])))
                if live:session.tasks.add(task)
                try:await task
                finally:
                    if live:session.tasks.discard(task)
                if not live:
                    await session.drain()
                remaining=len(record.metadata.get('pending_segments',{}))
                record.metadata['remote_backlog']={'status':'pending' if remaining else 'completed','count':remaining,
                    'message':'Windows 连接暂不可用，已保存片段等待补转' if remaining else '已补齐全部 Windows 转写片段'}
                record.save_all()
            if not remaining and on_completed:on_completed(record.id)
            # One clip per sweep keeps other course/UI work responsive.
            return True
    return False


async def recovery_loop(store, manager, live_sessions, unavailable, claim=None, on_completed=None, on_idle=None):
    last_idle_cleanup=0
    while True:
        await asyncio.sleep(5)
        try:
            worked=await retry_once(store,manager,live_sessions,unavailable,claim,on_completed)
            if not worked and on_idle and time.monotonic()-last_idle_cleanup>=30:
                on_idle();last_idle_cleanup=time.monotonic()
        except asyncio.CancelledError:raise
        except Exception:await asyncio.sleep(5)
