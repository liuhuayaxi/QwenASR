#!/usr/bin/env python3
"""Score generated section page links against a hand-checked ground truth."""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from qwen_asr_app import section_pages
sys.path.insert(0, str(ROOT / 'scripts'))
import section_page_links as cli


def evaluate(doc, truth):
    rows, stats = [], {k: 0 for k in ('mappable', 'absent', 'auto', 'auto_exact', 'auto_hit', 'auto_wrong_absent',
                                       'review', 'review_exact', 'review_hit', 'review_absent', 'hidden')}
    for s in doc['sections']:
        if s['status'] == 'excluded': continue
        gt = next((t for t in truth if s['heading'].startswith(t[0])), None) or next((t for t in truth if t[0] in s['heading']), None)
        if gt is None: rows.append((s, None, 'NO-TRUTH')); continue
        starts = gt[1]
        t = s['targets'][0] if s['targets'] else None
        shown = s['status'] in ('auto', 'verified', 'manual')
        if starts is None:
            stats['absent'] += 1
            verdict = 'ok-hidden' if not shown else 'WRONG-shown'
            if shown: stats['auto_wrong_absent'] += 1
            if s['status'] == 'review': stats['review_absent'] += 1
        else:
            stats['mappable'] += 1
            exact = t and t['page_start'] in starts
            hit = t and gt[2][0] <= t['page_start'] <= gt[2][1]
            if shown:
                stats['auto'] += 1; stats['auto_exact'] += bool(exact); stats['auto_hit'] += bool(hit)
            elif s['status'] == 'review':
                stats['review'] += 1; stats['review_exact'] += bool(exact); stats['review_hit'] += bool(hit)
            else: stats['hidden'] += 1
            verdict = 'exact' if exact else 'in-range' if hit else 'WRONG'
        rows.append((s, gt, verdict))
    return rows, stats


def main():
    truth_all = json.loads((section_pages.ROOT / 'pilot' / 'groundtruth.json').read_text())
    storage = cli.storage_root()
    total = {}
    for ident in [k for k in truth_all if not k.startswith('_')]:
        doc = section_pages.load(ident)
        if doc is None or '--fresh' in sys.argv:
            directory, session = cli.find(storage, ident)
            doc = section_pages.generate(directory, session, storage)
        rows, stats = evaluate(doc, truth_all[ident])
        print('==', ident)
        for s, gt, verdict in rows:
            t = s['targets'][0] if s['targets'] else {}
            print(f"  {verdict:<11} [{s['status']:<10}] {s['heading'][:40]:<40} got P{t.get('page_start')}-{t.get('page_end')} truth {gt[1] if gt else '?'}{' (uncertain)' if gt and len(gt) > 3 else ''}")
        print('  ', stats)
        for k, v in stats.items(): total[k] = total.get(k, 0) + v
    t = total
    print('TOTAL', t)
    if t['mappable']:
        print(f"coverage(auto shown / mappable) = {t['auto']}/{t['mappable']} = {t['auto']/t['mappable']:.0%}")
    if t['auto']:
        print(f"auto exact-start accuracy = {t['auto_exact']}/{t['auto']} = {t['auto_exact']/t['auto']:.0%}; auto start-in-range = {t['auto_hit']}/{t['auto']} = {t['auto_hit']/t['auto']:.0%}")
    if t['review']:
        print(f"review-list candidates exact = {t['review_exact']}/{t['review']}, in-range = {t['review_hit']}/{t['review']}")
    print(f"absent topics shown by mistake = {t['auto_wrong_absent']}/{t['absent']}")


if __name__ == '__main__':
    main()
