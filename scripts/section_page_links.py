#!/usr/bin/env python3
"""Backfill section→courseware page links for selected notes.

Dry-run by default (prints the plan, writes nothing). ``--write`` stores
``local-data/section-page-links/links/<id>.json``; reruns are idempotent and
keep manual verdicts. Notes and PDFs are never modified.

  .venv/bin/python scripts/section_page_links.py --id 20261106-202717-2c1b1e
  .venv/bin/python scripts/section_page_links.py --id ... --write
  .venv/bin/python scripts/section_page_links.py --enable | --disable
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from qwen_asr_app import section_pages  # noqa: E402


def storage_root():
    return Path(json.loads((ROOT / 'app-settings.json').read_text(encoding='utf-8'))['storage_root'])


def find(storage, ident):
    for path in storage.glob('*/上课录音/*/session.json'):
        try: session = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError): continue
        if session.get('id') == ident: return path.parent, session
    raise SystemExit(f'没有找到录音 {ident}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--id', action='append', default=[])
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--enable', action='store_true')
    parser.add_argument('--disable', action='store_true')
    parser.add_argument('--json', action='store_true', help='print full result')
    parser.add_argument('--apply-review', metavar='FILE', help='hand-checked verdicts (groundtruth.json) to record as verified/rejected')
    args = parser.parse_args()
    if args.enable or args.disable:
        section_pages.set_enabled(args.enable)
        print('feature flag:', 'on' if args.enable else 'off')
    storage = storage_root()
    for ident in args.id:
        directory, session = find(storage, ident)
        doc = section_pages.generate(directory, session, storage)
        old = section_pages.load(ident)
        doc = section_pages.merge_verified(doc, old)
        if args.apply_review:
            truth = json.loads(Path(args.apply_review).read_text(encoding='utf-8')).get(ident, [])
            section_pages.apply_review(doc, truth, reviewer='agent-pilot')
        same = old is not None and json.dumps(old, sort_keys=True) == json.dumps(doc, sort_keys=True)
        print(f'== {ident} {session.get("title")}')
        for r in doc['resources']: print('  resource:', r['name'], '→', r.get('viewer_name'), r.get('page_mapping'))
        for s in doc['sections']:
            t = s['targets'][0] if s['targets'] else None
            where = f"P{t['page_start']}" + (f"–{t['page_end']}" if t['page_end'] != t['page_start'] else '') + f" (viewer {t['viewer_page_start']})" if t else ''
            print(f"  [{s['status']:<10}] {'#' * s['level']} {s['heading'][:46]:<46} {where} {s.get('method', s.get('reason', ''))}")
        if args.json: print(json.dumps(doc, ensure_ascii=False, indent=1))
        if args.write:
            if same: print('  unchanged (idempotent, nothing written)')
            else: print('  written:', section_pages.save(doc))
        else:
            print('  dry-run: nothing written' + (' (would be unchanged)' if same else ''))


if __name__ == '__main__':
    main()
