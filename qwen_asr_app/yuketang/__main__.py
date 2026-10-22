"""Command line: ``python -m qwen_asr_app.yuketang <command>``.

    login                         open Chrome, scan the WeChat QR code once
    bind --course 通信原理 C --classroom 26260946 [--university 3278]
    sync [--course NAME | --classroom ID ...] [--refresh] [--transcribe]
    transcribe [--course NAME | --classroom ID] [--limit N]
    courses [--refresh]           list the student's classrooms (cached for the UI)
    status [--course NAME]
    list --course NAME [--query 关键词]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .store import BankStore


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _targets(store: BankStore, args) -> list[dict]:
    if getattr(args, "classroom", None):
        bound = {str(i["classroom_id"]): i for items in store.bindings().values() for i in items}
        return [bound.get(str(cid), {"classroom_id": str(cid), **({"university_id": args.university} if args.university is not None else {})})
                for cid in args.classroom]
    if getattr(args, "course", None):
        items = store.classrooms_for(args.course)
        if not items:
            raise SystemExit(f"课程“{args.course}”还没有绑定雨课堂班级；先运行 bind")
        return items
    items = list({str(i["classroom_id"]): i for v in store.bindings().values() for i in v}.values())
    if not items:
        raise SystemExit("还没有绑定任何雨课堂班级；用 --classroom 指定或先运行 bind")
    return items


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qwen_asr_app.yuketang", description="雨课堂题库导入（只读）")
    parser.add_argument("--store", type=Path, help="题库保存目录（默认 ~/.qwenasr/yuketang）")
    parser.add_argument("--profile", type=Path, help="Chrome 登录资料目录（默认 <store>/browser-profile）")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="打开 Chrome 窗口扫码登录")
    login.add_argument("--timeout", type=float, default=600)
    bind = sub.add_parser("bind", help="把 QwenASR 课程绑定到雨课堂班级")
    bind.add_argument("--course", required=True)
    bind.add_argument("--classroom", required=True)
    bind.add_argument("--university")
    bind.add_argument("--replace", action="store_true", help="替换这门课已有的绑定（改绑）")
    unbind = sub.add_parser("unbind")
    unbind.add_argument("--course", required=True)
    unbind.add_argument("--classroom", required=True)
    for name in ("sync", "transcribe"):
        command = sub.add_parser(name)
        command.add_argument("--course")
        command.add_argument("--classroom", action="append")
        command.add_argument("--university")
        if name == "sync":
            command.add_argument("--refresh", action="store_true", help="忽略增量记录，重新读取全部活动")
            command.add_argument("--headed", action="store_true", help="显示浏览器窗口")
            command.add_argument("--delay", type=float, default=1.0, help="请求间隔秒数（默认 1）")
            command.add_argument("--transcribe", action="store_true", help="同步后转写题目图片")
        else:
            command.add_argument("--limit", type=int)
    courses = sub.add_parser("courses", help="列出（--refresh 时重新读取）雨课堂课程")
    courses.add_argument("--refresh", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--course")
    listing = sub.add_parser("list")
    listing.add_argument("--course", required=True)
    listing.add_argument("--query", default="")
    listing.add_argument("--source", default="", help="exam / lesson / courseware")
    args = parser.parse_args(argv)

    store = BankStore(args.store)
    profile = args.profile or store.profile_dir
    if args.command == "login":
        from .client import YuketangSession
        print("正在打开 Chrome，请用微信扫码登录雨课堂……", flush=True)
        with YuketangSession(profile, headless=False) as session:
            session.interactive_login(timeout=args.timeout)
        print("登录成功，登录状态保存在", profile)
        return 0
    if args.command == "bind":
        _print(store.bind(args.course, args.classroom, university_id=args.university, replace=args.replace))
        return 0
    if args.command == "unbind":
        _print({"removed": store.unbind(args.course, args.classroom)})
        return 0
    if args.command == "sync":
        from .client import LoginRequired
        from .sync import sync_classrooms
        try:
            results = sync_classrooms(store, _targets(store, args), headless=not args.headed, delay=args.delay,
                                      refresh=args.refresh, profile_dir=profile, progress=lambda m: print(m, flush=True))
        except LoginRequired as exc:
            print(f"{exc}。请先运行：python -m qwen_asr_app.yuketang login", file=sys.stderr)
            return 2
        output = {"classrooms": results}
        if args.transcribe:
            from .vision import transcribe_classroom
            output["vision"] = [asyncio.run(transcribe_classroom(store, item["classroom_id"], progress=print))
                                for item in _targets(store, args)]
        _print(output)
        return 0
    if args.command == "transcribe":
        from .vision import transcribe_classroom
        _print([asyncio.run(transcribe_classroom(store, item["classroom_id"], limit=args.limit, progress=print))
                for item in _targets(store, args)])
        return 0
    if args.command == "courses":
        if args.refresh:
            from .client import LoginRequired
            from .sync import fetch_course_list
            try:
                fetch_course_list(store, profile_dir=profile)
            except LoginRequired as exc:
                print(f"{exc}。请先运行：python -m qwen_asr_app.yuketang login", file=sys.stderr)
                return 2
        listed = store.course_list()
        print(f"课程列表更新于 {listed['fetched_at'] or '从未'}")
        for c in listed["courses"]:
            print(f"{c['classroom_id']}\t{c['course_name']}\t{c['classroom_name']}\t{c['teacher']}\t学校 {c['university_id']}")
        return 0
    if args.command == "status":
        from .service import YuketangService
        _print(YuketangService(store).status(args.course))
        return 0
    if args.command == "list":
        for q in store.search(args.course, args.query, args.source, 1000):
            answer = "、".join(q.get("answer") or []) or "?"
            mine = "、".join(str(v)[:40] for v in q.get("student_answer") or [])
            mark = {True: "✓", False: "✗"}.get(q.get("correct"), "·")
            body = (q.get("body") or (q.get("vision") or {}).get("text") or "[图片题]").replace("\n", " ")[:80]
            print(f"{q.get('date')} {q.get('source_label')} {q.get('activity_title')} P{q.get('page') or q.get('index')} "
                  f"{q.get('type_text')} {mark} 答案:{answer} 我的:{mine} | {body}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
