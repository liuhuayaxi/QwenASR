"""Incremental, resumable import of one classroom's question bank.

Sequential and polite: one request at a time with a delay, activities that
were already imported and have not changed are skipped, and every activity is
checkpointed separately so an interrupted sync resumes where it stopped.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlsplit

from . import parsing
from .client import EXAM_HEADERS, EXAM_HOST, HOST, LoginRequired, classroom_headers
from .store import BankStore, now_iso

PAGE_SIZE = 20
MAX_PAGES = 100
# Re-check recent lessons whose subjective answers were not graded yet, and
# exams whose answers were not yet released, at most this often.
RECHECK_HOURS = 12
RETRY_DELAY = 3.0
RECENT_LESSON_DAYS = 30


class SyncAborted(RuntimeError):
    pass


def _image_name(question: dict[str, Any], index: int, image: dict[str, Any]) -> str:
    suffix = urlsplit(image.get("source") or image.get("url") or "").path.rsplit(".", 1)
    ext = suffix[-1].lower() if len(suffix) == 2 and suffix[-1].lower() in {"jpg", "jpeg", "png", "gif", "webp"} else "jpg"
    return f"{question['problem_id']}-{image.get('kind', 'img')}-{index}.{ext}"


class ClassroomSync:
    def __init__(self, session, store: BankStore, classroom_id: Any, *, university_id: Any = None,
                 progress: Callable[[str], None] | None = None, refresh: bool = False,
                 should_stop: Callable[[], bool] | None = None) -> None:
        self.session = session
        self.store = store
        self.cid = str(classroom_id)
        self.university_id = None if university_id in (None, "") else str(university_id)
        self.progress = progress or (lambda message: None)
        self.refresh = refresh
        self.should_stop = should_stop or (lambda: False)
        self.activity_image_errors = 0
        self.summary = {"activities": 0, "imported": 0, "skipped": 0, "pending": 0, "errors": 0,
                        "unsupported": 0, "images": 0, "image_errors": 0}

    # ------------------------------------------------------------ helpers
    def headers(self) -> dict[str, str]:
        return classroom_headers(self.cid, self.university_id or 0)

    def get(self, path: str) -> Any:
        return self.session.get_json(path if path.startswith("http") else HOST + path, self.headers())

    def check_stop(self) -> None:
        if self.should_stop():
            raise SyncAborted("同步已停止；已完成的活动已保存，下次会继续")

    # ------------------------------------------------------------ main
    def run(self) -> dict[str, Any]:
        self.store.ensure()
        info = self.classroom_info()
        activities = self.list_activities()
        self.summary["activities"] = len(activities)
        for number, activity in enumerate(activities, 1):
            self.check_stop()
            kind = parsing._int(activity.get("type"))
            label = f"[{number}/{len(activities)}] {activity.get('title') or activity.get('id')}"
            if kind not in parsing.SUPPORTED_ACTIVITIES:
                self.summary["unsupported"] += 1
                continue
            state = self.store.activity_state(self.cid, activity.get("id"))
            fingerprint = parsing.activity_fingerprint(activity)
            if not self.refresh and not self.needs_import(state, fingerprint):
                self.summary["skipped"] += 1
                continue
            self.progress(f"{label}：读取中")
            self.activity_image_errors = 0
            try:
                status, count, extra = self.import_activity(activity)
                if self.activity_image_errors:
                    # Signed image URLs are not kept, so retry on the next sync.
                    reason = "；".join(filter(None, [extra.get("reason"), f"{self.activity_image_errors} 张图片下载失败，下次同步重试"]))
                    status, extra = "images_pending", {**extra, "reason": reason}
            except LoginRequired:
                raise
            except SyncAborted:
                raise
            except Exception as exc:  # one broken activity must not stop the rest
                self.summary["errors"] += 1
                self.store.save_activity_state(self.cid, activity.get("id"), {
                    **state, "activity": activity, "fingerprint": fingerprint, "status": "error",
                    "error": str(exc)[:500], "checked_at": now_iso()})
                self.progress(f"{label}：失败（{exc}）")
                continue
            self.summary["imported" if status == "complete" else "pending"] += 1
            self.store.save_activity_state(self.cid, activity.get("id"), {
                "activity": activity, "fingerprint": fingerprint, "status": status,
                "question_count": count, "checked_at": now_iso(), **extra})
            self.progress(f"{label}：{count} 题" + ("" if status == "complete" else f"（{extra.get('reason') or '尚未全部公布'}，稍后再同步）"))
        questions = self.store.questions(self.cid)
        counts = self.store.counts(questions)
        info.update(last_sync=now_iso(), counts=counts, last_summary=self.summary)
        self.store.save_classroom(self.cid, info)
        return {"classroom_id": self.cid, "name": info.get("course_name", ""), "counts": counts, **self.summary}

    def needs_import(self, state: dict[str, Any], fingerprint: str) -> bool:
        if not state or state.get("fingerprint") != fingerprint or state.get("status") != "complete":
            if state.get("status") in {"pending", "ungraded"} and state.get("fingerprint") == fingerprint:
                return self._stale(state)
            return True
        return False

    def _stale(self, state: dict[str, Any]) -> bool:
        try:
            checked = datetime.fromisoformat(str(state.get("checked_at")))
        except ValueError:
            return True
        return datetime.now(checked.tzinfo) - checked >= timedelta(hours=RECHECK_HOURS)

    def classroom_info(self) -> dict[str, Any]:
        info = self.store.classroom(self.cid)
        try:
            payload = self.get(f"/v2/api/web/classrooms/{self.cid}?role=5")
            data = parsing.body_data(payload) or {}
        except parsing.YuketangAPIError as exc:
            raise RuntimeError(f"无法读取雨课堂班级 {self.cid}：{exc}") from exc
        if not self.university_id:
            self.university_id = str(data.get("uv_id") if data.get("uv_id") is not None else info.get("university_id") or 0)
        info.update(classroom_id=self.cid, university_id=self.university_id,
                    course_name=str(data.get("course_name") or info.get("course_name") or ""),
                    classroom_name=str(data.get("name") or info.get("classroom_name") or ""),
                    course_id=str(data.get("course_id") or ""))
        self.store.save_classroom(self.cid, info)
        return info

    def list_activities(self) -> list[dict[str, Any]]:
        activities: list[dict[str, Any]] = []
        seen = set()
        for page in range(MAX_PAGES):
            self.check_stop()
            payload = self.get(f"/v2/api/web/logs/learn/{self.cid}?actype=-1&page={page}&offset={PAGE_SIZE}&sort=-1")
            items, has_more, _ = parsing.parse_activities(payload)
            for item in items:
                key = (item.get("type"), item.get("id"))
                if key not in seen:
                    seen.add(key)
                    activities.append(item)
            if not has_more or not items:
                break
        return activities

    # ------------------------------------------------------------ per type
    def import_activity(self, activity: dict[str, Any]) -> tuple[str, int, dict[str, Any]]:
        kind = parsing._int(activity.get("type"))
        if kind == parsing.ACTIVITY_EXAM:
            return self.import_exam(activity)
        if kind == parsing.ACTIVITY_QUIZ:
            return self.import_quiz(activity)
        if kind == parsing.ACTIVITY_LESSON:
            return self.import_lesson(activity)
        return self.import_cards(activity)

    def save(self, activity: dict[str, Any], questions: list[dict[str, Any]]) -> int:
        self.download_images(activity, questions)
        self.store.save_questions(self.cid, activity.get("id"), questions)
        return len(questions)

    def download_images(self, activity: dict[str, Any], questions: list[dict[str, Any]]) -> None:
        existing = {q.get("id"): q for q in self.store.questions(self.cid)}
        for question in questions:
            old_images = {i.get("source"): i for i in (existing.get(question["id"]) or {}).get("images") or []}
            for index, image in enumerate(question.get("images") or []):
                url = image.pop("url", "")
                kept = old_images.get(image.get("source"))
                if kept and kept.get("path"):
                    try:
                        self.store.image_path(self.cid, kept["path"])
                        image.update(path=kept["path"], sha256=kept.get("sha256", ""))
                        continue
                    except FileNotFoundError:
                        pass
                if not url:
                    continue
                for attempt in range(2):
                    try:
                        data = self.session.download(url)
                        image.update(self.store.save_image(self.cid, activity.get("id"), _image_name(question, index, image), data))
                        image.pop("error", None)
                        self.summary["images"] += 1
                        break
                    except LoginRequired:
                        raise
                    except Exception as exc:
                        if attempt == 0:
                            time.sleep(RETRY_DELAY)
                            continue
                        # Playwright's call log echoes the signed URL; keep only
                        # the first line without query strings.
                        first = (str(exc).splitlines() or [""])[0]
                        image["error"] = re.sub(r"\?[^\s]*", "", first)[:200]
                        self.summary["image_errors"] += 1
                        self.activity_image_errors += 1

    def import_exam(self, activity: dict[str, Any]) -> tuple[str, int, dict[str, Any]]:
        aid, exam_id = activity.get("id"), activity.get("courseware_id")
        cover_main = self.get(f"/v/exam/cover?exam_id={exam_id}&classroom_id={self.cid}")
        self.store.save_raw(self.cid, aid, "cover", cover_main)
        cover = parsing.body_data(cover_main) or {}
        score = parsing.exam_score(cover)
        if not parsing.exam_results_visible(cover):
            reason = f"老师未开放试卷查看（成绩 {score}）" if score else "答案/成绩尚未公布"
            return "pending", 0, {"reason": reason, "score": score, "show_score_time": cover.get("show_score_time")}
        payloads = self.exam_payloads(exam_id)
        if payloads is None:
            return "pending", 0, {"reason": "考试页面暂不可查看试卷", "score": score}
        for name, payload in payloads.items():
            self.store.save_raw(self.cid, aid, name, payload)
        questions = parsing.parse_exam(activity, self.cid, payloads["show_paper"], payloads.get("problem_results"),
                                       payloads.get("exam_cover"))
        count = self.save(activity, questions)
        answered = all(q["answer_known"] for q in questions) if questions else False
        return ("complete" if answered else "pending"), count, {} if answered else {"reason": "部分题目答案未公布"}

    def exam_payloads(self, exam_id: Any) -> dict[str, Any] | None:
        def fetch():
            paper = self.session.get_json(f"{EXAM_HOST}/exam_room/show_paper?exam_id={exam_id}", EXAM_HEADERS)
            if paper.get("errcode") not in (0, None):
                return None
            results = self.session.get_json(f"{EXAM_HOST}/exam_room/problem_results?exam_id={exam_id}", EXAM_HEADERS)
            cover = self.session.get_json(f"{EXAM_HOST}/exam_room/cover?exam_id={exam_id}", EXAM_HEADERS)
            return {"show_paper": paper,
                    "problem_results": results if results.get("errcode") in (0, None) else None,
                    "exam_cover": cover if cover.get("errcode") in (0, None) else None}
        # The exam host keeps one open paper per browser session; a token from
        # an earlier exam answers 10006, so open this one first, like the UI.
        self.session.open_exam(self.cid, exam_id)
        return fetch()

    def import_quiz(self, activity: dict[str, Any]) -> tuple[str, int, dict[str, Any]]:
        aid, quiz_id = activity.get("id"), activity.get("courseware_id")
        result = self.get(f"/v2/api/web/quiz/personal_result?classroom_id={self.cid}&quiz_id={quiz_id}")
        self.store.save_raw(self.cid, aid, "personal_result", result)
        data = parsing.body_data(result) or {}
        shapes = {}
        for entry in [*(data.get("objective_result_list") or []), *(data.get("subjective_result_list") or [])]:
            pid = entry.get("problem_id") if isinstance(entry, dict) else None
            if pid is None:
                continue
            payload = self.get(f"/v2/api/web/quiz/problem_shape?classroom_id={self.cid}&quiz_id={quiz_id}&problem_id={pid}")
            self.store.save_raw(self.cid, aid, f"problem_shape_{pid}", payload)
            if payload.get("errcode") in (0, None):
                shapes[str(pid)] = payload
        questions = parsing.parse_quiz(activity, self.cid, result, shapes)
        count = self.save(activity, questions)
        visible = data.get("quiz_visible", True) and all(q["answer_known"] for q in questions)
        return ("complete" if visible else "pending"), count, {} if visible else {"reason": "答案尚未公布"}

    def import_lesson(self, activity: dict[str, Any]) -> tuple[str, int, dict[str, Any]]:
        aid, lesson_id = activity.get("id"), activity.get("courseware_id")
        info = self.get(f"/api/v3/classroom-report/student/lesson-info?lesson_id={lesson_id}")
        detail = self.get(f"/api/v3/classroom-report/student/detail?lesson_id={lesson_id}")
        self.store.save_raw(self.cid, aid, "lesson_info", info)
        self.store.save_raw(self.cid, aid, "student_detail", detail)
        identity = parsing.lesson_identity(info)
        answers = {}
        for item in parsing.lesson_problems(detail):
            pid = str(item.get("problemId") or "")
            if not pid or pid in answers or not identity:
                continue
            self.check_stop()
            payload = self.get(f"/api/v3/classroom-report/problem/student-answer?identityId={identity}"
                               f"&problemId={pid}&lesson_id={lesson_id}")
            self.store.save_raw(self.cid, aid, f"student_answer_{pid}", payload)
            if payload.get("code") in (0, None):
                answers[pid] = payload
        questions = parsing.parse_lesson(activity, self.cid, detail, answers, info)
        count = self.save(activity, questions)
        ungraded = any(not q.get("graded") for q in questions)
        recent = False
        try:
            recent = time.time() - float(activity.get("create_time") or 0) / 1000 < RECENT_LESSON_DAYS * 86400
        except (TypeError, ValueError):
            pass
        if ungraded and recent:
            return "ungraded", count, {"reason": "主观题尚未批改，近期会再次核对得分"}
        return "complete", count, {}

    def import_cards(self, activity: dict[str, Any]) -> tuple[str, int, dict[str, Any]]:
        aid, cards_id = activity.get("id"), activity.get("courseware_id")
        payload = self.get(f"/v2/api/web/cards/detlist/{cards_id}?classroom_id={self.cid}")
        self.store.save_raw(self.cid, aid, "detlist", payload)
        questions = parsing.parse_cards(activity, self.cid, payload)
        return "complete", self.save(activity, questions), {}


def sync_classrooms(store: BankStore, classrooms: list[dict[str, Any]], *, headless: bool = True,
                    delay: float = 1.0, refresh: bool = False, profile_dir=None,
                    progress: Callable[[str], None] | None = None,
                    should_stop: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    from .client import YuketangSession
    progress = progress or (lambda message: None)
    results = []
    with YuketangSession(profile_dir or store.profile_dir, headless=headless, delay=delay) as session:
        session.check_login()
        try:
            refresh_course_list(session, store)
        except LoginRequired:
            raise
        except Exception as exc:  # the binding dropdown is a convenience
            progress(f"课程列表未更新：{exc}")
        for item in classrooms:
            cid = str(item["classroom_id"])
            progress(f"同步雨课堂班级 {cid}")
            runner = ClassroomSync(session, store, cid, university_id=item.get("university_id"),
                                   progress=progress, refresh=refresh, should_stop=should_stop)
            results.append(runner.run())
    return results


COURSE_LIST = HOST + "/v2/api/web/courses/list?identity=2"


def refresh_course_list(session, store: BankStore) -> dict[str, Any]:
    """Cache the student's classrooms (identity=2) for the binding dropdown."""
    payload = session.get_json(COURSE_LIST, {"x-client": "web", "xt-agent": "web", "xtbz": "ykt"})
    return store.save_course_list(parsing.parse_course_list(payload))


def fetch_course_list(store: BankStore, *, headless: bool = True, profile_dir=None) -> dict[str, Any]:
    from .client import YuketangSession
    with YuketangSession(profile_dir or store.profile_dir, headless=headless) as session:
        session.check_login()
        return refresh_course_list(session, store)
