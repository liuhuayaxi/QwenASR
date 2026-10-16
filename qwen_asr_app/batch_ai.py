from __future__ import annotations

import json
import asyncio
import hashlib
import re
import logging
from datetime import datetime
from typing import Any, AsyncIterator

from .ai import (
    AIRequestError,
    context_window_for,
    concurrency_for,
    output_limit_for,
    uses_agy_gemini,
    agy_rewrite_source_budget,
    polish_and_translate,
    polish_timestamped_segments_resilient,
    retry_transient_ai,
    summarize_note_abstract,
    summarize_transcript,
    summary_instructions,
)
from .context_budget import chunk_text_blocks, estimate_tokens, context_safe_source_budget
from .note_content import transcript_blocks
from .storage import RecordingRecord, format_clock, local_now


logger = logging.getLogger(__name__)


def _needs_translation(config: dict[str, Any]) -> bool:
    return str(config.get("translation", "off")) != "off"


def segment_ai_complete(segment: dict[str, Any], config: dict[str, Any]) -> bool:
    if config.get("ai_correction") and not str(segment.get("corrected") or "").strip():
        return False
    if _needs_translation(config) and not str(segment.get("translation") or "").strip():
        return False
    return True


async def correct_batch_segment(
    record: RecordingRecord,
    segment: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Correct one ASR block immediately and persist it before the next block."""
    ai_config = json.loads(json.dumps(config))
    ai_config["_batch_segment_mode"] = True
    from .transcription_assist.service import evidence_config
    ai_config = await evidence_config(record, ai_config, str(segment.get('raw') or ''))
    previous = [
        str(item.get("corrected") or item.get("raw") or "")
        for item in record.segments
        if int(item.get("id", 0)) < int(segment.get("id", 0))
    ]
    ai_options = ai_config.get("ai") if isinstance(ai_config.get("ai"), dict) else {}
    try:
        history_chars = max(0, int(ai_options.get("history_chars", 2000)))
    except (TypeError, ValueError):
        history_chars = 2000
    joined_history = "\n".join(previous)
    ai_config["_transcript_context"] = joined_history[-history_chars:] if history_chars else ""
    try:
        enrichment = await retry_transient_ai(
            lambda: polish_and_translate(
                str(segment.get("raw") or ""),
                str(segment.get("language") or ""),
                ai_config,
            )
        )
        segment.update(enrichment)
        segment.pop("ai_error", None)
    except Exception as exc:
        segment["ai_error"] = str(exc)
    segment["ai_pending"] = False
    record.upsert_segment(segment)
    return segment


async def correct_batch_segments(
    record: RecordingRecord,
    segments: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Refine a continuous source batch in one request, keeping exact timestamps."""
    ai_config = json.loads(json.dumps(config))
    ai_config["_preserve_segment_boundaries"] = True
    ai_config["_batch_segment_mode"] = True
    from .transcription_assist.service import evidence_config
    ai_config = await evidence_config(record, ai_config, '\n'.join(str(s.get('raw') or '') for s in segments))
    from .refinement import context_for,commit_blocks
    ai_config["_transcript_context"] = context_for(record,segments,context_window_for(config,'correction'))
    corrected = {}
    candidate_by_id = {}
    provenance_by_id = {}
    error = None
    installed=set()
    saved_blocks={}
    def checkpoint(blocks):
        expected={int(s['id']) for s in segments}
        if (any(len(b.get('segment_ids',[]))!=1 or b['segment_ids'][0] not in expected
                or not str(b.get('corrected') or '').strip() for b in blocks)
                or len({b['segment_ids'][0] for b in blocks})!=len(blocks)):
            raise AIRequestError('批量精修的段落编号不完整，原文已保留',kind='invalid_segment_mapping')
        fresh=[b for b in blocks if b['segment_ids'][0] not in installed]
        if not fresh:return
        commit_blocks(record,fresh,destination='segments')
        for block in fresh:
            ident=block['segment_ids'][0]
            saved_blocks[ident]=block
            installed.add(ident)
    try:
        async def correct_remaining():
            remaining=[s for s in segments if int(s['id']) not in installed]
            if remaining:
                result=await polish_timestamped_segments_resilient(remaining,str(segments[0].get('language') or ''),
                    ai_config,mode='correction',on_checkpoint=checkpoint)
                # Validate mock/legacy adapters before accepting their response.
                expected=[int(s['id']) for s in remaining]
                actual=[int(b['segment_ids'][0]) for b in result if len(b.get('segment_ids',[]))==1]
                if actual!=expected:raise AIRequestError('批量精修的段落编号不完整，原文已保留',kind='invalid_segment_mapping')
                checkpoint(result)
            return [saved_blocks[int(s['id'])] for s in segments]
        blocks = await retry_transient_ai(correct_remaining)
        expected = [int(segment["id"]) for segment in segments]
        actual = [int(block["segment_ids"][0]) for block in blocks
                  if len(block.get("segment_ids", [])) == 1 and str(block.get("corrected") or "").strip()]
        if actual != expected or len(blocks) != len(expected):
            raise AIRequestError("批量精修的段落编号不完整，原文已保留", kind="invalid_segment_mapping")
        checkpoint(blocks)
        corrected = {int(block["segment_ids"][0]): str(block["corrected"]).strip() for block in blocks}
        candidate_by_id = {int(block['segment_ids'][0]):{**block['correction_review'],'segment_ids':block['segment_ids']} for block in blocks if block.get('correction_review')}
        provenance_by_id = {int(block['segment_ids'][0]):block['correction_provenance'] for block in blocks if block.get('correction_provenance')}
    except Exception as exc:
        error = str(exc)
    source_by_id = {int(segment["id"]): segment for segment in segments}
    updated = []
    for current in record.segments:
        ident = int(current.get("id", 0))
        if ident not in source_by_id:
            continue
        if ident in installed:
            updated.append(current)
            continue
        current.update(source_by_id[ident], ai_pending=False)
        if error is not None:
            current["ai_error"] = error
        else:
            current["corrected"] = corrected[ident]
            if ident in candidate_by_id:
                current['correction_review']=candidate_by_id[ident]
            if ident in provenance_by_id:
                current['correction_provenance']=provenance_by_id[ident]
            current.pop("ai_error", None)
        updated.append(current)
    record.metadata["updated_at"] = local_now()
    record.save_all()
    return updated


def _source_blocks(record: RecordingRecord) -> list[str]:
    return [
        f"[{format_clock(float(item.get('start', 0)))}–{format_clock(float(item.get('end', 0)))}]\n"
        f"{item.get('corrected') or item.get('raw') or ''}"
        for item in record.segments
        if str(item.get("corrected") or item.get("raw") or "").strip()
    ]


def global_source_groups(record,config):
    """Batch complete segment objects; grouping never changes output ownership."""
    window=context_window_for(config,'global')
    budget=min(context_safe_source_budget(window,fixed_overhead=1800),
        agy_rewrite_source_budget(config,'global') if uses_agy_gemini(config,'global') else 3500)
    groups=[];current=[];used=0
    for segment in record.segments:
        if segment.get('ai_suppressed') or not str(segment.get('corrected') or segment.get('raw') or '').strip():continue
        cost=estimate_tokens(str(segment.get('corrected') or segment.get('raw') or ''))+24
        if current and used+cost>budget:groups.append(current);current=[];used=0
        current.append(segment);used+=cost
    if current:groups.append(current)
    return groups


async def global_rewrite_events(
    record: RecordingRecord,
    config: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """Run a context-safe global pass and persist each recoverable part."""
    context_window = context_window_for(config, "global")
    groups=global_source_groups(record,config)
    chunks=['\n\n'.join(f"[{format_clock(float(s.get('start',0)))}–{format_clock(float(s.get('end',0)))}]\n"+
        str(s.get('corrected') or s.get('raw') or '') for s in group) for group in groups]
    detected_language = next(
        (
            str(item.get("language") or "")
            for item in record.segments
            if str(item.get("language") or "").strip()
        ),
        "",
    )
    from .transcription_assist.service import evidence_config
    global_config = await evidence_config(record, config, '\n'.join(str(s.get('raw') or '') for s in record.segments)[-5000:])
    global_config["_global_mode"] = True
    fingerprint = hashlib.sha256(
        json.dumps(['per-segment-global-v1',groups,detected_language,global_config], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    previous = record.metadata.get("ai_global_rewrite") or {}
    if previous and previous.get('fingerprint')!=fingerprint:
        from .durable import atomic_json
        old_hash=hashlib.sha256(json.dumps(previous,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        atomic_json(record.directory/'.refinement-history'/('global-before-'+old_hash+'.json'),previous)
    previous_parts = previous.get("parts") if previous.get("fingerprint") == fingerprint else []
    saved_by_index = {
        int(part["id"]) - 1: dict(part)
        for part in previous_parts or []
        if isinstance(part, dict)
        and isinstance(part.get("id"), int)
        and 1 <= int(part["id"]) <= len(chunks)
    }
    parts = [saved_by_index.get(index) for index in range(len(chunks))]
    global_result: dict[str, Any] = {
        "fingerprint": fingerprint,
        "context_window": context_window,
        "parts": [part for part in parts if part is not None],
        "text": "",
        "translation": "",
        "complete": False,
    }
    record.metadata["ai_global_rewrite"] = global_result
    record.save_all()

    semaphore = asyncio.Semaphore(concurrency_for(config, "global"))
    async def process(index, chunk):
        group=groups[index];expected=[int(s['id']) for s in group]
        previous_part=parts[index] or {}
        if (previous_part and not previous_part.get('ai_error')
                and [b.get('segment_ids') for b in previous_part.get('blocks',[])]==[[ident] for ident in expected]):
            return index, parts[index], True
        async with semaphore:
            ai_config = json.loads(json.dumps(global_config))
            ai_config["_transcript_context"] = chunks[index - 1][-2000:] if index else ""
            by_id={int(s['id']):s for s in group}
            saved={b['segment_ids'][0]:b for b in previous_part.get('blocks',[])
                if len(b.get('segment_ids',[]))==1 and b['segment_ids'][0] in by_id and str(b.get('corrected') or '').strip()}
            part = {"id": index + 1, "raw": chunk,"segment_ids":expected}
            def checkpoint(blocks):
                block_ids=[b.get('segment_ids') or [] for b in blocks]
                if (any(len(ids)!=1 or ids[0] not in by_id for ids in block_ids)
                        or len({ids[0] for ids in block_ids})!=len(block_ids)
                        or any(not str(b.get('corrected') or '').strip() for b in blocks)):
                    raise AIRequestError('全局整理未保留逐段映射',kind='invalid_segment_mapping')
                for block in blocks:
                    ids=block.get('segment_ids') or []
                    saved[ids[0]]={**block,'start':by_id[ids[0]]['start'],'end':by_id[ids[0]]['end']}
                part['blocks']=[saved[ident] for ident in expected if ident in saved]
                part['corrected']='\n\n'.join(b['corrected'] for b in part['blocks'])
                if any(b.get('translation') for b in part['blocks']):part['translation']='\n\n'.join(b.get('translation','') for b in part['blocks'])
                part['ai_error']='本批还有未完成片段'
                parts[index]=part;persist_global_parts()
            try:
                remaining=[s for s in group if int(s['id']) not in saved]
                mapped=await polish_timestamped_segments_resilient(remaining,detected_language,ai_config,mode='global',on_checkpoint=checkpoint) if remaining else []
                if ([b.get('segment_ids') for b in mapped]!=[[int(s['id'])] for s in remaining]
                        or any(not str(b.get('corrected') or '').strip() for b in mapped)):
                    raise AIRequestError('全局整理未保留逐段映射',kind='invalid_segment_mapping')
                checkpoint(mapped);part.pop('ai_error',None)
            except Exception as exc:
                part["ai_error"] = str(exc)
            parts[index] = part
            return index, part, False
    tasks = [asyncio.create_task(process(index, chunk)) for index, chunk in enumerate(chunks)]
    def persist_global_parts() -> None:
        global_result["parts"] = [part for part in parts if part is not None]
        global_result["text"] = "\n\n".join(
            str(part.get("corrected") or part["raw"]) for part in global_result["parts"]
        )
        global_result["translation"] = "\n\n".join(
            str(part["translation"])
            for part in global_result["parts"]
            if part.get("translation")
        )
        record.save_all()

    try:
        for completed, task in enumerate(asyncio.as_completed(tasks), 1):
            index, part, reused = await task
            persist_global_parts()
            yield {"type": "ai_global_part", "part": part, "index": index + 1,
                   "completed": completed, "count": len(chunks), "reused": reused}
    finally:
        # A cancellation can arrive after a worker finished but before the
        # as_completed iterator receives it. Collect those successful results
        # before stopping the remaining workers so the next retry only fills
        # genuinely missing parts.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            if task.cancelled():
                continue
            try:
                index, part, _ = task.result()
            except (Exception, asyncio.CancelledError):
                continue
            parts[index] = part
        persist_global_parts()

    # Each bounded part already receives a global editing pass. A second pass
    # over the same result adds latency and can shorten or damage good output.
    global_result["single_final_pass"] = len(chunks) <= 1
    global_result["context_split"] = len(chunks) > 1
    global_result["complete"] = all(not part.get("ai_error") for part in global_result["parts"])
    record.metadata["ai_global_rewrite"] = global_result
    record.save_all()
    yield {"type": "ai_global_complete", "global_rewrite": global_result}


def note_summary_chunks(blocks: list[str], config: dict[str, Any]) -> list[str]:
    """Prefer one complete input, reserving instructions, JSON and output space."""
    full_text = "\n\n".join(blocks).strip()
    if not full_text:
        return []
    window = context_window_for(config, "summary")
    output_reserve = output_limit_for(config, "summary", 32_768)
    # API-compatible providers need a conservative source/output balance.
    # AGY Gemini exposes a substantially larger context window, so let the
    # actual request-size check below use that window instead of forcing every
    # long lecture through an arbitrary ~1.5x-output slice.
    if uses_agy_gemini(config, "summary"):
        source_output_budget = max(512, window - output_reserve - 2_000)
    else:
        source_output_budget = max(512, int((output_reserve - 512) * 1.5))

    def request_size(text: str, *, full: bool) -> int:
        options = {**config, "_full_note": full, "_detailed_chunk": not full}
        payload = json.dumps({"previous_cumulative_summary": None, "new_transcript": text}, ensure_ascii=False)
        return estimate_tokens(summary_instructions(options)) + estimate_tokens(payload) + output_reserve + 320

    def fits_cli_input(text: str, *, full: bool) -> bool:
        if not uses_agy_gemini(config, "summary"):
            return True
        from .agy_cli import compose_prompt
        options = {**config, "_full_note": full, "_detailed_chunk": not full}
        return estimate_tokens(compose_prompt(summary_instructions(options), {
            "previous_cumulative_summary": None, "new_transcript": text,
        })) + 16000 <= window

    # Match the actual request serialization rather than counting only raw text.
    # Full-context input remains preferred, but do not choose it when a
    # faithful detailed response is predictably larger than the provider's
    # output cap. That previously produced a permanent finish_reason=length.
    try:
        forced_chunk_budget = int(config.get("_summary_force_chunk_tokens") or 0)
    except (TypeError, ValueError):
        forced_chunk_budget = 0
    if forced_chunk_budget > 0:
        source_output_budget = min(source_output_budget, max(512, forced_chunk_budget))
    if not forced_chunk_budget and estimate_tokens(full_text) <= source_output_budget and request_size(full_text, full=True) <= window and fits_cli_input(full_text, full=True):
        return [full_text]
    overhead = request_size("", full=False) + 64
    if overhead + 128 >= window:
        raise ValueError("总结提示词占用过多上下文，请缩短提示词或提高长上下文窗口")
    budget = min(window - overhead, source_output_budget)
    while True:
        chunks = chunk_text_blocks(blocks, window, max_source_tokens=budget,
                                   fixed_overhead=overhead, use_full_window=True)
        if all(request_size(chunk, full=False) <= window and fits_cli_input(chunk, full=False) for chunk in chunks):
            return chunks
        if budget <= 128:
            raise ValueError("总结内容在 JSON 编码后超过上下文窗口，请调整窗口设置")
        budget = max(128, budget // 2)


async def build_note_summary(
    record: RecordingRecord,
    config: dict[str, Any],
) -> dict[str, Any]:
    from .ai import provider_for_mode
    provider = provider_for_mode(config.get('ai', {}), 'summary')
    if provider.get('transport') == 'agy_cli':
        from .review_notes import generate
        return await generate(record, config)
    result = await _build_text_note_summary(record, config)
    result['review_status'] = 'unreviewed'
    result['review_warning'] = '当前提供商没有已验证的图文审查能力；完整 Markdown 已保存，未完成视觉审查'
    record.save_metadata()
    return result


async def _build_text_note_summary(
    record: RecordingRecord,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Generate the user-requested post-recording Markdown summary.

    This is stored separately from live summaries so only this editable result can
    ever participate in IdeaShell synchronization.
    """
    source_blocks = transcript_blocks(record)
    source_map = {str(index): {"start": block['start'], "end": block['end'], "segment_ids": block.get('segment_ids', [])}
                  for index, block in enumerate(source_blocks, 1)}
    blocks = [f"[来源 S{index}] [{format_clock(block['start'])}–{format_clock(block['end'])}]\n{block['text']}"
              for index, block in enumerate(source_blocks, 1)]
    from .transcription_assist.service import evidence_config
    from .classroom_intelligence.integrations import saved_notes_markdown
    chunk_config = json.loads(json.dumps(config))
    evidence = {}
    for block in source_blocks:
        part_config = await evidence_config(record, config, block['text'], limit=4)
        for item in part_config.get('_knowledge_evidence', []):
            evidence[item['citation_id']] = item
    if evidence:
        chunk_config['_knowledge_evidence'] = list(evidence.values())
    accepted_notes=saved_notes_markdown(record)
    if accepted_notes:
        from .transcription_assist.service import attach_accepted_evidence
        chunk_config=await attach_accepted_evidence(record,chunk_config)
        chunk_config['_accepted_classroom_notes'] = accepted_notes
    window = context_window_for(chunk_config, 'summary')
    chunks = note_summary_chunks(blocks, chunk_config)
    if not chunks:
        raise ValueError('录音还没有可用于生成总结的转写内容')
    chunk_config['_detailed_chunk'] = len(chunks)>1
    chunk_config['_full_note'] = len(chunks)==1
    # Low verbosity is appropriate for the library abstract, not lecture notes.
    provider = chunk_config.setdefault("ai", {}).setdefault("summary_provider", {})
    extra = provider.get("extra_params")
    if isinstance(extra, str):
        extra = json.loads(extra)
    extra = dict(extra or {})
    extra["text"] = {**(extra.get("text") if isinstance(extra.get("text"), dict) else {}), "verbosity": "high"}
    provider["extra_params"] = extra
    fingerprint = hashlib.sha256(json.dumps([chunks, chunk_config], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    draft = record.metadata.get("ai_note_summary_draft") or {}
    if draft.get("fingerprint") != fingerprint:
        draft = {"fingerprint": fingerprint, "parts": []}
    draft.update(total_parts=len(chunks), phase="全文笔记" if len(chunks) == 1 else "分块详细笔记")
    record.metadata["ai_note_summary_draft"] = draft
    record.save_metadata()
    saved_parts = draft["parts"]
    if isinstance(saved_parts, list):
        saved_parts = {str(index): text for index, text in enumerate(saved_parts)}
        draft["parts"] = saved_parts
    semaphore = asyncio.Semaphore(concurrency_for(config, "summary"))
    async def summarize_part(index, chunk):
        if str(index) in saved_parts:
            return
        async with semaphore:
            result = await summarize_transcript(chunk, "", chunk_config, provider_mode="summary")
            saved_parts[str(index)] = str(result["summary"])
            record.save_metadata()
    tasks = [asyncio.create_task(summarize_part(index, chunk)) for index, chunk in enumerate(chunks)]
    generation_error: BaseException | None = None
    try:
        await asyncio.gather(*tasks)
    except BaseException as exc:
        generation_error = exc
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if generation_error is not None:
        error_kind = str(getattr(generation_error, "kind", ""))
        if isinstance(generation_error, AIRequestError) and error_kind in {
            "output_truncated", "output_incomplete", "input_too_long",
        }:
            # Some compatible providers enforce a smaller real input/output
            # ceiling than their advertised context. Recursively halve the
            # source budget; completed parts remain durable between attempts.
            try:
                previous_budget = int(config.get("_summary_force_chunk_tokens") or 0)
                retry_depth = int(config.get("_summary_chunk_retry_depth") or 0)
            except (TypeError, ValueError):
                previous_budget, retry_depth = 0, 0
            if retry_depth >= 5:
                raise generation_error
            current_largest = max(estimate_tokens(chunk) for chunk in chunks)
            next_budget = (
                max(400, min(3_000, current_largest // 3))
                if previous_budget <= 0
                else max(400, previous_budget // 2)
            )
            if previous_budget and previous_budget <= 400:
                raise generation_error
            retry_config = json.loads(json.dumps(config))
            retry_config["_summary_force_chunk_tokens"] = next_budget
            retry_config["_summary_chunk_retry_depth"] = retry_depth + 1
            record.metadata.pop("ai_note_summary_draft", None)
            record.save_metadata()
            return await build_note_summary(record, retry_config)
        raise generation_error
    knowledge, formulas, details = [], [], []
    for index, chunk in enumerate(chunks):
        markdown = saved_parts[str(index)]
        label = f"片段 {index + 1}"
        times = re.findall(r"\[([0-9:]+)–([0-9:]+)\]", chunk)
        if times:
            label = f"{times[0][0]}–{times[-1][1]}"
        sections = re.split(r"^##\s+(知识点速览|公式速览|按时间展开)\s*$", markdown, flags=re.M)
        parsed = dict(zip(sections[1::2], sections[2::2]))
        if len(parsed) == 3:
            knowledge.append(f"### {label}\n\n{parsed['知识点速览'].strip()}")
            formulas.append(f"### {label}\n\n{parsed['公式速览'].strip()}")
            details.append(f"### {label}\n\n{parsed['按时间展开'].strip()}")
        else:
            # Preserve an unexpected response verbatim; make the missing index
            # visible instead of silently dropping any lecture content.
            knowledge.append(f"- {label}：模型未按速览格式输出，详见该时间段正文。")
            formulas.append(f"- {label}：请参阅正文中的公式。")
            details.append(f"### {label}\n\n{markdown}")
    rolling_summary = ("## 知识点速览\n\n" + "\n\n".join(knowledge)
                       + "\n\n## 公式速览\n\n" + "\n\n".join(formulas)
                       + "\n\n## 按课堂时间展开\n\n" + "\n\n".join(details))
    if len(chunks) == 1:
        rolling_summary = saved_parts["0"]
    key_points: list[str] = []
    started_at = str(record.metadata.get("started_at") or "")
    try:
        started = datetime.fromisoformat(started_at)
        date_text = started.strftime("%Y-%m-%d")
        time_text = started.strftime("%H:%M")
    except (TypeError, ValueError):
        date_text = started_at[:10]
        time_text = started_at[11:16]
    course = str(record.metadata.get("category") or "未分类")
    title = str(record.metadata.get("title") or "课堂录音")
    metadata_block = (
        f"# {title}\n\n"
        f"- 课程：{course}\n"
        f"- 日期：{date_text or '未知'}\n"
        f"- 时间：{time_text or '未知'}\n\n"
    )
    rolling_summary = metadata_block + rolling_summary.lstrip()
    rolling_summary = re.sub(r'\[([^\]]+)\]\(#source-(\d+)\)',
                             lambda match: match.group(0) if match.group(2) in source_map else match.group(1) + '（来源待核对）', rolling_summary)
    from .transcription_assist.service import validate_knowledge_answer
    rolling_summary, knowledge_validation = await asyncio.to_thread(validate_knowledge_answer,rolling_summary,chunk_config)
    knowledge_map={e['citation_id']:e for e in chunk_config.get('_knowledge_evidence',[])}
    rolling_summary=re.sub(r'\[(K[A-Za-z0-9_-]+)\](?!\()',
                           lambda m:'['+m[1]+']('+knowledge_map[m[1]]['open_url']+')' if m[1] in knowledge_map else m[0],rolling_summary)
    now = local_now()
    previous = record.metadata.get("ai_note_summary")
    item = {
        "summary": rolling_summary,
        "abstract": "",
        "key_points": key_points,
        "parts": len(chunks),
        "context_window": window,
        "input_mode": "full" if len(chunks) == 1 else "chunked",
        "created_at": previous.get("created_at", now) if isinstance(previous, dict) else now,
        "updated_at": now,
        "source": "post_recording",
        "source_map": source_map,
        "knowledge_source_map": knowledge_map,
        "knowledge_source_validation": knowledge_validation,
        "student_notes_included": bool(accepted_notes),
    }
    draft["phase"] = "录音库短摘要"
    # Persist the detailed Markdown before the independent short-abstract call.
    # A failed abstract must never discard a usable full summary.
    record.metadata["ai_note_summary"] = item
    sync = record.metadata.get("ideashell_sync")
    if isinstance(sync, dict) and sync.get("note_id"):
        sync["status"] = "local_changes"
    record.save_all()
    try:
        item["abstract"] = await summarize_note_abstract(rolling_summary, config)
        item.pop("abstract_error", None)
    except Exception as exc:
        item["abstract_error"] = str(exc)
        logger.error(
            "AI note abstract failed recording=%s; detailed summary was preserved: %s",
            record.id,
            exc,
        )
    item["updated_at"] = local_now()
    record.metadata.pop("ai_note_summary_draft", None)
    record.metadata["ai_note_summary"] = item
    record.save_all()
    return item


async def regenerate_note_abstract(
    record: RecordingRecord,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Regenerate only the 100-character library abstract for a saved summary."""
    previous = record.metadata.get("ai_note_summary")
    if not isinstance(previous, dict) or not str(previous.get("summary") or "").strip():
        raise ValueError("请先生成详细 AI 总结，再重新生成录音库短摘要")
    item = dict(previous)
    try:
        item["abstract"] = await summarize_note_abstract(str(item["summary"]), config)
        item.pop("abstract_error", None)
    except Exception as exc:
        item["abstract_error"] = str(exc)
        logger.error("AI note abstract retry failed recording=%s: %s", record.id, exc)
        item["updated_at"] = local_now()
        record.metadata["ai_note_summary"] = item
        record.save_all()
        raise RuntimeError(f"录音库短摘要生成失败：{exc}") from exc
    item["updated_at"] = local_now()
    record.metadata["ai_note_summary"] = item
    record.save_all()
    return item
