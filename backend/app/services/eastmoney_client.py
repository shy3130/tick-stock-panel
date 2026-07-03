"""Tiny Eastmoney HTTP helper for ext presets."""
from __future__ import annotations

import threading
import time
from typing import Any

import httpx

_LOCK = threading.Lock()
_LAST_TS = 0.0
_MIN_INTERVAL = 0.35


def get_json(url: str, params: dict[str, Any] | None = None) -> dict:
    global _LAST_TS
    with _LOCK:
        wait = _MIN_INTERVAL - (time.monotonic() - _LAST_TS)
        if wait > 0:
            time.sleep(wait)
        _LAST_TS = time.monotonic()

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
