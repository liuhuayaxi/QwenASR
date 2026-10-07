"""Narrow, local browser-auth handoff; never update viewing statistics."""
import asyncio
import json
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response

from .smartclass import PORTAL, LoginRequired, SmartClassError, portal_identity


def replay_bridge_router(client, platform_lock):
    router = APIRouter()
    root = Path(__file__).resolve().parent.parent

    def restore_login():
        try:
            saved = json.loads((client.state_dir / 'credentials.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            saved = {}
        if not saved.get('username') or not saved.get('password'):
            raise LoginRequired('请先在课程回放页配置南邮账号')
        client.login(saved['username'], saved['password'])

    def login_ticket():
        try:
            portal_identity(client.request('GET', PORTAL + '/UserSpace/CourseCenter.aspx').text)
            return client.browser_login_ticket()
        except LoginRequired:
            restore_login()
            return client.browser_login_ticket()

    @router.get('/api/replay-video-target')
    async def video_target(video: str):
        if not re.fullmatch(r'[0-9a-fA-F]{32}', video):
            raise HTTPException(400, '回放编号无效')
        def resolve():
            try:
                return client.browser_replay_target(video)
            except LoginRequired:
                restore_login()
                return client.browser_replay_target(video)
        try:
            async with platform_lock:
                return await asyncio.to_thread(resolve)
        except LoginRequired as exc:
            raise HTTPException(401, str(exc)) from exc
        except SmartClassError as exc:
            raise HTTPException(409, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, '学校回放信息暂时无法读取，请检查网络后重试') from exc

    @router.get('/replay', response_class=HTMLResponse)
    async def launch():
        return (root / 'static' / 'replay-launch.html').read_text(encoding='utf-8')

    @router.post('/api/browser-login')
    async def browser_login():
        try:
            async with platform_lock:
                ticket_url = await asyncio.to_thread(login_ticket)
        except LoginRequired as exc:
            raise HTTPException(401, str(exc)) from exc
        except SmartClassError as exc:
            raise HTTPException(409, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, '学校认证服务器暂时无法连接，请检查网络后重试') from exc
        return {'login_url': ticket_url}

    @router.get('/replay-script.user.js')
    async def replay_userscript():
        script = root / 'static' / 'userscripts' / 'smartclass-replay.user.js'
        return Response(script.read_text(encoding='utf-8'), media_type='text/javascript', headers={
            'Content-Disposition': 'inline; filename="smartclass-replay.user.js"',
            'Cache-Control': 'no-store',
        })

    return router
