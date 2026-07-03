"""Tiny Eastmoney HTTP helper for ext presets."""
from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx

_LOCK = threading.Lock()
_LAST_TS: dict[str, float] = {}
_MIN_INTERVAL = 0.35
_ALLOWED_HOSTS = {
    "datacenter-web.eastmoney.com",
    "reportapi.eastmoney.com",
    "search-api-web.eastmoney.com",
    "searchapi.eastmoney.com",
}


def _check_host(url: str) -> str:
    host = urlparse(url).hostname or ""
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"host not allowed: {host}")
    return host


def _throttle(host: str) -> None:
    with _LOCK:
        wait = _MIN_INTERVAL - (time.monotonic() - _LAST_TS.get(host, 0.0))
        _LAST_TS[host] = time.monotonic() + max(wait, 0.0)
    if wait > 0:
        time.sleep(wait)


def get_json(url: str, params: dict[str, Any] | None = None) -> dict:
    host = _check_host(url)
    _throttle(host)
    resp = httpx.get(
        url,
        params=params or {},
        timeout=10.0,
        headers={"User-Agent": "Mozilla/5.0"},
        trust_env=False,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"Eastmoney response is not object: {type(data)}")
    return data


def get_datacenter_paged(url: str, params: dict[str, Any], max_pages: int = 20) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        payload = get_json(url, params={**params, "pageNumber": str(page), "pageSize": "500"})
        result = payload.get("result") if isinstance(payload, dict) else None
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list) or not data:
            break
        rows.extend(row for row in data if isinstance(row, dict))
        if page >= int(result.get("pages") or 1):
            break
    return rows
