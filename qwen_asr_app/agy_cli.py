"""Small, non-interactive adapter for the local ``agy`` CLI.

Small prompts use stream-json stdin. Large prompts use bounded, hashed files
inside an OS sandbox, with completed read receipts for every line. Only the
final response from this exact completed conversation is returned; long
lecture text never goes into argv or an unbounded CLI user-message envelope.
"""
from __future__ import annotations
from contextvars import ContextVar

last_session_id = ContextVar('agy_last_session_id', default='')
last_prompt = ContextVar('agy_last_prompt', default='')

import json
import asyncio
import os
import re
import shutil
import subprocess
import hashlib
import sys
import uuid
import time
from pathlib import Path
from typing import Any

from .ai import AIRequestError
from .ai_activity import report_ai_activity
from .paths import PRIVATE_ROOT
from .context_budget import estimate_tokens, normalize_context_window


DEFAULT_MODEL = "gemini-3.8-flash-medium"
DEFAULT_TIMEOUT = 1800
# AGY's user-request envelope truncates near 192 KB, independently of the
# model's token window. Keep inline turns small; transport larger inputs as
# individually bounded, receipt-checked files without dropping source text.
INLINE_INPUT_BYTES = 150_000
# view_file returns a file completely up to ~30k characters (probed with AGY
# 1.4.5: a 20.7k-character file was complete, a 30k-character view was cut).
# Size chunks by characters, not bytes: CJK text is 3 bytes per character, so
# the old 16 KB byte limit produced ~5k-character files and 14+ tool reads.
FILE_INPUT_BYTES = 72_000
FILE_INPUT_CHARS = 22_000
MODEL_START_DELAYS = (2, 5)
PROTOCOL_RETRY_DELAYS = (2, 5)


def _text(value: Any) -> str:
    return str(value or "").strip()


def executable(provider: dict[str, Any]) -> str:
    configured = _text(provider.get("cli_path")) or "agy"
    path = Path(configured).expanduser()
    if path.is_absolute():
        if not path.is_file() or not os.access(path, os.X_OK):
            raise AIRequestError(
                f"找不到可执行的 AGY CLI：{path}", kind="agy_cli_unavailable", retriable=False
            )
        return str(path)
    found = shutil.which(configured)
    if found:
        return found
    # GUI-launched services often inherit a sanitized PATH that omits the
    # user's ~/.local/bin, even though the interactive shell can run `agy`.
    # Check the standard per-user install locations before reporting failure.
    candidates = [
        Path.home() / ".local" / "bin" / configured,
        Path.home() / ".cargo" / "bin" / configured,
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise AIRequestError(
        f"本机没有找到 AGY CLI（{configured}），请安装或填写 CLI 路径",
        kind="agy_cli_unavailable",
        retriable=False,
    )


def model(provider: dict[str, Any]) -> str:
    value = _text(provider.get("cli_model") or provider.get("model"))
    if not value:
        raise AIRequestError(
            "AGY CLI 模型不能为空，请选择一个已登录账号可用的模型",
            kind="agy_cli_config",
            retriable=False,
        )
    return value


def timeout_seconds(provider: dict[str, Any]) -> int | None:
    try:
        configured=provider.get("cli_timeout_seconds")
        value = DEFAULT_TIMEOUT if configured is None or configured=='' else int(configured)
    except (TypeError, ValueError) as exc:
        raise AIRequestError("AGY CLI 超时必须是整数秒", kind="agy_cli_config", retriable=False) from exc
    if value==0:return None
    if not 30 <= value <= 7200:
        raise AIRequestError("AGY CLI 超时必须为 0（等待完成）或在 30–7200 秒之间", kind="agy_cli_config", retriable=False)
    return value


def effort(provider: dict[str, Any]) -> str:
    value = _text(provider.get("cli_effort") or "high").lower()
    if value not in {"low", "medium", "high", "xhigh", "max"}:
        raise AIRequestError("AGY CLI 思考强度必须是 low、medium、high、xhigh 或 max", kind="agy_cli_config", retriable=False)
    return value


def _error_kind(message: str) -> tuple[str, bool]:
    value = message.lower()
    if "location is not supported" in value or "user location" in value:
        return "provider_region", False
    # Explicit rejected credentials must never become retryable merely because
    # a diagnostic also mentions OAuth, EOF, or an ambiguous handshake timeout.
    if re.search(r'\b(?:401|403)\b',value) or any(marker in value for marker in (
        'invalid_grant','invalid grant','invalid_client','invalid client','revoked',
        'bad credentials','invalid credentials','invalid api key','unauthorized',
        'not logged in','login required',
    )):
        return 'provider_auth',False
    # Classify transport evidence independently of endpoint spelling. Login,
    # userinfo, eligibility and generation can all lose the connection before
    # receiving an authentication decision. An OAuth URL is not that decision.
    transport_failure=re.search(
        r'dial tcp|lookup[^\n]*no such host|connection (?:reset|refused|closed)|'
        r'network is unreachable|no route to host|broken pipe|\beof\b|'
        r'i/o timeout|(?:tls handshake|connection|connect) timed?\s*out|'
        r'token refresh failed due to network error',value)
    if transport_failure:
        return 'network_transport',True
    # This CLI message covers a timed-out eligibility/auth handshake. It is
    # ambiguous, unlike an explicit revoked grant or login requirement, so
    # permit bounded transport retries before asking the user to sign in.
    if "authentication failed or timed out" in value:
        return "network_connect_timeout", True
    if any(marker in value for marker in ("resource exhausted", "rate limit", "too many requests", "429")):
        return "rate_limit", True
    if re.search(r'\b(?:502|503|504)\b',value) or any(marker in value for marker in ('service unavailable', 'service is currently unavailable', 'bad gateway', 'gateway timeout')):
        return 'provider_unavailable', True
    if any(marker in value for marker in ("deadline exceeded", "timed out", "timeout")):
        return "network_read_timeout", True
    if re.search(r'model .+? is not recognized as a known model|invalid model selection', value):
        return 'agy_model_unavailable', True
    if re.search(r'unknown (?:flag|option)|--effort.*not supported|invalid.*(?:flag|option)',value):
        return 'agy_cli_config', False
    # A terminated native language server reports only `interrupted`, without
    # the older "stream was interrupted" sentence. Local task cancellation is
    # handled separately as CancelledError and must never enter this retry path.
    if re.search(r'(?:^|[:：]\s*)interrupted[.!]?\s*$', value.strip()):
        return "network_transport", True
    if any(marker in value for marker in (
        "connection reset", "broken pipe", "unexpected eof", "post \"http", ": eof", "eof",
        "the stream was interrupted", "stream interrupted",
        "生成连接中断",
        "network issue connecting to the server",
    )):
        return "network_transport", True
    if any(marker in value for marker in ("unauthenticated", "authentication", "oauth", "login", "permission denied")):
        return "provider_auth", False
    if any(marker in value for marker in (
        "maximum output", "max tokens", "output token", "output limit",
        "context length", "too many tokens", "truncated", "cut off",
    )):
        return "output_truncated", True
    return "agy_cli_error", False


def _blocking_diagnostic(stderr):
    """Only explicit terminal rejections can override a verified continuation.

    Routine startup warnings (including denied discovery of unrelated files)
    are not authentication failures. Tool access is checked in the session.
    """
    lines=stderr.splitlines()
    # Native startup can log "not logged in" before restoring the session.
    # A later explicit successful login resolves those earlier warnings.
    restored=[i for i,line in enumerate(lines) if re.search(r'silent auth succeeded|OAuth: authenticated successfully',line)]
    if restored:lines=lines[restored[-1]+1:]
    for line in lines:
        if re.search(r'\b(?:401|403)\b|invalid[_ ](?:grant|client)|revoked|bad credentials|'
                     r'invalid credentials|invalid api key|unauthorized|not logged in|login required|'
                     r'user location|location is not supported|unknown (?:flag|option)|'
                     r'invalid model selection',line,re.I):
            kind,retriable=_error_kind(line)
            if not retriable or kind=='agy_model_unavailable':return kind
    return None


async def retry_model_start(operation):
    """Retry only pre-conversation model discovery, keeping the chosen model."""
    for attempt in range(len(MODEL_START_DELAYS)+1):
        try:
            return await operation()
        except (AIRequestError, RuntimeError) as exc:
            if (getattr(exc,'kind',None)!='agy_model_unavailable'
                    or getattr(exc,'session_id',None) or not getattr(exc,'retriable',False)):
                raise
            if attempt==len(MODEL_START_DELAYS):
                exc.retriable=False
                exc.args=('AGY 启动三次仍无法识别已选模型；请刷新模型列表后重试，已完成进度保留',)
                raise
            report_ai_activity('waiting')
            await asyncio.sleep(MODEL_START_DELAYS[attempt])


def _decode_output(stdout: str, stderr: str) -> str:
    """Decode the final stream-json result while tolerating diagnostic lines."""
    result: dict[str, Any] | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "result" and isinstance(event.get("result"), dict):
            result = event["result"]
    if result is None:
        message = (stderr or stdout).strip()[-2_000:]
        kind, retriable = _error_kind(message) if message else ('provider_unavailable',True)
        raise AIRequestError(f"AGY CLI 没有返回最终结果：{message or '空输出'}", kind=kind, retriable=retriable)
    if str(result.get("status") or "").upper() not in {"SUCCESS", "COMPLETED", "OK"}:
        message = _text(result.get("error")) or stderr.strip() or _text(result.get("status")) or "AGY CLI 请求失败"
        kind, retriable = _error_kind(message)
        # A short status must not hide a concrete credential/configuration
        # failure emitted on stderr. Do not mix arbitrary stdout into errors.
        if message.lower() == 'interrupted' and stderr.strip():
            diagnostic_kind, diagnostic_retry = _error_kind(stderr)
            if diagnostic_kind != 'agy_cli_error':
                kind, retriable = diagnostic_kind, diagnostic_retry
        if message.lower() == 'interrupted' and kind == 'network_transport':
            message = '生成连接中断；已完成内容保留，可从断点继续'
        elif kind == 'output_truncated':
            message = '生成达到输出上限；已保存结果保留，本批需要拆小重试'
        elif kind == 'agy_model_unavailable':
            message = '启动时尚未识别已选模型，正在进行有限重试'
        else:
            message = re.sub(r'Retries remaining:\s*\d+', 'AGY 内部重试提示（不代表应用重试次数）', message)
        raise AIRequestError(f"AGY CLI 请求失败：{message[:2_000]}", kind=kind, retriable=retriable)
    response = _text(result.get("response"))
    if not response:
        raise AIRequestError("AGY CLI 返回了空结果", kind="agy_cli_error", retriable=False)
    return response


def subprocess_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    entries = [str(Path.home() / ".local" / "bin"), str(Path.home() / ".cargo" / "bin")]
    entries.extend(env.get("PATH", "").split(os.pathsep))
    env["PATH"] = os.pathsep.join(dict.fromkeys(entry for entry in entries if entry))
    return env


def compose_prompt(instructions: str, payload: dict[str, Any]) -> str:
    return (
        "你正在作为 QwenASR 的本地 AI 整理提供商工作。\n"
        "系统要求只处理下面标记为 INPUT 的数据，不调用工具、不修改文件、不执行命令。\n"
        f"SYSTEM INSTRUCTIONS:\n{instructions.strip()}\n\n"
        f"INPUT (JSON):\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _file_prompt(prompt: str, working: Path, *, additional_read_paths=()):
    from .durable import atomic_json
    request = working/'requests'/uuid.uuid4().hex
    request.mkdir(parents=True, mode=0o700)
    files = {}
    fragments = [prompt[i:i+700] for i in range(0, len(prompt), 700)]
    groups = []
    current = []
    for fragment in fragments:
        candidate = [*current, fragment]
        encoded = json.dumps(candidate, ensure_ascii=False, indent=2)
        if current and (len(encoded) > FILE_INPUT_CHARS or len(encoded.encode()) > FILE_INPUT_BYTES):
            groups.append(current)
            current = []
        current.append(fragment)
    if current:
        groups.append(current)
    reconstructed = []
    for index, group in enumerate(groups):
        path = request/f'input-{index:04d}.json'
        atomic_json(path, {'part': index+1, 'parts': len(groups), 'content_fragments': group})
        data = path.read_bytes()
        files[str(path)] = {'sha256': hashlib.sha256(data).hexdigest(),
                            'lines': len(data.splitlines())}
        reconstructed.extend(group)
    if ''.join(reconstructed) != prompt:
        raise ValueError('AGY 资料分块校验失败，原始输入未发送')
    additional = sorted(str(Path(p).resolve()) for p in additional_read_paths)
    reading_rule = ('读取全部文字资料后，必须依完整任务要求实际查看指定图片。' if additional else
                    '不打开正文中提及的其他文件；排版写作只使用资料里已提供的图片清单和读图说明。')
    transport_prompt = (
        '这是 QwenASR 的独立文本整理请求。完整任务和原始资料以 JSON 字符串片段保存在下面的文件中，'
        '避免 CLI 截断长消息。先依照文件顺序逐个使用 view_file 完整读取全部文件。'
        '清单文件已拆成可完整读取的小文件；默认读取即可。当工具说明 entire, complete file contents 时，该文件已读完，直接读下一个文件，不得再猜测 ContentOffset 追加读取。'
        '每个文件的 content_fragments 按原顺序连接，JSON 字符串转义按正常 JSON 含义理解；'
        '再将所有文件连接，即为完整的同一个请求，不得遗漏末尾片段。'
        '按其中 SYSTEM INSTRUCTIONS 处理 INPUT 数据，直接返回任务要求的完整最终正文。'
        '外层读取协议优先于文件中“不调用工具”的普通文本模板：必须先读清单。'
        '旧版可用 view_file(AbsolutePath)，新版可用 read_file(Path)；二者路径均必须来自明确清单。'
        '只允许 view_file 读取清单文件，以及 allowed_additional_files 明确列出的原始资料和图片；'
        f'{reading_rule}'
        '不得执行命令、写文件、访问其他路径、浏览网页、发送消息或委派子代理。'
        '不要输出读取过程或声称已经完成而省略正文。\n'
        +json.dumps({'request_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                     'allowed_additional_files':additional,
                     'files': [{'path': path, 'lines': spec['lines']} for path, spec in files.items()]}, ensure_ascii=False)
    )
    atomic_json(request/'transport.json', {'version': 1, 'prompt': transport_prompt,
                'source_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                'source_bytes': len(prompt.encode()), 'files': files})
    return transport_prompt, request


def _finish_output(stdout, stderr, working, message, returncode=0, prior_prompts=(), conversation=''):
    last_session_id.set('')
    try:
        return _checked_output(stdout,stderr,working,message,returncode,prior_prompts=prior_prompts,conversation=conversation)
    except AIRequestError as exc:
        from .durable import atomic_json
        exc.session_id=last_session_id.get()
        # Store correlation and classifications, never raw prompts, responses,
        # stderr, credentials or account details. Native session remains intact.
        failure={'session_id':exc.session_id,'error_kind':exc.kind,'retriable':exc.retriable,
                 'exit_code':returncode,'stderr_bytes':len(stderr.encode()),
                 'request_sha256':hashlib.sha256(message.encode()).hexdigest()}
        atomic_json(Path(working)/'diagnostics'/f'failed-{uuid.uuid4().hex}.json',failure)
        raise


def _checked_output(stdout, stderr, working, message, returncode=0, prior_prompts=(), conversation=''):
    """Use stdout normally; verify the exact durable turn on missing output."""
    prompt = json.loads(message)['message']['content']
    manifest = Path(working)/'transport.json'
    files = json.loads(manifest.read_text())['files'] if manifest.is_file() else None
    session = ''
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            ident = event.get('conversation_id') or (event.get('result') or {}).get('conversation_id')
            if ident:
                session = str(ident)
        except (ValueError, TypeError, AttributeError):
            continue
    last_session_id.set(session)
    if conversation and session and session != conversation:
        raise AIRequestError('AGY 没有续接本节会话，结果未采纳', kind='agy_cli_error', retriable=False)
    from .agy_session import output_limit_before_failure
    continuation = bool(session and (output_limit_before_failure(session,prompt,prior_prompts=prior_prompts)
                                     if prior_prompts else output_limit_before_failure(session,prompt)))
    try:
        response = _decode_output(stdout, stderr)
    except AIRequestError as exc:
        if returncode in {-15, -2, 130, 143} and exc.kind in {'agy_cli_error','provider_unavailable'}:
            raise AIRequestError('AGY 生成进程已中断；已保存进度可继续',
                                 kind='network_transport', retriable=True) from exc
        if exc.kind not in {'output_truncated','network_transport','network_read_timeout','agy_cli_error'}:
            raise
        if not continuation and (str(exc) != 'AGY CLI 返回了空结果' or not session):
            if str(exc)=='AGY CLI 返回了空结果' and not session:
                raise AIRequestError('AGY 未返回正文或会话编号，可重试本批；已保存进度保留',
                                     kind='provider_unavailable',retriable=True) from exc
            raise
        response = None
    if continuation:
        blocked=_blocking_diagnostic(stderr)
        if blocked:
            raise AIRequestError('AGY 续写伴随明确的登录、地区或启动配置错误，未采纳结果',
                                 kind=blocked,retriable=False)
    elif returncode:
        kind, retriable = _error_kind(stderr)
        if returncode in {-15, -2, 130, 143} and kind == 'agy_cli_error':
            kind, retriable = 'network_transport', True
        raise AIRequestError(f'AGY CLI 异常退出（退出码 {returncode}），本次结果未采纳', kind=kind, retriable=retriable)
    if response is not None and files is None and not continuation:
        return response
    from .agy_session import read_session, SessionEvidenceError
    try:
        receipt = read_session(session, prompt, allowed_files=files, require_output_continuation=continuation,
                               **({'prior_prompts': prior_prompts} if prior_prompts else {}))
    except SessionEvidenceError as exc:
        detail = str(exc)
        if files is not None:
            from .durable import atomic_json
            atomic_json(Path(working)/'failure.json', {'session_id':session,'error':detail})
        incomplete=detail in {'AGY 会话完成记录尚不可用','AGY 未保存完整最终正文','AGY 超限后尚未完成续写','AGY 最终正文尚未完整结束'}
        kind = ('input_too_long' if '截断' in detail else
                'output_truncated' if continuation and (incomplete or exc.kind=='output_truncated') else exc.kind)
        # An unapproved tool or out-of-scope read is a protocol violation.
        # Keep it separate from transport failures so the task card explains why
        # no result was accepted and why a retry starts a fresh turn.
        raise AIRequestError(detail, kind=kind, retriable=kind=='output_truncated') from exc
    if files is not None:
        from .durable import atomic_json
        atomic_json(Path(working)/'receipt.json', {k: v for k, v in receipt.items() if k not in {'response','events'}})
    if response is not None and not continuation and response.strip() != receipt['response']:
        raise AIRequestError('AGY 最终正文与本次完成记录不一致，未采纳结果', kind='agy_cli_error', retriable=False)
    return receipt['response']


def _request_arguments(instructions: str, payload: dict[str, Any], provider: dict[str, Any], conversation: str = ''):
    prompt = compose_prompt(instructions, payload)
    window = normalize_context_window(provider.get('context_window'))
    estimated = estimate_tokens(prompt) + 16_000
    if estimated > window:
        raise AIRequestError(
            f"发送给 AGY CLI 的输入及协议预计 {estimated:,} tokens，超过已保存上下文预算 {window:,}；程序未发送请求",
            kind="input_too_long",
            retriable=False,
        )
    cli = executable(provider)
    selected_model = model(provider)
    selected_effort = effort(provider)
    timeout = timeout_seconds(provider)
    working = PRIVATE_ROOT / "agy-cli"
    working.mkdir(parents=True, exist_ok=True, mode=0o700)
    file_transport = len(prompt.encode()) > INLINE_INPUT_BYTES
    if file_transport:
        prompt, working = _file_prompt(prompt, working)
        manifest = json.loads((working/'transport.json').read_text())
        encoded_estimate = estimate_tokens(prompt) + sum(estimate_tokens(Path(p).read_text()) for p in manifest['files']) + 16_000
        if encoded_estimate > window:
            raise AIRequestError(f'AGY 完整资料文件及协议预计 {encoded_estimate:,} tokens，超过已保存上下文预算 {window:,}；未发送或截断资料',
                                 kind='input_too_long',retriable=False)
    # A single user message through stdin avoids argv limits and is supported
    # by AGY 1.3.x. Slash expansion is disabled and the working directory is
    # an empty private directory, so transcript text cannot become a command
    # or an edit to the QwenASR checkout.
    message = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
    command = [
        cli,
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--model", selected_model,
        "--mode", "plan",
        "--disable-slash-commands",
    ]
    if conversation:
        # Continue this lesson's conversation; earlier turns stay in context.
        command[1:1] = ["--conversation", conversation]
    # Current AGY fixes Gemini thinking in its model id, including the High
    # variant. Passing --effort now fails before inference. Keep the same ID.
    if selected_model.endswith("-high") and not selected_model.startswith('gemini-'):
        command[command.index("--mode"):command.index("--mode")] = ["--effort", selected_effort]
    if file_transport:
        if sys.platform != 'darwin' or not shutil.which('sandbox-exec'):
            raise AIRequestError('AGY 长文本受控资料通道需要本机资料沙箱', kind='material_sandbox_unavailable', retriable=False)
        from .agy_materials import sandbox_profile
        command = ['sandbox-exec', '-p', sandbox_profile(working, cli), *command]
        env = subprocess_environment()
        temporary = working/'.tmp'
        temporary.mkdir(mode=0o700)
        env['TMPDIR'] = str(temporary)
    else:
        env = subprocess_environment()
    return command, message, working, env, timeout


def run_sync(instructions: str, payload: dict[str, Any], provider: dict[str, Any]) -> str:
    for attempt in range(len(MODEL_START_DELAYS)+1):
        try:
            return _run_sync_once(instructions,payload,provider)
        except AIRequestError as exc:
            if exc.kind!='agy_model_unavailable' or getattr(exc,'session_id',None) or not exc.retriable:raise
            if attempt==len(MODEL_START_DELAYS):
                exc.retriable=False
                exc.args=('AGY 启动三次仍无法识别已选模型；请刷新模型列表后重试，已完成进度保留',)
                raise
            time.sleep(MODEL_START_DELAYS[attempt])


def _run_sync_once(instructions: str, payload: dict[str, Any], provider: dict[str, Any]) -> str:
    command, message, working, env, timeout = _request_arguments(instructions, payload, provider)
    try:
        completed = subprocess.run(
            command,
            input=message,
            text=True,
            capture_output=True,
            cwd=str(working),
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AIRequestError(
            f"AGY CLI 超过 {timeout} 秒仍未完成，请稍后重试",
            kind="network_read_timeout",
            retriable=True,
        ) from exc
    except OSError as exc:
        raise AIRequestError(f"启动 AGY CLI 失败：{exc}", kind="agy_cli_unavailable", retriable=False) from exc
    return _finish_output(completed.stdout, completed.stderr, working, message, completed.returncode)


# Kinds where a failed continued turn is retried once as a fresh conversation.
SESSION_FALLBACK_KINDS = {'agy_cli_error', 'material_read_path', 'material_tool_violation', 'input_too_long'}


async def request_text(instructions: str, payload: dict[str, Any], provider: dict[str, Any]) -> str:
    from .agy_lesson_session import current
    session = current.get()
    if session is None or session.lock.locked():
        return await _request_text_fresh(instructions, payload, provider)
    async with session.lock:
        text, shaped, conversation = session.plan(instructions, payload)
        if conversation:
            try:
                response, prompt = await _request_text_retrying(text, shaped, provider, conversation, list(session.prompts))
                session.record(session_id=last_session_id.get() or conversation, prompt=prompt, instructions=instructions,
                               payload=payload, response=response, continued=True)
                return response
            except AIRequestError as exc:
                session.reset('continue_failed:' + str(exc.kind))
                if exc.kind not in SESSION_FALLBACK_KINDS:
                    raise
                session.stats['fallbacks'] += 1
        response, prompt = await _request_text_retrying(instructions, payload, provider, '', [])
        if last_session_id.get():
            session.record(session_id=last_session_id.get(), prompt=prompt, instructions=instructions,
                           payload=payload, response=response, continued=False)
        return response


async def _request_text_fresh(instructions, payload, provider):
    return (await _request_text_retrying(instructions, payload, provider, '', []))[0]


async def _request_text_retrying(instructions, payload, provider, conversation, prior_prompts):
    # Unknown tools still invalidate a turn. Read-only view_file/read_file
    # receipts have their own explicit path/content validators.
    for attempt in range(len(PROTOCOL_RETRY_DELAYS) + 1):
        try:
            response = await retry_model_start(lambda: _request_text_once(instructions,payload,provider,conversation,prior_prompts))
            return response, last_prompt.get()
        except AIRequestError as exc:
            if exc.kind != 'material_tool_violation' or attempt == len(PROTOCOL_RETRY_DELAYS):
                raise
            await asyncio.sleep(PROTOCOL_RETRY_DELAYS[attempt])


async def _request_text_once(instructions: str, payload: dict[str, Any], provider: dict[str, Any],
                             conversation: str = '', prior_prompts=()):
    # Own the child in the task. Cancelling to_thread(subprocess.run) leaves
    # AGY alive, so a timed-out connection test previously continued making
    # requests for up to the provider's 30-minute generation timeout.
    command, message, working, env, timeout = _request_arguments(instructions, payload, provider, conversation)
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(working), env=env,
            limit=4 * 1024 * 1024,
        )
    except OSError as exc:
        raise AIRequestError(f"启动 AGY CLI 失败：{exc}", kind="agy_cli_unavailable", retriable=False) from exc

    async def read_stdout() -> bytes:
        chunks = []
        output_chars = 0
        while line := await process.stdout.readline():
            chunks.append(line)
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "init":
                from .ai_activity import bind_ai_session
                bind_ai_session(event.get('conversation_id'))
                report_ai_activity("processing")
            elif event.get("event") == "step_update":
                step = event.get("step_update")
                if not isinstance(step, dict):
                    continue
                delta = step.get("text_delta")
                if step.get("step_type") == "agent_response" and isinstance(delta, str) and delta:
                    output_chars += len(delta)
                    report_ai_activity("receiving", output_chars=output_chars)
                elif step.get("step_type") == "agent_response":
                    report_ai_activity("processing")
        return b"".join(chunks)

    async def communicate() -> tuple[bytes, bytes]:
        # Drain both pipes concurrently so diagnostic output cannot block the
        # model, while stdout events become visible before the final result.
        readers = [asyncio.create_task(read_stdout()), asyncio.create_task(process.stderr.read())]
        try:
            try:
                process.stdin.write(message.encode("utf-8"))
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # Early CLI failures can close stdin before producing their
                # structured error. Preserve that result instead of a pipe error.
                pass
            finally:
                try:process.stdin.close()
                except (BrokenPipeError,ConnectionResetError):pass
            stdout, stderr = await asyncio.gather(*readers)
            await process.wait()
            return stdout, stderr
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    try:
        stdout, stderr = await asyncio.wait_for(communicate(), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise AIRequestError(
            f"AGY CLI 超过 {timeout} 秒仍未完成，请稍后重试",
            kind="network_read_timeout", retriable=True,
        ) from exc
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    decoded=stdout.decode('utf-8',errors='replace')
    prompt = json.loads(message)['message']['content']
    ok = False
    try:
        response = _finish_output(decoded, stderr.decode("utf-8", errors="replace"), working, message, process.returncode,
                                  prior_prompts=prior_prompts, conversation=conversation)
        ok = True
        last_prompt.set(prompt)
        return response
    finally:
        _record_metrics(decoded, started, message, conversation, ok)


def _record_metrics(stdout, started, message, conversation, ok):
    """Per-request timing and token usage for the pipeline benchmark (no content)."""
    usage = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('event') == 'result':
            usage = (event.get('result') or {}).get('usage') or {}
    try:
        with open(PRIVATE_ROOT / 'agy-cli' / 'metrics.jsonl', 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({'at': time.time(), 'seconds': round(time.monotonic() - started, 1), 'ok': ok,
                                     'continued': bool(conversation), 'message_bytes': len(message.encode()),
                                     'session_id': last_session_id.get(), 'usage': usage}) + '\n')
    except OSError:
        pass


def status(provider: dict[str, Any]) -> dict[str, Any]:
    path = executable(provider)
    return {"available": True, "path": path, "model": model(provider), "effort": effort(provider), "timeout_seconds": timeout_seconds(provider)}
