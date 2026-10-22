"""One background Yuketang job at a time (login, sync or image transcription).

The browser profile can only be opened by one Chrome process, so login and
sync share a lock. Status is kept in memory and mirrored to ``job.json`` so the
UI can show the last result after a restart.
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from ..durable import atomic_json
from .store import BankStore, now_iso


class YuketangService:
    def __init__(self, store: BankStore | None = None) -> None:
        self.store = store or BankStore()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.job: dict[str, Any] = self._load_job()

    def _load_job(self) -> dict[str, Any]:
        try:
            job = json.loads((self.store.root / "job.json").read_text())
        except (OSError, ValueError):
            return {"status": "idle"}
        if job.get("status") == "running":
            job.update(status="interrupted", message="上次任务在服务重启时中断；已完成的部分已保存，可重新同步")
        return job

    def _save_job(self) -> None:
        try:
            self.store.ensure()
            atomic_json(self.store.root / "job.json", {k: v for k, v in self.job.items() if k != "log"} | {"log": self.job.get("log", [])[-40:]})
        except OSError:
            pass

    def _update(self, **values: Any) -> None:
        self.job.update(values, updated_at=now_iso())
        self._save_job()

    def _log(self, message: str) -> None:
        log = self.job.setdefault("log", [])
        log.append(f"{now_iso()[11:19]} {message}")
        del log[:-200]
        self._update(message=message)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self, category: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"job": {**self.job, "log": self.job.get("log", [])[-15:]}, "running": self.running,
                                  "store": str(self.store.root)}
        try:
            import playwright  # noqa: F401
            result["playwright"] = True
        except ImportError:
            result["playwright"] = False
        result["login"] = self.login_state()
        if category is not None:
            classrooms = []
            for item in self.store.classrooms_for(category):
                info = self.store.classroom(item["classroom_id"])
                questions = self.store.questions(item["classroom_id"])
                states = self.store.activity_states(item["classroom_id"])
                classrooms.append({**item, "course_name": info.get("course_name", ""),
                                   "classroom_name": info.get("classroom_name", ""),
                                   "last_sync": info.get("last_sync", ""),
                                   "counts": self.store.counts(questions),
                                   "pending_activities": [{"title": (s.get("activity") or {}).get("title"), "status": s.get("status"),
                                                           "reason": s.get("reason") or s.get("error", "")}
                                                          for s in states if s.get("status") not in {"complete"}][:20]})
            result.update(category=category, classrooms=classrooms,
                          counts=self.store.counts(self.store.course_questions(category)))
        return result

    def login_state(self) -> dict[str, Any]:
        state = self.job.get("login_state") if isinstance(self.job.get("login_state"), dict) else {}
        has_profile = (self.store.profile_dir / "Default").is_dir()
        listed = self.store.course_list()
        return {"profile_exists": has_profile, "course_list_at": listed["fetched_at"],
                "course_count": len(listed["courses"]), **state}

    # ------------------------------------------------------------ jobs
    def _start(self, kind: str, target) -> dict[str, Any]:
        with self._lock:
            if self.running:
                raise RuntimeError("已有雨课堂任务正在运行，请等待完成")
            self._stop.clear()
            self.job = {"status": "running", "kind": kind, "started_at": now_iso(), "log": [],
                        "login_state": self.job.get("login_state", {})}
            self._save_job()

            def run():
                try:
                    result = target()
                    self._update(status="completed", result=result, finished_at=now_iso(),
                                 message=self.job.get("message") or "完成")
                except Exception as exc:  # surfaced in the UI
                    from .client import LoginRequired, PlaywrightMissing
                    login = isinstance(exc, LoginRequired)
                    if login:
                        self.job["login_state"] = {"logged_in": False, "checked_at": now_iso(), "error": str(exc)}
                    self._update(status="login_required" if login else "failed", error=str(exc),
                                 missing_playwright=isinstance(exc, PlaywrightMissing), finished_at=now_iso(),
                                 message=str(exc))
            self._thread = threading.Thread(target=run, name=f"yuketang-{kind}", daemon=True)
            self._thread.start()
            return self.job

    def stop(self) -> None:
        self._stop.set()

    def start_login(self, timeout: float = 600) -> dict[str, Any]:
        def target():
            from .client import YuketangSession
            self._log("已打开 Chrome 窗口，请用微信扫码登录雨课堂（10 分钟内有效）")
            with YuketangSession(self.store.profile_dir, headless=False) as session:
                user = session.interactive_login(timeout=timeout)
                try:
                    from .sync import refresh_course_list
                    listed = refresh_course_list(session, self.store)
                    self._log(f"已读取 {len(listed['courses'])} 门雨课堂课程")
                except Exception as exc:
                    self._log(f"课程列表读取失败：{exc}")
            self.job["login_state"] = {"logged_in": True, "checked_at": now_iso()}
            self._log("雨课堂登录成功，登录状态已保存在本机")
            return {"logged_in": True, "user_id_known": bool(user.get("user_id"))}
        return self._start("login", target)

    def start_courses(self) -> dict[str, Any]:
        def target():
            from .sync import fetch_course_list
            listed = fetch_course_list(self.store)
            self.job["login_state"] = {"logged_in": True, "checked_at": now_iso()}
            self._log(f"已读取 {len(listed['courses'])} 门雨课堂课程")
            return {"courses": len(listed["courses"])}
        return self._start("courses", target)

    def courses(self, category: str | None = None) -> dict[str, Any]:
        """Cached course list, best match for ``category`` first."""
        listed = self.store.course_list()
        courses = listed["courses"]
        if category:
            from .retrieval import _tokens
            want = _tokens(category)
            def score(course):
                name = course.get("course_name", "")
                exact = name.replace(" ", "").lower() == category.replace(" ", "").lower()
                overlap = len(want & _tokens(name)) / max(1, len(want))
                return (exact, overlap)
            ranked = sorted(courses, key=score, reverse=True)
            best = ranked[0] if ranked and score(ranked[0])[1] >= 0.5 else None
            listed = {**listed, "suggested": best["classroom_id"] if best else ""}
        return listed

    def start_sync(self, category: str | None = None, classroom_ids: list[str] | None = None, *,
                   refresh: bool = False, transcribe: bool = False) -> dict[str, Any]:
        classrooms = self._targets(category, classroom_ids)

        def target():
            from .sync import sync_classrooms
            results = sync_classrooms(self.store, classrooms, refresh=refresh, progress=self._log,
                                      should_stop=self._stop.is_set)
            self.job["login_state"] = {"logged_in": True, "checked_at": now_iso()}
            output = {"classrooms": results}
            if transcribe:
                output["vision"] = self._transcribe([item["classroom_id"] for item in classrooms])
            self._log("雨课堂题库同步完成")
            return output
        return self._start("sync", target)

    def start_transcribe(self, category: str | None = None, classroom_ids: list[str] | None = None,
                         limit: int | None = None) -> dict[str, Any]:
        classrooms = self._targets(category, classroom_ids)
        return self._start("transcribe", lambda: {"vision": self._transcribe([c["classroom_id"] for c in classrooms], limit)})

    def _transcribe(self, classroom_ids: list[str], limit: int | None = None) -> list[dict[str, Any]]:
        from .vision import transcribe_classroom
        results = []
        for cid in classroom_ids:
            coroutine = transcribe_classroom(self.store, cid, limit=limit, progress=self._log)
            loop = getattr(self, "loop", None)
            if loop is not None and loop.is_running():
                # Share the service loop's AI gates with note generation.
                results.append(asyncio.run_coroutine_threadsafe(coroutine, loop).result())
            else:
                results.append(asyncio.run(coroutine))
        return results

    def _targets(self, category: str | None, classroom_ids: list[str] | None) -> list[dict[str, Any]]:
        if classroom_ids:
            from .store import classroom_id
            bound = {str(item["classroom_id"]): item for items in self.store.bindings().values() for item in items}
            return [bound.get(classroom_id(cid), {"classroom_id": classroom_id(cid)}) for cid in classroom_ids]
        if category:
            items = self.store.classrooms_for(category)
            if not items:
                raise ValueError("这门课还没有绑定雨课堂班级")
            return items
        items = [item for values in self.store.bindings().values() for item in values]
        if not items:
            raise ValueError("还没有绑定任何雨课堂班级")
        return list({str(item["classroom_id"]): item for item in items}.values())
