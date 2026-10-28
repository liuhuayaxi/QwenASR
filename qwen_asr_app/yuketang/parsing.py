"""Pure parsers from raw Yuketang JSON to normalized question records.

No network or filesystem access happens here so every parser is unit-testable
with redacted fixtures. A normalized question is a plain dict::

    {"id", "source", "source_label", "classroom_id", "activity_id",
     "activity_type", "activity_title", "lesson_id", "date", "time", "page",
     "problem_id", "problem_type", "type_text", "body", "options", "answer",
     "answer_known", "remark", "student_answer", "correct", "score",
     "max_score", "graded", "images", "needs_vision"}
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

CHINA = timezone(timedelta(hours=8))

# Activity types seen in /v2/api/web/logs/learn.
ACTIVITY_COURSEWARE = 2   # 课件 / cards
ACTIVITY_QUIZ = 4         # 试卷 (quiz, /v2/web/quizSummary)
ACTIVITY_EXAM = 5         # 考试 (exam host changjiang-exam)
ACTIVITY_LESSON = 14      # 课堂 (live lesson with in-class problems)
EXAM_LIKE = {ACTIVITY_QUIZ, ACTIVITY_EXAM}
SUPPORTED_ACTIVITIES = {ACTIVITY_COURSEWARE, ACTIVITY_QUIZ, ACTIVITY_EXAM, ACTIVITY_LESSON}

SOURCE_LABELS = {
    "exam": "课前/课后测试与考试",
    "quiz": "课前/课后测试与考试",
    "lesson": "课上习题",
    "cards": "课件习题",
}
BANK_CATEGORIES = {"exam": "exam", "quiz": "exam", "lesson": "lesson", "cards": "courseware"}

NUMERIC_TYPES = {1: "single", 2: "multiple", 3: "vote", 4: "fill_blank", 5: "subjective", 6: "judge"}
NAMED_TYPES = {
    "singlechoice": "single", "multiplechoice": "multiple", "judgement": "judge", "judge": "judge",
    "truefalse": "judge", "fillblank": "fill_blank", "shortanswer": "subjective",
    "subjective": "subjective", "vote": "vote",
}
TYPE_TEXT = {"single": "单选题", "multiple": "多选题", "judge": "判断题", "fill_blank": "填空题",
             "subjective": "主观题", "vote": "投票题", "unknown": "题目"}


def problem_type(value: Any) -> str:
    if isinstance(value, bool):
        return "unknown"
    if isinstance(value, (int, float)):
        return NUMERIC_TYPES.get(int(value), "unknown")
    text = str(value or "").strip().lower()
    if text.isdigit():
        return NUMERIC_TYPES.get(int(text), "unknown")
    return NAMED_TYPES.get(re.sub(r"[^a-z]", "", text), "unknown")


def strip_url(url: Any) -> str:
    """Drop signed query strings so normalized data never keeps auth tokens."""
    text = str(url or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


_IMG = re.compile(r"<img[^>]*?src=[\"']([^\"']+)[\"'][^>]*>", re.I)


def html_images(value: Any) -> list[str]:
    return [match for match in _IMG.findall(str(value or "")) if match.startswith("http")]


def html_text(value: Any) -> str:
    text = str(value or "")
    text = _IMG.sub(" [图片] ", text)
    text = re.sub(r"<\s*br\s*/?>|</p>|</div>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def has_meaningful_text(value: Any) -> bool:
    """Whether HTML-to-text output contains text beyond the image marker."""
    return bool(re.sub(r"\[图片\]", "", str(value or "")).strip())


def ms_to_local(value: Any) -> tuple[str, str]:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return "", ""
    if stamp <= 0:
        return "", ""
    if stamp > 1e11:
        stamp /= 1000
    moment = datetime.fromtimestamp(stamp, CHINA)
    return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M")


def body_data(payload: Any) -> Any:
    """Return the ``data`` member of a Yuketang envelope after checking errors."""
    if not isinstance(payload, dict):
        raise ValueError("雨课堂返回的不是 JSON 对象")
    if "errcode" in payload and payload.get("errcode") not in (0, None):
        raise YuketangAPIError(str(payload.get("errmsg") or payload.get("errcode")), code=payload.get("errcode"))
    if "code" in payload and payload.get("code") not in (0, None):
        raise YuketangAPIError(str(payload.get("msg") or payload.get("code")), code=payload.get("code"))
    if payload.get("success") is False:
        raise YuketangAPIError(str(payload.get("msg") or "请求失败"), code=payload.get("error_code"))
    return payload.get("data")


class YuketangAPIError(RuntimeError):
    def __init__(self, message: str, *, code: Any = None) -> None:
        super().__init__(message)
        self.code = code


def options_list(raw: Any) -> list[dict[str, str]]:
    result = []
    if isinstance(raw, dict):
        raw = [{"key": key, "value": value} for key, value in raw.items()]
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or item.get("Key") or item.get("label") or "").strip()
        value = html_text(item.get("value") if "value" in item else item.get("Value"))
        if key or value:
            result.append({"key": key, "value": value})
    return sorted(result, key=lambda option: option["key"])


def answer_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        return [part for part in re.split(r"[;；]", text) if part] if (";" in text or "；" in text) else [text]
    if isinstance(raw, dict):
        return [str(value) for _, value in sorted(raw.items(), key=lambda kv: _int(kv[0]))]
    if isinstance(raw, (list, tuple)):
        values = []
        for item in raw:
            if isinstance(item, (list, tuple)):
                values.append("/".join(str(value) for value in item))
            elif item not in (None, ""):
                values.append(str(item))
        return values
    return [str(raw)]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "correct", "对"}:
        return True
    if text in {"0", "false", "no", "wrong", "错"}:
        return False
    return None


def sort_choice(values: list[str]) -> list[str]:
    return sorted(values) if all(len(value) == 1 and value.isalpha() for value in values) else values


def base_question(*, source: str, classroom_id: Any, activity: dict[str, Any], problem_id: Any,
                  kind: str, date: str = "", time: str = "") -> dict[str, Any]:
    if not date:
        date, time = ms_to_local(activity.get("create_time"))
    return {
        "id": f"{source}:{activity.get('courseware_id') or activity.get('id')}:{problem_id}",
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "bank_category": BANK_CATEGORIES[source],
        "classroom_id": str(classroom_id),
        "activity_id": str(activity.get("id") or ""),
        "activity_type": _int(activity.get("type")),
        "activity_title": str(activity.get("title") or ""),
        "courseware_id": str(activity.get("courseware_id") or ""),
        "lesson_id": "",
        "date": date,
        "time": time,
        "page": None,
        "index": None,
        "problem_id": str(problem_id),
        "problem_type": kind,
        "type_text": TYPE_TEXT.get(kind, "题目"),
        "body": "",
        "options": [],
        "answer": [],
        "answer_known": False,
        "remark": "",
        "student_answer": [],
        "correct": None,
        "score": None,
        "max_score": None,
        "graded": True,
        "images": [],
        "needs_vision": False,
    }


def image_ref(kind: str, url: Any) -> dict[str, str] | None:
    text = str(url or "").strip()
    if not text.startswith("http"):
        return None
    return {"kind": kind, "url": text, "source": strip_url(text)}


def _rich_images(question: dict[str, Any], body: Any = None, options: Any = None, remark: Any = None) -> None:
    """Preserve inline pictures, including mixed text/image answer options."""
    seen = {(image.get("kind"), image.get("source")) for image in question["images"]}
    if isinstance(options, dict):
        options = list(options.values())
    values = [("body", body), ("remark", remark)]
    for option in options or []:
        values.append(("body", option.get("value", option.get("Value")) if isinstance(option, dict) else option))
    for kind, value in values:
        for url in html_images(value):
            image = image_ref(kind, url)
            key = (kind, image["source"]) if image else None
            if image and key not in seen:
                question["images"].append(image)
                seen.add(key)
    if any(image.get("kind") in {"body", "remark"} for image in question["images"]):
        question["needs_vision"] = True


def parse_course_list(payload: Any) -> list[dict[str, Any]]:
    """Student classrooms from ``/v2/api/web/courses/list?identity=2``.

    Keeps only what the binding dropdown needs: no avatars or user ids.
    """
    data = body_data(payload) or {}
    items = data.get("list") if isinstance(data, dict) else data
    result, seen = [], set()
    for item in items or []:
        if not isinstance(item, dict) or item.get("classroom_id") in (None, ""):
            continue
        cid = str(item["classroom_id"])
        if cid in seen:
            continue
        seen.add(cid)
        course = item.get("course") if isinstance(item.get("course"), dict) else {}
        teacher = item.get("teacher") if isinstance(item.get("teacher"), dict) else {}
        uv = course.get("university_id")
        result.append({"classroom_id": cid, "university_id": str(uv if uv is not None else 0),
                       "course_name": str(course.get("name") or item.get("name") or "").strip(),
                       "classroom_name": str(item.get("name") or "").strip(),
                       "teacher": str(teacher.get("name") or "").strip(),
                       "term": str(item.get("term") or ""),
                       "created": ms_to_local(item.get("time"))[0]})
    return result


def parse_activities(payload: Any) -> tuple[list[dict[str, Any]], bool, Any]:
    data = body_data(payload) or {}
    activities = [item for item in data.get("activities") or [] if isinstance(item, dict)]
    return activities, bool(data.get("has_more")), data.get("prev_id")


def activity_fingerprint(activity: dict[str, Any]) -> str:
    """Stable signature; fields that change when the student's result changes."""
    import hashlib
    import json
    keys = ("type", "id", "courseware_id", "title", "create_time", "status", "score", "deadline",
            "total_score", "problem_count", "progress", "unfinished", "is_finished", "attend_status",
            "count", "view")
    stable = {key: activity.get(key) for key in keys if key in activity}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- exams (type 5)

def exam_results_visible(cover: dict[str, Any]) -> bool:
    """Whether the exam host may already show the answers/score to the student."""
    if not isinstance(cover, dict):
        return False
    # ``show_perm`` is the teacher's "allow students to view the paper" switch.
    # Without it the exam host answers 10007 未到试卷查看时间 even when the score
    # is already published, so only the score can be recorded.
    return bool(cover.get("show_perm"))


def exam_score(cover: dict[str, Any]) -> str:
    result = cover.get("result") if isinstance(cover, dict) and isinstance(cover.get("result"), dict) else {}
    if not result.get("score_finish") or result.get("score") is None:
        return ""
    total = _num(cover.get("total_score"))
    return f"{float(result['score']):g}/{'?' if total is None else format(total, 'g')}"


def parse_exam(activity: dict[str, Any], classroom_id: Any, paper_payload: Any,
               results_payload: Any = None, cover_payload: Any = None) -> list[dict[str, Any]]:
    paper = body_data(paper_payload) or {}
    results = {}
    if results_payload is not None:
        for item in (body_data(results_payload) or {}).get("problem_results") or []:
            if isinstance(item, dict):
                results[str(item.get("problem_id"))] = item
    cover = body_data(cover_payload) if cover_payload is not None else {}
    date, time = ms_to_local((cover or {}).get("start_time") or activity.get("create_time"))
    questions = []
    for position, problem in enumerate(paper.get("problems") or [], 1):
        if not isinstance(problem, dict):
            continue
        pid = problem.get("ProblemID") or problem.get("problem_id")
        kind = problem_type(problem.get("Type") or problem.get("ProblemType") or problem.get("problem_type"))
        question = base_question(source="exam", classroom_id=classroom_id, activity=activity,
                                 problem_id=pid, kind=kind, date=date, time=time)
        if problem.get("TypeText"):
            question["type_text"] = str(problem["TypeText"])
        result = results.get(str(pid), {})
        question.update(
            index=_int(problem.get("index"), position - 1) + 1,
            body=html_text(problem.get("Body")),
            options=options_list(problem.get("Options")),
            remark=html_text(problem.get("Remark")),
            max_score=_num(problem.get("Score") if problem.get("Score") is not None else problem.get("score")),
        )
        answer = answer_list(problem.get("Answer")) or answer_list(result.get("answer"))
        if kind == "fill_blank" and not answer and isinstance(problem.get("Blanks"), list):
            answer = ["/".join(str(v) for v in (blank.get("Answers") or [])) for blank in problem["Blanks"] if isinstance(blank, dict)]
        question["answer"] = sort_choice(answer)
        question["answer_known"] = bool(answer)
        if result:
            question["student_answer"] = sort_choice(answer_list(result.get("result")))
            question["correct"] = bool_value(result.get("correct"))
            question["score"] = _num(result.get("grade"))
        for url in html_images(problem.get("Body")):
            ref = image_ref("body", url)
            if ref:
                question["images"].append(ref)
        question["needs_vision"] = not has_meaningful_text(question["body"]) and bool(question["images"])
        _rich_images(question, problem.get("Body"), problem.get("Options"), problem.get("Remark"))
        questions.append(question)
    return questions


# ---------------------------------------------------------------- quizzes (type 4)

def parse_quiz(activity: dict[str, Any], classroom_id: Any, result_payload: Any,
               shapes: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = body_data(result_payload) or {}
    shapes = shapes or {}
    date, time = ms_to_local(data.get("publish_time") or activity.get("create_time"))
    entries = [*(data.get("objective_result_list") or []), *(data.get("subjective_result_list") or [])]
    questions = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("problem_id")
        shape_payload = shapes.get(str(pid))
        shape = body_data(shape_payload) if shape_payload is not None else {}
        shape = shape or {}
        kind = problem_type(shape.get("Type") or entry.get("problem_type"))
        question = base_question(source="quiz", classroom_id=classroom_id, activity=activity,
                                 problem_id=pid, kind=kind, date=date, time=time)
        answer = answer_list(entry.get("answer"))
        if not answer and isinstance(shape.get("Blanks"), list):
            answer = ["/".join(str(v) for v in (blank.get("Answers") or [])) for blank in shape["Blanks"] if isinstance(blank, dict)]
        if not answer:
            answer = answer_list(shape.get("Answer"))
        bodies = [html_text(item.get("Body") if isinstance(item, dict) else item) for item in shape.get("ProblemBodys") or []]
        question.update(
            index=_int(entry.get("problem_index")) or None,
            body="\n".join(part for part in bodies if part) or html_text(shape.get("Body")),
            options=options_list(shape.get("Options")),
            answer=sort_choice(answer), answer_known=bool(answer),
            remark=html_text(shape.get("Remark")),
            student_answer=answer_list(entry.get("result")),
            correct=bool_value(entry.get("correct")),
            score=_num(entry.get("score")),
            max_score=_num(shape.get("Score")),
        )
        if isinstance(entry.get("correct_blanks"), list):
            question["correct_blanks"] = entry["correct_blanks"]
        for item in shape.get("Shapes") or []:
            if isinstance(item, dict):
                ref = image_ref("body", item.get("URL") or item.get("url"))
                if ref:
                    question["images"].append(ref)
        question["needs_vision"] = not has_meaningful_text(question["body"]) and bool(question["images"])
        _rich_images(question, shape.get("Body"), shape.get("Options"), shape.get("Remark"))
        questions.append(question)
    return questions


# ---------------------------------------------------------------- lessons (type 14)

def lesson_identity(lesson_info_payload: Any) -> str:
    data = body_data(lesson_info_payload) or {}
    return str(data.get("identityId") or "")


def lesson_problems(detail_payload: Any) -> list[dict[str, Any]]:
    data = body_data(detail_payload) or {}
    problem = data.get("problem") if isinstance(data.get("problem"), dict) else {}
    return [item for item in problem.get("studentList") or [] if isinstance(item, dict)]


def parse_lesson(activity: dict[str, Any], classroom_id: Any, detail_payload: Any,
                 answers: dict[str, Any] | None = None, lesson_info_payload: Any = None) -> list[dict[str, Any]]:
    answers = answers or {}
    info = body_data(lesson_info_payload) if lesson_info_payload is not None else {}
    lesson_id = str(activity.get("courseware_id") or (info or {}).get("lessonId") or "")
    lesson_date, lesson_time = ms_to_local((info or {}).get("start") or activity.get("create_time"))
    questions = []
    seen = set()
    for item in lesson_problems(detail_payload):
        pid = str(item.get("problemId") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        raw_answer = answers.get(pid)
        answer_data = body_data(raw_answer) if raw_answer is not None else {}
        content = (answer_data or {}).get("content") or {}
        mine = (answer_data or {}).get("problem") or {}
        kind = problem_type(content.get("problemType") or item.get("problem_type"))
        question = base_question(source="lesson", classroom_id=classroom_id, activity=activity,
                                 problem_id=pid, kind=kind, date=lesson_date, time=lesson_time)
        sent_date, sent_time = ms_to_local(item.get("send_time"))
        if sent_date:
            question["date"], question["time"] = sent_date, sent_time
        question.update(lesson_id=lesson_id, page=_int(item.get("idx")) or None,
                        index=_int(content.get("index")) or _int(item.get("idx")) or None,
                        send_time=item.get("send_time"), answer_seconds=round(_int(item.get("problemTime")) / 1000) or None)
        max_score = _num(item.get("score"))
        if max_score is None and _num(content.get("score")) is not None:
            max_score = _num(content.get("score")) / 100
        student_score = _num(item.get("problemscore"))
        graded = not (student_score is not None and student_score < 0)
        result = item.get("result")
        if isinstance(result, dict):
            student = [str(result.get("content") or "").strip()] if str(result.get("content") or "").strip() else []
            for pic in result.get("pics") or []:
                ref = image_ref("student", pic.get("pic") if isinstance(pic, dict) else pic)
                if ref:
                    question["images"].append(ref)
        else:
            student = answer_list(result)
        correct_flag = item.get("correct")
        correct = None
        if kind in {"single", "multiple", "judge", "fill_blank"} and graded and correct_flag in (0, 1, True, False):
            correct = bool(correct_flag)
        parsed_correct = bool_value(mine.get("correct"))
        if parsed_correct is not None and kind != "subjective":
            correct = parsed_correct
        answer = answer_list(content.get("answer"))
        if not answer and correct is True and student:
            answer = list(student)
        body = html_text(content.get("body"))
        question.update(
            body=body,
            options=options_list(content.get("options")),
            answer=sort_choice(answer),
            answer_known=bool(answer),
            remark=html_text(content.get("remark")),
            student_answer=sort_choice(student) if kind != "subjective" else student,
            correct=correct,
            score=student_score if graded else None,
            max_score=max_score,
            graded=graded,
        )
        cover = item.get("cover") or content.get("cover")
        slide = image_ref("slide", cover)
        if slide:
            question["images"].insert(0, slide)
        rich = content.get("remarkRich") if isinstance(content.get("remarkRich"), dict) else {}
        for shape in rich.get("shapes") or []:
            if isinstance(shape, dict):
                ref = image_ref("remark", shape.get("url") or shape.get("URL"))
                if ref:
                    question["images"].append(ref)
        if rich.get("shapes"):
            question["has_remark_images"] = True
        # The slide image is the only record of a subjective prompt and of any
        # figure not present in the structured body.
        question["needs_vision"] = bool(slide) and (not has_meaningful_text(body) or kind == "subjective" or bool(rich.get("shapes")))
        _rich_images(question, content.get("body"), content.get("options"), content.get("remark"))
        questions.append(question)
    return questions


# ---------------------------------------------------------------- courseware cards (type 2)

def parse_cards(activity: dict[str, Any], classroom_id: Any, detlist_payload: Any) -> list[dict[str, Any]]:
    data = body_data(detlist_payload) or {}
    results: dict[str, dict[str, Any]] = {}
    raw_results = data.get("problem_results")
    if isinstance(raw_results, dict):
        raw_results = [{"problem_id": key, **value} if isinstance(value, dict) else {"problem_id": key, "result": value}
                       for key, value in raw_results.items()]
    for item in raw_results or []:
        if isinstance(item, dict):
            results[str(item.get("problem_id") or item.get("ProblemID") or "")] = item
    questions = []
    for slide in data.get("Slides") or []:
        if not isinstance(slide, dict):
            continue
        problem = slide.get("Problem") or slide.get("problem")
        if not isinstance(problem, dict) or not problem:
            continue
        pid = problem.get("ProblemID") or problem.get("problem_id") or slide.get("SlideID")
        kind = problem_type(problem.get("Type") or problem.get("ProblemType"))
        question = base_question(source="cards", classroom_id=classroom_id, activity=activity,
                                 problem_id=pid, kind=kind)
        result = results.get(str(pid), {})
        answer = answer_list(problem.get("Answer"))
        if not answer and isinstance(problem.get("Blanks"), list):
            answer = ["/".join(str(v) for v in (blank.get("Answers") or [])) for blank in problem["Blanks"] if isinstance(blank, dict)]
        question.update(
            page=_int(slide.get("PageIndex")) or None,
            index=_int(slide.get("PageIndex")) or None,
            body=html_text(problem.get("Body")),
            options=options_list(problem.get("Options")),
            answer=sort_choice(answer), answer_known=bool(answer),
            remark=html_text(problem.get("Remark")),
            max_score=_num(problem.get("Score")),
            student_answer=answer_list(result.get("result") or result.get("answer")),
            correct=bool_value(result.get("correct")),
            score=_num(result.get("score") if result.get("score") is not None else result.get("grade")),
        )
        if kind in {"single", "multiple", "judge"}:
            question["student_answer"] = sort_choice(question["student_answer"])
        slide_ref = image_ref("slide", slide.get("Cover"))
        if slide_ref:
            question["images"].append(slide_ref)
        question["needs_vision"] = not has_meaningful_text(question["body"]) and bool(slide_ref)
        _rich_images(question, problem.get("Body"), problem.get("Options"), problem.get("Remark"))
        questions.append(question)
    return questions


def question_text(question: dict[str, Any]) -> str:
    """Searchable/retrievable text of a question including vision transcription."""
    parts = [question.get("activity_title"), question.get("body")]
    parts += [f"{o.get('key')}. {o.get('value')}" for o in question.get("options") or []]
    parts += [question.get("remark"), (question.get("vision") or {}).get("text")]
    return "\n".join(str(part) for part in parts if part)


def search_text(question: dict[str, Any]) -> str:
    """Everything a student may search for: text, options, answers, remarks,
    vision transcriptions and the student's own answers."""
    vision = question.get("vision") or {}
    parts = [question_text(question), *(question.get("answer") or []), vision.get("answer_text"),
             *(str(value) for value in question.get("student_answer") or []), question.get("type_text"),
             question.get("source_label")]
    return "\n".join(str(part) for part in parts if part)
