"""Provider capability gates."""
from __future__ import annotations

import json
from typing import Any

from app.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.config import settings

_CAPSET_CACHE_FILE = "capabilities.json"
_CACHE_SCHEMA_VERSION = 8


def _active_provider_name() -> str:
    from app.data_providers.registry import get_active_provider_name

    return get_active_provider_name()


def _provider_capset() -> CapabilitySet:
    from app.data_providers import get_provider

    provider = get_provider(_active_provider_name())
    caps = provider.capabilities
    out: dict[Cap, CapabilityLimits] = {}
    if caps.realtime:
        out[Cap.QUOTE_BY_SYMBOL] = CapabilityLimits(batch=50)
        out[Cap.QUOTE_BATCH] = CapabilityLimits(batch=50)
        out[Cap.QUOTE_POOL] = CapabilityLimits(batch=5000)
    if caps.daily:
        out[Cap.KLINE_DAILY_BY_SYMBOL] = CapabilityLimits(batch=1)
        out[Cap.KLINE_DAILY_BATCH] = CapabilityLimits(batch=500)
    if caps.minute:
        out[Cap.KLINE_MINUTE_BY_SYMBOL] = CapabilityLimits(batch=1)
        out[Cap.KLINE_MINUTE_BATCH] = CapabilityLimits(batch=200)
        if caps.minute_month_extension:
            out[Cap.KLINE_MINUTE_MONTH] = CapabilityLimits(batch=200)
    if caps.financial:
        out[Cap.FINANCIAL] = CapabilityLimits(batch=100)
    if caps.adj_factor:
        out[Cap.ADJ_FACTOR] = CapabilityLimits(batch=500)
    if caps.depth:
        out[Cap.DEPTH5] = CapabilityLimits(rpm=30, batch=1)
        out[Cap.DEPTH5_BATCH] = CapabilityLimits(rpm=30, batch=50)
    return CapabilitySet(out)


def detect_capabilities(force: bool = False) -> CapabilitySet:  # noqa: ARG001
    """Build gates from the active provider declaration."""
    capset = _provider_capset()
    _persist(
        capset,
        _active_provider_name().capitalize(),
        log=["使用 DATA_PROVIDER 数据源能力声明"],
        missing=[],
        extras=[],
    )
    return capset


def _persist(
    capset: CapabilitySet,
    label: str,
    log: list[str] | None = None,
    missing: list[str] | None = None,
    extras: list[str] | None = None,
) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "provider": _active_provider_name(),
        "label": label,
        "capabilities": capset.to_dict(),
        "probe_log": log or [],
        "missing_caps": missing or [],
        "extras_caps": extras or [],
    }
    with (settings.data_dir / _CAPSET_CACHE_FILE).open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _read_cache() -> dict[str, Any]:
    cache_path = settings.data_dir / _CAPSET_CACHE_FILE
    if not cache_path.exists():
        return {}
    with cache_path.open(encoding="utf-8") as f:
        return json.load(f)


def _capset_from_json(data: dict[str, Any]) -> CapabilitySet:
    caps: dict[Cap, CapabilityLimits] = {}
    for cap_name, lim in data.get("capabilities", {}).items():
        try:
            cap = Cap(cap_name)
        except ValueError:
            continue
        caps[cap] = CapabilityLimits(
            rpm=lim.get("rpm"),
            batch=lim.get("batch"),
            subscribe=lim.get("subscribe"),
        )
    return CapabilitySet(caps)


def tier_label() -> str:
    return _read_cache().get("label", "Unknown")


def probe_log() -> list[str]:
    return _read_cache().get("probe_log", [])


def missing_caps() -> list[str]:
    return _read_cache().get("missing_caps", [])


def extras_caps() -> list[str]:
    return _read_cache().get("extras_caps", [])
