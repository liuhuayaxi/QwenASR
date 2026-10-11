from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qwen_asr_app import course_tasks


class CourseTaskFeedTests(unittest.TestCase):
    def write_state(self, root: Path, items, imports):
        state = root / "smartclass"
        state.mkdir(parents=True)
        (state / "queue.json").write_text(json.dumps({"items": items, "paused": False}), encoding="utf-8")
        (state / "imports.json").write_text(json.dumps(imports), encoding="utf-8")

    def group(self, ident, title):
        return {"id": ident, "title": title, "course_name": "测试课程", "start": "2026-10-11 08:00", "stop": "2026-10-11 09:30"}

    def test_completed_course_groups_are_removed_and_live_phases_are_exposed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            live = self.group("live", "正在处理的课次")
            done = self.group("done", "已经完成的课次")
            self.write_state(root, [{"group": live, "state": "running"}, {"group": done, "state": "completed"}], {
                "live": {"progress": 37, "state": "running", "message": "正在转写", "phases": {
                    "download": {"label": "获取回放音频", "percent": 100, "detail": "已获取", "state": "completed"},
                    "asr": {"label": "本地语音转写", "percent": 42, "detail": "42.0/90.0 分钟", "state": "running"},
                    "correction": {"label": "AI 精修", "percent": 10, "detail": "2/20 段", "state": "running"},
                    "summary": {"label": "课堂笔记", "percent": 0, "detail": "等待转写完成", "state": "pending"},
                }},
                "done": {"progress": 100, "state": "completed", "phases": {}},
            })
            with patch.object(course_tasks, "PRIVATE_ROOT", root):
                result = course_tasks.course_tasks()
            self.assertEqual(len(result), 1)
            task = result[0]
            self.assertEqual(task["total"], 1)
            self.assertEqual(task["completed"], 0)
            self.assertEqual(task["progress"], 37)
            self.assertEqual([item["title"] for item in task["course_items"]], ["正在处理的课次"])
            self.assertEqual(len(task["course_items"][0]["phases"]), 4)
            self.assertEqual(task["course_items"][0]["phases"][1]["detail"], "42.0/90.0 分钟")

    def test_all_completed_queue_is_absent_from_task_feed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            done = self.group("done", "已经完成的课次")
            self.write_state(root, [{"group": done, "state": "completed"}], {"done": {"state": "completed"}})
            with patch.object(course_tasks, "PRIVATE_ROOT", root):
                self.assertEqual(course_tasks.course_tasks(), [])


if __name__ == "__main__":
    unittest.main()
