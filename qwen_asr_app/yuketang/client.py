"""Playwright-backed, read-only Yuketang session.

A persistent Chrome profile (first login by WeChat QR in a visible window)
holds the cookies. Every data call is a GET through the browser context, which
shares those cookies; exams are opened by navigating the same transit page the
web app uses ("查看试卷"), because the exam host issues a per-exam token.
Nothing here submits answers or changes anything on Yuketang.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

HOST = "https://changjiang.yuketang.cn"
EXAM_HOST = "https://changjiang-exam.yuketang.cn"
LOGIN_URL = f"{HOST}/web"
ALLOWED_IMAGE_HOSTS = (".yuketang.cn", ".xuetangx.com")


class LoginRequired(RuntimeError):
    """The saved session is missing or expired; ask the user to log in again."""


class PlaywrightMissing(RuntimeError):
    pass


def classroom_headers(classroom_id: Any, university_id: Any) -> dict[str, str]:
    uv = str(university_id if university_id not in (None, "") else 0)
    return {
        "accept": "application/json, text/plain, */*",
        "classroom-id": str(classroom_id or 0),
        "university-id": uv,
        "uv-id": uv,
        "x-client": "web",
        "xt-agent": "web",
        "xtbz": "ykt",
    }


EXAM_HEADERS = {"accept": "application/json, text/plain, */*", "x-client": "web", "xtbz": "cloud"}


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on local install
        raise PlaywrightMissing(
            "未安装 Playwright：请运行 .venv/bin/pip install playwright（使用本机 Google Chrome，无需下载浏览器）"
        ) from exc
    return sync_playwright


class YuketangSession:
    """Context manager around a persistent Chrome profile."""

    def __init__(self, profile_dir: Path, *, headless: bool = True, delay: float = 1.0,
                 channel: str = "chrome", log: Callable[[str], None] | None = None) -> None:
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.delay = max(0.0, float(delay))
        self.channel = channel
        self.log = log or (lambda message: None)
        self._manager = None
        self._playwright = None
        self.context = None
        self._page = None
        self._last = 0.0
        self.requests = 0
        self.user: dict[str, Any] = {}

    def __enter__(self) -> "YuketangSession":
        sync_playwright = _playwright()
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._manager = sync_playwright()
        self._playwright = self._manager.__enter__()
        options = dict(headless=self.headless, viewport={"width": 1280, "height": 860}, locale="zh-CN")
        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), channel=self.channel, **options)
        except Exception:
            if not self.channel:
                raise
            # Fall back to Playwright's bundled Chromium when Chrome is absent.
            self.context = self._playwright.chromium.launch_persistent_context(str(self.profile_dir), **options)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self._manager is not None:
                self._manager.__exit__(*exc)

    # ------------------------------------------------------------ pacing
    def _pace(self) -> None:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    @property
    def page(self):
        if self._page is None or self._page.is_closed():
            pages = [page for page in self.context.pages if not page.is_closed()]
            self._page = pages[0] if pages else self.context.new_page()
        return self._page

    # ------------------------------------------------------------ auth
    def has_session_cookie(self) -> bool:
        return any(cookie.get("name") == "sessionid" and cookie.get("value") for cookie in self.context.cookies(HOST))

    def check_login(self) -> dict[str, Any]:
        if not self.has_session_cookie():
            raise LoginRequired("尚未登录雨课堂，或登录已过期")
        try:
            payload = self.get_json(f"{HOST}/v2/api/web/userinfo", classroom_headers(0, 0))
        except LoginRequired:
            raise
        except Exception as exc:
            raise LoginRequired(f"无法确认雨课堂登录状态：{exc}") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if payload.get("errcode") not in (0, None) or not data:
            raise LoginRequired("雨课堂登录已过期，请重新扫码登录")
        user = data[0] if isinstance(data, list) else data
        self.user = {"user_id": str(user.get("user_id") or "")}
        return self.user

    def interactive_login(self, timeout: float = 600, on_wait: Callable[[], None] | None = None) -> dict[str, Any]:
        """Open the login page in the visible window and wait for WeChat QR login."""
        try:
            return self.check_login()
        except LoginRequired:
            pass
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.has_session_cookie():
                try:
                    return self.check_login()
                except LoginRequired:
                    pass
            if on_wait:
                on_wait()
            time.sleep(2)
        raise LoginRequired("等待扫码登录超时，请重试")

    # ------------------------------------------------------------ data
    def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        self._pace()
        self.requests += 1
        response = self.context.request.get(url, headers=headers or {}, timeout=30_000, max_redirects=0)
        status = response.status
        if status in (401, 403) and url.startswith(HOST) and "/userinfo" in url:
            raise LoginRequired("雨课堂登录已过期，请重新扫码登录")
        if status >= 300 and status < 400:
            raise LoginRequired("雨课堂要求重新登录")
        text = response.text()
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise RuntimeError(f"雨课堂接口返回非 JSON（HTTP {status}）：{url.split('?')[0]}") from exc
        if status >= 400:
            raise RuntimeError(f"雨课堂接口 HTTP {status}：{url.split('?')[0]}")
        return payload

    def download(self, url: str) -> bytes:
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname or ""
        if not any(host.endswith(suffix) for suffix in ALLOWED_IMAGE_HOSTS):
            raise ValueError(f"拒绝下载非雨课堂域名的图片：{host}")
        self._pace()
        self.requests += 1
        response = self.context.request.get(url, timeout=60_000)
        if response.status >= 400:
            raise RuntimeError(f"图片下载失败 HTTP {response.status}")
        body = response.body()
        if len(body) > 25 * 1024 * 1024:
            raise ValueError("图片超过 25 MB，未保存")
        return body

    def open_exam(self, classroom_id: Any, exam_id: Any, timeout: float = 30) -> bool:
        """Navigate the web app's exam transit page so the exam host grants a token.

        Returns True once the browser reaches the exam host result page.
        """
        self._pace()
        target = f"{HOST}/v2/web/trans/{classroom_id}/{exam_id}?status=4&isFrom=2"
        page = self.page
        page.goto(target, wait_until="domcontentloaded")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if page.url.startswith(EXAM_HOST) and f"/{exam_id}" in page.url:
                time.sleep(1.5)
                return True
            if "/login" in page.url and page.url.startswith(HOST):
                raise LoginRequired("雨课堂登录已过期，请重新扫码登录")
            time.sleep(0.5)
        return False
