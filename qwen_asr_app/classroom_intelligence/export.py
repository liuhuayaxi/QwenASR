"""Markdown export for classroom scribbles and enhanced notes."""
from __future__ import annotations

from typing import Any

from ..note_content import clock
from ..storage import local_now
from .store import read_notes_doc


def export_notes_markdown(record: Any, base_url: str = '') -> str:
    """Generate structured Markdown download for the recording's notes."""
    doc = read_notes_doc(record)
    notes = doc.get('notes', [])

    title = str(record.metadata.get('title') or getattr(record, 'id', '') or '未命名课堂')
    category = str(record.metadata.get('category') or '未分类')
    date_str = str(record.metadata.get('started_at') or '')
    duration = float(getattr(record, 'duration', 0) or 0)

    lines: list[str] = [
        f"# {title} - 课堂速记与整理笔记",
        "",
        f"- **课程**：{category}",
        f"- **录音时长**：{clock(duration)}",
        f"- **导出时间**：{local_now()}",
    ]
    if date_str:
        lines.append(f"- **上课时间**：{date_str}")
    lines.extend(["", "---", ""])

    if not notes:
        lines.append("*（本节课尚未记录速记）*")
        return "\n".join(lines) + "\n"

    anchor_names = {'current': '当前课堂', 'previous': '上一段讲解', 'manual': '手动指定'}

    for idx, note in enumerate(notes, 1):
        s = clock(float(note.get('start') or 0))
        e = clock(float(note.get('end') or 0))
        anchor_label = anchor_names.get(note.get('anchor', 'current'), note.get('anchor', ''))
        locked_badge = " · [已锁定]" if note.get('locked') else ""

        lines.append(f"## {idx}. [{s}–{e}] （{anchor_label}）{locked_badge}")
        lines.append("")
        lines.append(f"**速记原文**：")
        lines.append(f"> {note.get('raw', '')}")
        lines.append("")
        lines.append(f"**整理笔记**：")
        lines.append(note.get('content', ''))
        lines.append("")

        # Include verified evidence if present
        evidence = note.get('evidence') or []
        if evidence:
            lines.append("**参考与依据**：")
            for ev in evidence:
                cid = ev.get('citation_id', '')
                source_link=(' · [打开来源]('+base_url.rstrip('/')+ev['open_url']+')') if str(ev.get('open_url') or '').startswith('/') else ''
                if ev.get('type') == 'classroom':
                    lines.append(f"- [{cid}] 本节定稿转写 [{ev.get('time', '')}]: \"{ev.get('quote') or ev.get('text', '')}\"{source_link}")
                else:
                    page_str = f"第 {ev.get('page')} 页" if ev.get('page') else ""
                    head_str = f"（{ev.get('heading')}）" if ev.get('heading') else ""
                    lines.append(f"- [{cid}] 课程资料《{ev.get('title', '参考资料')}》{page_str}{head_str}: \"{ev.get('quote') or ev.get('text', '')}\"{source_link}")
            lines.append("")

        # If suggestion is pending or present
        if note.get('suggestion') and not note.get('suggestion', {}).get('stale'):
            sug = note['suggestion']
            lines.append("> [!TIP]")
            lines.append("> **待确认的 AI 建议**：")
            for line in sug.get('content', '').splitlines():
                lines.append(f"> {line}")
            lines.append("")

        lines.extend(["---", ""])

    return "\n".join(lines).strip() + "\n"
