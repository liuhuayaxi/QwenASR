#!/usr/bin/env python3
"""Write sharingnotes page images and PPT links after a courseware publish.

Uses the AI PDFs already cached by sharingnotes ``scripts/sync_site.py``
(``~/.local/state/sharingnotes/prepared-assets.json`` and ``media/pdf-<sha>.pdf``).
Those release assets are not uploaded again. Unchanged decks are not rendered.
Decks that published buttons no longer reference lose their image folder.

  .venv/bin/python scripts/publish_courseware_view.py
  .venv/bin/python scripts/publish_courseware_view.py --dry-run
  .venv/bin/python scripts/publish_courseware_view.py --no-push

``--stage`` writes the snapshot sync_site is about to copy. Together with
``--no-commit`` this is what ``sharingnotes/scripts/sync_site.py`` runs after
upload, so the images land in that same docs/data commit. A rerun that finds
every referenced deck already rendered leaves ``page-links.json`` byte-for-byte
unchanged and does not commit.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from qwen_asr_app.courseware_publish import publish_courseware_view  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sharingnotes', default=str(ROOT.parent / 'sharingnotes'))
    parser.add_argument('--stage', default='', help='sync_site snapshot directory; skips the git commit')
    parser.add_argument('--assets', default='', help='prepared-assets.json from sync_site')
    parser.add_argument('--links', default='', help='section-page-links directory; defaults to QwenASR private data')
    parser.add_argument('--no-commit', action='store_true')
    parser.add_argument('--no-push', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    sharing = Path(args.sharingnotes)
    links_dir = Path(args.links) if args.links else None
    if not (sharing / 'docs' / 'data').is_dir():
        raise SystemExit(f'没有找到 sharingnotes 数据目录：{sharing / "docs" / "data"}')
    if args.dry_run:
        from qwen_asr_app import section_pages as sp
        from qwen_asr_app.courseware_publish import export_page_links
        data = Path(args.stage) if args.stage else sharing / 'docs' / 'data'
        result, report = export_page_links(links_dir or sp.LINKS, data, sp.enabled())
        for row in report:
            print(*row)
        decks = {t['sha256'] for note in result['notes'].values() for section in note['sections'] for t in section['targets']}
        print('referenced decks', len(decks))
        return
    result = publish_courseware_view(
        sharing, stage=Path(args.stage) if args.stage else None,
        assets_path=Path(args.assets) if args.assets else None,
        links_dir=links_dir, commit=not args.no_commit and not args.stage, push=not args.no_push,
    )
    print(json.dumps({k: result[k] for k in ('links_changed', 'notes', 'pages', 'commit')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
