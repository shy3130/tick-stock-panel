"""Agent Reach 驱动的单标的公开消息适配层。

该模块不是通用 shell：只执行固定的 `agent-reach doctor --json` 与经 doctor
确认的只读平台命令。调用方只传规范证券代码和清洗名称，不传数量、成本、
仓位比例或账户。所有返回内容均是不可信的 C 级公开信息，只能作为辅助证据。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

_DOCTOR_TIMEOUT_SECONDS = 20.0
_SEARCH_TIMEOUT_SECONDS = 8.0
_DOCTOR_TTL_SECONDS = 300.0
_RESULT_TTL_SECONDS = 600.0
_FAILURE_TTL_SECONDS = 60.0
_CIRCUIT_FAILURES = 3
_CIRCUIT_SECONDS = 300.0
_MAX_OUTPUT_BYTES = 256 * 1024
_MAX_RESULTS = 3
_SYMBOL_RE = re.compile(r"^[0-9A-Z]{1,8}\.(SH|SZ|BJ|HK|ETF)$")
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z\u3400-\u9fff]+")
_ALLOWED_ENV_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "PATH",
        "TMPDIR",
        "USER",
        "XDG_CONFIG_HOME",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
    }
)
_ALLOWED_TWITTER_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})


class AgentReachChannel(StrEnum):
    TWITTER = "twitter"


class PublicResearchSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    name: str | None = Field(default=None, max_length=40)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not _SYMBOL_RE.fullmatch(normalized):
            raise ValueError("symbol 必须是规范证券代码")
        return normalized

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _SAFE_NAME_RE.sub("", value)[:32]
        return cleaned or None


class PublicResearchEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["twitter"]
    source: str
    url: str
    author: str | None = None
    excerpt: str
    published_at: str | None = None
    retrieved_at: str
    evidence_grade: Literal["C"] = "C"
    unverified: Literal[True] = True
PublicResearchScope = Literal["primary_position_only", "single_stock_analysis"]




class PublicResearchBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["disabled", "available", "partial", "unavailable"] = "disabled"
    scope: PublicResearchScope = "primary_position_only"
    subject_symbol: str | None = None
    channels_requested: list[str] = Field(default_factory=list)
    channels_used: list[str] = Field(default_factory=list)
    evidence: list[PublicResearchEvidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    retrieved_at: str | None = None


@dataclass(frozen=True)
class _CacheEntry:
    value: Any
    expires_at: float


Runner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


class AgentReachResearchAdapter:
    """只读 Agent Reach 路由；缓存与熔断均为进程内状态。"""

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._runner = runner or _run_command
        self._monotonic = monotonic
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._doctor_cache: _CacheEntry | None = None
        self._result_cache: dict[tuple[str, str, str, tuple[str, ...]], _CacheEntry] = {}
        self._failures: dict[str, int] = {}
        self._circuit_until: dict[str, float] = {}

    def fetch(
        self,
        subject: PublicResearchSubject,
        channels: tuple[AgentReachChannel, ...],
        *,
        scope: PublicResearchScope,
    ) -> PublicResearchBundle:
        requested = tuple(dict.fromkeys(channel.value for channel in channels))
        cache_scope = str(scope)
        retrieved_at = self._now().isoformat(timespec="seconds")
        if not requested:
            return PublicResearchBundle(
                status="disabled",
                scope=scope,
                subject_symbol=subject.symbol,
                retrieved_at=retrieved_at,
            )

        key = (cache_scope, subject.symbol, subject.name or "", requested)
        cached = self._cache_get(key)
        if isinstance(cached, PublicResearchBundle):
            return cached

        doctor = self._doctor()
        evidence: list[PublicResearchEvidence] = []
        used: list[str] = []
        warnings: list[str] = []
        for channel_name in requested:
            channel = AgentReachChannel(channel_name)
            if self._circuit_is_open(channel.value):
                warnings.append(f"{channel.value}:circuit_open")
                continue
            if channel is AgentReachChannel.TWITTER:
                items, warning = self._fetch_twitter(subject, doctor, retrieved_at)
            else:  # pragma: no cover - enum exhaustiveness guard
                items, warning = [], f"{channel.value}:unsupported"
            if items:
                used.append(channel.value)
                evidence.extend(items)
                self._record_success(channel.value)
            elif warning:
                warnings.append(warning)
                self._record_failure(channel.value)

        if evidence and warnings:
            status = "partial"
        elif evidence:
            status = "available"
        else:
            status = "unavailable"
        bundle = PublicResearchBundle(
            status=status,
            scope=scope,
            subject_symbol=subject.symbol,
            channels_requested=list(requested),
            channels_used=used,
            evidence=evidence,
            warnings=warnings,
            retrieved_at=retrieved_at,
        )
        self._cache_put(
            key,
            bundle,
            _RESULT_TTL_SECONDS if evidence else _FAILURE_TTL_SECONDS,
        )
        return bundle

    def health(self) -> dict[str, dict[str, str | None]]:
        """返回脱敏 doctor/运行状态；不暴露 Cookie、路径、原始异常或帮助文本。"""
        doctor = self._doctor()
        output: dict[str, dict[str, str | None]] = {}
        for channel in AgentReachChannel:
            item = doctor.get(channel.value) if isinstance(doctor, dict) else None
            runtime_state = self._runtime_state(channel.value)
            output[channel.value] = {
                "status": _doctor_value(item, "status"),
                "active_backend": _doctor_value(item, "active_backend"),
                "runtime_state": runtime_state,
            }
        return output

    def _doctor(self) -> dict[str, Any]:
        with self._lock:
            now = self._monotonic()
            if self._doctor_cache is not None and self._doctor_cache.expires_at > now:
                value = self._doctor_cache.value
                return value if isinstance(value, dict) else {}

        result = self._runner(
            ["agent-reach", "doctor", "--json"],
            _DOCTOR_TIMEOUT_SECONDS,
        )
        value = _json_object(result.stdout) if result.returncode == 0 else {}
        with self._lock:
            self._doctor_cache = _CacheEntry(
                value=value,
                expires_at=now + _DOCTOR_TTL_SECONDS,
            )
        return value

    def _fetch_twitter(
        self,
        subject: PublicResearchSubject,
        doctor: dict[str, Any],
        retrieved_at: str,
    ) -> tuple[list[PublicResearchEvidence], str | None]:
        state = doctor.get(AgentReachChannel.TWITTER.value)
        if _doctor_value(state, "status") != "ok":
            return [], "twitter:backend_unavailable"
        if _doctor_value(state, "active_backend") != "OpenCLI":
            return [], "twitter:unsupported_backend"

        code = subject.symbol.split(".", 1)[0]
        query = f'"{subject.name}" OR "{code}" lang:zh' if subject.name else f'"{code}" lang:zh'
        result = self._runner(
            [
                "opencli",
                "twitter",
                "search",
                query,
                "--limit",
                str(_MAX_RESULTS),
                "--product",
                "live",
                "--exclude",
                "retweets",
                "--format",
                "json",
                "--window",
                "background",
                "--site-session",
                "persistent",
            ],
            _SEARCH_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return [], f"twitter:{_safe_failure_code(result)}"
        rows = _json_rows(result.stdout)
        output: list[PublicResearchEvidence] = []
        for row in rows[:_MAX_RESULTS]:
            url = _safe_twitter_url(row.get("url"))
            excerpt = _clean_excerpt(row.get("text"))
            if url is None or excerpt is None:
                continue
            output.append(
                PublicResearchEvidence(
                    platform="twitter",
                    source="agent-reach:twitter:OpenCLI",
                    url=url,
                    author=_clean_author(row.get("author")),
                    excerpt=excerpt,
                    published_at=_clean_optional_text(row.get("created_at"), 64),
                    retrieved_at=retrieved_at,
                )
            )
        return output, None if output else "twitter:empty"

    def _cache_get(self, key: tuple[str, str, str, tuple[str, ...]]) -> Any | None:
        with self._lock:
            entry = self._result_cache.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._monotonic():
                self._result_cache.pop(key, None)
                return None
            return entry.value

    def _cache_put(
        self,
        key: tuple[str, str, str, tuple[str, ...]],
        value: Any,
        ttl: float,
    ) -> None:
        with self._lock:
            self._result_cache[key] = _CacheEntry(
                value=value,
                expires_at=self._monotonic() + ttl,
            )

    def _runtime_state(self, channel: str) -> str:
        with self._lock:
            if self._circuit_until.get(channel, 0.0) > self._monotonic():
                return "circuit_open"
            if self._failures.get(channel, 0) > 0:
                return "recent_failure"
            return "not_exercised"

    def _circuit_is_open(self, channel: str) -> bool:
        with self._lock:
            return self._circuit_until.get(channel, 0.0) > self._monotonic()

    def _record_success(self, channel: str) -> None:
        with self._lock:
            self._failures[channel] = 0
            self._circuit_until.pop(channel, None)

    def _record_failure(self, channel: str) -> None:
        with self._lock:
            failures = self._failures.get(channel, 0) + 1
            self._failures[channel] = failures
            if failures >= _CIRCUIT_FAILURES:
                self._circuit_until[channel] = self._monotonic() + _CIRCUIT_SECONDS


def get_agent_reach_research_adapter(app_state: Any) -> AgentReachResearchAdapter:
    """Return the process-scoped adapter shared by all Agent Reach consumers."""
    adapter = getattr(app_state, "agent_reach_research_adapter", None)
    if not isinstance(adapter, AgentReachResearchAdapter):
        adapter = AgentReachResearchAdapter()
        app_state.agent_reach_research_adapter = adapter
    return adapter



def _run_command(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(args[0])
    if executable is None:
        return subprocess.CompletedProcess(args, 127, "", "command_unavailable")
    safe_env = {
        key: value
        for key in _ALLOWED_ENV_KEYS
        if (value := os.environ.get(key)) is not None
    }
    safe_env["PATH"] = _safe_child_path()
    try:
        result = subprocess.run(  # noqa: S603
            [executable, *args[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            cwd=tempfile.gettempdir(),
            env=safe_env,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timeout")
    except OSError as exc:
        logger.warning("Agent Reach 命令失败 (%s)", type(exc).__name__)
        return subprocess.CompletedProcess(args, 126, "", "os_error")
    if len(result.stdout.encode()) + len(result.stderr.encode()) > _MAX_OUTPUT_BYTES:
        return subprocess.CompletedProcess(args, 125, "", "output_too_large")
    return result


def _safe_child_path() -> str:
    """只保留 Agent Reach/OpenCLI 运行所需目录，避免探测无关认证 CLI。"""
    directories: list[str] = []
    for command in ("opencli", "node"):
        resolved = shutil.which(command)
        if resolved:
            directories.append(os.path.dirname(resolved))
    directories.extend(("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"))
    return os.pathsep.join(dict.fromkeys(directories))


def _doctor_value(value: Any, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    item = value.get(key)
    return str(item) if item is not None else None


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_rows(raw: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("data", "items", "results", "rows"):
        rows = value.get(key)
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    return []


def _safe_failure_code(result: subprocess.CompletedProcess[str]) -> str:
    combined = f"{result.stdout}\n{result.stderr}"[:4096]
    for code in (
        "BROWSER_CONNECT",
        "AUTH_REQUIRED",
        "RATE_LIMITED",
        "TIMEOUT",
    ):
        if code.lower() in combined.lower():
            return code
    if result.returncode == 124:
        return "TIMEOUT"
    if result.returncode == 127:
        return "COMMAND_UNAVAILABLE"
    return "BACKEND_ERROR"


def _safe_twitter_url(value: Any) -> str | None:
    text = _clean_optional_text(value, 500)
    if text is None:
        return None
    parsed = urlparse(text)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_TWITTER_HOSTS:
        return None
    return text


def _clean_excerpt(value: Any) -> str | None:
    return _clean_optional_text(value, 280)


def _clean_author(value: Any) -> str | None:
    return _clean_optional_text(value, 80)


def _clean_optional_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).replace("\x00", " ").split())[:limit]
    return text or None
