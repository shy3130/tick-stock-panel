"""AI provider adapter for OpenAI-compatible APIs and local Codex CLI."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from types import TracebackType
from urllib.parse import urlsplit, urlunsplit

from app import secrets_store
from app.config import settings

OPENAI_COMPAT_PROVIDER = "openai_compat"
CODEX_CLI_PROVIDER = "codex_cli"
CODEX_DEFAULT_COMMAND = "codex"
CODEX_SUPPORTED_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}

_CODEX_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "TEMP",
    "TMP",
    "TMPDIR",
    "SHELL",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)

Message = dict[str, str]

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# ----------------------------------------------------------------
# 用户 focus 输入净化 — 防止通过"特别关注"绕过红线诱导 AI 给出买卖建议
# 命中任一敏感词时,整个 focus 被丢弃(返回空串),由各 analyzer 据此跳过注入。
# ----------------------------------------------------------------
_FOCUS_BLOCKLIST = re.compile(
    r"买入|卖出|加仓|减仓|轻仓|重仓|半仓|全仓|仓位|止损|止盈|"
    r"操作建议|买卖点|买卖区间|建仓|平仓|清仓|调仓|"
    r"追高|低吸|反包|抄底|逃顶|进攻|防守|"
    r"激进|稳健|保守|目标价|能涨|会跌|预测涨|预测跌|"
    r"荐股|推荐买|推荐卖|值得投资|现在买|可以买|能买|要不要买|买吗|卖吗|"
    r"明日基调|交易计划|下单",
    re.IGNORECASE,
)


def sanitize_focus(focus: str) -> str:
    """净化用户输入的 focus 文本。

    命中交易指令/投资建议类敏感词时返回空串,阻止其注入 AI 提示词。
    这是对系统提示词红线的兜底:即便用户试图通过 focus 绕过,也不会生效。
    """
    if not focus:
        return ""
    text = focus.strip()
    if not text:
        return ""
    if _FOCUS_BLOCKLIST.search(text):
        return ""
    return text


def current_ai_provider() -> str:
    return secrets_store.get_ai_config("ai_provider", settings.ai_provider) or OPENAI_COMPAT_PROVIDER


def current_ai_model() -> str:
    if current_ai_provider() == CODEX_CLI_PROVIDER:
        return normalize_codex_model(str(secrets_store.load().get("ai_model") or ""))
    return secrets_store.get_ai_config("ai_model", settings.ai_model)


def current_codex_command() -> str:
    return normalize_codex_command(
        secrets_store.get_ai_config("ai_codex_command", settings.ai_codex_command),
        strict=False,
    )


def current_codex_reasoning_effort() -> str:
    return normalize_codex_reasoning_effort(
        secrets_store.get_ai_config(
            "ai_codex_reasoning_effort",
            settings.ai_codex_reasoning_effort,
        )
    )


def is_codex_cli_provider(provider: str | None = None) -> bool:
    return (provider or current_ai_provider()) == CODEX_CLI_PROVIDER


def normalize_codex_model(model: str) -> str:
    value = model.strip()
    aliases = {
        "gpt5.5": "gpt-5.5",
        "gpt5.6": "gpt-5.6-sol",
        "gpt5.6-sol": "gpt-5.6-sol",
        "gpt5.6-terra": "gpt-5.6-terra",
        "gpt5.6-luna": "gpt-5.6-luna",
    }
    return aliases.get(value.lower(), value)


def normalize_codex_reasoning_effort(effort: str | None) -> str:
    value = (effort or "").strip().lower()
    return value if value in CODEX_SUPPORTED_REASONING_EFFORTS else ""


def normalize_codex_command(command: str | None, *, strict: bool = True) -> str:
    value = (command or "").strip()
    if not value or value.lower() == CODEX_DEFAULT_COMMAND:
        return CODEX_DEFAULT_COMMAND
    if strict:
        raise ValueError("Codex CLI 仅支持使用默认 codex 命令自动解析, 不支持自定义可执行路径")
    return CODEX_DEFAULT_COMMAND


_VERSION_SEGMENT_RE = re.compile(r"/v\d+(?:\.\d+)?$", re.IGNORECASE)


def normalize_openai_base_url(url: str) -> str:
    """Return the OpenAI-compatible base URL expected by the OpenAI SDK.

    识别 URL 中已有的版本段 (/v1、/v2、/v4 等) 时保持原样 —— 部分 OpenAI 兼容
    服务用非 v1 的版本号 (如智谱 GLM 用 /api/paas/v4), 旧实现无条件补 /v1 会拼成
    不存在的 /api/paas/v4/v1/chat/completions 导致 404。仅在无版本段时才补 /v1。
    """
    base = (url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")].rstrip("/")
    if _VERSION_SEGMENT_RE.search(base):
        return base
    return f"{base}/v1"


def codex_cli_available() -> bool:
    try:
        _codex_base_command()
        return True
    except RuntimeError:
        return False


def ai_configured(provider: str | None = None) -> bool:
    provider = provider or current_ai_provider()
    if is_codex_cli_provider(provider):
        return codex_cli_available()
    return bool(secrets_store.get_ai_key())


async def generate_ai_text(
    messages: Sequence[Message],
    *,
    temperature: float | None = 0.3,
    max_tokens: int = 3000,
    timeout: float = 180.0,
) -> str:
    """Return a complete AI response from the currently configured provider."""
    if is_codex_cli_provider():
        return await _run_codex_cli(messages, max_tokens=max_tokens, timeout=max(timeout, 600.0))
    return await _run_openai_once(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


async def stream_ai_text(
    messages: Sequence[Message],
    *,
    temperature: float | None = 0.5,
    max_tokens: int = 4000,
    timeout: float = 180.0,
) -> AsyncIterator[str]:
    """Yield text deltas from the configured provider.

    Codex CLI only exposes the final assistant message for this use case, so it
    yields one complete chunk after the command exits.
    """
    if is_codex_cli_provider():
        yield await _run_codex_cli(messages, max_tokens=max_tokens, timeout=max(timeout, 600.0))
        return

    async for chunk in _stream_openai(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    ):
        yield chunk


async def _run_openai_once(
    messages: Sequence[Message],
    *,
    temperature: float | None,
    max_tokens: int,
    timeout: float,
) -> str:
    ai_key = secrets_store.get_ai_key()
    if not ai_key:
        raise RuntimeError("AI API Key 未配置, 请在设置页配置")

    client = _openai_client(ai_key, timeout)
    model = current_ai_model()
    req_messages = list(messages)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=req_messages,
            **_openai_kwargs(temperature=temperature, max_tokens=max_tokens),
        )
    except Exception as exc:
        # Reasoning 类模型 (如 kimi-k2.7-code, deepseek-r1, o 系列) 拒绝非约定
        # temperature (Moonshot 报 "only 1 is allowed for this model")。不再靠
        # 模型名猜测, 而是捕获该错误后去掉 temperature 重试一次 —— 对所有此类模型都稳。
        if temperature is not None and _is_temperature_rejected(exc):
            resp = await client.chat.completions.create(
                model=model,
                messages=req_messages,
                **_openai_kwargs(temperature=None, max_tokens=max_tokens),
            )
        else:
            if _is_openai_transport_error(exc):
                raise RuntimeError(_format_openai_error(exc)) from exc
            raise
    if not resp.choices:
        return ""
    return (resp.choices[0].message.content or "").strip()


async def _stream_openai(
    messages: Sequence[Message],
    *,
    temperature: float | None,
    max_tokens: int,
    timeout: float,
) -> AsyncIterator[str]:
    ai_key = secrets_store.get_ai_key()
    if not ai_key:
        raise RuntimeError("AI API Key 未配置, 请在设置页配置")

    client = _openai_client(ai_key, timeout)
    model = current_ai_model()
    req_messages = list(messages)

    async def _iter(stream):
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=req_messages,
            **_openai_kwargs(temperature=temperature, max_tokens=max_tokens),
            stream=True,
        )
    except Exception as exc:
        # 流尚未开始 yield, 可安全重建: 去掉 temperature 后重开 stream。
        if temperature is not None and _is_temperature_rejected(exc):
            stream = await client.chat.completions.create(
                model=model,
                messages=req_messages,
                **_openai_kwargs(temperature=None, max_tokens=max_tokens),
                stream=True,
            )
        else:
            if _is_openai_transport_error(exc):
                raise RuntimeError(_format_openai_error(exc)) from exc
            raise

    try:
        async for piece in _iter(stream):
            yield piece
    except Exception as exc:
        if _is_openai_transport_error(exc):
            raise RuntimeError(_format_openai_error(exc)) from exc
        raise


def _openai_client(api_key: str, timeout: float):
    from openai import AsyncOpenAI

    user_agent = secrets_store.get_ai_config("ai_user_agent", "") or settings.ai_user_agent
    return AsyncOpenAI(
        api_key=api_key,
        base_url=normalize_openai_base_url(secrets_store.get_ai_config("ai_base_url", settings.ai_base_url)),
        timeout=timeout,
        max_retries=0,
        default_headers={"User-Agent": user_agent},
    )


# Reasoning / thinking 类模型 (kimi-k2.7-code, deepseek-r1, OpenAI o 系列等) 不接受
# 任意 temperature, 上游会以 400 拒绝 (如 Moonshot: "only 1 is allowed for this model")。
# 这里不靠模型名猜测, 而是在真正命中该错误后自动去掉 temperature 重试 (见
# _run_openai_once / _stream_openai), 对任意 reasoning 模型都稳健。
_TEMP_REJECT_HINTS = ("temperature", "only 1 is allowed", "unsupported parameter")


def _is_temperature_rejected(exc: Exception) -> bool:
    """True if the upstream 400 is specifically about the temperature param."""
    if getattr(exc, "status_code", None) != 400:
        return False
    text = _openai_error_detail(exc) or str(exc)
    return any(h in text.lower() for h in _TEMP_REJECT_HINTS)


def _openai_kwargs(*, temperature: float | None, max_tokens: int) -> dict:
    """Build OpenAI create() kwargs; temperature omitted when None."""
    kwargs: dict = {"max_tokens": max_tokens}
    if temperature is not None:
        kwargs["temperature"] = temperature
    return kwargs


def _is_openai_transport_error(exc: Exception) -> bool:
    try:
        import openai
    except ImportError:
        openai = None

    if openai is not None and isinstance(exc, openai.APIError):
        return True

    try:
        import httpx
    except ImportError:
        return False

    return isinstance(exc, httpx.HTTPError)


def _format_openai_error(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)

    class_name = exc.__class__.__name__
    if "Timeout" in class_name:
        return "AI 服务请求超时, 请稍后重试或检查 AI Base URL / 网络"
    if "Connection" in class_name:
        return "AI 服务连接失败, 请检查 AI Base URL / 网络"

    detail = _openai_error_detail(exc)
    status_messages = {
        400: "请求参数无效, 请检查模型名称和上下文长度",
        401: "API Key 无效或无权限, 请检查设置页配置",
        403: "AI 服务拒绝访问, 请检查账号权限或网关配置",
        404: "模型或接口地址不存在, 请检查 AI Base URL 和模型名称",
        408: "AI 服务请求超时, 请稍后重试",
        429: "AI 服务限流或额度不足, 请稍后重试或检查额度",
        500: "AI 服务内部错误, 请稍后重试",
        502: "AI 网关返回错误, 请稍后重试或检查 AI Base URL",
        503: "AI 服务暂时不可用, 请稍后重试",
        504: "AI 上游服务超时, 请稍后重试或检查 AI Base URL / 网络",
    }
    # 优先透出上游真实错误 (如 Moonshot 的 "model not found"), 仅在没有
    # 可读 detail 时才回落到按状态码的通用文案, 避免吞掉排障关键信息。
    message = detail or status_messages.get(status) or "请稍后重试或检查 AI 服务配置"
    if status:
        return f"AI 服务请求失败({status}): {message}"
    return f"AI 服务请求失败: {message}"


def _openai_error_detail(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            text = error.get("message") or error.get("code") or error.get("type")
            return _compact_error_text(str(text or ""))
        if isinstance(error, str):
            return _compact_error_text(error)

    response = getattr(exc, "response", None)
    content_type = ""
    text = ""
    if response is not None:
        content_type = response.headers.get("content-type", "").lower()
        try:
            text = response.text
        except Exception:
            text = ""
    if not text and isinstance(body, str):
        text = body
    if not text:
        text = str(exc)
    if _looks_like_html(text, content_type):
        return ""
    return _compact_error_text(text)


def _looks_like_html(text: str, content_type: str) -> bool:
    sample = text.lstrip()[:200].lower()
    return "html" in content_type or sample.startswith("<!doctype html") or sample.startswith("<html")


def _compact_error_text(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


async def _run_codex_cli(
    messages: Sequence[Message],
    *,
    max_tokens: int,
    timeout: float,
) -> str:
    prompt = _codex_prompt(messages, max_tokens=max_tokens)
    run_path = Path(tempfile.mkdtemp(prefix="tickflow-codex-run-"))
    try:
        codex_home_path = run_path / "codex-home"
        workspace_path = run_path / "workspace"
        codex_home_path.mkdir()
        workspace_path.mkdir()
        output_path = codex_home_path / "last-message.txt"
        _prepare_codex_home(codex_home_path)

        args = [
            *_codex_base_command(),
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--output-last-message",
            str(output_path),
        ]
        model = current_ai_model().strip()
        if model:
            args.extend(["--model", model])
        args.extend(["--cd", str(workspace_path), "-"])

        env = _codex_process_env(codex_home_path)

        returncode, stdout, stderr = await asyncio.to_thread(
            _run_codex_process,
            args,
            prompt,
            env,
            timeout,
        )

        out = _clean_process_text(stdout)
        err = _clean_process_text(stderr)
        final_message = _read_output_file(output_path)
        if returncode != 0:
            detail = err or out or f"exit code {returncode}"
            raise RuntimeError(f"Codex CLI 调用失败: {detail[-1200:]}")
        result = final_message or out
        if not result:
            raise RuntimeError("Codex CLI 未返回内容")
        return result
    finally:
        await asyncio.to_thread(_remove_tree_best_effort, run_path)


def _run_codex_process(
    args: Sequence[str],
    prompt: str,
    env: dict[str, str],
    timeout: float,
) -> tuple[int, bytes, bytes]:
    try:
        proc = subprocess.run(
            list(args),
            input=prompt.encode("utf-8"),
            capture_output=True,
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Codex CLI 调用超时, 请稍后重试或检查本机 Codex 登录状态") from exc
    return proc.returncode, proc.stdout, proc.stderr


def _codex_process_env(codex_home_path: Path) -> dict[str, str]:
    """Pass only OS, locale, certificate, and proxy settings to Codex."""
    env: dict[str, str] = {}
    seen: set[str] = set()
    for name in _CODEX_ENV_ALLOWLIST:
        normalized = name.casefold() if os.name == "nt" else name
        if normalized in seen:
            continue
        value = os.environ.get(name)
        if value:
            env[name] = value
            seen.add(normalized)
    env["NO_COLOR"] = "1"
    env["CODEX_HOME"] = str(codex_home_path)
    return env


def _remove_tree_best_effort(path: Path) -> None:
    _remove_auth_files(path)
    for attempt in range(4):
        try:
            shutil.rmtree(path, onerror=_make_writable_and_retry)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 3:
                break
            time.sleep(0.2 * (attempt + 1))
    _remove_auth_files(path)
    shutil.rmtree(path, ignore_errors=True)
    _remove_auth_files(path)


def _remove_auth_files(path: Path) -> None:
    try:
        auth_files = list(path.rglob("auth.json"))
    except OSError:
        return
    for auth_file in auth_files:
        try:
            os.chmod(auth_file, stat.S_IWRITE)
            auth_file.unlink(missing_ok=True)
        except OSError:
            pass


def _make_writable_and_retry(
    func: Callable[[str], object],
    path: str,
    exc_info: tuple[type[BaseException], BaseException, TracebackType],
) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise exc_info[1] from None


def _codex_prompt(messages: Sequence[Message], *, max_tokens: int) -> str:
    parts = [
        "You are TickFlow Stock Panel's local AI provider.",
        "This is a text-generation task. The working directory is intentionally empty.",
        "Use only the user-provided prompt content below; do not inspect or modify local files.",
        "Return only the final requested content; do not include execution logs.",
    ]
    if max_tokens > 0:
        parts.append(f"Keep the final answer within about {max_tokens} output tokens.")
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        parts.append(f"\n<{role}>\n{content}\n</{role}>")
    return "\n".join(parts)


def _codex_base_command() -> list[str]:
    command = current_codex_command()
    resolved = _resolve_command(command)
    if not resolved:
        raise RuntimeError(f"未找到 Codex CLI 命令: {command}")

    if sys.platform == "win32" and resolved.lower().endswith(".ps1"):
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", resolved]
    return [resolved]


def _resolve_command(command: str) -> str | None:
    if command.lower() != CODEX_DEFAULT_COMMAND:
        return None

    if sys.platform == "win32":
        desktop_codex = _resolve_windows_desktop_codex()
        if desktop_codex:
            return desktop_codex

    resolved = shutil.which(command)
    if sys.platform == "win32" and resolved:
        resolved_path = Path(resolved)
        if not resolved_path.suffix:
            cmd_path = resolved_path.with_suffix(".cmd")
            if cmd_path.exists():
                return str(cmd_path)
    if not resolved and sys.platform == "win32" and not command.lower().endswith(".cmd"):
        resolved = shutil.which(f"{command}.cmd")
    if not resolved and sys.platform == "win32":
        resolved = _resolve_windows_codex_command(command)
    return resolved


def _resolve_windows_codex_command(command: str) -> str | None:
    """Find npm-installed Codex when the backend process has a minimal PATH."""
    raw = Path(command)
    if raw.parent != Path("."):
        return None

    names = [command]
    if not raw.suffix:
        names = [f"{command}.cmd", f"{command}.exe", f"{command}.bat", f"{command}.ps1", command]

    dirs: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / "npm")
    dirs.append(Path.home() / "AppData" / "Roaming" / "npm")

    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        value = os.environ.get(env_name)
        if value:
            dirs.append(Path(value) / "nodejs")

    for directory in dirs:
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
    return None


def _resolve_windows_desktop_codex() -> str | None:
    """Prefer the Codex Desktop bundled CLI over an older npm shim."""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None

    root = Path(local_appdata) / "OpenAI" / "Codex" / "bin"
    if not root.exists():
        return None

    candidates = list(root.glob("*/codex.exe"))
    direct = root / "codex.exe"
    if direct.exists():
        candidates.append(direct)
    if not candidates:
        return None

    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return str(newest)


def _prepare_codex_home(target: Path) -> None:
    """Create an isolated CODEX_HOME that reuses auth but not fragile config."""
    source = _codex_home()
    auth_file = source / "auth.json"
    if auth_file.exists():
        shutil.copy2(auth_file, target / "auth.json")
    _write_compatible_codex_config(target / "config.toml")


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _write_compatible_codex_config(path: Path) -> None:
    config = _read_codex_config()
    lines: list[str] = []
    local_provider = _docker_codex_local_provider(config)

    if local_provider:
        lines.append(_toml_string("model_provider", "codex_local_access"))

    model = current_ai_model() or normalize_codex_model(str(config.get("model") or ""))
    if model:
        lines.append(_toml_string("model", model))

    effort = current_codex_reasoning_effort() or normalize_codex_reasoning_effort(
        str(config.get("model_reasoning_effort") or "")
    )
    if effort:
        lines.append(_toml_string("model_reasoning_effort", effort))

    lines.append(_toml_string("approval_policy", "never"))
    lines.append(_toml_string("sandbox_mode", "read-only"))

    if local_provider:
        lines.append("")
        lines.append("[model_providers.codex_local_access]")
        for key in ("name", "base_url", "wire_api", "experimental_bearer_token"):
            value = local_provider.get(key)
            if isinstance(value, str) and value:
                lines.append(_toml_string(key, value))
        for key in ("requires_openai_auth", "supports_websockets"):
            value = local_provider.get(key)
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _docker_codex_local_provider(config: dict) -> dict | None:
    """Return the local-access provider adapted to Docker's host gateway."""
    docker_host = os.environ.get("CODEX_DOCKER_HOST", "").strip()
    if not docker_host or config.get("model_provider") != "codex_local_access":
        return None

    providers = config.get("model_providers")
    if not isinstance(providers, dict):
        return None
    source = providers.get("codex_local_access")
    if not isinstance(source, dict):
        return None

    provider = dict(source)
    base_url = str(provider.get("base_url") or "").strip()
    parsed = urlsplit(base_url)
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        port = f":{parsed.port}" if parsed.port else ""
        provider["base_url"] = urlunsplit(parsed._replace(netloc=f"{docker_host}{port}"))
    return provider


def _read_codex_config() -> dict:
    path = _codex_home() / "config.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError:
        return _read_codex_config_lenient(path)
    except OSError:
        return {}


def _read_codex_config_lenient(path: Path) -> dict:
    config: dict[str, str] = {}
    pattern = re.compile(r'^\s*([A-Za-z0-9_-]+)\s*=\s*"([^"]*)"\s*$')
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = pattern.match(line)
            if match:
                config[match.group(1)] = match.group(2)
    except OSError:
        pass
    return config


def _toml_string(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key} = "{escaped}"'


def _clean_process_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    return _ANSI_RE.sub("", text).strip()


def _read_output_file(path: Path) -> str:
    if path.exists():
        return _ANSI_RE.sub("", path.read_text(encoding="utf-8", errors="replace")).strip()
    return ""
