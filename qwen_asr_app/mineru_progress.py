"""Observed MinerU stages; no fabricated total percentage or page position.

The standalone node ships the same parser in client/mineru_progress.py.
"""
import re

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
WINDOW = re.compile(r'Hybrid processing window\s+(\d+)/(\d+):\s+pages\s+(\d+)-(\d+)/(\d+)')
PHASES = {
    'Layout Predict': '版面分析', 'Table orientation det': '表格方向检测',
    'Table orientation rec': '表格方向文字识别', 'Table orientation': '表格方向识别',
    'MFR Predict': '公式识别', 'OCR-det Predict': '文字区域检测', 'OCR-det': '文字区域检测',
    'OCR-rec Predict': '文字识别', 'Table-wireless Predict': '无线表格识别',
    'Table-wired Predict': '有线表格识别', 'Processing pages': '逐页整理',
    'Predict': '视觉内容识别', 'VLM Predict': '视觉内容识别',
    'Loading weights': '加载模型权重', 'Loading safetensors checkpoint shards': '加载模型分片',
}
BAR = re.compile(r'(?<![\w-])(' + '|'.join(re.escape(k) for k in sorted(PHASES, key=len, reverse=True))
                 + r'):\s*(\d+)%([^\r\n]*)')
STAGES = {
    'prepare': '准备文档', 'parse': '文档分析', 'save': '保存结构化结果',
    'content_list': '生成逐页内容映射', 'done': '结构化结果已保存',
    'pdf.prepare': '准备页面与模型', 'pdf.render': '渲染页面', 'pdf.layout': '版面分析',
    'pdf.table_orientation': '表格方向识别', 'pdf.native_tables': '提取原生表格',
    'pdf.formulas': '公式识别', 'pdf.table_recognition': '表格识别',
    'pdf.formula_numbers': '匹配公式编号', 'pdf.ocr_detection': '文字区域检测',
    'pdf.ocr_recognition': '文字识别', 'pdf.text_fill': '回填原文与行信息',
    'pdf.seals': '印章识别', 'pdf.image_assets': '提取图片',
    'pdf.geometry': '整理页面坐标', 'pdf.cleanup': '释放文档资源',
}
MARKER = re.compile(r'QWENASR_STAGE ([\w.]+)')


def mineru_progress(text):
    text = ANSI.sub('', text)
    windows = list(WINDOW.finditer(text))
    window = windows[-1] if windows else None
    hint = ''
    if window:
        hint = f' · 第 {window[3]}–{window[4]} 页/{window[5]} · 窗口 {window[1]}/{window[2]}'
    candidates = []
    if window:
        candidates.append((window.start(), '处理当前页段' + hint))
    for m in BAR.finditer(text):
        if window and m.start() < window.end():
            continue
        percent = int(m[2])
        if percent > 100:
            continue
        count = re.search(r'(\d+)\s*/\s*(\d+)\s*\[', m[3])
        label = PHASES[m[1]] + f' · 阶段 {percent}%'
        if count:
            label += f' · {count[1]}/{count[2]}'
        timing = re.search(r'\[([\d:]+)<([^,\]]+)', m[3])
        if timing:
            label += f' · 已用 {timing[1]}'
            if timing[2].strip() != '?':
                label += f' · 阶段剩余 {timing[2].strip()}'
        candidates.append((m.start(), label + (hint if 'Loading' not in m[1] else '')))
    for m in MARKER.finditer(text):
        if m[1] in STAGES:
            candidates.append((m.start(), STAGES[m[1]] + (hint if m[1].startswith('pdf.') else '')))
    for m in re.finditer(r'model_list infer finished, file_suffix=\w+, pages=(\d+)', text):
        candidates.append((m.start(), f'页面分析完成 · {m[1]} 页 · 整理文档结构'))
    if not candidates:
        return None
    return 'MinerU · ' + max(candidates, key=lambda x: x[0])[1], None
