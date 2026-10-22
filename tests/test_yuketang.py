from tests import isolation  # Must precede application imports.

import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from qwen_asr_app.yuketang import parsing, retrieval
from qwen_asr_app.yuketang.store import BankStore
from qwen_asr_app.yuketang.sync import ClassroomSync
from qwen_asr_app.yuketang.vision import transcribe_classroom

FIXTURES = Path(__file__).parent / "fixtures" / "yuketang"
OBJECTIVE_LESSON = "1801117984421960576"
SUBJECTIVE_LESSON = "1801064923783613184"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def activities():
    return {str(a["id"]): a for a in fixture("activities.json")["data"]["activities"]}


def lesson_answers(folder):
    return {path.stem.split("_")[-1]: json.loads(path.read_text()) for path in (FIXTURES / folder).glob("student_answer_*.json")}


class FakeSession:
    """Answers the read-only GETs the sync makes from the sanitized fixtures."""

    def __init__(self):
        self.calls, self.opened, self.downloads = [], [], []

    def get_json(self, url, headers=None):
        self.calls.append(url)
        parsed = urlparse(url)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        path = parsed.path
        if "/v2/api/web/classrooms/" in path:
            return {"errcode": 0, "data": {"uv_id": 3278, "course_name": "通信原理C（混合式）", "name": "示例班级"}}
        if "/logs/learn/" in path:
            return fixture("activities.json")
        if path == "/v/exam/cover":
            return fixture({"2187205": "exam/cover_main.json", "2176756": "exam/cover_closed.json"}.get(query["exam_id"], "exam/cover_pending.json"))
        if path.endswith("/exam_room/show_paper"):
            return fixture("exam/show_paper.json")
        if path.endswith("/exam_room/problem_results"):
            return fixture("exam/problem_results.json")
        if path.endswith("/exam_room/cover"):
            return fixture("exam/exam_cover.json")
        folder = {OBJECTIVE_LESSON: "lesson_objective", SUBJECTIVE_LESSON: "lesson_subjective"}.get(query.get("lesson_id"))
        if path.endswith("/student/lesson-info"):
            return fixture(f"{folder}/lesson_info.json")
        if path.endswith("/student/detail"):
            return fixture(f"{folder}/student_detail.json")
        if path.endswith("/problem/student-answer"):
            name = f"{folder}/student_answer_{query['problemId']}.json"
            if (FIXTURES / name).is_file():
                return fixture(name)
            return {"code": 0, "data": {"content": {}, "problem": {}}}
        if path.endswith("/quiz/personal_result"):
            return fixture("quiz/personal_result.json")
        if path.endswith("/quiz/problem_shape"):
            return fixture(f"quiz/problem_shape_{query['problem_id']}.json")
        if "/cards/detlist/" in path:
            return fixture("cards/detlist.json")
        raise AssertionError(f"unexpected request {url}")

    def download(self, url):
        self.downloads.append(url)
        return b"\x89PNG\r\n\x1a\n" + url.encode()

    def open_exam(self, cid, exam_id, timeout=30):
        self.opened.append(str(exam_id))
        return True


class FixturePrivacyTests(unittest.TestCase):
    # Real names, the student number and user ids are kept only as hash
    # prefixes so this guard does not itself publish them.
    PERSONAL = [(3, '9faa0c7511737f44'), (9, '06e3677921415291'), (3, '1e8d527b34011d08'), (3, '79ffe5731926e0ea'), (8, 'eb49c7491446cfcb'), (8, 'ac302881bf1aa1c6')]

    def test_fixtures_hold_no_personal_data_or_signed_urls(self):
        import hashlib
        banned = re.compile(r"thirdwx|token=|auth_key|[?&]e=\d")
        files = list(FIXTURES.rglob("*.json"))
        self.assertGreater(len(files), 10)
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(banned.search(text), path.name)
            for size, digest in self.PERSONAL:
                windows = {text[i:i + size] for i in range(len(text) - size + 1)}
                self.assertFalse(any(hashlib.sha256(w.encode()).hexdigest()[:16] == digest for w in windows), path.name)


class ParsingTests(unittest.TestCase):
    def test_activity_list_and_fingerprint(self):
        items, has_more, _ = parsing.parse_activities(fixture("activities.json"))
        self.assertFalse(has_more)
        self.assertEqual({item["type"] for item in items}, {2, 4, 5, 14, 15})
        exam = next(item for item in items if item["courseware_id"] == "2187205")
        self.assertEqual(parsing.activity_fingerprint(exam), parsing.activity_fingerprint(dict(exam)))
        self.assertNotEqual(parsing.activity_fingerprint(exam), parsing.activity_fingerprint({**exam, "score": 1}))

    def test_api_errors_raise(self):
        with self.assertRaises(parsing.YuketangAPIError):
            parsing.body_data({"errcode": 10007, "errmsg": "未到试卷查看时间"})

    def test_strip_url_drops_signatures(self):
        self.assertEqual(parsing.strip_url("https://a.yuketang.cn/x.jpg?e=1&token=abc"), "https://a.yuketang.cn/x.jpg")

    def test_exam_visibility_follows_teacher_paper_switch(self):
        self.assertTrue(parsing.exam_results_visible(parsing.body_data(fixture("exam/cover_main.json"))))
        closed = parsing.body_data(fixture("exam/cover_closed.json"))
        self.assertFalse(parsing.exam_results_visible(closed))
        self.assertEqual(parsing.exam_score(closed), "6/6")
        self.assertFalse(parsing.exam_results_visible(parsing.body_data(fixture("exam/cover_pending.json"))))

    def test_parse_exam_merges_answers_and_results(self):
        activity = activities()["34905382"]
        questions = parsing.parse_exam(activity, "26260946", fixture("exam/show_paper.json"),
                                       fixture("exam/problem_results.json"), fixture("exam/exam_cover.json"))
        self.assertEqual(len(questions), 3)
        for question in questions:
            self.assertEqual(question["bank_category"], "exam")
            self.assertTrue(question["body"])
            self.assertTrue(question["answer_known"])
            self.assertTrue(question["answer"])
            self.assertIn(question["correct"], {True, False})
            self.assertTrue(question["id"].startswith("exam:2187205:"))

    def test_parse_objective_lesson(self):
        activity = activities()["35003874"]
        questions = parsing.parse_lesson(activity, "26260946", fixture("lesson_objective/student_detail.json"),
                                         lesson_answers("lesson_objective"), fixture("lesson_objective/lesson_info.json"))
        self.assertEqual(len(questions), 2)
        first = questions[0]
        self.assertEqual(first["source_label"], "课上习题")
        self.assertTrue(first["body"] and first["options"] and first["answer"])
        self.assertTrue(first["page"])
        self.assertTrue(first["graded"])
        self.assertTrue(any(image["kind"] == "slide" for image in first["images"]))
        self.assertTrue(all("?" not in image["source"] for q in questions for image in q["images"]))

    def test_parse_subjective_lesson_needs_vision_and_is_ungraded(self):
        activity = activities()["34996679"]
        questions = parsing.parse_lesson(activity, "26549233", fixture("lesson_subjective/student_detail.json"),
                                         lesson_answers("lesson_subjective"), fixture("lesson_subjective/lesson_info.json"))
        self.assertEqual(len(questions), 2)
        for question in questions:
            self.assertEqual(question["type_text"], "主观题")
            self.assertTrue(question["needs_vision"])
            self.assertFalse(question["graded"])
            self.assertIsNone(question["correct"])
            self.assertTrue(question["student_answer"])
            self.assertTrue(any(image["kind"] == "remark" for image in question["images"]))
        self.assertEqual(parsing.lesson_identity(fixture("lesson_subjective/lesson_info.json")), "10000002")

    def test_parse_quiz(self):
        activity = activities()["34665451"]
        shapes = {path.stem.split("_")[-1]: json.loads(path.read_text()) for path in (FIXTURES / "quiz").glob("problem_shape_*.json")}
        questions = parsing.parse_quiz(activity, "26549233", fixture("quiz/personal_result.json"), shapes)
        self.assertEqual(len(questions), 2)
        self.assertTrue(all(q["bank_category"] == "exam" and q["answer"] and q["images"] for q in questions))
        self.assertTrue(all(q["needs_vision"] for q in questions))

    def test_parse_cards_without_problems(self):
        activity = activities()["33658965"]
        self.assertEqual(parsing.parse_cards(activity, "26260946", fixture("cards/detlist.json")), [])


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = BankStore(Path(temporary.name) / "bank")

    def test_bind_unbind_and_rename(self):
        self.store.bind("通信原理 C", "26260946", university_id="3278")
        self.store.bind("通信原理 C", "26260946", university_id="3278")
        self.assertEqual([item["classroom_id"] for item in self.store.classrooms_for("通信原理 C")], ["26260946"])
        with self.assertRaises(ValueError):
            self.store.bind("通信原理 C", "abc")
        self.store.rename_category("通信原理 C", "通信原理")
        self.assertEqual(self.store.categories_for("26260946"), ["通信原理"])
        self.store.rename_category("通信原理", None)
        self.assertEqual(self.store.classrooms_for("通信原理"), [])
        self.store.bind("光电子学", "26549233", university_id="0")
        self.assertTrue(self.store.unbind("光电子学", "26549233"))
        self.assertFalse(self.store.unbind("光电子学", "26549233"))

    def test_resaving_questions_keeps_vision_and_images(self):
        question = {"id": "lesson:1:2", "needs_vision": True, "images": [{"kind": "slide", "source": "https://x/a.jpg"}]}
        self.store.save_questions("1", "10", [question])
        self.store.update_question("1", "lesson:1:2", {"vision": {"status": "done", "text": "题干"},
                                                       "images": [{"kind": "slide", "source": "https://x/a.jpg", "path": "images/10/a.jpg"}]})
        self.store.save_questions("1", "10", [dict(question)])
        saved = self.store.questions("1")[0]
        self.assertEqual(saved["vision"]["text"], "题干")
        self.assertEqual(saved["images"][0]["path"], "images/10/a.jpg")

    def test_image_path_is_confined(self):
        with self.assertRaises(FileNotFoundError):
            self.store.image_path("1", "../../bindings.json")


class SyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = BankStore(Path(temporary.name) / "bank")

    def run_sync(self, session, **kwargs):
        return ClassroomSync(session, self.store, "26260946", **kwargs).run()

    def test_full_sync_from_fixtures(self):
        session = FakeSession()
        result = self.run_sync(session)
        counts = result["counts"]
        self.assertEqual(counts["exam"], 5)          # 3 exam paper problems + 2 quiz problems
        self.assertEqual(counts["lesson"], 4)
        self.assertEqual(counts["courseware"], 0)
        self.assertEqual(result["unsupported"], 1)  # MOOC video
        self.assertEqual(result["errors"], 0)
        self.assertEqual(session.opened, ["2187205"])  # closed and unpublished exams are never opened
        states = {s["activity"]["courseware_id"]: s for s in self.store.activity_states("26260946")}
        self.assertEqual(states["2194951"]["status"], "pending")
        self.assertIn("未开放试卷", states["2176756"]["reason"])
        self.assertEqual(states["2187205"]["status"], "complete")
        self.assertGreater(result["images"], 0)
        self.assertTrue(all("?" not in url or "token" not in url for url in session.downloads))
        saved = json.dumps(self.store.questions("26260946"), ensure_ascii=False)
        self.assertNotIn("token=", saved)
        self.assertTrue(all(image.get("path") for q in self.store.questions("26260946") for image in q.get("images", []) if image.get("source")))

    def test_second_sync_is_incremental(self):
        self.run_sync(FakeSession())
        session = FakeSession()
        result = self.run_sync(session)
        self.assertGreaterEqual(result["skipped"], 4)
        self.assertEqual(session.opened, [])
        self.assertFalse(any("student-answer" in url and OBJECTIVE_LESSON in url for url in session.calls))
        self.assertEqual(result["counts"]["lesson"], 4)

    def test_failed_image_download_is_retried_without_leaking_signed_url(self):
        class Flaky(FakeSession):
            def download(self, url):
                if "cover" in url:
                    raise RuntimeError(f"APIRequestContext.get: socket hang up\nCall log:\n  - GET {url}?e=1&token=secret")
                return super().download(url)
        with patch("qwen_asr_app.yuketang.sync.RETRY_DELAY", 0):
            result = self.run_sync(Flaky())
        self.assertGreater(result["image_errors"], 0)
        saved = json.dumps(self.store.questions("26260946"), ensure_ascii=False)
        self.assertNotIn("token", saved)
        self.assertIn("socket hang up", saved)
        states = {s["activity"]["courseware_id"]: s for s in self.store.activity_states("26260946")}
        self.assertEqual(states[OBJECTIVE_LESSON]["status"], "images_pending")
        session = FakeSession()
        self.run_sync(session)
        states = {s["activity"]["courseware_id"]: s for s in self.store.activity_states("26260946")}
        self.assertEqual(states[OBJECTIVE_LESSON]["status"], "complete")
        self.assertTrue(all(image.get("path") for q in self.store.questions("26260946") for image in q.get("images", [])))

    def test_stop_aborts_between_activities(self):
        from qwen_asr_app.yuketang.sync import SyncAborted
        with self.assertRaises(SyncAborted):
            self.run_sync(FakeSession(), should_stop=lambda: True)


class VisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcription_is_cached_and_resumable(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = BankStore(Path(temporary.name) / "bank")
        ClassroomSync(FakeSession(), store, "26260946").run()
        pending = [q for q in store.questions("26260946") if q.get("needs_vision")]
        self.assertTrue(pending)
        calls = []

        async def request(instructions, folder, paths):
            calls.append(paths)
            for path in paths:
                self.assertTrue(Path(path).is_relative_to(folder))
            ids = re.findall(r'"image_id": "(img\d+)"', instructions)
            return {"images": [{"image_id": i, "text": "转写题干", "answer_text": "参考答案"} for i in ids]}

        summary = await transcribe_classroom(store, "26260946", limit=1, request=request)
        self.assertEqual(summary["done"], 1)
        done = [q for q in store.questions("26260946") if (q.get("vision") or {}).get("status") == "done"]
        self.assertEqual(len(done), 1)
        self.assertIn("转写题干", done[0]["vision"]["text"] + done[0]["vision"]["answer_text"])
        before = len(calls)
        summary = await transcribe_classroom(store, "26260946", request=request)
        self.assertEqual(summary["pending"], len(pending) - 1)
        self.assertGreaterEqual(len(calls), before)

    async def test_without_vision_channel_questions_stay_pending(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = BankStore(Path(temporary.name) / "bank")
        ClassroomSync(FakeSession(), store, "26260946").run()
        with patch("qwen_asr_app.yuketang.vision.vision_provider", return_value=None):
            summary = await transcribe_classroom(store, "26260946")
        self.assertTrue(summary["skipped_reason"])
        self.assertEqual(summary["done"], 0)


def bank_question(**values):
    base = {"id": values.get("id", "x"), "source": "exam", "source_label": "课前/课后测试与考试", "activity_title": "测试",
            "date": "2026-09-01", "type_text": "单选题", "body": "", "options": [], "answer": ["A"], "answer_known": True}
    return {**base, **values}


class RetrievalTests(unittest.TestCase):
    def test_same_day_lesson_problems_rank_first(self):
        questions = [
            bank_question(id="old", body="随机过程的平稳性与自相关函数", date="2026-09-10"),
            bank_question(id="same", source="lesson", source_label="课上习题", body="码型", date="2026-10-21", page=3),
            bank_question(id="near", body="线路编码 AMI 码 HDB3 码的特点", date="2026-10-19"),
            bank_question(id="far", body="无关内容", date="2026-06-01"),
        ]
        ranked = retrieval.rank(questions, lecture_day="2026-10-21", transcript="今天讲 AMI 码和 HDB3 码，线路编码的特点")
        ids = [q["id"] for q in ranked]
        self.assertEqual(ids[0], "same")
        self.assertIn("near", ids)
        self.assertNotIn("far", ids)
        self.assertTrue(ranked[0]["_relevance"]["same_lesson"])

    def test_compact_and_appendix(self):
        item = retrieval.compact({**bank_question(id="q1", body="HDB3 码最长连零个数？", options=[{"key": "A", "value": "3"}, {"key": "B", "value": "4"}],
                                                  student_answer=["B"], correct=False, score=0.0, max_score=2.0, remark="不超过 3 个连零"),
                                  "_relevance": {"same_lesson": True}})
        self.assertEqual(item["是否答对"], False)
        self.assertEqual(item["得分"], "0/2")
        self.assertNotIn("images", item)
        text = retrieval.appendix([item])
        self.assertIn("## 相关习题与答案", text)
        self.assertIn("正确答案：A", text)
        self.assertIn("我的答案：B（答错，得分 0/2）", text)
        self.assertEqual(retrieval.ensure_section("# 笔记\n\n## 相关习题与答案\n\n已写", [item]), "# 笔记\n\n## 相关习题与答案\n\n已写")
        self.assertTrue(retrieval.ensure_section("# 笔记", [item]).endswith(text))
        self.assertEqual(retrieval.ensure_section("# 笔记", []), "# 笔记")

    def test_summary_instructions_include_question_block(self):
        from qwen_asr_app.ai import summary_instructions
        item = retrieval.compact(bank_question(id="q1", body="HDB3 码最长连零个数？"))
        full = summary_instructions({"_yuketang_questions": [item]})
        self.assertIn("<yuketang_questions>", full)
        self.assertIn("HDB3 码最长连零个数？", full)
        self.assertIn("## 相关习题与答案", full)
        self.assertIn("考点", full)
        chunk = summary_instructions({"_yuketang_questions": [item], "_detailed_chunk": True})
        self.assertIn("不要输出", chunk)
        self.assertNotIn("<yuketang_questions>", summary_instructions({}))
        self.assertNotIn("<yuketang_questions>", summary_instructions({"_yuketang_questions": [item]}, live=True))


class NoteInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_note_gets_questions_and_answer_section(self):
        import app
        from qwen_asr_app.storage import RecordingRecord, RecordingStore
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        bank = BankStore(root / "bank")
        bank.bind("通信原理 C", "26260946", university_id="3278")
        bank.save_questions("26260946", "1", [bank_question(
            id="lesson:1:2", source="lesson", source_label="课上习题", activity_title="第六章", date="2026-10-21", page=7,
            body="AMI 码中“1”如何编码？", options=[{"key": "A", "value": "交替极性"}, {"key": "B", "value": "固定正电平"}],
            student_answer=["B"], correct=False, score=0.0, max_score=2.0)])
        record = RecordingRecord(root / "original", {
            "id": "ykt-note", "title": "第六章", "category": "通信原理 C", "status": "completed",
            "started_at": "2026-10-21T08:00:00+08:00", "sample_count": 16000})
        record.segments = [{"id": 1, "start": 0, "end": 1, "raw": "AMI 码把“1”交替编成正负极性。"}]
        record.save_all()
        store = RecordingStore.__new__(RecordingStore)
        store.active = None
        store.paths = {record.id: record.directory}
        summary = AsyncMock(return_value={"summary": "## 知识点\n\nAMI 码交替极性。"})
        with patch.dict(os.environ, {"QWEN_ASR_YUKETANG_DIR": str(root / "bank")}), \
             patch.object(app, "recording_store", store), \
             patch.object(app.app_settings, "storage_root", root), \
             patch.object(app.app_settings, "library_layout", "", create=True), \
             patch.object(app.app_settings, "add_category", side_effect=lambda value: value), \
             patch.object(app.ai_preferences, "read", return_value={}), \
             patch.object(app, "generate_recording_title", AsyncMock(return_value="第六章")), \
             patch("qwen_asr_app.batch_ai.summarize_transcript", summary), \
             patch("qwen_asr_app.batch_ai.summarize_note_abstract", AsyncMock(return_value="AMI 码。")):
            app.start_summary_job(store.find(record.id), {"config": {"ai": {"summary_provider": {"model": "mock"}}}}, automatic=True)
            await app.background_summary_tasks[record.id]
            saved = store.find(record.id)
            config = summary.await_args.args[2]
            self.assertEqual(config["_yuketang_questions"][0]["题干"], "AMI 码中“1”如何编码？")
            note = saved.ai_summary_path.read_text(encoding="utf-8")
            self.assertIn("## 相关习题与答案", note)
            self.assertIn("我的答案：B（答错", note)
            self.assertEqual(saved.metadata["ai_note_summary"]["yuketang_question_ids"], ["lesson:1:2"])


class RouteTests(unittest.TestCase):
    def test_bind_status_questions_and_guard(self):
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        from qwen_asr_app.yuketang.routes import yuketang_router
        from qwen_asr_app.yuketang.service import YuketangService
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = BankStore(Path(temporary.name) / "bank")
        ClassroomSync(FakeSession(), store, "26260946").run()

        def guard(request):
            if request.headers.get("X-QwenASR-Client") != "course-files":
                raise HTTPException(403, "forbidden")
        api = FastAPI()
        api.include_router(yuketang_router(YuketangService(store), guard, lambda: ["通信原理 C"]))
        client = TestClient(api)
        headers = {"X-QwenASR-Client": "course-files"}
        self.assertEqual(client.post("/api/yuketang/bind", json={"category": "通信原理 C", "classroom_id": "26260946"}).status_code, 403)
        self.assertEqual(client.post("/api/yuketang/bind", headers=headers, json={"category": "不存在", "classroom_id": "1"}).status_code, 404)
        bound = client.post("/api/yuketang/bind", headers=headers, json={"category": "通信原理 C", "classroom_id": "26260946", "university_id": "3278"}).json()
        self.assertEqual(bound["classrooms"][0]["counts"]["total"], 9)
        self.assertTrue(bound["classrooms"][0]["pending_activities"])
        listed = client.get("/api/yuketang/questions", params={"category": "通信原理 C", "source": "lesson"}).json()
        self.assertEqual(len(listed["questions"]), 4)
        image = next(i for q in listed["questions"] for i in q["images"] if i.get("path"))
        self.assertEqual(client.get(f"/api/yuketang/image/26260946/{image['path']}").status_code, 200)
        self.assertEqual(client.get("/api/yuketang/image/26260946/bindings.json").status_code, 404)


if __name__ == "__main__":
    unittest.main()
