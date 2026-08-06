"""通用日志敏感信息脱敏。

过滤器只修改即将输出的 ``LogRecord``，不读取 secrets store，也不持久化秘密。
任何脱敏异常都 fail-open，不能阻断业务日志。
"""
from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "webhook_secret",
        "password",
        "sign",
    }
)
_QUERY_KEYS = frozenset({"api_key", "apikey", "key", "token", "access_token", "secret", "sign"})
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]+)")
_AUTH_RE = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?!bearer\s+)([^\s,;]+)")
_KEY_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|sign)[\"']?\s*[:=]\s*[\"']?)(?P<value>[^\s,;\"'}\]]+)"
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_WEBHOOK_PATH_RE = re.compile(r"(?i)(/(?:hook|robot/send)/)([^/?#\s]+)")


def redact_secret(value: object, keep_prefix: int = 3, keep_suffix: int = 2) -> str:
    """Mask a secret while retaining a small diagnostic prefix/suffix.

    Short values are fully masked; unlike the upstream helper, no short secret is
    returned verbatim.
    """
    text = "" if value is None else str(value)
    if not text:
        return ""
    keep_prefix = max(0, int(keep_prefix))
    keep_suffix = max(0, int(keep_suffix))
    if len(text) <= keep_prefix + keep_suffix:
        return "*" * len(text)
    hidden = "*" * max(3, len(text) - keep_prefix - keep_suffix)
    return f"{text[:keep_prefix]}{hidden}{text[-keep_suffix:] if keep_suffix else ''}"


def _normalized_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = [
            (key, redact_secret(value) if _normalized_key(key) in _QUERY_KEYS else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        path = _WEBHOOK_PATH_RE.sub(lambda m: f"{m.group(1)}{redact_secret(m.group(2))}", parts.path)
        return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query, doseq=True), parts.fragment))
    except Exception:  # noqa: BLE001
        return url


def redact_text(value: object) -> str:
    """Redact common secret forms from arbitrary log text."""
    text = "" if value is None else str(value)
    text = _URL_RE.sub(lambda match: _redact_url(match.group(0)), text)
    text = _BEARER_RE.sub(lambda match: f"{match.group(1)}{redact_secret(match.group(2))}", text)
    text = _AUTH_RE.sub(lambda match: f"{match.group(1)}{redact_secret(match.group(2))}", text)
    return _KEY_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}{redact_secret(match.group('value'))}", text
    )


def redact_mapping(
    payload: object,
    sensitive_keys: set[str] | frozenset[str] = _SENSITIVE_KEYS,
) -> object:
    """Recursively redact mappings/sequences without mutating the input."""
    normalized = {_normalized_key(key) for key in sensitive_keys}
    if isinstance(payload, Mapping):
        out: dict[object, object] = {}
        for key, value in payload.items():
            key_name = _normalized_key(key)
            if key_name in normalized:
                if isinstance(value, str) and ("url" in key_name or value.startswith(("http://", "https://"))):
                    out[key] = _redact_url(value)
                else:
                    out[key] = redact_secret(value)
            else:
                out[key] = redact_mapping(value, sensitive_keys)
        return out
    if isinstance(payload, list):
        return [redact_mapping(item, sensitive_keys) for item in payload]
    if isinstance(payload, tuple):
        return tuple(redact_mapping(item, sensitive_keys) for item in payload)
    if isinstance(payload, set):
        return {redact_mapping(item, sensitive_keys) for item in payload}
    if isinstance(payload, BaseException):
        return redact_text(payload)
    if isinstance(payload, str):
        return redact_text(payload)
    return payload


class SecretRedactionFilter(logging.Filter):
    """Sanitize messages, args and exception text before a handler formats them."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_mapping(record.msg)
            if record.args:
                record.args = redact_mapping(record.args)
            if record.exc_info:
                rendered = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = redact_text(rendered)
                record.exc_info = None
            if record.stack_info:
                record.stack_info = redact_text(record.stack_info)
        except Exception:  # noqa: BLE001
            pass
        return True


def install_secret_redaction_filter(logger: logging.Logger | None = None) -> int:
    """Install one filter per current handler; return the number added.

    The operation is idempotent. Handler filters are required because filters on
    the root logger are not applied to records propagated from child loggers.
    """
    target = logger or logging.getLogger()
    added = 0
    if not any(isinstance(item, SecretRedactionFilter) for item in target.filters):
        target.addFilter(SecretRedactionFilter())
        added += 1
    for handler in target.handlers:
        if any(isinstance(item, SecretRedactionFilter) for item in handler.filters):
            continue
        handler.addFilter(SecretRedactionFilter())
        added += 1
    return added


__all__ = [
    "SecretRedactionFilter",
    "install_secret_redaction_filter",
    "redact_mapping",
    "redact_secret",
    "redact_text",
]
