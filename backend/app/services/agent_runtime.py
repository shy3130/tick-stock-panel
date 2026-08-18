from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from collections import deque
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from time import perf_counter
from typing import Any

from app import secrets_store
from app.config import settings
from app.services import agent_tools, ai_profiles
from app.services.agent_loop import (
    ALLOWED_AGENT_TOOLS,
    MAX_TOOL_ROUNDS,
    _final_system,
    _tools_system,
)

_MAX_PROTOCOL_LINE_BYTES = 256 * 1024
_MAX_STDERR_CHARS = 2_000
_PROCESS_STOP_TIMEOUT_S = 2.0
_MAX_TOOL_REQUESTS = MAX_TOOL_ROUNDS * 5
_WORKER_ENV_KEYS = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NODE_EXTRA_CA_CERTS",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "WINDIR",
    }
)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger(__name__)


async def run_agent_stream(
    messages: list[dict],
    app_state: Any,
    profile_id: str | None = None,
) -> AsyncIterator[str]:
    """Run one Agent turn through the configured runtime adapter.

    The caller-facing interface intentionally remains the legacy NDJSON stream.
    Session state, attempts, cancellation, event replay and persistence stay in
    Python; the optional Pi worker only owns one model/tool loop.
    """

    runtime = str(settings.agent_runtime or "python").strip().lower()
    if runtime == "python":
        from app.services.agent_loop import run_agent_stream as run_legacy_stream

        async for line in run_legacy_stream(messages, app_state, profile_id):
            yield line
        return

    if runtime != "pi":
        yield _error_line("未知 Agent 运行时，请检查 AGENT_RUNTIME", 0.0)  # noqa: RUF001
        return

    started_at = perf_counter()
    logger.info("Pi Agent runtime attempt started")
    secret = ""
    try:
        profile, secret = _resolve_pi_profile(profile_id)
        async for line in _run_pi_worker(messages, app_state, profile, secret):
            yield line
    except asyncio.CancelledError:
        logger.info("Pi Agent runtime attempt cancelled")
        raise
    except Exception as exc:
        elapsed_ms = round((perf_counter() - started_at) * 1000, 1)
        message = _sanitize_runtime_error(exc, secret)
        logger.warning("Pi Agent runtime attempt failed: %s", type(exc).__name__)
        yield _error_line(message, elapsed_ms)
    else:
        logger.info(
            "Pi Agent runtime attempt completed in %.1fms",
            (perf_counter() - started_at) * 1000,
        )


def _resolve_pi_profile(profile_id: str | None) -> tuple[dict[str, Any], str]:
    if getattr(sys, "frozen", False):
        raise RuntimeError("Pi Agent 试点不支持桌面打包环境")

    saved = ai_profiles.resolve_profile(profile_id)
    if profile_id and (saved is None or saved.get("id") != profile_id):
        raise RuntimeError("Pi Agent profile 不存在")
    provider = (saved or {}).get("provider") or secrets_store.get_ai_config(
        "ai_provider", settings.ai_provider
    )
    if provider != ai_profiles.OPENAI_COMPAT_PROVIDER:
        raise RuntimeError("Pi Agent 试点仅支持 openai_compat profile")

    api_key = (saved or {}).get("api_key") or secrets_store.get_ai_key()
    base_url = (saved or {}).get("base_url") or secrets_store.get_ai_config(
        "ai_base_url", settings.ai_base_url
    )
    model = (saved or {}).get("model") or secrets_store.get_ai_config(
        "ai_model", settings.ai_model
    )
    if not api_key:
        raise RuntimeError("Pi Agent profile 缺少 API Key")
    if not base_url:
        raise RuntimeError("Pi Agent profile 缺少 Base URL")
    if not model:
        raise RuntimeError("Pi Agent profile 缺少模型名称")

    user_agent = (saved or {}).get("user_agent") or settings.ai_user_agent
    profile: dict[str, Any] = {
        "provider": ai_profiles.OPENAI_COMPAT_PROVIDER,
        "base_url": str(base_url),
        "api_key": str(api_key),
        "model": str(model),
        "context_window": 128_000,
        "max_tokens": 1_600,
        "temperature": 0.2,
        "thinking_level": "off",
    }
    if user_agent:
        profile["extra_headers"] = {"User-Agent": str(user_agent)}
    return profile, str(api_key)


def _worker_path() -> Path:
    configured = str(settings.agent_pi_worker_path or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = _PROJECT_ROOT / path
    else:
        path = _PROJECT_ROOT / "pi-agent-worker" / "src" / "worker.js"
    return path.resolve()


def _resolve_node_command() -> str:
    configured = str(settings.agent_pi_node_command or "node").strip()
    if not configured:
        raise RuntimeError("Pi Agent Node 命令未配置")
    resolved = shutil.which(configured)
    if resolved is None:
        raise RuntimeError("Pi Agent 需要 Node.js 22.19 或更高版本")
    return resolved


def _tool_specs() -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for tool in ALLOWED_AGENT_TOOLS:
        if tool.get("read_only") is not True:
            raise RuntimeError("Pi Agent 试点拒绝非只读工具")
        schema = tool.get("parameters") or tool.get("input_schema")
        if not isinstance(schema, dict):
            raise RuntimeError("Pi Agent 工具缺少输入 schema")
        specs.append(
            {
                "name": str(tool["name"]),
                "description": str(tool["description"]),
                "input_schema": schema,
                "read_only": True,
            }
        )
    return specs

def _worker_environment() -> dict[str, str]:
    """Pass only process settings required by Node itself, never backend secrets."""
    return {
        key: value
        for key in _WORKER_ENV_KEYS
        if (value := os.environ.get(key)) is not None
    }


async def _run_pi_worker(
    messages: list[dict],
    app_state: Any,
    profile: dict[str, Any],
    secret: str,
) -> AsyncIterator[str]:
    worker_path = _worker_path()
    if not worker_path.is_file():
        raise RuntimeError("Pi Agent worker 文件不存在，请先安装试点依赖")  # noqa: RUF001
    node_command = _resolve_node_command()

    proc = await asyncio.create_subprocess_exec(
        node_command,
        str(worker_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(worker_path.parent),
        env=_worker_environment(),
        limit=_MAX_PROTOCOL_LINE_BYTES + 1,
    )
    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        await _stop_process(proc)
        raise RuntimeError("Pi Agent worker 管道初始化失败")

    stderr_tail: deque[str] = deque()
    stderr_task = asyncio.create_task(_drain_stderr(proc.stderr, stderr_tail, secret))
    terminal = False
    started_at = perf_counter()
    try:
        try:
            ready = await asyncio.wait_for(
                _read_envelope(proc.stdout),
                timeout=float(settings.agent_pi_ready_timeout_s),
            )
        except TimeoutError as exc:
            raise RuntimeError("Pi Agent worker ready 超时") from exc
        if ready.get("type") == "fatal":
            raise RuntimeError(_message_field(ready, "Pi Agent worker 启动失败"))
        if ready.get("type") != "ready":
            raise RuntimeError("Pi Agent worker 未返回 ready")

        start_message = {
            "type": "start",
            "profile": profile,
            "messages": _history_messages(messages),
            "system_prompt": _tools_system(),
            "final_prompt": _final_system(),
            "tools": _tool_specs(),
            "max_tool_rounds": MAX_TOOL_ROUNDS,
        }
        seen_request_ids: set[str] = set()
        tool_request_count = 0
        await _write_envelope(proc.stdin, start_message)

        allowed_names = {tool["name"] for tool in ALLOWED_AGENT_TOOLS}
        while True:
            try:
                envelope = await asyncio.wait_for(
                    _read_envelope(proc.stdout),
                    timeout=float(settings.agent_pi_response_timeout_s),
                )
            except TimeoutError as exc:
                raise RuntimeError("Pi Agent worker 响应超时") from exc
            event_type = envelope.get("type")

            if event_type == "delta":
                content = envelope.get("content")
                if not isinstance(content, str):
                    raise RuntimeError("Pi Agent worker 返回了无效 delta")
                yield json.dumps({"type": "delta", "content": content}, ensure_ascii=False)
                continue

            if event_type == "tool_request":
                request_id = envelope.get("request_id")
                name = envelope.get("name")
                args = envelope.get("args")
                if not isinstance(request_id, str) or not request_id:
                    raise RuntimeError("Pi Agent worker 返回了无效 tool_request")
                if not isinstance(name, str) or name not in allowed_names:
                    raise RuntimeError("Pi Agent worker 请求了未授权工具")
                if not isinstance(args, dict):
                    raise RuntimeError("Pi Agent worker 返回了无效工具参数")
                if request_id in seen_request_ids:
                    raise RuntimeError("Pi Agent worker 重复使用 request_id")
                seen_request_ids.add(request_id)
                tool_request_count += 1
                if tool_request_count > _MAX_TOOL_REQUESTS:
                    raise RuntimeError("Pi Agent worker 工具请求超过安全上限")

                yield json.dumps(
                    {"type": "tool_call", "name": name, "args": args},
                    ensure_ascii=False,
                )
                tool_started_at = perf_counter()
                ok = True
                try:
                    result = await asyncio.to_thread(agent_tools.call_tool, name, app_state, args)
                except Exception as exc:
                    ok = False
                    result = {"error": agent_tools.sanitize_tool_error(exc)}

                reply: dict[str, Any] = {
                    "type": "tool_result",
                    "request_id": request_id,
                    "ok": ok,
                }
                if ok:
                    reply["result"] = result
                else:
                    reply["error"] = str(result["error"])
                await _write_envelope(proc.stdin, reply)

                yield json.dumps(
                    {
                        "type": "tool_result",
                        "name": name,
                        "result": result,
                        "elapsed_ms": round((perf_counter() - tool_started_at) * 1000, 1),
                    },
                    ensure_ascii=False,
                    default=str,
                )
                continue

            if event_type == "done":
                await _wait_for_process_exit(proc)
                if proc.returncode != 0:
                    raise RuntimeError(_stderr_summary(stderr_tail) or "Pi Agent worker 异常退出")
                terminal = True
                elapsed = envelope.get("elapsed_ms")
                elapsed_ms = (
                    float(elapsed)
                    if isinstance(elapsed, (int, float))
                    else round((perf_counter() - started_at) * 1000, 1)
                )
                yield json.dumps({"type": "done", "elapsed_ms": elapsed_ms}, ensure_ascii=False)
                return

            if event_type == "fatal":
                await _wait_for_process_exit(proc)
                terminal = True
                message = _message_field(envelope, "Pi Agent worker 执行失败")
                raise RuntimeError(message)

            raise RuntimeError("Pi Agent worker 返回了未知协议事件")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # A process-level crash may close stdout without a protocol fatal.
        # Preserve a bounded, already-redacted stderr hint for recovery.
        if proc.returncode is not None:
            await asyncio.gather(stderr_task, return_exceptions=True)
        summary = _stderr_summary(stderr_tail)
        if summary and str(exc) == "Pi Agent worker 在终态前退出":
            raise RuntimeError(summary) from exc
        raise
    finally:
        if not terminal or proc.returncode is None:
            await _stop_process(proc)
        if not stderr_task.done():
            stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)


def _history_messages(messages: list[dict]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            raise RuntimeError("Pi Agent 会话消息格式无效")
        out.append({"role": role, "content": content})
    if not out or out[-1]["role"] != "user":
        raise RuntimeError("Pi Agent 会话必须以用户消息结束")
    return out


async def _read_envelope(reader: asyncio.StreamReader) -> dict[str, Any]:
    raw = await reader.readline()
    if not raw:
        raise RuntimeError("Pi Agent worker 在终态前退出")
    if len(raw) > _MAX_PROTOCOL_LINE_BYTES:
        raise RuntimeError("Pi Agent worker 协议消息过大")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Pi Agent worker 返回了无效 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Pi Agent worker 协议消息必须是对象")
    return value


async def _write_envelope(writer: asyncio.StreamWriter, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    writer.write(payload.encode("utf-8") + b"\n")
    await writer.drain()


async def _drain_stderr(
    reader: asyncio.StreamReader,
    tail: deque[str],
    secret: str,
) -> None:
    while True:
        chunk = await reader.read(1024)
        if not chunk:
            return
        text = chunk.decode("utf-8", errors="replace")
        tail.append(_sanitize_runtime_error(text, secret))
        while sum(len(item) for item in tail) > _MAX_STDERR_CHARS and tail:
            tail.popleft()


def _stderr_summary(tail: deque[str]) -> str:
    return " ".join(item for item in tail if item).strip()[:_MAX_STDERR_CHARS]


async def _wait_for_process_exit(proc: asyncio.subprocess.Process) -> None:
    try:
        await asyncio.wait_for(proc.wait(), timeout=_PROCESS_STOP_TIMEOUT_S)
    except TimeoutError:
        await _stop_process(proc)


async def _stop_process(proc: asyncio.subprocess.Process) -> None:
    if proc.stdin is not None and not proc.stdin.is_closing():
        proc.stdin.close()
        with suppress(TimeoutError, BrokenPipeError, ConnectionResetError):
            await asyncio.wait_for(proc.stdin.wait_closed(), timeout=0.5)
    if proc.returncode is not None:
        return
    with suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=_PROCESS_STOP_TIMEOUT_S)
        return
    except TimeoutError:
        pass
    with suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


def _message_field(envelope: dict[str, Any], fallback: str) -> str:
    message = envelope.get("message")
    return message if isinstance(message, str) and message.strip() else fallback


def _sanitize_runtime_error(exc: BaseException | str, secret: str = "") -> str:
    message = str(exc) or type(exc).__name__
    if secret:
        message = message.replace(secret, "***")
    message = agent_tools.sanitize_tool_error(RuntimeError(message))
    return " ".join(message.split())[:1_200] or "Pi Agent 运行失败"


def _error_line(message: str, elapsed_ms: float) -> str:
    return json.dumps(
        {"type": "error", "message": f"Agent 失败: {message}", "elapsed_ms": elapsed_ms},
        ensure_ascii=False,
    )
