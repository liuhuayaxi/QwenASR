#!/usr/bin/env python3
"""从 VGA 录屏提取最终稳定画面，过滤桌面及重复画面并生成分节课件 PDF。"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qwen_asr_app.vga_slides import extract_slides
from qwen_asr_app.vga_filter import filter_slides, write_filtered_pdf
from qwen_asr_app.vga_emphasis import apply_emphasis


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video')
    parser.add_argument('-o', '--output', required=True)
    parser.add_argument('--manifest')
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument('--audio', help='本机录音路径，使用已保存的 ASR 设置转写')
    sources.add_argument('--transcript', help='已有转写 JSON 或带时间戳的文本路径')
    parser.add_argument('--category', default='未分类', help='录音归档的课程名称')
    parser.add_argument('--no-emphasis', action='store_true', help='不根据录音标记重点页')
    for name, default, kind in (
        ('sample-interval', 0.5, float), ('min-stable-sec', 1.0, float),
        ('comparison-width', 320, int), ('pixel-threshold', 20, int),
        ('stable-threshold', 0.01, float), ('animation-threshold', 0.3, float),
        ('retention-threshold', 0.1, float), ('duplicate-threshold', 0.01, float),
        ('blank-std', 3.0, float), ('black-threshold', 12, int)):
        parser.add_argument('--' + name, default=default, type=kind)
    parser.add_argument('--keep-revisited', action='store_true')
    parser.add_argument('--no-filter', action='store_true', help='关闭过滤，保留原始提取结果')
    parser.add_argument('--no-homework', action='store_true', help='过滤时不保留作业讲解部分')
    args = vars(parser.parse_args())
    video, output, manifest = (args.pop(key) for key in ('video', 'output', 'manifest'))
    args['skip_revisited'] = not args.pop('keep_revisited')
    enabled, keep_homework = not args.pop('no_filter'), not args.pop('no_homework')
    emphasis = {key: args.pop(key) for key in ('audio', 'transcript', 'category')}
    emphasis['enabled'] = not args.pop('no_emphasis')
    try:
        slides = extract_slides(video, deduplicate=not enabled, **args)
        filtered = filter_slides(slides, enabled=enabled, keep_homework=keep_homework)
        asyncio.run(apply_emphasis(filtered, video, **emphasis, close_runtime=True,
                                   progress=lambda stage: print(stage, flush=True)))
        result = write_filtered_pdf(filtered, output)
        for warning in result['warnings']:
            print('警告：' + warning, file=sys.stderr)
        if manifest:
            Path(manifest).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(f'已保存 {len(result["pages"])} 页课件至 {output}')
    except (ValueError, OSError, KeyboardInterrupt) as exc:
        parser.exit(1, f'提取失败：{exc}\n')


if __name__ == '__main__':
    main()
