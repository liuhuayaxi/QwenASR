"""NJUPT CAS authentication and SmartClass course import (local credentials only)."""
from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from .paths import PRIVATE_ROOT
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PORTAL = "https://njupt.smartclass.cn"
CAS = "https://i.njupt.edu.cn"
SERVICE = PORTAL + "/SystemSpace/Redirect.aspx"
STATE_DIR = PRIVATE_ROOT / "smartclass"
CHECK_KEY = "1629428467008"
# Public transport cipher shipped by SmartClass's hber.js; not an account secret.
SMARTCLASS_KEY = b"80bdbdbaf7494add99198960d715d41b"
SMARTCLASS_IV = b"bdbaf7494add9919"


class SmartClassError(Exception):
    pass


class LoginRequired(SmartClassError):
    pass


class VerificationRequired(SmartClassError):
    pass


def connection_error(exc: httpx.HTTPError, stage: str) -> str:
    """Describe the failed stage without exposing query strings or credentials."""
    host = exc.request.url.host if getattr(exc, "request", None) else "学校服务器"
    if isinstance(exc, httpx.TimeoutException):
        reason = "连接超时" if isinstance(exc, httpx.ConnectTimeout) else "响应超时"
    elif isinstance(exc, httpx.ConnectError):
        reason = "域名解析失败" if any(t in str(exc).lower() for t in ("nodename", "name resolution", "getaddrinfo")) else "连接未建立"
    else:
        reason = "连接在传输中断开"
    return f"{stage}失败：{host} {reason}。切换网络后可点“使用已保存账号登录”重连。"


def current_term(now: datetime | None = None) -> str:
    now = now or datetime.now()
    year = now.year if now.month >= 8 else now.year - 1
    return f"{year}-{year + 1}-{'1' if now.month >= 8 or now.month == 1 else '2'}"


def course_key(course: dict) -> str:
    if course.get("replay_catalog"):
        return "replays:" + hashlib.sha256(json.dumps(
            [course["CourseName"], course.get("TeacherName") or "", course["term"]],
            ensure_ascii=False).encode()).hexdigest()[:20]
    return ":".join(str(course.get(k, "")) for k in ("CourseID", "ClassID", "TeacherID"))


def term_dates(term: str) -> tuple[str, str]:
    year, _, semester = term.split("-")
    return ((f"{year}-08-01", f"{int(year) + 1}-01-31") if semester == "1"
            else (f"{int(year) + 1}-02-01", f"{int(year) + 1}-07-31"))


def parse_date(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("/", "-"))


def lesson_groups(videos: list[dict], course: dict) -> list[dict]:
    """Pair consecutive periods without merging different days, teachers or titles."""
    unique = {v["NewID"]: v for v in videos if v.get("NewID")}
    rows = sorted(unique.values(), key=lambda v: (v["StartTime"], v.get("NodeName", "")))
    groups = []
    for row in rows:
        title = str(row.get("Title") or "")
        teacher = str(row.get("Teachers") or course.get("TeacherName") or "")
        name = title.split(teacher, 1)[0].strip() if teacher and teacher in title else course["CourseName"]
        start = parse_date(row["StartTime"])
        stop = parse_date(row.get("EndTime") or row.get("StopTime"))
        previous = groups[-1] if groups else None
        joins = False
        if previous and len(previous["videos"]) == 1:
            last = previous["videos"][-1]
            gap = (start - parse_date(last.get("EndTime") or last.get("StopTime"))).total_seconds()
            node1, node2 = str(last.get("NodeName", "")), str(row.get("NodeName", ""))
            adjacent = not (node1.isdigit() and node2.isdigit()) or int(node2) == int(node1) + 1
            joins = (previous["course_name"] == name and previous["teacher"] == teacher
                     and last.get("ClassRoomName") == row.get("ClassRoomName")
                     and previous["date"] == start.date().isoformat() and 0 <= gap <= 10 * 60 and adjacent)
        if joins:
            previous["videos"].append(row)
            previous["stop"] = stop.isoformat(sep=" ")
        else:
            groups.append({"course_key": course_key(course), "course_name": name, "teacher": teacher,
                           "date": start.date().isoformat(), "start": start.isoformat(sep=" "),
                           "stop": stop.isoformat(sep=" "), "videos": [row]})
    for group in groups:
        ids = [v["NewID"] for v in group["videos"]]
        group["id"] = hashlib.sha256("|".join(ids).encode()).hexdigest()[:20]
        group["periods"] = [str(v.get("NodeName") or "") for v in group["videos"]]
        group["duration"] = sum(float(v.get("Duration") or 0) for v in group["videos"])
        group["title"] = f"{group['course_name']} · {group['date']} {group['start'][11:16]}–{group['stop'][11:16]}"
    return sorted(groups, key=lambda g: g["start"], reverse=True)


def media_url(url: str) -> str:
    p = urlsplit(url)
    if (p.scheme != "https" or not re.fullmatch(r"njuptvod\d+\.smartclass\.cn", p.hostname or "")
            or p.port not in (None, 443, 8010) or p.username or p.password):
        raise SmartClassError("回放媒体服务器不在南邮已适配的范围内")
    return url


def vga_track(index: dict) -> dict:
    return next((track for key, track in index.items()
                 if 'vga' in key.lower() and isinstance(track, dict) and track.get('Src')), {})


def parse_index(text: str) -> dict:
    # Index.json is a JS assignment containing JSON. Parse data; never execute JS.
    raw = re.sub(r"^\s*(?:var|let|const)\s+\w+\s*=\s*", "", text.lstrip("\ufeff"))
    value = json.loads(raw.strip().rstrip(";"))
    if not isinstance(value, dict):
        raise SmartClassError("回放索引格式无效")
    if value.get("FictitiousCutVideos"):
        raise SmartClassError("此回放包含虚拟剪辑，暂不支持自动提取")
    return value


def extract_audio(sources: list[dict], destination: Path, progress=lambda *args: None, stop=None,
                  sample_seconds: float | None = None, sample_offset: float = 0) -> list[dict]:
    from .course_codec import run_codec
    return run_codec('extract', sources, destination=str(Path(destination).resolve()),
        progress=progress, stop=stop, sample_seconds=sample_seconds, sample_offset=sample_offset)


def _extract_audio_inline(sources: list[dict], destination: Path, progress=lambda *args: None, stop=None,
                  sample_seconds: float | None = None, sample_offset: float = 0) -> list[dict]:
    """Decode only audio, joining source periods into one continuous lossless FLAC."""
    import av
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".partial.flac")
    manifest, total_samples = [], 0
    try:
        with av.open(str(temporary), "w", format="flac") as output:
            encoder = output.add_stream("flac", rate=16000)
            encoder.layout = "mono"
            for index, item in enumerate(sources):
                if stop and stop.is_set():
                    raise SmartClassError("任务已停止；已完成的转写可继续")
                url = media_url(item["url"])
                source_start = total_samples
                with av.open(url, options={"rw_timeout": "20000000", "user_agent": "Mozilla/5.0",
                                           "protocol_whitelist": "https,tls,tcp", "headers": f"Referer: {PORTAL}/\r\n"}) as source:
                    stream = next(iter(source.streams.audio), None)
                    if stream is None:
                        raise SmartClassError("video1 中没有音轨")
                    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
                    available = float(stream.duration * stream.time_base) if stream.duration else float(item["duration"])
                    if not sample_seconds and available < float(item["duration"]) * .95:
                        raise SmartClassError("video1 音轨短于课次时长，请等待平台处理完成后重试")
                    offset = max(0, sample_offset) if sample_seconds else 0
                    limit = min(max(0, available - offset), sample_seconds) if sample_seconds else available
                    target_pts = (stream.start_time or 0) + int(offset / stream.time_base)
                    if offset:
                        source.seek(target_pts, stream=stream)
                    last_notice = 0

                    def encode(frame):
                        nonlocal total_samples
                        frame.pts = total_samples
                        frame.time_base = Fraction(1, 16000)
                        total_samples += frame.samples
                        for packet in encoder.encode(frame):
                            output.mux(packet)

                    for frame in source.decode(stream):
                        if stop and stop.is_set():
                            raise SmartClassError("任务已停止；已完成的转写可继续")
                        if offset and frame.pts is not None and frame.pts < target_pts:
                            continue
                        for converted in resampler.resample(frame):
                            encode(converted)
                        seconds = (total_samples - source_start) / 16000
                        if time.monotonic() - last_notice >= 1:
                            progress(index, seconds, limit)
                            last_notice = time.monotonic()
                        if sample_seconds and seconds >= sample_seconds:
                            break
                    for frame in resampler.resample(None):
                        encode(frame)
                    actual = (total_samples - source_start) / 16000
                    if actual < max(0.2, limit * .97):
                        raise SmartClassError("音频未完整下载，请重试当前课次")
                    manifest.append({"video_id": item["video_id"], "segment_id": item["segment_id"],
                                     "offset_seconds": source_start / 16000, "duration_seconds": actual,
                                     "source_title": item["title"], "source_start": item["start"],
                                     "source_offset_seconds": offset})
            for packet in encoder.encode(None):
                output.mux(packet)
        if total_samples < 3200:
            raise SmartClassError("回放没有有效音频")
        temporary.replace(destination)
        return manifest
    except SmartClassError:
        raise
    except Exception:
        raise SmartClassError("音频读取失败，媒体可能尚未就绪或网络中断，请重试") from None
    finally:
        temporary.unlink(missing_ok=True)


def private_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def encrypt_cas(value: str) -> str:
    """Matches the university's published AES-CBC login form implementation."""
    key = ("iam" + CHECK_KEY).encode()
    padder = padding.PKCS7(128).padder()
    raw = padder.update(value.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return (enc.update(raw) + enc.finalize()).hex()


def decrypt_platform(value: str) -> str:
    decoder = Cipher(algorithms.AES(SMARTCLASS_KEY), modes.CBC(SMARTCLASS_IV)).decryptor()
    raw = decoder.update(bytes.fromhex(value)) + decoder.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(raw) + unpadder.finalize()).decode("utf-8")


def portal_identity(source: str) -> dict:
    match = re.search(r"var\s+userInfo\s*=\s*(\[.*?\])\s*;", source, re.S)
    if match:
        try:
            users = json.loads(match.group(1))
            if users and users[0].get("UserID"):
                return users[0]
        except (ValueError, IndexError, TypeError):
            pass
    raise LoginRequired("课程平台尚未建立有效登录会话")


class SmartClassClient:
    def __init__(self, state_dir: Path = STATE_DIR, transport=None):
        self.state_dir = state_dir
        self.transport = transport
        self.last_stage = "连接学校"
        self._new_http()
        self.user = {}
        self.platform_token = ""
        self.csrf_key = ""
        self.time_offset = 0
        self._operation = threading.local()

    @contextmanager
    def operation(self, cancelled, deadline):
        self._operation.cancelled = cancelled
        self._operation.deadline = deadline
        try:
            yield
        finally:
            self._operation.__dict__.clear()

    def check_operation(self):
        cancelled = getattr(self._operation, 'cancelled', None)
        if cancelled is not None and cancelled.is_set():
            raise SmartClassError('已取消登录或课程刷新')
        remaining = getattr(self._operation, 'deadline', float('inf')) - time.monotonic()
        if remaining <= 0:
            raise SmartClassError('学校响应超时，请稍后重试')
        return remaining

    def _new_http(self):
        cookies = self.http.cookies if hasattr(self, "http") else None
        if hasattr(self, "http"):
            self.http.close()
        self.http = httpx.Client(timeout=httpx.Timeout(90, connect=15), transport=self.transport, headers={
            "User-Agent": "Mozilla/5.0 QwenASR-CourseSync/1.0",
        }, cookies=cookies, trust_env=False)

    def close(self):
        self.http.close()

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Follow only the university/platform CAS chain; never downgrade TLS."""
        for _ in range(12):
            parts = urlsplit(url)
            if parts.hostname not in {"i.njupt.edu.cn", "njupt.smartclass.cn"} or parts.port not in (None, 443, 80):
                raise SmartClassError("认证跳转到了未配置的域名，已停止")
            url = urlunsplit(("https", parts.hostname, parts.path, parts.query, ""))
            for attempt in range(3 if method.upper() == "GET" else 1):
                remaining = self.check_operation()
                try:
                    if remaining != float('inf'):
                        kwargs['timeout'] = min(15, remaining)
                    response = self.http.request(method, url, **kwargs)
                    self.check_operation()
                    # A temporary gateway failure is not a rejected login.
                    # Retry only reads; never loop credential POSTs on a 4xx.
                    if method.upper() == "GET" and response.status_code in {429, 502, 503, 504} and attempt < 2:
                        response.close()
                        time.sleep(.5 * (attempt + 1))
                        continue
                    break
                except httpx.HTTPError:
                    if method.upper() != "GET" or attempt == 2:
                        raise
                    self._new_http()
                    time.sleep(.5 * (attempt + 1))
            kwargs.pop("params", None)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise SmartClassError("认证响应缺少跳转地址")
                url = urljoin(str(response.url), location)
                if response.status_code == 303 or (response.status_code in (301, 302) and method.upper() == "POST"):
                    method = "GET"
                    kwargs = {}
                elif urlsplit(url).hostname != response.url.host and method.upper() != "GET":
                    raise SmartClassError("认证请求禁止把登录表单转发到其他域名")
                continue
            if response.is_error:
                raise SmartClassError(f"平台请求失败（HTTP {response.status_code}）")
            return response
        raise SmartClassError("认证跳转次数过多")

    def cas_json(self, path: str) -> dict:
        value = self.request("GET", CAS + path).json()
        if not value.get("success") or not isinstance(value.get("result"), dict):
            raise SmartClassError("无法读取学校登录配置")
        return value["result"]

    def csrf_token(self) -> str:
        if not self.csrf_key:
            value = self.request("GET", PORTAL + "/config.json").json()
            config = json.loads(decrypt_platform(value["domainConfig"]))
            self.csrf_key = config["csrkKey"]
        return "".join(self.csrf_key[int(c)] for c in str(int(time.time() * 1000 + self.time_offset)))

    def api(self, path: str, params: dict | None = None, method: str = "GET"):
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": PORTAL + "/UserSpace/CourseCenter.aspx"}
        if self.platform_token:
            headers["platform_token"] = self.platform_token
        query = {"csrkToken": self.csrf_token()}
        kwargs = {"headers": headers}
        if method == "GET":
            query.update(params or {})
        else:
            kwargs["data"] = params or {}
        response = self.request(method, PORTAL + path, params=query, **kwargs)
        try:
            value = response.json()
        except ValueError:
            raise LoginRequired("课程接口没有返回有效数据，登录可能已过期")
        if isinstance(value, dict) and "Success" in value:
            if not value["Success"]:
                raise SmartClassError("课程接口拒绝请求，请重新连接账号后重试")
            value = value.get("Value")
        return value

    def prepare_api(self):
        self.csrf_key = ""
        self.time_offset = 0
        now = self.api("/Home/GettimeDif")
        self.time_offset = int(now) - time.time() * 1000

    def ensure_login(self) -> dict:
        """Check the actual portal session and restore saved credentials once."""
        self.last_stage = "检查学校登录状态"
        try:
            self.user = portal_identity(self.request("GET", PORTAL + "/UserSpace/CourseCenter.aspx").text)
        except LoginRequired:
            self.user = {}
            try:
                saved = json.loads((self.state_dir / "credentials.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = {}
            if not saved.get("username") or not saved.get("password"):
                raise LoginRequired("请先配置并保存本机南邮账号") from None
            return self.login(saved["username"], saved["password"])
        return {"name": self.user.get("Name", ""), "school_id": self.user.get("SchoolID")}

    def my_videos(self, term: str, course: dict | None = None) -> list[dict]:
        """Use the portal's '与我相关' feed, including teacherless replays."""
        start, stop = term_dates(term)
        rows, seen = [], set()
        for page in range(1, 1001):
            data = self.api("/WebApi/V1/video/GetMyVideoList", {
                "PageSize": 50, "PageNumber": page, "StartDate": start, "EndDate": stop,
                "Sort": "StartTime", "Order": 1, "IncludePublic": "false",
                "TitleKey": course["CourseName"] if course else "",
            })
            if not isinstance(data, dict) or not isinstance(data.get("Data"), list):
                raise SmartClassError("个人回放列表格式变化，无法继续同步")
            fresh = 0
            for row in data["Data"]:
                if not row.get("NewID") or row["NewID"] in seen:
                    continue
                seen.add(row["NewID"])
                fresh += 1
                # Enforce the requested semester even if the platform ignores its date filter.
                if start <= str(row.get("StartTime") or "")[:10] <= stop:
                    if not course or (row.get("CourseName") == course["CourseName"]
                                      and (row.get("Teachers") or "") == (course.get("TeacherName") or "")):
                        rows.append(row)
            if data["Data"] and not fresh:
                raise SmartClassError("个人回放分页重复，已停止以免漏读")
            if not data["Data"] or page * 50 >= int(data.get("TotalCount", page * 50)):
                return rows
        raise SmartClassError("个人回放分页超出预期")

    def courses(self, term: str | None = None) -> list[dict]:
        self.last_stage = "获取本学期课程"
        if not self.user:
            self.user = portal_identity(self.request("GET", PORTAL + "/UserSpace/CourseCenter.aspx").text)
        self.prepare_api()
        if self.user.get("UserType") != 2:
            raise SmartClassError("当前课程同步适配学生账号")
        term = term or current_term()
        start_date, stop_date = term_dates(term)
        courses, seen = [], set()
        for page in range(1, 1001):
            data = self.api("/WebApi/V1/video/GetCourseLists", {
                "schoolId": self.user["SchoolID"], "StudentID": self.user["ID"],
                "type": 0, "pageNumber": page, "pageSize": 12,
                "IsDeleted": "false", "IsPublish": "true", "sort": "StartTime", "order": "",
                "startDate": start_date, "stopDate": stop_date, "CourseID": "", "TeacherID": "",
                "TeacherUserID": "", "organizationID": "", "titleKey": "", "attribute": "",
                "Source": "", "IsProcessSuccess": "", "IsProcessing": "", "IsNeedLive": "",
                "CollectorID": "", "ViewerID": "", "IsPublic": "", "ClassroomID": "",
                "NodeID": "", "GradeID": "", "subjectID": "", "ActivityID": "", "TagID": "",
            })
            if not isinstance(data, dict) or not isinstance(data.get("Data"), list):
                raise SmartClassError("课程列表格式变化，无法继续同步")
            rows = data["Data"]
            fresh = 0
            for row in rows:
                key = (row.get("CourseID"), row.get("ClassID"), row.get("TeacherID"))
                if key not in seen:
                    seen.add(key)
                    fresh += 1
                    courses.append(row)
            if rows and not fresh:
                raise SmartClassError("课程分页重复，已停止以免漏读")
            if not rows or page * 12 >= int(data.get("TotalCount", len(courses))):
                discovered = {}
                for video in self.my_videos(term):
                    name, teacher = video.get("CourseName"), video.get("Teachers") or ""
                    if not name:
                        continue
                    matches = [c for c in courses if c.get("CourseName") == name
                               and (c.get("TeacherName") or "") == teacher]
                    if matches:
                        continue
                    supplemental = discovered.setdefault((name, teacher), {
                        "CourseName": name, "TeacherName": teacher, "VideoTotal": 0,
                        "replay_catalog": True, "term": term,
                    })
                    supplemental["VideoTotal"] += 1
                courses.extend(discovered.values())
                return courses
        raise SmartClassError("课程分页超出预期，已停止")

    def videos(self, course: dict) -> list[dict]:
        self.last_stage = "获取课次回放"
        if course.get("replay_catalog"):
            self.prepare_api()
            return self.my_videos(course["term"], course)
        rows, seen = [], set()
        for page in range(1, 1001):
            data = self.api("/WebApi/V1/video/GetCourseVideos", {
                "courseid": course["CourseID"], "teacherID": course["TeacherID"],
                "classID": course["ClassID"], "pageSize": 50, "pageNumber": page, "Type": 0, "order": 0,
            })
            if not isinstance(data, dict) or not isinstance(data.get("Data"), list):
                raise SmartClassError("回放列表格式变化")
            fresh = 0
            for row in data["Data"]:
                if row.get("NewID") and row["NewID"] not in seen:
                    seen.add(row["NewID"])
                    rows.append(row)
                    fresh += 1
            if not data["Data"] or len(seen) >= int(data.get("TotalCount", len(rows))):
                return rows
            if not fresh:
                raise SmartClassError("回放分页重复，已停止以免漏读")
        raise SmartClassError("回放分页超出预期")

    def audio_sources(self, group: dict) -> list[dict]:
        return self.media_sources(group)

    def vga_sources(self, group: dict) -> list[dict]:
        return self.media_sources(group, screen=True)

    def media_sources(self, group: dict, *, screen=False) -> list[dict]:
        sources = []
        for video in group["videos"]:
            info = self.api("/Video/GetVideoInfoDtoByID", {
                "NewId": video["NewID"], "isGetLink": "true", "isloadstudent": "false",
            })
            segments = info.get("VideoSegmentInfo") or []
            if not segments:
                raise SmartClassError("这节回放尚未生成可用视频")
            for segment in sorted(segments, key=lambda s: s.get("Index", 0)):
                if segment.get("IsProcessing") or not segment.get("IsProcessSuccess"):
                    raise SmartClassError("平台仍在处理这节视频，请稍后刷新")
                index_url = media_url(segment["IndexJsonFileUri"])
                # VOD requests deliberately use no university cookies or tokens.
                response = httpx.get(index_url, timeout=30, follow_redirects=False, headers={"Referer": PORTAL + "/"})
                if response.status_code != 200:
                    raise SmartClassError("无法读取回放索引，请刷新登录状态后重试")
                index = parse_index(response.text)
                track = vga_track(index) if screen else index.get("Video1") or {}
                if screen and not track:
                    return []
                if not track.get("Src") or (not screen and not track.get("Width")):
                    raise SmartClassError("这节回放未提供可访问的 video1")
                if str(index.get("File", "mp4")).lower() != "mp4":
                    raise SmartClassError("当前只适配平台 MP4 回放")
                source_url = media_url(urljoin(index_url, track["Src"].replace("\\", "/")))
                sources.append({"url": source_url, "video_id": video["NewID"], "segment_id": segment["ID"],
                                "duration": float(segment["Duration"]), "title": video["Title"], "start": video["StartTime"],
                                "end": video.get('EndTime') or video.get('StopTime')})
        return sources

    def browser_replay_target(self, video_id: str) -> dict:
        """Resolve the first playable segment without downloading audio or creating a recording."""
        if not re.fullmatch(r'[0-9a-fA-F]{32}', video_id):
            raise SmartClassError('回放编号无效')
        info = self.api('/Video/GetVideoInfoDtoByID', {
            'NewId': video_id, 'isGetLink': 'true', 'isloadstudent': 'false',
        })
        segments = info.get('VideoSegmentInfo') or []
        if not segments:
            raise SmartClassError('这节回放尚未生成可用视频')
        segment = min(segments, key=lambda item: item.get('Index', 0))
        if segment.get('IsProcessing') or not segment.get('IsProcessSuccess'):
            raise SmartClassError('平台仍在处理这节视频，请稍后刷新')
        ident = str(segment.get('ID') or '')
        if not re.fullmatch(r'[1-9][0-9]*', ident):
            raise SmartClassError('学校返回的视频片段编号无效')
        return {'video_id': video_id, 'segment_id': ident, 'seconds': 0,
                'source_title': str(info.get('Title') or '课堂回放'),
                'url': PORTAL + '/PlayPages/Video.aspx?' + urlencode({'NewID': video_id, 'SegementID': ident})}

    def browser_login_ticket(self) -> str:
        """Acquire a fresh CAS service ticket for consumption by the browser.

        Stop before the portal redirect: consuming it here would authenticate
        only this HTTP client instead of the user's browser.
        """
        url = CAS + '/cas/login?service=' + quote(SERVICE, safe='')
        used_session = False
        for _ in range(12):
            parsed = urlsplit(url)
            if (parsed.hostname not in {'i.njupt.edu.cn', 'njupt.smartclass.cn'}
                    or parsed.username or parsed.password or parsed.port not in (None, 80, 443)):
                raise SmartClassError('浏览器认证跳转地址无效')
            url = urlunsplit(('https', parsed.hostname, parsed.path, parsed.query, ''))
            if parsed.hostname == 'njupt.smartclass.cn':
                if parsed.path == '/SystemSpace/Redirect.aspx' and parse_qs(parsed.query).get('ticket'):
                    return url
                raise SmartClassError('浏览器认证未返回服务票据')
            response = self.http.get(url)
            if response.status_code in (301, 302, 303, 307, 308) and response.headers.get('location'):
                url = urljoin(str(response.url), response.headers['location'])
                continue
            session = parse_qs(response.url.query.decode()).get('service', [''])[0]
            if not used_session and session:
                used_session = True
                url = CAS + '/ssoLogin/index?' + urlencode({'sessionId': session})
                continue
            raise LoginRequired('学校登录会话已失效，请重新登录南邮账号')
        raise SmartClassError('浏览器认证跳转次数过多')

    def login(self, username: str, password: str) -> dict:
        # A new Wi-Fi route must not reuse sockets from the previous network.
        self._new_http()
        self.last_stage = "打开统一认证"
        self.user = {}
        self.platform_token = ""
        self.csrf_key = ""
        self.http.cookies.clear()
        entry = self.request("GET", CAS + "/cas/login", params={"service": SERVICE})
        service = parse_qs(entry.url.query.decode()).get("service", [""])[0]
        if not service:
            raise SmartClassError("学校没有返回 CAS 登录会话标识")
        rule = self.cas_json("/loginrule/tLoginRule/getActiveRule")
        if rule.get("passwordLoginFlag") != 1:
            raise VerificationRequired("学校当前未开放密码登录")
        if rule.get("verificationCodeFlag") == 1 and rule.get("codeFailCount") == 0:
            raise VerificationRequired("学校当前要求验证码，需在统一认证页面完成验证")
        allocation = self.cas_json("/AppLoginAllocation/queryLoginAllocationByService?service=" + service)
        payload = {
            "checkKey": CHECK_KEY,
            "username": encrypt_cas(username),
            "password": encrypt_cas(password),
            "captchaVerification": None,
            "appId": allocation.get("loginAppId") or allocation.get("appId"),
            "mode": "none",
        }
        self.last_stage = "提交统一认证登录"
        response = self.request("POST", CAS + "/ssoLogin/login", json=payload).json()
        if response.get("code") != 200:
            # Do not return arbitrary upstream content, which may echo credentials.
            if response.get("code") in (511, 512):
                raise VerificationRequired("学校要求修改密码或绑定手机号，请先在统一认证页面处理")
            if response.get("result", {}) and isinstance(response.get("result"), dict) and response["result"].get("codeType"):
                raise VerificationRequired("学校要求验证码，本次自动登录已停止")
            raise LoginRequired("统一认证登录未成功，请检查账号密码；程序不会循环尝试错误密码")
        result = response.get("result") or {}
        token = result.get("token")
        if not token:
            raise SmartClassError("统一认证响应缺少登录令牌")
        self.http.cookies.set("tgc", token, domain="i.njupt.edu.cn", path="/")
        self.last_stage = "建立课程平台会话"
        self.request("GET", CAS + "/ssoLogin/index", params={"sessionId": service})
        page = self.request("GET", PORTAL + "/UserSpace/CourseCenter.aspx")
        self.user = portal_identity(page.text)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Keep account metadata local and private; never include the password.
        private_json(self.state_dir / "identity.json", self.user)
        return {"name": self.user.get("Name", ""), "school_id": self.user.get("SchoolID")}
