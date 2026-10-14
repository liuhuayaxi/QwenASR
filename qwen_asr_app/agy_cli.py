"""Small, non-interactive adapter for the local ``agy`` CLI.

The CLI is deliberately isolated from the web process' shell environment: a
single prompt is sent through AGY's stream-json stdin protocol and only the
final response is returned.  This keeps long lecture text out of argv (macOS
has a relatively small argv limit) and prevents the agent from editing the
QwenASR checkout while it is being used as an AI provider.
"""
from __future__ import annotations

import json
import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .ai import AIRequestError
from .ai_activity import report_ai_activity
from .paths import PRIVATE_ROOT


DEFAULT_MODEL = "gemini-3.8-flash-medium"
DEFAULT_TIMEOUT = 1800
MAX_PROMPT_CHARS = 900_000


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


def timeout_seconds(provider: dict[str, Any]) -> int:
    try:
        value = int(provider.get("cli_timeout_seconds") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError) as exc:
        raise AIRequestError("AGY CLI 超时必须是整数秒", kind="agy_cli_config", retriable=False) from exc
    if not 30 <= value <= 7200:
        raise AIRequestError("AGY CLI 超时必须在 30–7200 秒之间", kind="agy_cli_config", retriable=False)
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
    # This CLI message covers a timed-out eligibility/auth handshake. It is
    # ambiguous, unlike an explicit revoked grant or login requirement, so
    # permit bounded transport retries before asking the user to sign in.
    if "authentication failed or timed out" in value:
        return "network_connect_timeout", True
    if any(marker in value for marker in ("unauthenticated", "authentication", "oauth", "login", "not logged in", "permission denied")):
        return "provider_auth", False
    if any(marker in value for marker in ("resource exhausted", "rate limit", "too many requests", "429")):
        return "rate_limit", True
    if any(marker in value for marker in ('unavailable (code 503)', 'service unavailable', 'service is currently unavailable', 'bad gateway', 'gateway timeout')):
        return 'provider_unavailable', True
    if any(marker in value for marker in ("deadline exceeded", "timed out", "timeout")):
        return "network_read_timeout", True
    if any(marker in value for marker in (
        "connection reset", "unexpected eof", "post \"http", ": eof", "eof",
        "the stream was interrupted", "stream interrupted",
        "network issue connecting to the server",
    )):
        return "network_transport", True
    if any(marker in value for marker in (
        "maximum output", "max tokens", "output token", "output limit",
        "context length", "too many tokens", "truncated", "cut off",
    )):
        return "output_truncated", True
    return "agy_cli_error", False


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
        kind, retriable = _error_kind(message)
        raise AIRequestError(f"AGY CLI 没有返回最终结果：{message or '空输出'}", kind=kind, retriable=retriable)
    if str(result.get("status") or "").upper() not in {"SUCCESS", "COMPLETED", "OK"}:
        message = _text(result.get("error")) or (stderr or "AGY CLI 请求失败").strip()
        kind, retriable = _error_kind(message)
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


def _request_arguments(instructions: str, payload: dict[str, Any], provider: dict[str, Any]):
    prompt = compose_prompt(instructions, payload)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise AIRequestError(
            f"发送给 AGY CLI 的整理片段过长（{len(prompt)} 字符），程序未发送请求",
            kind="input_too_long",
            retriable=True,
        )
    cli = executable(provider)
    selected_model = model(provider)
    selected_effort = effort(provider)
    timeout = timeout_seconds(provider)
    working = PRIVATE_ROOT / "agy-cli"
    working.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    # Current AGY fixes Gemini thinking in its model id, including the High
    # variant. Passing --effort now fails before inference. Keep the same ID.
    if selected_model.endswith("-high") and not selected_model.startswith('gemini-'):
        command[command.index("--mode"):command.index("--mode")] = ["--effort", selected_effort]
    return command, message, working, subprocess_environment(), timeout


def run_sync(instructions: str, payload: dict[str, Any], provider: dict[str, Any]) -> str:
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
    return _decode_output(completed.stdout, completed.stderr)


async def request_text(instructions: str, payload: dict[str, Any], provider: dict[str, Any]) -> str:
    # Own the child in the task. Cancelling to_thread(subprocess.run) leaves
    # AGY alive, so a timed-out connection test previously continued making
    # requests for up to the provider's 30-minute generation timeout.
    command, message, working, env, timeout = _request_arguments(instructions, payload, provider)
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
                process.stdin.close()
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
    return _decode_output(stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"))


def status(provider: dict[str, Any]) -> dict[str, Any]:
    path = executable(provider)
    return {"available": True, "path": path, "model": model(provider), "effort": effort(provider), "timeout_seconds": timeout_seconds(provider)}
