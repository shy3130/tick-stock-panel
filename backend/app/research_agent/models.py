"""Shared, JSON-safe models used by the research-agent harness."""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

_SENSITIVE_KEY_RE = re.compile(
    r"(?:password|passwd|secret|api[_-]?key|authorization|cookie|session|"
    r"token|credential|keyy|vidd|abc|def|xyz)",
    re.IGNORECASE,
)
_HIBOR_SESSION_URL_RE = re.compile(r"https?://(?:sys\.)?hibor\.com\.cn\S*", re.IGNORECASE)


def china_now_iso() -> str:
    """Return a timezone-aware timestamp for user-visible run metadata."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def json_safe(value: Any, *, max_depth: int = 6, _key: str = "") -> Any:
    """Normalize and redact provider values before persistence or model use."""
    if max_depth <= 0:
        return "[truncated]"
    if _SENSITIVE_KEY_RE.search(_key):
        return "[redacted]"
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): json_safe(item, max_depth=max_depth - 1, _key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item, max_depth=max_depth - 1, _key=_key) for item in value]
    if isinstance(value, str):
        # A copied Hibor session URL is equivalent to a credential. It must never
        # enter a durable research record, including inside unstructured text.
        return _HIBOR_SESSION_URL_RE.sub("[redacted Hibor session URL]", value)
    if value is None or isinstance(value, (int, bool)):
        return value
    return str(value)


def safe_url(value: str | None) -> str:
    """Keep public source links while dropping credential-bearing URL parameters."""
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"}:
            return ""
        if any(
            _SENSITIVE_KEY_RE.search(key)
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        ):
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        return raw
    except ValueError:
        return ""


def evidence(
    *,
    source: str,
    title: str,
    status: str,
    summary: str,
    data: Any | None = None,
    as_of: str | None = None,
    url: str | None = None,
    error_type: str | None = None,
) -> dict:
    """Create a stable evidence record. Citation ids are assigned per run later."""
    return {
        "citation": "",
        "source": source,
        "title": title,
        "status": status,
        "summary": summary,
        # Collector payloads are already compacted. Preserve enough nested detail
        # for a human to audit a financial row or report excerpt after persistence.
        "data": json_safe(data, max_depth=12) if data is not None else {},
        "as_of": as_of,
        "retrieved_at": china_now_iso(),
        "url": safe_url(url),
        "error_type": error_type or "",
    }
