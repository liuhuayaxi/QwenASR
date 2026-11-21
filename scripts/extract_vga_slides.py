#!/usr/bin/env python3
"""Extract final stable slide states from a VGA recording into a PDF."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qwen_asr_app.vga_slides import extract_slides, write_slides_pdf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video')
    parser.add_argument('-o', '--output', required=True)
    parser.add_argument('--manifest')
    for name, default, kind in (
        ('sample-interval', 0.5, float), ('min-stable-sec', 1.0, float),
        ('comparison-width', 320, int), ('pixel-threshold', 20, int),
        ('stable-threshold', 0.01, float), ('animation-threshold', 0.3, float),
        ('retention-threshold', 0.1, float), ('duplicate-threshold', 0.01, float),
        ('blank-std', 3.0, float), ('black-threshold', 12, int)):
        parser.add_argument('--' + name, default=default, type=kind)
    parser.add_argument('--keep-revisited', action='store_true')
    args = vars(parser.parse_args())
    video, output, manifest = (args.pop(key) for key in ('video', 'output', 'manifest'))
    args['skip_revisited'] = not args.pop('keep_revisited')
    try:
        slides = extract_slides(video, **args)
        result = write_slides_pdf(slides, output)
        if manifest:
            Path(manifest).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(f'Extracted {len(slides)} slides to {output}')
    except (ValueError, OSError, KeyboardInterrupt) as exc:
        parser.exit(1, f'Extraction failed: {exc}\n')


if __name__ == '__main__':
    main()
