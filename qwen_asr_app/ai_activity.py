"""Live AI request activity, separate from durable completion progress.

Only phases, timings and character counts leave the request context. Prompts,
model output and credentials never enter the task feed.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any


_tasks: dict[str, "Activity"] = {}
_current: ContextVar["Activity | None"] = ContextVar("ai_task_activity", default=None)
_request: ContextVar["RequestActivity | None"] = ContextVar("ai_request_activity", default=None)


class Activity:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.requests: list[RequestActivity] = []

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        active = [request for request in self.requests if request.active]
        for request in active:request.refresh_session()
        priority = {"waiting": 0, "connecting": 1, "processing": 2,'reading':2,'thinking':2,'provider_busy':2,'upstream_retry':2, "receiving": 3, "checking": 4}
        latest = max(active, key=lambda request: (priority.get(request.phase, 0), request.updated), default=None)
        # Age the request whose phase is displayed. A newly queued sibling
        # must not make an old, silent output stream appear active again.
        last_event = latest.updated if latest else max((request.updated for request in self.requests), default=self.started)
        return {
            "phase": latest.phase if latest else "checking" if self.requests else "waiting",
            "elapsed_seconds": int(max(0, now - self.started)),
            "idle_seconds": int(max(0, now - last_event)),
            "output_chars": sum(request.output_chars for request in self.requests),
            "current_output_chars": sum(request.output_chars for request in active),
            "request_elapsed_seconds": int(max(0,now-min((request.started for request in active),default=now))),
            "active_requests": len(active),
            "requests": len(self.requests),
            'request_activity':[request.snapshot(now) for request in active],
        }


class RequestActivity:
    def __init__(self, activity: Activity) -> None:
        self.phase = "waiting"
        self.updated = time.monotonic()
        self.started = self.updated
        self.output_chars = 0
        self.active = True
        self.label='';self.pages=[];self.session=None;self.session_stats={}
        activity.requests.append(self)

    def refresh_session(self):
        if self.session is None:return
        stats=self.session.snapshot()
        if stats.get('last_event_at',0)>self.session_stats.get('last_event_at',0):
            self.phase=stats['phase']
            self.updated=time.monotonic()-max(0,time.time()-stats['last_event_at'])
        self.session_stats=stats

    def snapshot(self,now):
        return {'label':self.label,'pages':self.pages,'phase':self.phase,
            'elapsed_seconds':int(max(0,now-self.started)),'idle_seconds':int(max(0,now-self.updated)),
            'output_chars':self.output_chars,**{k:v for k,v in self.session_stats.items() if k not in {'phase','last_event_at'}}}

    def update(self, phase: str, *, output_chars: int | None = None) -> None:
        self.phase = phase
        self.updated = time.monotonic()
        if output_chars is not None:
            self.output_chars = max(self.output_chars, output_chars)


@contextmanager
def track_ai_activity(task_id: str):
    activity = Activity()
    _tasks[task_id] = activity
    token = _current.set(activity)
    try:
        yield activity
    finally:
        _current.reset(token)
        if _tasks.get(task_id) is activity:
            del _tasks[task_id]


@contextmanager
def track_ai_request(*,label='',pages=()):
    activity = _current.get()
    request = RequestActivity(activity) if activity is not None else None
    if request is not None:request.label=label;request.pages=list(pages)
    token = _request.set(request)
    try:
        yield
    finally:
        _request.reset(token)
        if request is not None:
            request.active = False


def report_ai_activity(phase: str, *, output_chars: int | None = None) -> None:
    request = _request.get()
    if request is not None:
        request.update(phase, output_chars=output_chars)


def bind_ai_session(session_id):
    request=_request.get()
    if request is None or request.session is not None:return
    from .agy_activity import SessionActivity
    try:request.session=SessionActivity(session_id)
    except ValueError:pass


def task_ai_activity(task_id: str) -> dict[str, Any] | None:
    activity = _tasks.get(task_id)
    return activity.snapshot() if activity is not None else None
