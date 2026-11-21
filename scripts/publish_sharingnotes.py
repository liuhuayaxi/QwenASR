#!/usr/bin/env python3
"""Publish changed notes to Sharing Notes through the v1 publish API.

Only notes whose published content changed since the last accepted upsert are
sent; unchanged notes cost no request. The running app does this on its own
after every note save when the publisher is configured; this command is for a
manual catch-up, a dry run, or testing against the in-process mock API.

  .venv/bin/python scripts/publish_sharingnotes.py --dry-run
  .venv/bin/python scripts/publish_sharingnotes.py --mock /tmp/sharingnotes-mock --courses 信号与系统
  .venv/bin/python scripts/publish_sharingnotes.py --serve-mock /tmp/sharingnotes-mock --port 8799
  .venv/bin/python scripts/publish_sharingnotes.py --session <recording-id>
  .venv/bin/python scripts/publish_sharingnotes.py --delete <note-id> --expected-version <version>

Configure ``SHARINGNOTES_PUBLISH_URL`` / ``SHARINGNOTES_PUBLISH_TOKEN`` /
``SHARINGNOTES_PUBLISH_COURSES`` or ``<private data>/sharingnotes-publish/config.json``.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qwen_asr_app.sharingnotes_export import export_note  # noqa: E402
from qwen_asr_app.sharingnotes_publish import (  # noqa: E402
    HttpTransport, PublishConfig, PublishState, Publisher, build_upsert, state_path, store_loader)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--session', action='append', default=[], help='只发布指定录音（可重复）')
    parser.add_argument('--courses', help='逗号分隔的课程，覆盖配置')
    parser.add_argument('--force', action='store_true', help='内容未变也重新提交')
    parser.add_argument('--dry-run', action='store_true', help='只在本机生成消息并统计，不发请求')
    parser.add_argument('--mock', metavar='DIR', help='发到保存在 DIR 的本机模拟接口')
    parser.add_argument('--serve-mock', metavar='DIR', help='在 127.0.0.1:--port 运行保存在 DIR 的模拟接口')
    parser.add_argument('--port', type=int, default=8799)
    parser.add_argument('--state', metavar='FILE', help='发布状态文件（默认在私有数据目录）')
    parser.add_argument('--delete', metavar='NOTE_ID')
    parser.add_argument('--expected-version')
    args = parser.parse_args()
    if args.serve_mock:
        from qwen_asr_app.sharingnotes_mock_api import serve
        print(f'模拟发布接口：http://127.0.0.1:{args.port}（令牌 mock-token）', flush=True)
        return serve(args.serve_mock, args.port)

    config = PublishConfig.load()
    courses = [c for c in args.courses.split(',') if c.strip()] if args.courses is not None else config.courses
    from qwen_asr_app.storage import recording_store
    ids = args.session or [record.id for record in recording_store.iter_records()]

    if args.dry_run:
        counts, sizes = Counter(), 0
        for ident in ids:
            session = store_loader(recording_store, ident)()
            if not session or session.get('category') not in courses:
                counts['skipped'] += 1
                continue
            try:
                exported = export_note(session)
            except ValueError as exc:
                counts['invalid'] += 1
                print(f'{ident}: {exc}', file=sys.stderr)
                continue
            if exported:
                counts['notes'] += 1
                sizes += len(json.dumps(build_upsert(exported), ensure_ascii=False).encode())
        print(json.dumps({**counts, 'messageBytes': sizes}, ensure_ascii=False))
        return

    if args.mock:
        from qwen_asr_app.sharingnotes_mock_api import MockPublishAPI
        transport = MockPublishAPI(args.mock)
        state = PublishState(args.state or Path(args.mock) / 'publisher-state.json')
    else:
        if not (config.url and config.token):
            raise SystemExit('没有配置发布接口：设置 SHARINGNOTES_PUBLISH_URL 和 SHARINGNOTES_PUBLISH_TOKEN')
        transport = HttpTransport(config.url, config.token)
        state = PublishState(args.state or state_path(config.url))
    publisher = Publisher(transport, courses, state)

    if args.delete:
        if not args.expected_version:
            raise SystemExit('删除需要 --expected-version（当前线上版本）')
        print(json.dumps(publisher.delete_note(args.delete, args.expected_version), ensure_ascii=False))
        return
    if not courses:
        raise SystemExit('没有要发布的课程：设置 SHARINGNOTES_PUBLISH_COURSES 或 --courses')
    results = publisher.sync([(ident, store_loader(recording_store, ident)) for ident in ids], force=args.force)
    for item in results:
        if item['status'] not in {'unchanged', 'skipped_course', 'skipped_unpublishable', 'skipped_deleted'}:
            print(json.dumps(item, ensure_ascii=False))
    print(json.dumps(dict(Counter(item['status'] for item in results)), ensure_ascii=False))
    if any(item['status'] in {'retry', 'conflict', 'rejected', 'invalid'} for item in results):
        sys.exit(1)


if __name__ == '__main__':
    main()
