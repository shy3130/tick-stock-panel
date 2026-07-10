"""AI provider adapter for OpenAI-compatible APIs and local Codex CLI."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
import tomllib
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from app import secrets_store
from app.config import settings

OPENAI_COMPAT_PROVIDER = "openai_compat"
CODEX_CLI_PROVIDER = "codex_cli"
CODEX_DEFAULT_COMMAND = "codex"
CODEX_SERVICE_TIER_FALLBACK = "fast"
CODEX_SUPPORTED_SERVICE_TIERS = {"fast", "flex"}

Message = dict[str, str]

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


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


def is_codex_cli_provider(provider: str | None = None) -> bool:
    return (provider or current_ai_provider()) == CODEX_CLI_PROVIDER


def normalize_codex_model(model: str) -> str:
    value = model.strip()
    aliases = {
        "gpt5": "gpt-5",
        "gpt5.5": "gpt-5.5",
    }
    return aliases.get(value.lower(), value)


def normalize_codex_command(command: str | None, *, strict: bool = True) -> str:
    value = (command or "").strip()
    if not value or value.lower() == CODEX_DEFAULT_COMMAND:
        return CODEX_DEFAULT_COMMAND
    if strict:
        raise ValueError("Codex CLI 仅支持使用默认 codex 命令自动解析, 不支持自定义可执行路径")
    return CODEX_DEFAULT_COMMAND


def normalize_openai_base_url(url: str) -> str:
    """Return the OpenAI-compatible base URL expected by the OpenAI SDK."""
    base = (url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")].rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


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
    temperature: float = 0.3,
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
    temperature: float = 0.5,
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
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    ai_key = secrets_store.get_ai_key()
    if not ai_key:
        raise RuntimeError("AI API Key 未配置, 请在设置页配置")

    client = _openai_client(ai_key, timeout)
    try:
        resp = await client.chat.completions.create(
            model=current_ai_model(),
            messages=list(messages),
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        if _is_openai_transport_error(exc):
            raise RuntimeError(_format_openai_error(exc)) from exc
        raise
    if not resp.choices:
        return ""
    return (resp.choices[0].message.content or "").strip()


async def _stream_openai(
    messages: Sequence[Message],
    *,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> AsyncIterator[str]:
    ai_key = secrets_store.get_ai_key()
    if not ai_key:
        raise RuntimeError("AI API Key 未配置, 请在设置页配置")

    client = _openai_client(ai_key, timeout)
    try:
        stream = await client.chat.completions.create(
            model=current_ai_model(),
            messages=list(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content
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
    message = status_messages.get(status) or detail or "请稍后重试或检查 AI 服务配置"
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
    with tempfile.TemporaryDirectory(prefix="tickflow-codex-run-") as run_dir:
        run_path = Path(run_dir)
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

        env = os.environ.copy()
        env.setdefault("NO_COLOR", "1")
        env["CODEX_HOME"] = str(codex_home_path)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=timeout,
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RuntimeError("Codex CLI 调用超时, 请稍后重试或检查本机 Codex 登录状态") from exc

        out = _clean_process_text(stdout)
        err = _clean_process_text(stderr)
        final_message = _read_output_file(output_path)
        if proc.returncode != 0:
            detail = err or out or f"exit code {proc.returncode}"
            raise RuntimeError(f"Codex CLI 调用失败: {detail[-1200:]}")
        result = final_message or out
        if not result:
            raise RuntimeError("Codex CLI 未返回内容")
        return result


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

    tier = str(config.get("service_tier") or "").strip()
    if tier not in CODEX_SUPPORTED_SERVICE_TIERS:
        tier = CODEX_SERVICE_TIER_FALLBACK
    lines.append(_toml_string("service_tier", tier))
    lines.append(_toml_string("approval_policy", "never"))
    lines.append(_toml_string("sandbox_mode", "read-only"))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
